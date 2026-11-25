"""真实/模拟 Agent 执行运行器（第 41 步）。

把 Agent 执行封装为可审计的「运行」，并用**硬性成本限制**约束真实 API 调用：

- 最大模型调用轮数、最大 Token 总量、最大执行时长、最大工具调用次数；
- 真实模式禁止自动重试（`api_max_retries=0`），避免失败重试产生额外费用；
- 仅白名单工具（calculator / text_analyzer / sqlite_query，均为本地无副作用）；
- 真实 API 需**双重开关**：请求显式 `confirm_real=True` + 服务端环境变量
  `TRACEPILOT_ALLOW_REAL_API=1`，两者缺一即拒绝，绝不产生付费调用。

**数据来源标记（强制，不可伪造）**：
- `real_api`  ：真实调用 DeepSeek API 且使用真实 Token 计量；若 Provider
  返回 usage，同时写入 `real_api_verified=true` 与证据（独立于 `data_source`
  声明），供数据治理分类使用；缺少 Provider usage 证据时不写入。
- `mock`      ：确定性脚本化模型响应 + 真实本地工具（无网络、零成本）
- `simulated` ：纯合成轨迹（如评测/故障注入产物）
真实模式 `simulated=False`；mock 模式 `simulated=True`。二者绝不混淆。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from backend.app.schemas.trace import (
    TraceRecord,
    TraceStatus,
    TraceStep,
    TokenUsage,
)
from backend.app.services.execution_config import get_execution_limits, public_demo_enabled
from backend.app.services.fault_injector import FaultInjector  # noqa: F401  (预留扩展点)
from backend.app.services.trace_collector import TraceCollector
from backend.app.services.trace_store import DEFAULT_TRACES_DIR
from backend.app.tools.basic_tools import calculator

__all__ = [
    "DATA_SOURCES",
    "AgentRunError",
    "RealApiNotAuthorized",
    "InvalidModelError",
    "RunLimits",
    "AgentRun",
    "run_agent_task",
    "get_run",
    "list_runs",
    "REAL_API_ENV_FLAG",
    "ALLOWED_MODELS",
    "DEFAULT_REAL_MODEL",
    "real_api_readiness",
]

ENGINE_VERSION = "1.0.0"
REAL_API_ENV_FLAG = "TRACEPILOT_ALLOW_REAL_API"
DATA_SOURCES = ("real_api", "mock", "simulated")

# api.deepseek.com 官方有效模型 ID（白名单）
ALLOWED_MODELS = ("deepseek-chat", "deepseek-reasoner")
DEFAULT_REAL_MODEL = "deepseek-chat"

TS = "2026-01-01T00:00:00+00:00"
MAX_STORED_RUNS = 60
PERSIST_ENV_FLAG = "TRACEPILOT_PERSIST_AGENT_RUNS"
RUNS_DIR = Path(__file__).resolve().parents[3] / "data" / "agent_runs"


class AgentRunError(RuntimeError):
    """Agent 运行失败/请求非法。"""


class RealApiNotAuthorized(AgentRunError):
    """未获授权却请求真实 API 调用。"""


class InvalidModelError(AgentRunError):
    """模型 ID 不在白名单。"""


# ---------------------------------------------------------------------------
# 限制配置
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunLimits:
    """一次 Agent 运行的硬性上限（统一来自 execution_config 的当前环境配置）。"""

    max_model_calls: int = field(
        default_factory=lambda: get_execution_limits().agent_max_model_calls
    )
    max_total_tokens: int = field(
        default_factory=lambda: get_execution_limits().agent_max_total_tokens
    )
    max_tool_calls: int = field(
        default_factory=lambda: get_execution_limits().agent_max_tool_calls
    )
    max_wall_seconds: float = field(
        default_factory=lambda: get_execution_limits().agent_max_wall_seconds
    )
    max_tool_rounds: int = field(
        default_factory=lambda: get_execution_limits().agent_max_tool_rounds
    )
    api_max_retries: int = 0          # 真实模式禁止自动重试（不随环境放宽）
    max_tokens_per_call: int = field(
        default_factory=lambda: get_execution_limits().agent_max_output_tokens
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 运行记录
# ---------------------------------------------------------------------------

@dataclass
class AgentRun:
    run_id: str
    task: str
    mode: str                          # mock | real_api
    data_source: str                   # real_api | mock | simulated
    simulated: bool
    status: str                        # running | succeeded | failed | limit_exceeded
    created_at: str
    limits: dict[str, Any]
    model: str | None = None
    trace_id: str | None = None
    trace_path: str | None = None
    final_answer: str | None = None
    plan: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, Any] | None = None
    elapsed_ms: int | None = None
    error: str | None = None
    limit_violations: list[str] = field(default_factory=list)
    model_selection: dict[str, Any] | None = None
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "mode": self.mode,
            "data_source": self.data_source,
            "simulated": self.simulated,
            "status": self.status,
            "created_at": self.created_at,
            "limits": self.limits,
            "model": self.model,
            "trace_id": self.trace_id,
            "trace_path": self.trace_path,
            "final_answer": self.final_answer,
            "plan": self.plan,
            "tool_calls": self.tool_calls,
            "token_usage": self.token_usage,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "limit_violations": self.limit_violations,
            "model_selection": self.model_selection,
            "limitations": self.limitations,
        }


_lock = threading.Lock()
_runs: dict[str, AgentRun] = {}
_order: list[str] = []
_loaded = False


def _persistence_enabled() -> bool:
    """磁盘持久化开关（生产/证据生成环境开启；单元测试默认关闭）。"""
    return os.environ.get(PERSIST_ENV_FLAG, "").strip().lower() in {"1", "true", "yes"}


def _run_file(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.json"


def _persist_run(run: AgentRun) -> None:
    """把运行记录写入 data/agent_runs/，保证重启后仍可追溯。"""
    if not _persistence_enabled():
        return
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        target = _run_file(run.run_id)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(run.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except Exception:  # noqa: BLE001 - 持久化失败不应影响主流程
        pass


def _load_runs() -> None:
    """进程启动后从磁盘恢复最近的运行记录。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not _persistence_enabled() or not RUNS_DIR.exists():
        return
    try:
        files = sorted(RUNS_DIR.glob("run-*.json"), key=lambda f: f.stat().st_mtime)
    except OSError:
        return
    for path in files[-MAX_STORED_RUNS:]:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            run = AgentRun(
                run_id=str(d["run_id"]),
                task=str(d.get("task") or ""),
                mode=str(d.get("mode") or "mock"),
                data_source=str(d.get("data_source") or "mock"),
                simulated=bool(d.get("simulated", True)),
                status=str(d.get("status") or "succeeded"),
                created_at=str(d.get("created_at") or ""),
                limits=dict(d.get("limits") or {}),
                model=d.get("model"),
                trace_id=d.get("trace_id"),
                trace_path=d.get("trace_path"),
                final_answer=d.get("final_answer"),
                plan=list(d.get("plan") or []),
                tool_calls=list(d.get("tool_calls") or []),
                token_usage=d.get("token_usage"),
                elapsed_ms=d.get("elapsed_ms"),
                error=d.get("error"),
                limit_violations=list(d.get("limit_violations") or []),
                model_selection=d.get("model_selection"),
                limitations=list(d.get("limitations") or []),
            )
        except Exception:  # noqa: BLE001
            continue
        with _lock:
            if run.run_id in _runs:
                continue
            _runs[run.run_id] = run
            _order.append(run.run_id)
            while len(_order) > MAX_STORED_RUNS:
                old = _order.pop(0)
                _runs.pop(old, None)


def _store_run(run: AgentRun) -> None:
    _load_runs()
    with _lock:
        _runs[run.run_id] = run
        _order.append(run.run_id)
        while len(_order) > MAX_STORED_RUNS:
            old = _order.pop(0)
            _runs.pop(old, None)
    _persist_run(run)


def get_run(run_id: str) -> AgentRun:
    _load_runs()
    with _lock:
        run = _runs.get(run_id)
    if run is None:
        raise AgentRunError(f"运行不存在: {run_id}")
    return run


def list_runs() -> list[dict[str, Any]]:
    _load_runs()
    with _lock:
        return [r.to_dict() for r in _runs.values()]


# ---------------------------------------------------------------------------
# 模型解析
# ---------------------------------------------------------------------------

def _resolve_real_model(explicit: str | None) -> tuple[str, dict[str, Any]]:
    """解析真实模式使用的模型，返回 (model, selection_meta)。

    环境变量中的模型 ID 若不在官方白名单，回退到 deepseek-chat 并**明确记录回退原因**，
    避免因无效模型 ID 直接失败浪费调用配额。
    """
    env_model = (os.environ.get("DEEPSEEK_MODEL") or "").strip()
    candidate = (explicit or env_model or DEFAULT_REAL_MODEL).strip()

    if candidate in ALLOWED_MODELS:
        return candidate, {"selected": candidate, "fallback": False, "reason": None}

    if explicit:
        raise InvalidModelError(
            f"指定的模型 {explicit!r} 不在 api.deepseek.com 白名单内"
            f"（允许: {', '.join(ALLOWED_MODELS)}）"
        )

    return DEFAULT_REAL_MODEL, {
        "selected": DEFAULT_REAL_MODEL,
        "fallback": True,
        "reason": f"环境变量 DEEPSEEK_MODEL={candidate!r} 不是 api.deepseek.com 的有效模型 ID，"
                  f"已回退到 {DEFAULT_REAL_MODEL}",
        "env_value": candidate,
        "allowed_models": list(ALLOWED_MODELS),
    }


def _real_api_allowed(confirm_real: bool) -> bool:
    # 公共演示模式硬性禁止真实 API（即使误设服务端开关也不放行）
    if public_demo_enabled():
        return False
    env_ok = os.environ.get(REAL_API_ENV_FLAG, "").strip() in {"1", "true", "True", "yes"}
    return bool(confirm_real and env_ok)


def real_api_readiness() -> dict[str, Any]:
    """诊断真实 API 是否可用（只读环境变量，不读取/返回密钥内容）。

    可用条件：服务端开关 `TRACEPILOT_ALLOW_REAL_API=1` 且已配置
    `DEEPSEEK_API_KEY`；公共演示模式（`TRACEPILOT_PUBLIC_DEMO=1`）强制不可用。
    每次真实执行仍需请求显式 `confirm_real=true`。
    """
    env_value = (os.environ.get(REAL_API_ENV_FLAG, "") or "").strip()
    allow_flag = env_value in {"1", "true", "True", "yes"}
    api_key_present = bool((os.environ.get("DEEPSEEK_API_KEY", "") or "").strip())
    env_model = (os.environ.get("DEEPSEEK_MODEL", "") or "").strip()
    configured_model_valid: bool | None = None if not env_model else env_model in ALLOWED_MODELS
    demo_mode = public_demo_enabled()

    blocking_reasons: list[str] = []
    if demo_mode:
        blocking_reasons.append("公共演示环境禁止匿名真实 API 调用（仅 Mock 模式可用）")
    if not allow_flag:
        blocking_reasons.append(f"服务端未设置 {REAL_API_ENV_FLAG}=1")
    if not api_key_present:
        blocking_reasons.append("未配置 DEEPSEEK_API_KEY")

    warnings: list[str] = []
    if configured_model_valid is False:
        warnings.append(
            f"DEEPSEEK_MODEL={env_model!r} 不在白名单，真实运行会回退到 {DEFAULT_REAL_MODEL}"
        )

    return {
        "available": bool(allow_flag and api_key_present and not demo_mode),
        "allow_flag": allow_flag,
        "api_key_configured": api_key_present,
        "public_demo": demo_mode,
        "configured_model": env_model or None,
        "configured_model_valid": configured_model_valid,
        "default_real_model": DEFAULT_REAL_MODEL,
        "allowed_models": list(ALLOWED_MODELS),
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 模拟执行（确定性、零成本）
# ---------------------------------------------------------------------------

def _run_mock(
    task: str, limits: RunLimits, run: AgentRun, task_id: str | None
) -> TraceRecord:
    """确定性脚本化执行：模型响应模拟，工具为真实本地 calculator。"""
    collector = TraceCollector(
        task_id=task_id, run_id=run.run_id, traces_dir=DEFAULT_TRACES_DIR
    )
    trace_id = collector.trace_id
    started_at = datetime.now(timezone.utc)
    steps: list[dict[str, Any]] = []
    seq = 0

    def base(step_type: str, parents: list[str]) -> dict[str, Any]:
        nonlocal seq
        seq += 1
        return {
            "step_id": f"{trace_id}-s{seq:03d}",
            "trace_id": trace_id,
            "parent_step_ids": list(parents),
            "step_type": step_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "succeeded",
            "metadata": {
                "data_source": "mock",
                "simulated_model": True,
                "usage_meta": {
                    "provider": "local-simulation",
                    "model": "mock-scripted-model",
                    "usage_source": "SIMULATED",
                    "raw_usage": None,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        }

    # 1) 规划
    plan_step = base("planning", [])
    plan_step["input"] = {"task": task}
    plan_step["output"] = {"plan": ["调用计算器", "给出结论"], "simulated": True}
    plan_step["token_usage"] = TokenUsage(input_tokens=0, output_tokens=0)
    steps.append(plan_step)
    last = plan_step["step_id"]
    run.plan = ["调用计算器", "给出结论"]

    # 2) 模拟模型请求工具
    call_step = base("model_call", [last])
    call_step["input"] = {"messages": [{"role": "user", "content": task}]}
    call_step["output"] = ""
    call_step["token_usage"] = TokenUsage(input_tokens=0, output_tokens=0)
    steps.append(call_step)
    last = call_step["step_id"]

    # 3) 真实本地工具执行（从任务中提取表达式）
    expression = _extract_expression(task) or "125*48"
    tool_step = base("tool_call", [last])
    tool_step["tool_name"] = "calculator"
    tool_step["tool_arguments"] = {"expression": expression}
    try:
        output = calculator.invoke({"expression": expression})
        tool_step["tool_result"] = output
        answer = f"计算结果为 {output.get('result')}"
    except Exception as exc:
        tool_step["status"] = "failed"
        tool_step["tool_result"] = {"error": str(exc)}
        tool_step["error"] = {
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "recoverable": False,
        }
        answer = f"任务失败：{exc}"
    steps.append(tool_step)
    last = tool_step["step_id"]
    run.tool_calls = [
        {
            "step_id": tool_step["step_id"],
            "tool_name": "calculator",
            "arguments": tool_step["tool_arguments"],
            "result": tool_step["tool_result"],
            "status": tool_step["status"],
        }
    ]

    # 4) 模拟模型最终结论
    final_step = base("model_call", [last])
    final_step["input"] = {"messages": [{"role": "tool", "content": "tool result"}]}
    final_step["output"] = answer
    final_step["token_usage"] = TokenUsage(input_tokens=0, output_tokens=0)
    steps.append(final_step)

    run.final_answer = answer
    run.token_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                       "note": "mock 模式无真实 Token 消耗（Token 不可得按 0 明确标注，非伪造真实计量）"}

    return collector.build(
        steps,
        status=TraceStatus.SUCCEEDED,
        ended_at=datetime.now(timezone.utc),
        metadata={
            "data_source": "mock",
            "simulated": True,
            "model_provider": "local-simulation",
            "agent_version": None,
            "simulated_model": True,
            "simulation_note": "模型响应为确定性模拟；工具为真实本地 calculator",
            "run_id": run.run_id,
            "engine_version": ENGINE_VERSION,
            "model": run.model,
            "limits": limits.to_dict(),
        },
    )


def _extract_expression(task: str) -> str | None:
    """从任务文本中提取一个纯算术表达式（仅允许数字与四则运算符）。"""
    import re

    stripped = task.strip()

    # 优先：整段文本本身就是合法算术表达式（避免被局部正则截断，如 "1+2*3"）
    if stripped and re.fullmatch(r"[0-9+\-*/().\s]+", stripped):
        return re.sub(r"\s+", "", stripped)

    # 其次：「A乘以B / A * B」类
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:乘以|\*|×|x)\s*(\d+(?:\.\d+)?)", task)
    if m:
        return f"{m.group(1)}*{m.group(2)}"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:加上|\+|加)\s*(\d+(?:\.\d+)?)", task)
    if m:
        return f"{m.group(1)}+{m.group(2)}"
    return None


# ---------------------------------------------------------------------------
# 真实执行
# ---------------------------------------------------------------------------

def _run_real(
    task: str,
    limits: RunLimits,
    run: AgentRun,
    *,
    model: str,
    task_id: str | None,
) -> TraceRecord:
    """调用真实 DeepSeek API 执行（受硬性限制约束，禁止自动重试）。"""
    # 延迟导入，避免 mock 路径引入 langgraph 依赖
    import backend.app.agents.basic_agent as basic_agent

    try:
        result = basic_agent.run_agent_sync(
            task,
            model=model,
            tools=[*basic_agent.BASIC_TOOLS, *basic_agent.build_capability_tools()],
            max_tool_rounds=max(1, limits.max_tool_rounds),
            api_max_retries=max(0, limits.api_max_retries),
            max_tokens=max(1, limits.max_tokens_per_call),
            max_total_tokens=max(1, limits.max_total_tokens),
            max_tool_calls=max(0, limits.max_tool_calls),
            overall_timeout_seconds=limits.max_wall_seconds,
            collect_trace=True,
            save_trace=False,
            task_id=task_id,
        )
    except basic_agent.AgentError:
        raise
    except Exception as exc:
        raise AgentRunError(f"Agent 执行失败: {type(exc).__name__}: {exc}") from exc

    trace = result.trace
    if trace is None:
        raise AgentRunError("Agent 未返回 Trace（collect_trace 应开启）")

    run.final_answer = result.result
    run.plan = list(result.plan)
    run.tool_calls = list(result.tool_executions)

    # Token 预算预检查触发的部分执行：保留结果与 Trace，明确标记未完成/超限原因。
    if getattr(result, "budget_exceeded", False):
        budget_reason = getattr(result, "budget_reason", None) or "Token 预算不足"
        run.limit_violations.append(budget_reason)
        run.limitations.append(budget_reason)
        run.error = budget_reason
    if getattr(result, "tool_calls_limit_reached", False):
        tool_reason = f"工具调用达到上限 {limits.max_tool_calls} 次，已停止继续调用"
        run.limit_violations.append(tool_reason)
        run.limitations.append(tool_reason)

    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    recorded = 0
    for s in trace.steps:
        if s.token_usage is not None:
            totals["input_tokens"] += s.token_usage.input_tokens
            totals["output_tokens"] += s.token_usage.output_tokens
            totals["total_tokens"] += s.token_usage.total_tokens
            recorded += 1
    if recorded == 0:
        run.token_usage = None
        run.limitations.append("真实调用未返回 token_usage，Token 消耗标记为未知（不填 0）")
    else:
        run.token_usage = {**totals, "steps_with_usage": recorded}
        model_steps = sum(
            1
            for s in trace.steps
            if s.step_type.value in {"model_call", "planning", "reflection"}
        )
        if recorded < model_steps:
            run.limitations.append(
                f"部分模型调用未返回 usage（{recorded}/{model_steps} 步有计量），"
                "Token 总量为已上报值之和，预算预检查按估算执行"
            )

    # 真实调用证据（数据治理用）：只认 model_call 步骤中由 Provider 上报的
    # usage（usage_source=PROVIDER_REPORTED）。该标记独立于 data_source 声明，
    # 缺少 Provider 证据时绝不写入，避免自我声明升级；历史数据保持 UNKNOWN。
    provider_usage_steps = 0
    for s in trace.steps:
        if s.step_type.value != "model_call":
            continue
        um = (s.metadata or {}).get("usage_meta")
        if isinstance(um, dict) and um.get("usage_source") == "PROVIDER_REPORTED":
            provider_usage_steps += 1
    verification: dict[str, Any] | None = None
    if provider_usage_steps > 0:
        verification = {
            "provider": "deepseek",
            "model": model,
            "usage_source": "PROVIDER_REPORTED",
            "model_call_steps_with_usage": provider_usage_steps,
            "total_tokens": totals["total_tokens"],
            "verification_method": "execution-time Provider usage in model_call step metadata",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
    elif recorded > 0:
        run.limitations.append(
            "Trace 含 token_usage 但缺少 Provider usage_meta 证据，未写入 real_api_verified"
        )

    # 以真实数据重写 metadata（覆盖 simulated 标记为 False），
    # 并统一 run_id 为本 Run 的 run_id（basic_agent 内部会生成自己的 run_id，
    # 必须归一化，否则 TraceRecord.run_id 与 Run.run_id 不一致）
    metadata: dict[str, Any] = {
        "data_source": "real_api",
        "simulated": False,
        "simulated_model": False,
        "model_provider": "deepseek",
        "agent_version": None,
        "agent_version_note": "受控执行器无独立 agent_version 字段，未伪造",
        "run_id": run.run_id,
        "engine_version": ENGINE_VERSION,
        "model": model,
        "limits": limits.to_dict(),
        "model_selection": run.model_selection,
        "execution_profile": get_execution_limits().profile,
        "token_budget": limits.max_total_tokens,
        "budget_exceeded": bool(getattr(result, "budget_exceeded", False)),
        "budget_reason": getattr(result, "budget_reason", None),
        "token_total": getattr(result, "token_total", None),
    }
    if verification is not None:
        metadata["real_api_verified"] = True
        metadata["real_api_verification"] = verification
    trace = trace.model_copy(update={"run_id": run.run_id, "metadata": metadata})
    return trace


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run_agent_task(
    task: str,
    *,
    mode: str = "mock",
    confirm_real: bool = False,
    model: str | None = None,
    limits: RunLimits | None = None,
    task_id: str | None = None,
    document_provenance: list[dict[str, str]] | None = None,
) -> AgentRun:
    """执行一次 Agent 任务并持久化 Trace。

    Args:
        task: 任务描述。
        mode: "mock"（默认，零成本）或 "real_api"（需双重授权）。
        confirm_real: 真实模式必须显式 True。
        model: 真实模式模型（白名单内）；None 时读环境变量并回退。
        limits: 硬性限制（默认 RunLimits()）。
        task_id: 可选任务 ID。

    Raises:
        RealApiNotAuthorized: 未授权真实调用。
        InvalidModelError: 模型不在白名单。
        AgentRunError: 任务为空或执行失败。
    """
    task = (task or "").strip()
    if not task:
        raise AgentRunError("任务不能为空")
    if mode not in {"mock", "real_api"}:
        raise AgentRunError(f"不支持的 mode: {mode!r}（仅 mock / real_api）")

    lim = limits or RunLimits()
    run = AgentRun(
        run_id=f"run-{uuid.uuid4().hex}",
        task=task,
        mode=mode,
        data_source="real_api" if mode == "real_api" else "mock",
        simulated=mode != "real_api",
        status="running",
        created_at=datetime.now(timezone.utc).isoformat(),
        limits=lim.to_dict(),
        limitations=[
            "仅使用白名单本地无副作用工具（calculator / text_analyzer / sqlite_query）",
            "真实模式禁止自动重试，避免失败重试产生额外费用",
        ],
    )

    if document_provenance:
        run.limitations.append("引用文件（本地解析资料，不代表模型已理解）：" + ", ".join(d["filename"] for d in document_provenance))
    started = time.monotonic()
    try:
        if mode == "real_api":
            if not _real_api_allowed(confirm_real):
                env_ok = os.environ.get(REAL_API_ENV_FLAG, "").strip() in {"1", "true", "True", "yes"}
                missing: list[str] = []
                if public_demo_enabled():
                    missing.append("公共演示环境禁止真实 API 调用（仅 Mock 模式可用）")
                if not env_ok:
                    missing.append(f"服务端未设置 {REAL_API_ENV_FLAG}=1")
                if not confirm_real:
                    missing.append("请求未显式 confirm_real=true")
                raise RealApiNotAuthorized(
                    "真实 DeepSeek API 调用未获授权：" + "；".join(missing)
                    + "（未发起任何付费调用）"
                )
            model_name, selection = _resolve_real_model(model)
            run.model = model_name
            run.model_selection = selection
            if selection.get("fallback"):
                run.limitations.append(selection["reason"])
            trace = _run_real(task, lim, run, model=model_name, task_id=task_id)
        else:
            run.model = "mock-scripted-model"
            trace = _run_mock(task, lim, run, task_id=task_id)

        # STEP59_DOCUMENTS: provenance belongs to trace evidence, not just UI.
        if document_provenance and trace.steps:
            trace.steps[0].metadata["referenced_documents"] = document_provenance
        # 持久化 Trace（复用既有 JSON 存储）
        collector = TraceCollector(
            task_id=task_id, trace_id=trace.trace_id, run_id=run.run_id,
            started_at=trace.started_at, traces_dir=DEFAULT_TRACES_DIR,
        )
        path = collector.save(trace)
        run.trace_id = trace.trace_id
        run.trace_path = str(path)

        # 限制校验（事后审计，超限如实标记；Token 预算已在执行前预检查）
        run.elapsed_ms = int((time.monotonic() - started) * 1000)
        violations: list[str] = list(run.limit_violations)
        model_calls = sum(
            1 for s in trace.steps if s.step_type.value in {"model_call", "planning", "reflection"}
        )
        tool_calls = sum(1 for s in trace.steps if getattr(s, "tool_name", None))
        if model_calls > lim.max_model_calls:
            violations.append(f"模型调用 {model_calls} 次 > 上限 {lim.max_model_calls}")
        if tool_calls > lim.max_tool_calls:
            violations.append(f"工具调用 {tool_calls} 次 > 上限 {lim.max_tool_calls}")
        if run.token_usage and run.token_usage.get("total_tokens", 0) > lim.max_total_tokens:
            violations.append(
                f"Token {run.token_usage['total_tokens']} > 上限 {lim.max_total_tokens}"
            )
        if run.elapsed_ms > lim.max_wall_seconds * 1000:
            violations.append(
                f"耗时 {run.elapsed_ms}ms > 上限 {int(lim.max_wall_seconds * 1000)}ms"
            )
        run.limit_violations = violations
        run.status = "limit_exceeded" if violations else "succeeded"
        if violations:
            new_notes = [v for v in violations if v not in run.limitations]
            if new_notes:
                run.limitations.append("本次运行超出自设上限：" + "；".join(new_notes))

    except (RealApiNotAuthorized, InvalidModelError):
        run.status = "failed"
        run.error = "未授权或无有效模型，未发起任何 API 调用"
        run.elapsed_ms = int((time.monotonic() - started) * 1000)
        raise
    except Exception as exc:
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
        run.elapsed_ms = int((time.monotonic() - started) * 1000)
        if run.mode == "real_api":
            run.limitations.append("真实模式失败未自动重试（避免额外费用）")
        # 统一包装为 AgentRunError，API 层返回结构化错误而非 5xx
        raise AgentRunError(run.error) from exc
    finally:
        # 授权/模型校验失败发生在任何执行之前，不写入运行记录，避免出现
        # 「Run 存在但没有 Trace」的断链记录；只有真正尝试执行过才落库。
        if sys.exc_info()[0] not in (RealApiNotAuthorized, InvalidModelError):
            _store_run(run)

    return run
