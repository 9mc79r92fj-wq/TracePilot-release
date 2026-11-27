"""最小可运行 Agent 执行引擎（第 12 步 + 第 13 步工具集成 + 第 16 步轨迹采集）。

基于 LangGraph 实现的 Agent，执行流程：

    接收任务 -> 任务理解 -> 生成结构化计划 -> Agent 工具循环 -> 返回最终结果

- 工具循环支持 DeepSeek Function Calling：模型选择工具，Python 执行并回传结果；
- 执行期间自动采集 Trace 步骤（model_call / planning / tool_call），
  由 TraceCollector 组装为符合 Trace Schema 的 TraceRecord。

设计要点：
- 模型服务：DeepSeek API（OpenAI 兼容接口，使用 openai SDK 调用）；
- API Key 仅通过环境变量 DEEPSEEK_API_KEY 读取，代码中不硬编码任何密钥；
- 全异步实现（asyncio + LangGraph ainvoke），并提供同步便捷包装；
- 每次模型调用与整体执行均设置合理超时；
- 处理网络 / HTTP / 响应解析 / 超时等 API 异常，抛出带上下文的 AgentError；
- 工具执行异常在 tools 节点内捕获，以结构化错误回传给模型，不让异常穿透；
- 不请求、不输出模型隐藏 Chain-of-Thought：
  只取响应中的显式回答（message.content），即使使用 reasoning 模型，
  其 reasoning_content 也绝不会出现在任何输出中。
"""

import asyncio
import json
import operator
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from openai import APITimeoutError, APIError, AsyncOpenAI
from pydantic import ValidationError

from backend.app.schemas.trace import TraceRecord, TraceStatus
from backend.app.services.execution_config import get_execution_limits
from backend.app.services.fault_injector import FaultInjectionError, FaultInjector
from backend.app.services.trace_collector import TraceCollector
from backend.app.tools.basic_tools import (
    BASIC_TOOLS,
    ToolError,
    build_function_schemas,
    get_tool_registry,
)
from backend.app.tools.capability_tools import (
    CAPABILITY_STEP_TYPES,
    build_capability_tools,
)

__all__ = [
    "AgentError",
    "MissingApiKeyError",
    "ModelCallError",
    "AgentTimeoutError",
    "AgentResult",
    "run_agent",
    "run_agent_sync",
    "safe_tool_names",
]

# ---------------------------------------------------------------------------
# 默认配置（统一由 execution_config 管理；环境变量仍可覆盖连接参数）
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_TOKENS = get_execution_limits().agent_max_output_tokens
DEFAULT_MAX_TOOL_ROUNDS = get_execution_limits().agent_max_tool_rounds


def safe_tool_names() -> list[str]:
    """Agent 可用的本地安全工具名（基础工具 + 能力工具，均为本地无副作用实现）。

    这是 `/api/agent/status` 与执行器共用的白名单唯一来源；不含系统命令、
    任意 Python 代码执行或网络请求工具。
    """
    return [
        *[str(getattr(t, "name", "")) for t in BASIC_TOOLS],
        *[str(getattr(t, "name", "")) for t in build_capability_tools()],
    ]

ENV_API_KEY = "DEEPSEEK_API_KEY"
ENV_MODEL = "DEEPSEEK_MODEL"
ENV_BASE_URL = "DEEPSEEK_BASE_URL"
ENV_TIMEOUT = "DEEPSEEK_TIMEOUT"


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------

class AgentError(RuntimeError):
    """Agent 执行失败基类。"""


class MissingApiKeyError(AgentError):
    """未配置 API Key。"""


class ModelCallError(AgentError):
    """模型调用失败（网络 / HTTP / 响应解析错误）。"""


class AgentBudgetError(AgentError):
    """真实模型调用预算不足（未发出上游请求）。"""


class AgentTimeoutError(AgentError):
    """模型调用或整体执行超时。"""


# ---------------------------------------------------------------------------
# 结果与状态结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentResult:
    """Agent 执行结果（不含任何隐藏推理内容）。"""

    task: str
    understanding: str
    plan: list[str]
    result: str
    model: str
    elapsed_ms: int
    tool_call_count: int = 0
    tool_executions: list[dict[str, Any]] = field(default_factory=list)
    trace: TraceRecord | None = None
    trace_path: str | None = None
    # Token 预算预检查触发的部分执行（保留结果与 Trace，但绝不标记为成功）
    budget_exceeded: bool = False
    budget_reason: str | None = None
    token_total: int | None = None
    tool_calls_limit_reached: bool = False


class AgentState(TypedDict, total=False):
    """LangGraph 状态。"""

    task: str
    understanding: str
    plan: list[str]
    # 工具循环对话历史（Annotated 加法 reducer 累积各节点追加的消息）
    messages: Annotated[list[dict[str, Any]], operator.add]
    # 工具执行明细（name / arguments / result），供 CLI 展示
    tool_executions: Annotated[list[dict[str, Any]], operator.add]
    # 采集的原始 Trace 步骤（Annotated 加法 reducer 累积）
    trace_steps: Annotated[list[dict[str, Any]], operator.add]
    # 步骤计数器 / 最近步骤 id / 下一 agent 节点的父步骤 id 列表（用于依赖关联）
    step_seq: int
    last_step_id: str
    pending_parents: list[str]
    result: str
    # 执行预算（Token 累计 / 工具调用累计 / 预算停止原因）
    token_total: int
    tool_call_total: int
    budget_exceeded: bool
    budget_reason: str
    usage_estimated: bool
    tool_calls_limit_reached: bool


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------

def _resolve_config(
    api_key: str | None,
    model: str | None,
    base_url: str | None,
    timeout_seconds: float | None,
) -> tuple[str, str, str, float]:
    """解析运行配置：显式参数优先，其次环境变量，最后默认值。"""
    key = (api_key or os.environ.get(ENV_API_KEY, "")).strip()
    if not key:
        raise MissingApiKeyError(
            f"未配置 DeepSeek API Key：请设置环境变量 {ENV_API_KEY}，"
            f"或通过参数 api_key 传入"
        )
    model_name = (model or os.environ.get(ENV_MODEL, DEFAULT_MODEL)).strip()
    base = (base_url or os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL)).rstrip("/")
    try:
        timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get(ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS)
        )
    except ValueError as exc:
        raise ModelCallError(f"超时配置无效：{ENV_TIMEOUT} 必须为数字（秒）") from exc
    return key, model_name, base, timeout


# ---------------------------------------------------------------------------
# 模型调用
# ---------------------------------------------------------------------------

async def _chat(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    tools: list[dict[str, Any]] | None = None,
) -> tuple[Any, dict[str, int] | None, dict[str, Any]]:
    """调用 DeepSeek 对话接口，返回 (message, token_usage, usage_meta)。

    绝不返回 / 输出推理内容（reasoning_content 被忽略）。

    Token 用量口径（第 53 步）：
    - token_usage 来自 response.usage，映射为 Trace Schema 的 TokenUsage 三个字段；
    - usage_meta 记录 provider / model / usage_source / recorded_at / raw_usage，
      写入 step.metadata（扩展字段不放 Trace Schema，避免破坏既有结构）；
    - raw_usage 保留 Provider 返回的全部用量字段（如 prompt_cache_hit_tokens 等）；
    - Provider 未返回 usage 时 token_usage=None 且 usage_source="UNKNOWN"（绝不填 0）。
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
        "temperature": 0.3,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    base_url = str(getattr(client, "base_url", "") or "")
    local_model = "api.deepseek.com" not in base_url
    if not local_model:
        # 每次真实上游调用前扣减一次持久化预算；不足时直接拒绝，不发出请求。
        from backend.app.services.real_api_budget import RealCallBudgetExceeded, reserve_real_call
        from backend.app.services.request_context import get_client_key

        try:
            reserve_real_call(
                kind="agent_model_call",
                model=model,
                visitor_key=get_client_key(),
            )
        except RealCallBudgetExceeded as exc:
            raise AgentBudgetError(f"真实模型调用预算不足：{exc}") from exc

    try:
        response = await client.chat.completions.create(**kwargs)
    except APITimeoutError as exc:
        raise AgentTimeoutError(f"DeepSeek API 调用超时: {exc}") from exc
    except APIError as exc:
        raise ModelCallError(f"DeepSeek API 调用失败: {exc}") from exc

    try:
        message = response.choices[0].message
    except (AttributeError, IndexError) as exc:
        raise ModelCallError("DeepSeek API 响应缺少 choices[0].message") from exc

    # Provider 标识按实际 base_url 判定：本地脚本化服务（验收/离线回放）不能被
    # 标注成 deepseek，否则前端成本与来源展示会把模拟模型误认为真实付费 Provider。
    usage_meta: dict[str, Any] = {
        "provider": "local-scripted-llm" if local_model else "deepseek",
        "model": model,
        "usage_source": "SIMULATED" if local_model else "UNKNOWN",
        "raw_usage": None,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    usage = None
    if getattr(response, "usage", None) is not None:
        u = response.usage
        usage = {
            "input_tokens": getattr(u, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(u, "completion_tokens", 0) or 0,
            "total_tokens": getattr(u, "total_tokens", 0) or 0,
        }
        raw_usage: dict[str, Any] | None = None
        try:
            raw_usage = u.model_dump() if hasattr(u, "model_dump") else dict(u)
        except Exception:  # pragma: no cover - Provider SDK 版本差异兜底
            raw_usage = None
        usage_meta["usage_source"] = "SIMULATED" if local_model else "PROVIDER_REPORTED"
        usage_meta["raw_usage"] = raw_usage
        if not local_model:
            from backend.app.services.real_api_budget import record_real_usage
            from backend.app.services.request_context import get_client_key

            record_real_usage(
                kind="agent_model_call",
                model=model,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                visitor_key=get_client_key(),
            )
    return message, usage, usage_meta

# ---------------------------------------------------------------------------
# Trace 步骤构造
# ---------------------------------------------------------------------------

def _make_step(
    trace_id: str,
    seq: int,
    *,
    step_type: str,
    parent_ids: list[str],
    **fields: Any,
) -> dict[str, Any]:
    """构造一个原始 Trace 步骤 dict（字段与 TraceStep 对齐，待采集器校验）。"""
    return {
        "step_id": f"{trace_id}-s{seq:03d}",
        "trace_id": trace_id,
        "parent_step_ids": list(parent_ids),
        "step_type": step_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "succeeded",
        "metadata": {},
        **fields,
    }


def _sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把对话历史收敛为可记录的轻量字段（role/content/tool_call_id/name/tool_calls）。"""
    out: list[dict[str, Any]] = []
    for m in messages:
        item: dict[str, Any] = {"role": m.get("role")}
        if m.get("content"):
            item["content"] = m["content"]
        if m.get("tool_call_id"):
            item["tool_call_id"] = m["tool_call_id"]
        if m.get("name"):
            item["name"] = m["name"]
        if m.get("tool_calls"):
            item["tool_calls"] = m["tool_calls"]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Token 预算预检查（执行前估算，避免先超限再审计）
# ---------------------------------------------------------------------------

_MIN_OUTPUT_FALLBACK = 128


def _estimate_tokens(text: str) -> int:
    """保守估算 token 数（宁可高估，避免付费调用超预算）。

    口径（DeepSeek tokenizer 经验值）：中文约 0.6-1 token/字，英文约 0.25-0.3
    token/字符。这里按「非 ASCII 字符 1 token、ASCII 字符 0.25 token」估算并
    加少量消息开销，对中英混合文本略高估，用于执行前预检查。
    """
    if not text:
        return 1
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii = len(text) - ascii_chars
    return max(1, int(non_ascii + ascii_chars / 4) + 1)


def _estimate_request_tokens(
    messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]]
) -> int:
    """估算一次模型请求的输入 Token（消息 + 工具 schema + 固定开销）。"""
    try:
        blob = json.dumps(
            {"messages": messages, "tools": tool_schemas or []},
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        blob = str(messages) + str(tool_schemas)
    return _estimate_tokens(blob) + 32


def _budget_plan(
    state: AgentState,
    cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]] | None = None,
) -> tuple[bool, int, str, int]:
    """返回 (是否允许调用, 本次可用输出上限, 停止原因, 已用 Token)。

    预算不足时返回 False，调用方必须**不发起模型请求**，并记录停止原因。
    """
    budget = int(cfg.get("max_total_tokens") or 0)
    configured_max = int(cfg.get("max_tokens") or 0)
    used = int(state.get("token_total", 0) or 0)
    if budget <= 0:
        return True, configured_max, "", used
    remaining = budget - used
    estimated_input = _estimate_request_tokens(messages, tool_schemas or [])
    available_output = remaining - estimated_input
    min_output = int(cfg.get("min_output_tokens") or _MIN_OUTPUT_FALLBACK)
    if available_output < min_output:
        reason = (
            f"Token 预算预检查触发：已用 {used}，剩余 {remaining}，"
            f"本次输入预计 {estimated_input}，不足最小输出额度 {min_output}"
            f"（预算 {budget}），已停止新增模型调用"
        )
        return False, 0, reason, used
    return True, min(configured_max, available_output), "", used


def _usage_total(usage: dict[str, int] | None) -> int | None:
    """读取 Provider 返回的 usage 总 Token；未返回时返回 None。"""
    if not usage:
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))


def _budget_metadata(
    *, used_before: int, used_after: int, budget: int, call_max_tokens: int, estimated: bool
) -> dict[str, Any]:
    return {
        "token_budget": budget,
        "token_used_before": used_before,
        "token_used_after": used_after,
        "call_max_output_tokens": call_max_tokens,
        "usage_estimated": estimated,
    }


# ---------------------------------------------------------------------------
# LangGraph 节点
# ---------------------------------------------------------------------------

def _context_prefix(extra_context: Any) -> str:
    """上下文工程：把调用方提供的补充上下文规范化并截断（最多 4000 字符）。"""
    text = str(extra_context or "").strip()
    if not text:
        return ""
    return "\n\n[补充上下文（调用方提供）]\n" + text[:4000]


async def _understand_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """节点 1：任务理解（调用前做 Token 预算预检查）。"""
    cfg = (config or {}).get("configurable", {})
    client: AsyncOpenAI = cfg["client"]
    model: str = cfg["model"]
    trace_id: str = cfg["trace_id"]

    messages = [
        {
            "role": "system",
            "content": (
                "你是任务分析助手。请用 1-2 句话简要说明用户任务的核心目标与"
                "输入输出。直接给出结论，不要输出任何内部思考或推理过程。"
                + _context_prefix(cfg.get("extra_context"))
            ),
        },
        {"role": "user", "content": state["task"]},
    ]
    allowed, call_max, stop_reason, used_before = _budget_plan(state, cfg, messages)
    if not allowed:
        return {
            "budget_exceeded": True,
            "budget_reason": stop_reason,
            "token_total": used_before,
        }

    started = time.monotonic()
    message, usage, usage_meta = await _chat(
        client, model, messages, max_tokens=call_max,
    )
    used = _usage_total(usage)
    estimated = used is None
    if used is None:
        used = _estimate_request_tokens(messages, []) + _estimate_tokens(
            message.content or ""
        )
    budget = int(cfg.get("max_total_tokens") or 0)

    seq = state.get("step_seq", 0)
    step = _make_step(
        trace_id, seq + 1, step_type="model_call", parent_ids=[],
        input={"task": state["task"]},
        output=message.content or "",
        duration_ms=int((time.monotonic() - started) * 1000),
        token_usage=usage,
        metadata={
            "usage_meta": usage_meta,
            "budget": _budget_metadata(
                used_before=used_before,
                used_after=used_before + used,
                budget=budget,
                call_max_tokens=call_max,
                estimated=estimated,
            ),
        },
    )
    return {
        "understanding": message.content or "",
        "trace_steps": [step],
        "step_seq": seq + 1,
        "last_step_id": step["step_id"],
        "pending_parents": [step["step_id"]],
        "token_total": used_before + used,
        "usage_estimated": bool(state.get("usage_estimated")) or estimated,
    }


async def _plan_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """节点 2：生成结构化执行计划（调用前做 Token 预算预检查）。"""
    cfg = (config or {}).get("configurable", {})
    if state.get("budget_exceeded"):
        return {}
    client: AsyncOpenAI = cfg["client"]
    model: str = cfg["model"]
    trace_id: str = cfg["trace_id"]

    messages = [
        {
            "role": "system",
            "content": (
                "你是任务规划助手。请为任务生成简短的执行计划："
                "按编号列表输出，每步一句话，最多 5 步。"
                "只输出编号列表本身，不要输出任何解释或推理过程。"
                + _context_prefix(cfg.get("extra_context"))
            ),
        },
        {"role": "user", "content": f"任务：{state['task']}\n任务理解：{state.get('understanding', '')}"},
    ]
    allowed, call_max, stop_reason, used_before = _budget_plan(state, cfg, messages)
    if not allowed:
        return {
            "budget_exceeded": True,
            "budget_reason": stop_reason,
            "token_total": used_before,
        }

    started = time.monotonic()
    message, usage, usage_meta = await _chat(
        client, model, messages, max_tokens=call_max,
    )
    used = _usage_total(usage)
    estimated = used is None
    if used is None:
        used = _estimate_request_tokens(messages, []) + _estimate_tokens(
            message.content or ""
        )
    budget = int(cfg.get("max_total_tokens") or 0)

    plan = _parse_plan(message.content or "")
    plan_bullets = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(plan))

    seq = state.get("step_seq", 0)
    parents = [state["last_step_id"]] if state.get("last_step_id") else []
    step = _make_step(
        trace_id, seq + 1, step_type="planning", parent_ids=parents,
        input={"task": state["task"], "understanding": state.get("understanding", "")},
        output={"plan": plan},
        duration_ms=int((time.monotonic() - started) * 1000),
        token_usage=usage,
        metadata={
            "usage_meta": usage_meta,
            "budget": _budget_metadata(
                used_before=used_before,
                used_after=used_before + used,
                budget=budget,
                call_max_tokens=call_max,
                estimated=estimated,
            ),
        },
    )
    return {
        "plan": plan,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是执行助手。请按照执行计划完成用户任务，可以使用提供的工具"
                    "获取真实数据。优先调用工具验证，最后直接用中文给出最终结论。"
                    "不要输出内部思考或推理过程。"
                    + _context_prefix(cfg.get("extra_context"))
                ),
            },
            {"role": "user", "content": f"任务：{state['task']}\n执行计划：\n{plan_bullets}"},
        ],
        "trace_steps": [step],
        "step_seq": seq + 1,
        "last_step_id": step["step_id"],
        "pending_parents": [step["step_id"]],
        "token_total": used_before + used,
        "usage_estimated": bool(state.get("usage_estimated")) or estimated,
    }


async def _agent_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """节点 3：Agent 循环体（调用前做 Token 预算预检查与工具调用次数检查）。"""
    cfg = (config or {}).get("configurable", {})
    if state.get("budget_exceeded"):
        return {}
    client: AsyncOpenAI = cfg["client"]
    model: str = cfg["model"]
    trace_id: str = cfg["trace_id"]
    tool_schemas: list[dict[str, Any]] = cfg.get("tool_schemas", [])
    max_tool_rounds: int = int(cfg.get("max_tool_rounds") or DEFAULT_MAX_TOOL_ROUNDS)
    max_tool_calls: int = int(cfg.get("max_tool_calls") or 0)

    messages = state.get("messages") or []
    rounds = sum(1 for m in messages if m.get("role") == "tool")
    tool_calls_total = int(state.get("tool_call_total", 0) or 0)

    allowed, call_max, stop_reason, used_before = _budget_plan(
        state, cfg, messages, tool_schemas
    )
    if not allowed:
        return {
            "budget_exceeded": True,
            "budget_reason": stop_reason,
            "token_total": used_before,
        }

    started = time.monotonic()
    message, usage, usage_meta = await _chat(
        client, model, messages, max_tokens=call_max, tools=tool_schemas or None
    )
    used = _usage_total(usage)
    estimated = used is None
    if used is None:
        used = _estimate_request_tokens(messages, tool_schemas) + _estimate_tokens(
            message.content or ""
        )
    budget = int(cfg.get("max_total_tokens") or 0)

    assistant: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
    if getattr(message, "tool_calls", None):
        assistant["tool_calls"] = [tc.model_dump() for tc in message.tool_calls]

    seq = state.get("step_seq", 0)
    parents = list(state.get("pending_parents") or [])
    if not parents and state.get("last_step_id"):
        parents = [state["last_step_id"]]
    step_meta: dict[str, Any] = {
        "usage_meta": usage_meta,
        "budget": _budget_metadata(
            used_before=used_before,
            used_after=used_before + used,
            budget=budget,
            call_max_tokens=call_max,
            estimated=estimated,
        ),
    }
    step = _make_step(
        trace_id, seq + 1, step_type="model_call", parent_ids=parents,
        input={"messages": _sanitize_messages(messages)},
        output=assistant.get("content") or "",
        duration_ms=int((time.monotonic() - started) * 1000),
        token_usage=usage,
        metadata=step_meta,
    )
    token_total = used_before + used

    pending_calls = len(assistant.get("tool_calls") or [])
    remaining_tool_calls = max(0, max_tool_calls - tool_calls_total) if max_tool_calls > 0 else 0
    can_call_tools = (
        pending_calls > 0
        and rounds < max_tool_rounds
        and remaining_tool_calls >= pending_calls
    )
    tool_limit_reached = pending_calls > 0 and not can_call_tools
    if tool_limit_reached:
        assistant["content"] = (
            f"（工具调用达到本地上限：已调用 {tool_calls_total} 次，"
            f"本轮请求 {pending_calls} 次，上限 {max_tool_calls} 次；已停止继续调用）"
        )
        assistant.pop("tool_calls", None)
        step["output"] = assistant["content"]
        step_meta["tool_call_limit_reached"] = True
        step_meta["tool_calls_used"] = tool_calls_total

    if can_call_tools:
        return {
            "messages": [assistant],
            "trace_steps": [step],
            "step_seq": seq + 1,
            "last_step_id": step["step_id"],
            "pending_parents": [step["step_id"]],
            "token_total": token_total,
            "usage_estimated": bool(state.get("usage_estimated")) or estimated,
            "tool_calls_limit_reached": bool(state.get("tool_calls_limit_reached")),
        }

    if not assistant.get("content"):
        assistant["content"] = "（模型未返回文本内容）"
    return {
        "messages": [assistant],
        "result": assistant["content"],
        "trace_steps": [step],
        "step_seq": seq + 1,
        "last_step_id": step["step_id"],
        "token_total": token_total,
        "usage_estimated": bool(state.get("usage_estimated")) or estimated,
        "tool_calls_limit_reached": bool(state.get("tool_calls_limit_reached")) or tool_limit_reached,
    }


async def _tools_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """节点 4：执行模型选中的工具，记录工具步骤，回传结果给模型。"""
    cfg = (config or {}).get("configurable", {})
    registry: dict[str, Any] = cfg.get("tool_registry", {})
    trace_id: str = cfg["trace_id"]
    injector: FaultInjector | None = cfg.get("fault_injector")

    messages = state.get("messages") or []
    last = messages[-1] if messages else {}
    tool_calls = last.get("tool_calls", [])

    # 本轮所有工具步骤的父节点 = 触发它们的那个 agent 步骤
    parents = list(state.get("pending_parents") or [])
    seq = state.get("step_seq", 0)

    results: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    trace_steps: list[dict[str, Any]] = []

    for call in tool_calls:
        call_id = call.get("id", "")
        name = (call.get("function") or {}).get("name", "")
        raw_args = (call.get("function") or {}).get("arguments") or "{}"

        started = time.monotonic()
        seq += 1
        step_id = f"{trace_id}-s{seq:03d}"
        step_type = CAPABILITY_STEP_TYPES.get(name, "tool_call")

        # 前置类故障：命中即抛异常，记录为失败步骤（含故障标记）
        if injector is not None:
            try:
                injector.before_tool(name, step_id)
            except FaultInjectionError as exc:
                error_result = {"error": exc.message}
                results.append({
                    "role": "tool", "tool_call_id": call_id,
                    "content": json.dumps(error_result, ensure_ascii=False),
                })
                executions.append({"name": name, "arguments": raw_args, "result": error_result})
                trace_steps.append(_make_step(
                    trace_id, seq, step_type=step_type, parent_ids=parents,
                    tool_name=name, tool_arguments=None, tool_result=error_result,
                    status="failed",
                    error={"error_type": exc.fault_type.value, "error_message": exc.message, "recoverable": False},
                    duration_ms=int((time.monotonic() - started) * 1000),
                    metadata={"fault_injection": {"type": exc.fault_type.value, "injected": True, "message": exc.message}},
                ))
                continue

        try:
            arguments = json.loads(raw_args)
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as exc:
            error_result = {"error": f"工具参数解析失败: {exc}"}
            results.append({
                "role": "tool", "tool_call_id": call_id,
                "content": json.dumps(error_result, ensure_ascii=False),
            })
            executions.append({"name": name, "arguments": raw_args, "result": error_result})
            trace_steps.append(_make_step(
                trace_id, seq, step_type=step_type, parent_ids=parents,
                tool_name=name, tool_arguments=None, tool_result=error_result,
                status="failed",
                error={"error_type": "ArgumentError", "error_message": str(exc), "recoverable": False},
                duration_ms=int((time.monotonic() - started) * 1000),
                metadata={"raw_arguments": raw_args},
            ))
            continue

        tool_obj = registry.get(name)
        if tool_obj is None:
            error_result = {"error": f"未知工具: {name!r}"}
            results.append({
                "role": "tool", "tool_call_id": call_id,
                "content": json.dumps(error_result, ensure_ascii=False),
            })
            executions.append({"name": name, "arguments": arguments, "result": error_result})
            trace_steps.append(_make_step(
                trace_id, seq, step_type=step_type, parent_ids=parents,
                tool_name=name, tool_arguments=arguments, tool_result=error_result,
                status="failed",
                error={"error_type": "UnknownTool", "error_message": f"未知工具: {name!r}", "recoverable": False},
                duration_ms=int((time.monotonic() - started) * 1000),
            ))
            continue

        tool_timeout = float(cfg.get("tool_timeout_seconds") or 30.0)
        try:
            if tool_timeout > 0:
                output = await asyncio.wait_for(
                    asyncio.to_thread(tool_obj.invoke, arguments),
                    timeout=tool_timeout,
                )
            else:
                output = await asyncio.to_thread(tool_obj.invoke, arguments)
            fault_meta = None
            if injector is not None:
                output, fault_meta = injector.after_tool(name, step_id, output)
            content = json.dumps(output, ensure_ascii=False, default=str)
            executions.append({"name": name, "arguments": arguments, "result": output})
            step_meta = {"fault_injection": fault_meta} if fault_meta else {}
            trace_steps.append(_make_step(
                trace_id, seq, step_type=step_type, parent_ids=parents,
                tool_name=name, tool_arguments=arguments, tool_result=output,
                duration_ms=int((time.monotonic() - started) * 1000),
                metadata=step_meta,
            ))
        except asyncio.TimeoutError:
            # 执行环境管理：工具超时被强制终止，并返回结构化错误（不崩溃整个运行）
            error_result = {
                "error": f"工具执行超时（>{tool_timeout:g}s）",
                "error_type": "ToolTimeoutError",
            }
            content = json.dumps(error_result, ensure_ascii=False)
            executions.append({"name": name, "arguments": arguments, "result": error_result})
            trace_steps.append(_make_step(
                trace_id, seq, step_type=step_type, parent_ids=parents,
                tool_name=name, tool_arguments=arguments, tool_result=error_result,
                status="failed",
                error={
                    "error_type": "ToolTimeoutError",
                    "error_message": f"工具执行超时（>{tool_timeout:g}s）",
                    "recoverable": True,
                },
                duration_ms=int((time.monotonic() - started) * 1000),
                metadata={"timeout_seconds": tool_timeout},
            ))
        except ToolError as exc:
            error_result = {"error": str(exc)}
            content = json.dumps(error_result, ensure_ascii=False)
            executions.append({"name": name, "arguments": arguments, "result": error_result})
            trace_steps.append(_make_step(
                trace_id, seq, step_type=step_type, parent_ids=parents,
                tool_name=name, tool_arguments=arguments, tool_result=error_result,
                status="failed",
                error={"error_type": "ToolError", "error_message": str(exc), "recoverable": False},
                duration_ms=int((time.monotonic() - started) * 1000),
            ))
        except Exception as exc:
            error_result = {"error": f"{type(exc).__name__}: {exc}"}
            content = json.dumps(error_result, ensure_ascii=False)
            executions.append({"name": name, "arguments": arguments, "result": error_result})
            trace_steps.append(_make_step(
                trace_id, seq, step_type=step_type, parent_ids=parents,
                tool_name=name, tool_arguments=arguments, tool_result=error_result,
                status="failed",
                error={"error_type": type(exc).__name__, "error_message": str(exc), "recoverable": False},
                duration_ms=int((time.monotonic() - started) * 1000),
            ))

        results.append({"role": "tool", "tool_call_id": call_id, "content": content})

    # 反思能力（可选，默认关闭）：工具失败后触发一次模型反思，并记录 reflection 步骤。
    # 反思只用于下一步修正建议，不保存模型内部推理链；失败不阻断主流程。
    # 反思同样受 Token 预算预检查约束。
    failed_steps = [s for s in trace_steps if s.get("status") == "failed"]
    already_reflected = any(
        m.get("role") == "user" and str(m.get("content", "")).startswith("【反思提示】")
        for m in messages
    )
    tool_call_total = int(state.get("tool_call_total", 0) or 0) + len(tool_calls)
    token_total = int(state.get("token_total", 0) or 0)
    if cfg.get("enable_reflection") and failed_steps and not already_reflected:
        client = cfg.get("client")
        model = cfg.get("model")
        mt = int(cfg.get("max_tokens") or get_execution_limits().agent_max_output_tokens)
        if client is not None and model:
            seq += 1
            reflected_started = time.monotonic()
            failed_names = sorted(
                {str(s.get("tool_name") or s.get("step_type")) for s in failed_steps}
            )
            reflection_prompt = (
                "【反思提示】上一轮工具调用失败（"
                + ", ".join(failed_names)
                + "）。请用 1-2 句话反思可能原因，并给出下一步修正建议；"
                "不要输出内部思维链，也不要重复已失败且无变化的调用。"
            )
            reflection_messages = [
                *messages,
                *results,
                {"role": "user", "content": reflection_prompt},
            ]
            allowed, call_max, stop_reason, used_before = _budget_plan(
                state, cfg, reflection_messages
            )
            if not allowed:
                trace_steps.append(_make_step(
                    trace_id,
                    seq,
                    step_type="reflection",
                    parent_ids=[s["step_id"] for s in failed_steps],
                    output=None,
                    status="failed",
                    error={
                        "error_type": "TokenBudgetExceeded",
                        "error_message": stop_reason,
                        "recoverable": True,
                    },
                    duration_ms=0,
                    metadata={
                        "kind": "reflection_skipped_budget",
                        "failed_tools": failed_names,
                    },
                ))
            else:
                try:
                    reflection_message, usage, usage_meta = await _chat(
                        client,
                        model,
                        reflection_messages,
                        max_tokens=min(mt, 512, call_max),
                    )
                    reflection_used = _usage_total(usage)
                    if reflection_used is None:
                        reflection_used = _estimate_request_tokens(
                            reflection_messages, []
                        ) + _estimate_tokens(reflection_message.content or "")
                    token_total = used_before + reflection_used
                    reflection_text = (reflection_message.content or "")[:1500]
                    trace_steps.append(_make_step(
                        trace_id,
                        seq,
                        step_type="reflection",
                        parent_ids=[s["step_id"] for s in failed_steps],
                        output=reflection_text,
                        token_usage=usage,
                        duration_ms=int((time.monotonic() - reflected_started) * 1000),
                        metadata={
                            "usage_meta": usage_meta,
                            "kind": "reflection_after_tool_failure",
                            "failed_tools": failed_names,
                            "is_chain_of_thought": False,
                            "budget": _budget_metadata(
                                used_before=used_before,
                                used_after=token_total,
                                budget=int(cfg.get("max_total_tokens") or 0),
                                call_max_tokens=min(mt, 512, call_max),
                                estimated=_usage_total(usage) is None,
                            ),
                        },
                    ))
                    if reflection_text:
                        results.append(
                            {"role": "user", "content": "【反思提示】" + reflection_text}
                        )
                except Exception as exc:  # noqa: BLE001 - 反思失败不影响主流程
                    trace_steps.append(_make_step(
                        trace_id,
                        seq,
                        step_type="reflection",
                        parent_ids=[s["step_id"] for s in failed_steps],
                        output=None,
                        status="failed",
                        error={
                            "error_type": type(exc).__name__,
                            "error_message": f"反思调用失败: {exc}",
                            "recoverable": True,
                        },
                        duration_ms=int((time.monotonic() - reflected_started) * 1000),
                        metadata={"kind": "reflection_failed", "failed_tools": failed_names},
                    ))

    new_parents = [s["step_id"] for s in trace_steps]
    return {
        "messages": results,
        "tool_executions": executions,
        "trace_steps": trace_steps,
        "step_seq": seq,
        "last_step_id": trace_steps[-1]["step_id"] if trace_steps else state.get("last_step_id"),
        "pending_parents": new_parents,
        "tool_call_total": tool_call_total,
        "token_total": token_total,
        "usage_estimated": bool(state.get("usage_estimated")),
    }


def _route_after_agent(state: AgentState) -> str:
    """路由：预算不足或最后一条 assistant 消息带 tool_calls 时才继续，否则结束。"""
    if state.get("budget_exceeded"):
        return END
    messages = state.get("messages") or []
    if messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
        return "tools"
    return END


# ---------------------------------------------------------------------------
# 计划解析
# ---------------------------------------------------------------------------

_PLAN_PREFIX = re.compile(r"^\s*(?:\d+[.、)）:]|步骤\s*\d+\s*[:：])\s*")


def _parse_plan(text: str, max_steps: int = 5) -> list[str]:
    """把模型输出的编号列表解析为步骤字符串列表（截断到 max_steps）。"""
    steps: list[str] = []
    for line in text.splitlines():
        stripped = _PLAN_PREFIX.sub("", line.strip().lstrip("-#*·•")).strip()
        if not stripped:
            continue
        steps.append(stripped)
        if len(steps) >= max_steps:
            break
    return steps or [text.strip()]


# ---------------------------------------------------------------------------
# 图构建与主入口
# ---------------------------------------------------------------------------

def _build_graph():
    """构建 LangGraph：understand -> plan -> agent <-> tools。"""
    builder = StateGraph(AgentState)
    builder.add_node("understand", _understand_node)
    builder.add_node("plan", _plan_node)
    builder.add_node("agent", _agent_node)
    builder.add_node("tools", _tools_node)

    builder.add_edge(START, "understand")
    builder.add_edge("understand", "plan")
    builder.add_edge("plan", "agent")
    builder.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")
    return builder.compile()


# 编译一次，多次复用（langgraph 未安装时，import 阶段即会失败）
_GRAPH = _build_graph()


def _save_failed_trace(
    collector: TraceCollector | None, save_trace: bool, error_type: str, error_message: str
) -> None:
    """失败时尝试保存一条空步骤的 failed Trace（仅当开启保存时）。"""
    if collector is None or not save_trace:
        return
    try:
        record = collector.build(
            [], status=TraceStatus.FAILED,
            metadata={"error_type": error_type, "error_message": error_message},
        )
        collector.save(record)
    except Exception:
        pass  # 保存失败不掩盖原始异常


async def run_agent(
    task: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
    overall_timeout_seconds: float | None = None,
    tools: list[Any] | None = None,
    max_tool_rounds: int | None = None,
    api_max_retries: int = 1,
    max_tokens: int | None = None,
    max_total_tokens: int | None = None,
    max_tool_calls: int | None = None,
    collect_trace: bool = True,
    save_trace: bool = False,
    traces_dir: str | None = None,
    task_id: str | None = None,
    fault_injector: FaultInjector | None = None,
    extra_context: str | None = None,
    enable_reflection: bool = False,
    tool_timeout_seconds: float | None = 30.0,
) -> AgentResult:
    """异步运行 Agent（支持工具调用与轨迹采集）。

    Args:
        task: 用户任务描述。
        api_key / model / base_url / timeout_seconds / overall_timeout_seconds:
            模型连接配置，默认读环境变量。
        tools: 工具列表；默认「基础工具 + 能力工具（RAG/Memory/Skill/Context Review/HITL）」，
            传空列表禁用。
        max_tool_rounds: 工具循环最大轮数；None 时使用统一执行配置（LOCAL 6 / PROD 3）。
        max_tokens: 单次模型输出上限；None 时使用统一执行配置（LOCAL 2048 / PROD 512）。
        max_total_tokens: 单次任务总 Token 预算；None 时使用统一执行配置（LOCAL 12000 / PROD 4000）。
            预算在执行前预检查，耗尽时停止新增模型调用并保留部分 Trace。
        max_tool_calls: 工具调用总次数上限；None 时使用统一执行配置（LOCAL 8 / PROD 3）。
        collect_trace: 是否在结果中返回 TraceRecord（默认 True）。
        save_trace: 是否把 Trace 保存到 data/traces/（默认 False）。
        traces_dir: 轨迹保存目录（默认 data/traces/）。
        task_id: 任务 ID（默认自动生成）。
        extra_context: 上下文工程补充上下文（注入 system prompt，最多 4000 字符）。
        enable_reflection: 工具失败后是否触发一次反思步骤（默认关闭）。
        tool_timeout_seconds: 单个工具执行超时秒数；0/None 表示不限制（执行环境管理）。

    Returns:
        AgentResult: 含结果与（可选）TraceRecord；预算不足时 budget_exceeded=True。

    Raises:
        MissingApiKeyError / ModelCallError / AgentTimeoutError / AgentError
    """
    if not task or not task.strip():
        raise AgentError("任务不能为空")

    limits = get_execution_limits()
    effective_rounds = (
        limits.agent_max_tool_rounds if max_tool_rounds is None else max(1, int(max_tool_rounds))
    )
    effective_max_tokens = (
        limits.agent_max_output_tokens if max_tokens is None else max(1, int(max_tokens))
    )
    effective_total_tokens = (
        limits.agent_max_total_tokens
        if max_total_tokens is None
        else max(0, int(max_total_tokens))
    )
    effective_tool_calls = (
        limits.agent_max_tool_calls
        if max_tool_calls is None
        else max(0, int(max_tool_calls))
    )

    key, model_name, base, timeout = _resolve_config(
        api_key, model, base_url, timeout_seconds
    )

    selected_tools = (
        [*BASIC_TOOLS, *build_capability_tools()] if tools is None else list(tools)
    )
    tool_schemas = build_function_schemas(selected_tools) if selected_tools else []
    tool_registry = get_tool_registry(selected_tools)

    collector = TraceCollector(task_id=task_id, traces_dir=traces_dir)

    client = AsyncOpenAI(
        api_key=key,
        base_url=base,
        timeout=timeout,
        max_retries=max(0, int(api_max_retries)),
    )
    overall = (
        overall_timeout_seconds
        if overall_timeout_seconds is not None
        else limits.agent_max_wall_seconds
    )

    started = time.monotonic()
    run_config: RunnableConfig = {
        "configurable": {
            "client": client,
            "model": model_name,
            "trace_id": collector.trace_id,
            "tool_schemas": tool_schemas,
            "tool_registry": tool_registry,
            "max_tool_rounds": effective_rounds,
            "max_tokens": effective_max_tokens,
            "max_total_tokens": effective_total_tokens,
            "max_tool_calls": effective_tool_calls,
            "min_output_tokens": limits.agent_min_output_tokens,
            "fault_injector": fault_injector,
            "extra_context": extra_context,
            "enable_reflection": bool(enable_reflection),
            "tool_timeout_seconds": tool_timeout_seconds,
        }
    }

    state: AgentState = {}
    try:
        coro = _GRAPH.ainvoke({"task": task.strip()}, config=run_config)
        state = await asyncio.wait_for(coro, timeout=overall)
    except asyncio.TimeoutError as exc:
        _save_failed_trace(collector, save_trace, "AgentTimeoutError", str(exc))
        raise AgentTimeoutError(f"Agent 整体执行超时（>{overall:g}s）") from exc
    except AgentError as exc:
        _save_failed_trace(collector, save_trace, type(exc).__name__, str(exc))
        raise
    except Exception as exc:
        _save_failed_trace(collector, save_trace, type(exc).__name__, str(exc))
        raise AgentError(f"Agent 执行失败: {exc}") from exc
    finally:
        await client.close()

    messages = state.get("messages") or []
    tool_call_count = int(state.get("tool_call_total", 0) or 0)
    tool_call_count = max(
        tool_call_count,
        sum(len(m.get("tool_calls") or []) for m in messages if m.get("role") == "assistant"),
    )
    budget_exceeded = bool(state.get("budget_exceeded"))
    budget_reason = str(state.get("budget_reason") or "") or None
    token_total = int(state.get("token_total", 0) or 0)
    tool_calls_limit_reached = bool(state.get("tool_calls_limit_reached"))
    token_budget_overrun = bool(effective_total_tokens > 0 and token_total > effective_total_tokens)

    result_text = str(state.get("result") or "")
    if budget_exceeded and not result_text:
        result_text = (
            "（Token 预算不足，任务未完成；已保留此前执行步骤与 Trace，"
            "可在提高预算后重试或缩小任务范围）"
        )

    trace: TraceRecord | None = None
    trace_path: str | None = None
    if collect_trace:
        raw_steps = list(state.get("trace_steps", []))
        fault_indices = [
            i for i, s in enumerate(raw_steps)
            if s.get("metadata", {}).get("fault_injection")
        ]
        recovery_observed = bool(fault_indices) and (max(fault_indices) < len(raw_steps) - 1)
        meta: dict[str, Any] = {
            "model": model_name,
            "tool_call_count": tool_call_count,
            "capability_context_used": bool(extra_context),
            "reflection_enabled": bool(enable_reflection),
            "tool_timeout_seconds": tool_timeout_seconds,
            "capability_tools": list(CAPABILITY_STEP_TYPES),
            "execution_profile": limits.profile,
            "token_budget": effective_total_tokens,
            "token_total_estimate": token_total,
            "token_usage_estimated": bool(state.get("usage_estimated")),
            "token_budget_overrun": token_budget_overrun,
            "tool_call_limit": effective_tool_calls,
            "max_tool_rounds": effective_rounds,
            "max_output_tokens_per_call": effective_max_tokens,
            "budget_exceeded": budget_exceeded,
            "budget_reason": budget_reason,
            "tool_calls_limit_reached": tool_calls_limit_reached,
        }
        if fault_injector is not None:
            meta["fault_injection_config"] = fault_injector.get_config()
            meta["fault_injections"] = list(fault_injector.fired)
            meta["recovery_observed"] = recovery_observed
        try:
            trace = collector.build(
                raw_steps,
                status=(
                    TraceStatus.FAILED
                    if budget_exceeded
                    else (
                        TraceStatus.PARTIAL
                        if (tool_calls_limit_reached or token_budget_overrun or not result_text)
                        else TraceStatus.SUCCEEDED
                    )
                ),
                metadata=meta,
            )
            if save_trace:
                trace_path = str(collector.save(trace))
        except ValidationError as exc:
            raise AgentError(f"轨迹数据不符合 Trace Schema: {exc}") from exc

    return AgentResult(
        task=task.strip(),
        understanding=state.get("understanding", ""),
        plan=list(state.get("plan", [])),
        result=result_text,
        model=model_name,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        tool_call_count=tool_call_count,
        tool_executions=list(state.get("tool_executions", [])),
        trace=trace,
        trace_path=trace_path,
        budget_exceeded=budget_exceeded,
        budget_reason=budget_reason,
        token_total=token_total,
        tool_calls_limit_reached=tool_calls_limit_reached,
    )


def run_agent_sync(task: str, **kwargs: Any) -> AgentResult:
    """同步便捷包装：内部使用 asyncio.run 调用 run_agent。"""
    return asyncio.run(run_agent(task, **kwargs))
