"""Failure Replay 服务（第 30 步）。

两种重放模式：

模式一 Recorded Replay：只读原始故障 Trace，重建可观察事件序列。
- 不重新执行任何工具；
- 不调用任何模型 API；
- 不把 Trace 内容当作可执行指令。

模式二 Controlled Re-run：读取原始 Trace 的故障配置，在隔离环境中
用「模拟模型 + 无副作用本地工具」重新运行：
- 使用固定白名单场景（不允许任意工具 / 任意表达式 / 任意文件）；
- 生成新的独立 trace_id / run_id，绝不覆盖原始 Trace；
- 对比两次运行，标记可重复现象与不一致行为。

安全边界：
- 仅允许白名单内的安全工具（不允许 subprocess / 文件写入 / 系统命令）；
- 场景参数经白名单校验（工具名白名单、表达式白名单、文本长度上限）；
- 不访问业务数据库（不触碰建表 / 写入路径）；
- 不调用付费 API（模拟模型响应固定）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from backend.app.schemas.trace import TraceRecord, TraceStatus, TokenUsage
from backend.app.services.trace_comparator import TraceComparator
from backend.app.services.trace_dag import build_dag
from backend.app.tools.basic_tools import calculator, text_analyzer

__all__ = [
    "ReplayError",
    "UnsupportedScenarioError",
    "SCENARIO_WHITELIST",
    "recorded_replay",
    "controlled_rerun",
]

# 固定、受控的故障场景白名单：仅允许无副作用的本地计算/统计工具
# 与固定表达式；不允许任何系统命令、文件写操作、网络访问。
SCENARIO_WHITELIST: dict[str, dict[str, Any]] = {
    "calculator_add": {
        "tool": "calculator",
        "arguments": {"expression": "1+1"},
        "label": "计算 1+1",
    },
    "calculator_div_zero": {
        "tool": "calculator",
        "arguments": {"expression": "1/0"},
        "label": "计算 1/0（除零）",
    },
    "text_analyzer": {
        "tool": "text_analyzer",
        "arguments": {"text": "TracePilot failure replay sanity check"},
        "label": "文本统计",
    },
}

# 安全工具注册表：仅这三个本地无副作用工具
_SAFE_TOOLS = {
    "calculator": calculator,
    "text_analyzer": text_analyzer,
}

_ALLOWED_FAULT_TYPES = {
    "tool_timeout",
    "tool_exception",
    "invalid_tool_result",
    "empty_retrieval",
    "invalid_arguments",
    "permission_denied",
}

MAX_REPLAY_STEPS = 20


class ReplayError(ValueError):
    """重放请求非法。"""


class UnsupportedScenarioError(ReplayError):
    """场景不在白名单内。"""


# ---------------------------------------------------------------------------
# 模式一：Recorded Replay
# ---------------------------------------------------------------------------

def recorded_replay(trace: TraceRecord) -> dict[str, Any]:
    """只读重放：重建可观察事件序列，不执行任何工具 / 模型。"""
    dag = build_dag(trace)
    by_id = {s.step_id: s for s in trace.steps}
    events: list[dict[str, Any]] = []

    for index, sid in enumerate(dag.topological_order):
        step = by_id[sid]
        fi = step.metadata.get("fault_injection")
        events.append(
            {
                "index": index,
                "step_id": sid,
                "step_type": step.step_type.value,
                "status": step.status.value,
                "timestamp": step.timestamp.isoformat(),
                "is_fault": bool(fi and fi.get("injected") is True),
                "fault_type": fi.get("type") if fi else None,
                "error_type": step.error.error_type if step.error else None,
                "error_message": step.error.error_message if step.error else None,
                "tool_name": step.tool_name,
                "duration_ms": step.duration_ms,
                "parent_step_ids": list(step.parent_step_ids),
            }
        )

    fault_events = [e for e in events if e["is_fault"]]
    first_fault = fault_events[0]["index"] if fault_events else None
    after_fault = events[first_fault + 1:] if first_fault is not None else events

    return {
        "mode": "recorded",
        "trace_id": trace.trace_id,
        "status": trace.status.value,
        "step_count": len(events),
        "events": events,
        "fault_event_indices": [e["index"] for e in fault_events],
        "first_fault_index": first_fault,
        "subsequent_execution": [
            {"index": e["index"], "step_id": e["step_id"], "step_type": e["step_type"], "status": e["status"]}
            for e in after_fault
        ],
        "executed_anything": False,
        "note": "只读重放：未执行工具、未调用模型 API。",
    }


# ---------------------------------------------------------------------------
# 模式二：Controlled Re-run（隔离模拟）
# ---------------------------------------------------------------------------

def _extract_fault_config(trace: TraceRecord) -> list[dict[str, Any]]:
    """从原始 Trace 中提取故障配置（只读取 metadata，不执行任何内容）。"""
    cfg = trace.metadata.get("fault_injection_config")
    if isinstance(cfg, list) and cfg:
        return [c for c in cfg if isinstance(c, dict)]
    # 回退：从故障步骤的 metadata 归纳
    out: list[dict[str, Any]] = []
    for s in trace.steps:
        fi = s.metadata.get("fault_injection")
        if fi and fi.get("injected") is True:
            out.append(
                {
                    "fault_type": fi.get("type"),
                    "target_tool": s.tool_name,
                    "target_step": None,
                    "count": 1,
                }
            )
    return out


def _scenario_from_trace(trace: TraceRecord) -> str:
    """根据故障步骤使用的工具推断白名单场景（不使用 Trace 内容作为指令）。"""
    for s in trace.steps:
        fi = s.metadata.get("fault_injection")
        if fi and fi.get("injected") is True:
            if s.tool_name == "calculator":
                return "calculator_add"
            if s.tool_name == "text_analyzer":
                return "text_analyzer"
    return "calculator_add"


def _validate_fault_config(configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for c in configs:
        ft = c.get("fault_type")
        if ft not in _ALLOWED_FAULT_TYPES:
            raise UnsupportedScenarioError(f"不支持的故障类型: {ft!r}")
        tool = c.get("target_tool")
        if tool is not None and tool not in _SAFE_TOOLS:
            raise UnsupportedScenarioError(f"不允许对工具 {tool!r} 注入故障（仅限白名单安全工具）")
        validated.append(
            {
                "fault_type": ft,
                "target_tool": tool,
                "count": max(1, min(int(c.get("count") or 1), 10)),
            }
        )
    return validated


def controlled_rerun(
    trace: TraceRecord,
    scenario: str | None = None,
) -> dict[str, Any]:
    """受控重跑：模拟模型 + 白名单安全工具，生成新 Trace。

    Returns:
        {"new_trace_id", "old_trace_id", "scenario", "trace": TraceRecord 序列化,
         "comparison": 对比报告, "repeatable": [...], "inconsistent": [...]}
    """
    scen_key = scenario or _scenario_from_trace(trace)
    if scen_key not in SCENARIO_WHITELIST:
        raise UnsupportedScenarioError(f"场景 {scen_key!r} 不在白名单内")

    scen = SCENARIO_WHITELIST[scen_key]
    tool_name: str = scen["tool"]
    if tool_name not in _SAFE_TOOLS:
        raise UnsupportedScenarioError(f"场景工具 {tool_name!r} 不在安全白名单内")

    configs = _validate_fault_config(_extract_fault_config(trace))

    from backend.app.services.fault_injector import FaultInjector, FaultType

    injector = FaultInjector()
    for c in configs:
        injector.add_rule(
            FaultType(c["fault_type"]),
            target_tool=c["target_tool"],
            count=c["count"],
        )

    new_trace_id = f"trace-replay-{uuid.uuid4().hex}"
    new_run_id = f"run-replay-{uuid.uuid4().hex}"
    started_at = datetime.now(timezone.utc)

    steps: list[dict[str, Any]] = []
    seq = 0

    def _base_step(step_type: str, parents: list[str]) -> dict[str, Any]:
        nonlocal seq
        seq += 1
        return {
            "step_id": f"{new_trace_id}-s{seq:03d}",
            "trace_id": new_trace_id,
            "parent_step_ids": list(parents),
            "step_type": step_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "succeeded",
            "metadata": {"replay": True, "scenario": scen_key},
        }

    # 1) 模拟模型：规划步骤（固定响应，非真实 API）
    plan = _base_step("planning", [])
    plan["input"] = {"task": scen["label"]}
    plan["output"] = {"plan": ["调用工具", "给出结论"], "simulated": True}
    plan["token_usage"] = TokenUsage(input_tokens=0, output_tokens=0)
    plan["metadata"]["simulated_model"] = True
    steps.append(plan)
    last = plan["step_id"]

    # 2) 模型请求工具（模拟）
    call = _base_step("model_call", [last])
    call["input"] = {"messages": [{"role": "user", "content": scen["label"]}]}
    call["output"] = ""
    call["token_usage"] = TokenUsage(input_tokens=0, output_tokens=0)
    call["metadata"]["simulated_model"] = True
    steps.append(call)
    last = call["step_id"]

    # 3) 工具执行（真实本地白名单工具 + 故障注入）
    tool_step = _base_step("tool_call", [last])
    tool_step["tool_name"] = tool_name
    tool_step["tool_arguments"] = dict(scen["arguments"])
    fault_raised = False
    try:
        injector.before_tool(tool_name, tool_step["step_id"])
    except Exception as exc:  # FaultInjectionError
        tool_step["status"] = "failed"
        tool_step["tool_result"] = {"error": str(exc)}
        tool_step["error"] = {
            "error_type": getattr(exc, "fault_type", FaultType("tool_exception")).value,
            "error_message": str(exc),
            "recoverable": False,
        }
        fault_raised = True
    if not fault_raised:
        try:
            output = _SAFE_TOOLS[tool_name].invoke(dict(scen["arguments"]))
            output, fault_meta = injector.after_tool(tool_name, tool_step["step_id"], output)
            tool_step["tool_result"] = output
            if fault_meta:
                tool_step["metadata"]["fault_injection"] = fault_meta
        except Exception as exc:
            tool_step["status"] = "failed"
            tool_step["tool_result"] = {"error": str(exc)}
            tool_step["error"] = {
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "recoverable": False,
            }
    if fault_raised:
        tool_step["metadata"]["fault_injection"] = {
            "type": tool_step["error"]["error_type"],
            "injected": True,
            "message": tool_step["error"]["error_message"],
        }
    steps.append(tool_step)
    last = tool_step["step_id"]

    # 4) 模拟模型：最终结论
    final = _base_step("model_call", [last])
    final["input"] = {"messages": [{"role": "tool", "content": "replay"}]}
    final["output"] = "最终回答：重放完成"
    final["token_usage"] = TokenUsage(input_tokens=0, output_tokens=0)
    final["metadata"]["simulated_model"] = True
    steps.append(final)

    from backend.app.schemas.trace import TraceStep

    new_record = TraceRecord(
        trace_id=new_trace_id,
        task_id=trace.task_id,
        run_id=new_run_id,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc),
        status=TraceStatus.SUCCEEDED,
        steps=[TraceStep.model_validate(s) for s in steps],
        metadata={
            "replay_of": trace.trace_id,
            "scenario": scen_key,
            "fault_injection_config": injector.get_config(),
            "fault_injections": injector.fired,
            "simulated": True,
            "simulation_note": "模型响应为固定模拟；工具为白名单安全本地工具",
        },
    )

    comparison = TraceComparator().compare(trace, new_record)

    # 可重复现象 vs 不一致行为
    old_faults = sorted(
        {
            s.metadata["fault_injection"].get("type")
            for s in trace.steps
            if s.metadata.get("fault_injection") and s.metadata["fault_injection"].get("injected") is True
        }
    )
    new_faults = sorted(
        {
            s.metadata["fault_injection"].get("type")
            for s in new_record.steps
            if s.metadata.get("fault_injection") and s.metadata["fault_injection"].get("injected") is True
        }
    )
    repeatable = sorted(set(old_faults) & set(new_faults))
    inconsistent: list[str] = [f"原始有、重放未复现的故障: {t}" for t in sorted(set(old_faults) - set(new_faults))]
    inconsistent += [f"重放新增、原始没有的故障: {t}" for t in sorted(set(new_faults) - set(old_faults))]

    return {
        "mode": "controlled",
        "scenario": scen_key,
        "old_trace_id": trace.trace_id,
        "new_trace_id": new_trace_id,
        "fault_config": injector.get_config(),
        "trace": new_record.model_dump(mode="json"),
        "comparison": comparison,
        "repeatable_faults": repeatable,
        "inconsistent_behaviors": inconsistent,
        "executed_real_tools": [tool_name],
        "called_paid_api": False,
        "note": "受控重跑：模拟模型 + 白名单安全工具，未调用付费 API。",
    }
