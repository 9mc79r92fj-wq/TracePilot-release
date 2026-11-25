"""Counterfactual Engine 反事实实验引擎（第 31 步）。

在受控条件下，对已有故障 Trace 施加单一/受控干预（关闭故障、替换工具结果、
调整超时/重试），重新执行受限 Agent 流程，生成独立反事实 Trace，并对比
「观察结果 vs 干预结果」。

核心纪律（不可违反）：
- 原始 Trace 只读，绝不修改；
- 每次实验生成独立 experiment_id / run_id / trace_id；
- 仅支持白名单干预类型，其他一律拒绝；
- 默认模拟模型（标注 simulated=true），不调用付费 API；
- 仅使用本地无副作用白名单工具（calculator / text_analyzer）；
- 有界重试次数、有界执行步数；
- 不执行任意 Shell / Python / 代码 / 外网访问。

因果可信度分级（causal_evidence_level）：
- none        仅观察，无干预（无法推断因果）
- weak        单次干预重放，存在模型随机性/模拟模型等未控因素
- moderate    在同一受控配置下多次重放结果一致（可复现），但仍为模拟环境
- 绝不输出 strong：真实因果需真实模型、真实随机重放、外部效度验证。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from backend.app.schemas.trace import TraceRecord, TraceStatus, TraceStep, TokenUsage
from backend.app.services.failure_replay import SCENARIO_WHITELIST
from backend.app.services.trace_comparator import TraceComparator
from backend.app.tools.basic_tools import calculator, text_analyzer

__all__ = [
    "CounterfactualError",
    "UnsupportedInterventionError",
    "INTERVENTION_TYPES",
    "ENGINE_VERSION",
    "run_counterfactual_experiment",
]

ENGINE_VERSION = "0.1.0"
MODEL_INFO = {
    "provider": "local-simulation",
    "model": "simulated-scripted-model",
    "simulated": True,
    "paid_api_called": False,
}

# 安全工具白名单
_SAFE_TOOLS = {"calculator": calculator, "text_analyzer": text_analyzer}

# 允许的干预类型（白名单）
INTERVENTION_TYPES = {
    "remove_fault",          # 关闭指定故障
    "replace_tool_result",   # 替换白名单模拟工具结果
    "adjust_timeout_retry",  # 调整超时 / 重试次数
}

MAX_ATTEMPTS_LIMIT = 5
MAX_TIMEOUT_MS_LIMIT = 60_000
MAX_REPLAYS = 5


class CounterfactualError(ValueError):
    """反事实实验请求非法。"""


class UnsupportedInterventionError(CounterfactualError):
    """干预类型不支持或超出白名单。"""


# ---------------------------------------------------------------------------
# 输入验证
# ---------------------------------------------------------------------------

def _validate_intervention(intervention: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(intervention, dict):
        raise CounterfactualError("intervention 必须是对象")
    kind = intervention.get("type")
    if kind not in INTERVENTION_TYPES:
        raise UnsupportedInterventionError(
            f"不支持的干预类型: {kind!r}（允许: {', '.join(sorted(INTERVENTION_TYPES))}）"
        )

    if kind == "remove_fault":
        return {"type": kind}

    if kind == "replace_tool_result":
        tool = intervention.get("tool")
        if tool not in _SAFE_TOOLS:
            raise UnsupportedInterventionError(
                f"仅允许替换白名单工具结果，收到: {tool!r}"
            )
        value = intervention.get("value")
        # 限制体积与类型，避免注入任意结构
        try:
            import json as _json

            size = len(_json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise CounterfactualError(f"value 必须可 JSON 序列化: {exc}") from exc
        if size > 4096:
            raise CounterfactualError("value 过大（上限 4KB）")
        return {"type": kind, "tool": tool, "value": value}

    # adjust_timeout_retry
    timeout = intervention.get("timeout_ms")
    retries = intervention.get("max_retries")
    if timeout is not None and (
        not isinstance(timeout, int) or not (0 < timeout <= MAX_TIMEOUT_MS_LIMIT)
    ):
        raise CounterfactualError(f"timeout_ms 必须为 1..{MAX_TIMEOUT_MS_LIMIT} 的整数")
    if retries is not None and (
        not isinstance(retries, int) or not (0 <= retries <= MAX_ATTEMPTS_LIMIT)
    ):
        raise CounterfactualError(f"max_retries 必须为 0..{MAX_ATTEMPTS_LIMIT} 的整数")
    return {"type": kind, "timeout_ms": timeout, "max_retries": retries}


def _last_tool_status(record: TraceRecord) -> str | None:
    """返回最后一条工具调用步骤的状态（无工具步骤返回 None）。"""
    for s in reversed(record.steps):
        if s.step_type.value == "tool_call":
            return s.status.value
    return None


def _extract_fault_config(trace: TraceRecord) -> list[dict[str, Any]]:
    cfg = trace.metadata.get("fault_injection_config")
    if isinstance(cfg, list) and cfg:
        return [c for c in cfg if isinstance(c, dict)]
    out: list[dict[str, Any]] = []
    for s in trace.steps:
        fi = s.metadata.get("fault_injection")
        if isinstance(fi, dict) and fi.get("injected") is True:
            out.append({"fault_type": fi.get("type"), "target_tool": s.tool_name, "count": 1})
    return out


def _pick_scenario(trace: TraceRecord, scenario: str | None) -> str:
    if scenario:
        if scenario not in SCENARIO_WHITELIST:
            raise UnsupportedInterventionError(f"场景 {scenario!r} 不在白名单内")
        return scenario
    for s in trace.steps:
        fi = s.metadata.get("fault_injection")
        if isinstance(fi, dict) and fi.get("injected") is True:
            if s.tool_name == "text_analyzer":
                return "text_analyzer"
            return "calculator_add"
    return "calculator_add"


# ---------------------------------------------------------------------------
# 受控执行（单次）
# ---------------------------------------------------------------------------

def _run_controlled(
    *,
    trace_id: str,
    run_id: str,
    task_id: str,
    scenario: str,
    fault_config: list[dict[str, Any]],
    intervention: dict[str, Any],
    max_retries: int,
) -> TraceRecord:
    """在有界条件下执行一次受控模拟流程，生成 TraceRecord。"""
    from backend.app.services.fault_injector import FaultInjector, FaultType

    scen = SCENARIO_WHITELIST[scenario]
    tool_name: str = scen["tool"]
    if tool_name not in _SAFE_TOOLS:
        raise UnsupportedInterventionError(f"场景工具 {tool_name!r} 不在安全白名单内")

    kind = intervention["type"]

    # 构建故障注入器（受干预影响）
    injector = FaultInjector()
    if kind != "remove_fault":
        for c in fault_config:
            ft = c.get("fault_type")
            if ft not in {t.value for t in FaultType}:
                continue
            injector.add_rule(
                FaultType(ft),
                target_tool=c.get("target_tool"),
                count=max(1, min(int(c.get("count") or 1), MAX_ATTEMPTS_LIMIT)),
            )

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
            "metadata": {"counterfactual": True, "simulated_model": True},
        }

    # 1) 模拟模型规划
    plan = base("planning", [])
    plan["input"] = {"task": scen["label"]}
    plan["output"] = {"plan": ["调用工具", "给出结论"], "simulated": True}
    plan["token_usage"] = TokenUsage(input_tokens=0, output_tokens=0)
    steps.append(plan)
    last = plan["step_id"]

    # 2) 模拟模型请求工具
    call = base("model_call", [last])
    call["input"] = {"messages": [{"role": "user", "content": scen["label"]}]}
    call["output"] = ""
    call["token_usage"] = TokenUsage(input_tokens=0, output_tokens=0)
    steps.append(call)
    last = call["step_id"]

    # 3) 工具执行（有界重试）
    tool_ok = False
    attempts = 0
    max_attempts = max(1, min(max_retries, MAX_ATTEMPTS_LIMIT))
    tool_step: dict[str, Any] | None = None
    while attempts < max_attempts and not tool_ok:
        attempts += 1
        tool_step = base("tool_call", [last])
        tool_step["tool_name"] = tool_name
        tool_step["tool_arguments"] = dict(scen["arguments"])
        tool_step["metadata"]["attempt"] = attempts
        if kind == "replace_tool_result" and intervention.get("tool") == tool_name:
            tool_step["tool_result"] = intervention["value"]
            tool_step["metadata"]["intervened"] = "replace_tool_result"
            tool_ok = True
            steps.append(tool_step)
            last = tool_step["step_id"]
            break
        try:
            injector.before_tool(tool_name, tool_step["step_id"])
        except Exception as exc:  # FaultInjectionError
            tool_step["status"] = "failed"
            tool_step["tool_result"] = {"error": str(exc)}
            tool_step["error"] = {
                "error_type": getattr(getattr(exc, "fault_type", None), "value", "tool_exception"),
                "error_message": str(exc),
                "recoverable": True,
            }
            tool_step["metadata"]["fault_injection"] = {
                "type": tool_step["error"]["error_type"],
                "injected": True,
                "message": tool_step["error"]["error_message"],
            }
            steps.append(tool_step)
            last = tool_step["step_id"]
            continue
        try:
            out = _SAFE_TOOLS[tool_name].invoke(dict(scen["arguments"]))
            out, fmeta = injector.after_tool(tool_name, tool_step["step_id"], out)
            tool_step["tool_result"] = out
            if fmeta:
                tool_step["metadata"]["fault_injection"] = fmeta
            tool_ok = True
        except Exception as exc:
            tool_step["status"] = "failed"
            tool_step["tool_result"] = {"error": str(exc)}
            tool_step["error"] = {
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "recoverable": True,
            }
        steps.append(tool_step)
        last = tool_step["step_id"]

    # 4) 模拟模型最终结论
    final = base("model_call", [last])
    final["input"] = {"messages": [{"role": "tool", "content": "done" if tool_ok else "failed"}]}
    final["output"] = "任务完成" if tool_ok else "任务失败：工具执行未成功"
    final["token_usage"] = TokenUsage(input_tokens=0, output_tokens=0)
    if not tool_ok:
        final["status"] = "failed"
        final["error"] = {
            "error_type": "ToolFailure",
            "error_message": "工具在允许的重试次数内未成功",
            "recoverable": False,
        }
    steps.append(final)

    from backend.app.schemas.trace import TraceStatus

    return TraceRecord(
        trace_id=trace_id,
        task_id=task_id,
        run_id=run_id,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc),
        status=TraceStatus.SUCCEEDED if tool_ok else TraceStatus.FAILED,
        steps=[TraceStep.model_validate(s) for s in steps],
        metadata={
            "counterfactual": True,
            "scenario": scenario,
            "intervention": intervention,
            "fault_injection_config": injector.get_config(),
            "fault_injections": injector.fired,
            "attempts": attempts,
            "simulated": True,
        },
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run_counterfactual_experiment(
    trace: TraceRecord,
    intervention: dict[str, Any],
    *,
    scenario: str | None = None,
    replays: int = 1,
    experiment_id: str | None = None,
    persist_derived: bool = True,
    derivation: str = "counterfactual",
) -> dict[str, Any]:
    """对故障 Trace 执行受控反事实实验。

    Args:
        trace: 原始故障 Trace（只读）。
        intervention: 干预定义（type ∈ INTERVENTION_TYPES）。
        scenario: 白名单场景 key；None 时自动推断。
        replays: 重复次数 1..5（用于可复现性判定）。

    Returns:
        实验报告 dict（含 observed/intervention outcome、差异、因果证据等级、限制）。
    """
    if not (1 <= replays <= MAX_REPLAYS):
        raise CounterfactualError(f"replays 必须在 1..{MAX_REPLAYS}")

    validated = _validate_intervention(intervention)
    scen_key = _pick_scenario(trace, scenario)
    fault_config = _extract_fault_config(trace)

    # 若上层（experiment_store）已分配 experiment_id，则复用，
    # 避免「登记表 ID」与「引擎结果 ID」两套标识导致证据链断裂
    experiment_id = experiment_id or f"exp-{uuid.uuid4().hex}"

    # 观察结果（不执行任何东西，仅读取原始 Trace）
    observed_status = trace.status.value
    observed_faults = sorted(
        {
            s.metadata["fault_injection"].get("type")
            for s in trace.steps
            if isinstance(s.metadata.get("fault_injection"), dict)
            and s.metadata["fault_injection"].get("injected") is True
        }
    )

    runs: list[TraceRecord] = []
    for i in range(replays):
        run_id = f"run-cf-{uuid.uuid4().hex}"
        trace_id = f"trace-cf-{uuid.uuid4().hex}"
        runs.append(
            _run_controlled(
                trace_id=trace_id,
                run_id=run_id,
                task_id=trace.task_id,
                scenario=scen_key,
                fault_config=fault_config,
                intervention=validated,
                max_retries=(
                    validated.get("max_retries")
                    if validated["type"] == "adjust_timeout_retry" and validated.get("max_retries") is not None
                    else 1
                ),
            )
        )

    # 派生 Trace 落盘：使 trace-cf-* 可被 /api/traces/{id} 查询（证据链可追溯）。
    # 落盘失败不影响实验本身，但会被记录到 persisted_errors。
    persisted_errors: list[str] = []
    persisted_ids: list[str] = []
    if persist_derived:
        try:
            from backend.app.services.derived_trace import save_derived_trace
        except Exception:  # pragma: no cover - 导入异常降级
            save_derived_trace = None  # type: ignore[assignment]
        for r in runs:
            try:
                if save_derived_trace is not None:
                    save_derived_trace(
                        r,
                        trace,
                        derivation=derivation,
                        engine_version=ENGINE_VERSION,
                        extra={"counterfactual_experiment_id": experiment_id},
                    )
                    persisted_ids.append(r.trace_id)
            except Exception as exc:  # 落盘失败不伪造成功
                persisted_errors.append(f"{r.trace_id}: {type(exc).__name__}: {exc}")

    primary = runs[0]

    intervention_faults = sorted(
        {
            s.metadata["fault_injection"].get("type")
            for s in primary.steps
            if isinstance(s.metadata.get("fault_injection"), dict)
            and s.metadata["fault_injection"].get("injected") is True
        }
    )

    comparison = TraceComparator().compare(trace, primary)

    # 可复现性：多次重放的最终状态与故障集合是否一致
    reproducible = all(
        r.status == primary.status
        and sorted(
            {
                s.metadata["fault_injection"].get("type")
                for s in r.steps
                if isinstance(s.metadata.get("fault_injection"), dict)
                and s.metadata["fault_injection"].get("injected") is True
            }
        )
        == intervention_faults
        for r in runs
    )

    observed_outcome = {
        "trace_id": trace.trace_id,
        "status": observed_status,
        "fault_types": observed_faults,
        "executed": False,
        "note": "仅为原始 Trace 的观察记录，未重新执行",
    }
    intervention_outcome = {
        "trace_id": primary.trace_id,
        "run_id": primary.run_id,
        "status": primary.status.value,
        "fault_types": intervention_faults,
        "attempts": primary.metadata.get("attempts"),
        "simulated": True,
        # 工具是否最终恢复（最后一次工具调用成功）；用于判定"重试等修复是否生效"
        "tool_recovered": _last_tool_status(primary) == "succeeded",
        "tool_step_statuses": [
            s.status.value for s in primary.steps if s.step_type.value == "tool_call"
        ],
    }

    observed_difference = {
        "status_changed": observed_status != primary.status.value,
        "fault_types_removed": sorted(set(observed_faults) - set(intervention_faults)),
        "fault_types_added": sorted(set(intervention_faults) - set(observed_faults)),
        "comparison_summary": {
            "diff_node_count": len(comparison["diff_nodes"]),
            "unmatched_reference": comparison["unmatched"]["reference_only"],
            "unmatched_target": comparison["unmatched"]["target_only"],
        },
    }

    # 因果证据等级（保守，但必须反映"确实施加了干预"这一事实）
    # - none：无任何可观察差异（干预未改变故障集合与状态）
    # - weak：观察到差异，但为单次重放 / 模拟环境
    # - moderate：观察到差异 + 多次重放可复现，且最终状态确有变化
    fault_set_changed = observed_faults != intervention_faults
    status_changed = observed_status != primary.status.value
    if not fault_set_changed and not status_changed:
        level = "none"
    elif reproducible and replays > 1 and status_changed:
        level = "moderate"
    else:
        level = "weak"

    return {
        "experiment_id": experiment_id,
        "engine_version": ENGINE_VERSION,
        "source_trace_id": trace.trace_id,
        "task_id": trace.task_id,
        "scenario": scen_key,
        "intervention": validated,
        "intervention_applied": True,
        "replays": replays,
        "reproducible": reproducible,
        "derived_trace_persisted": persisted_ids,
        "derived_trace_errors": persisted_errors,
        "observed_outcome": observed_outcome,
        "intervention_outcome": intervention_outcome,
        "observed_difference": observed_difference,
        "comparison": comparison,
        "causal_evidence_level": level,
        "causal_evidence_note": {
            "none": "仅有观察数据，不能推断因果。",
            "weak": "单次/有限干预重放；存在模拟模型、执行路径简化等未控因素，仅支持弱因果提示。",
            "moderate": "同一受控配置多次重放结果一致；仍为模拟环境，不能推广到真实模型或生产条件。",
        }[level],
        "environment": {
            "model": MODEL_INFO,
            "tools": sorted(_SAFE_TOOLS),
            "fault_scenarios": sorted(SCENARIO_WHITELIST),
            "bounded": {"max_retries": MAX_ATTEMPTS_LIMIT, "max_replays": MAX_REPLAYS},
        },
        "control_conditions": [
            "固定白名单场景与参数（无自由输入执行）",
            "模拟模型响应固定（无随机采样）",
            "单一干预变量，其余故障配置保持原始",
            "工具仅限本地无副作用白名单",
        ],
        "confounders": [
            "模拟模型与真实模型行为差异",
            "原始 Trace 采集时的模型版本/温度未知",
            "执行路径简化为 规划→工具→结论，未复现原始完整计划",
            "工具为模拟或简化实现",
        ],
        "limitations": [
            "不可将 observed_difference 视为因果证明",
            "未覆盖真实 API 随机性与真实环境故障",
            "仅支持 3 种白名单干预类型",
            "不产生 strong 级因果证据",
        ],
        "original_trace_modified": False,
    }
