"""创建一条受控的「工具超时 + 3 次重试」复杂失败 Trace（第 55-B/C 步验证据）。

执行链路（全部本地、零网络、零付费 API）：
- Mock/模拟 Agent：basic_agent.run_agent_sync，模型调用替换为脚本化 fake `_chat`；
- 真实本地工具：text_analyzer 真实执行成功；calculator 连续 3 次被注入 TOOL_TIMEOUT；
- 模型在每个失败后重试同一工具（第 1/2/3 次执行事件），最终给出失败结论；
- Trace 状态 FAILED（与 evaluation_engine 判定规则一致：失败工具步骤 + 最终回答声明失败）。

metadata 标记 data_source=simulated / simulated=true / simulated_model=true，
来源分类为 SIMULATED（不是 REAL_API，也不是 MOCK）。

每个 tool_call 步骤写入 attempt / retry_of / max_retries，供 DAG 区分「执行事件」与「重试次数」。
只写入 data/traces/ 下的 1 个新文件；同一 scenario_id 已存在则跳过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.agents import basic_agent as ba  # noqa: E402
from backend.app.schemas.trace import TraceStatus  # noqa: E402
from backend.app.services.data_governance import classify_trace_record  # noqa: E402
from backend.app.services.failure_propagation import analyze_failure_propagation  # noqa: E402
from backend.app.services.fault_injector import (  # noqa: E402
    FaultInjector,
    FaultRule,
    FaultType,
)
from backend.app.services.trace_collector import TraceCollector  # noqa: E402
from backend.app.services.trace_dag import build_dag  # noqa: E402
from backend.app.services.trace_store import load_trace  # noqa: E402
from backend.app.tools.basic_tools import calculator, text_analyzer  # noqa: E402

DATA_TRACES = PROJECT_ROOT / "data" / "traces"
SCENARIO_ID = "scenario-simulated-tool-timeout-3-retries"
MAX_RETRIES = 3


class _FakeMessage:
    def __init__(self, content: str, tool_calls: list[Any] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.call_id = call_id
        self.name = name
        self.arguments = arguments

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


def _make_fake_chat():
    """脚本化模拟模型：text_analyzer 成功后，calculator 连续重试 3 次，最终失败。"""

    async def fake_chat(client, model, messages, max_tokens=None, tools=None):
        del client, model, max_tokens, tools
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        usage_meta = {
            "provider": "local-simulation",
            "model": "simulated-model",
            "usage_source": "SIMULATED",
            "raw_usage": None,
        }
        system = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
        if "任务分析助手" in system:
            return _FakeMessage("先分析文本，再计算表达式；失败时重试"), usage, usage_meta
        if "任务规划助手" in system:
            return _FakeMessage("1. 文本分析\n2. 计算器（失败最多重试 3 次）\n3. 汇总结论"), usage, usage_meta
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        calls = len(tool_messages)
        if calls == 0:
            return (
                _FakeMessage(
                    "",
                    [
                        _FakeToolCall(
                            "call-text-1",
                            "text_analyzer",
                            json.dumps({"text": "TracePilot 超时重试验证"}),
                        )
                    ],
                ),
                usage,
                usage_meta,
            )
        if calls <= 1 + MAX_RETRIES:
            return (
                _FakeMessage(
                    "",
                    [
                        _FakeToolCall(
                            f"call-calc-{calls}",
                            "calculator",
                            json.dumps({"expression": "21*2"}),
                        )
                    ],
                ),
                usage,
                usage_meta,
            )
        return (
            _FakeMessage(
                f"任务失败：calculator 连续 {MAX_RETRIES + 1} 次超时，重试 {MAX_RETRIES} 次后仍无法完成计算。"
            ),
            usage,
            usage_meta,
        )

    return fake_chat


def _existing_trace() -> str | None:
    for path in DATA_TRACES.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if (data.get("metadata") or {}).get("scenario_id") == SCENARIO_ID:
            return str(data.get("trace_id"))
    return None


def _annotate_retries(raw_steps: list[dict[str, Any]]) -> None:
    """给实际发生的多次 tool_call 事件补充 attempt / retry_of / retry_index / max_retries。

    按工具分别计数（避免把不同工具的执行混算成同一次重试）；不改写任何执行结果。
    """
    last_tool_step: dict[str, str] = {}
    attempt_by_tool: dict[str, int] = {}
    for step in raw_steps:
        if step.get("step_type") != "tool_call" or not step.get("tool_name"):
            continue
        tool = str(step["tool_name"])
        attempt = attempt_by_tool.get(tool, 0) + 1
        attempt_by_tool[tool] = attempt
        meta = dict(step.get("metadata") or {})
        meta.update(
            {
                "attempt": attempt,
                "retry_index": attempt - 1,
                "max_retries": MAX_RETRIES if tool == "calculator" else 0,
                "retry_policy": f"{tool}_timeout_retry" if tool == "calculator" else "none",
            }
        )
        if tool in last_tool_step:
            meta["retry_of"] = last_tool_step[tool]
        step["metadata"] = meta
        last_tool_step[tool] = str(step.get("step_id"))


def main() -> int:
    existing = _existing_trace()
    if existing:
        print(f"SCENARIO_EXISTS trace_id={existing}")
        return 0

    task = "分析文本「TracePilot 超时重试验证」并计算 21*2；工具超时最多重试 3 次"
    injector = FaultInjector(
        # 首次执行 + MAX_RETRIES 次重试全部注入超时（共 MAX_RETRIES + 1 次）
        [FaultRule(FaultType.TOOL_TIMEOUT, target_tool="calculator", count=MAX_RETRIES + 1)]
    )
    original_chat = ba._chat
    ba._chat = _make_fake_chat()
    try:
        result = ba.run_agent_sync(
            task,
            api_key="simulated-local-key",
            model="simulated-model",
            tools=[text_analyzer, calculator],
            fault_injector=injector,
            collect_trace=True,
            save_trace=False,
        )
    finally:
        ba._chat = original_chat

    trace = result.trace
    if trace is None:
        raise SystemExit("未生成 Trace")
    raw_steps = [step.model_dump(mode="json") for step in trace.steps]
    _annotate_retries(raw_steps)

    tool_steps = [s for s in raw_steps if s.get("step_type") == "tool_call"]
    failed_tool = [s for s in tool_steps if s.get("status") == "failed"]
    if len(tool_steps) < 1 + (MAX_RETRIES + 1):
        raise SystemExit(
            f"重试事件不足：tool_call={len(tool_steps)}，需要至少 {1 + MAX_RETRIES + 1}"
        )
    if len(failed_tool) != MAX_RETRIES + 1:
        raise SystemExit(f"失败重试次数异常：{len(failed_tool)}，期望 {MAX_RETRIES + 1}")
    if "失败" not in (result.result or ""):
        raise SystemExit(f"最终回答未声明失败：{result.result!r}")

    metadata = dict(trace.metadata or {})
    metadata.update(
        {
            "data_source": "simulated",
            "simulated": True,
            "simulated_model": True,
            "model_provider": "local-simulation",
            "model": "simulated-model",
            "scenario_id": SCENARIO_ID,
            "scenario_kind": "tool_timeout_with_retries",
            "max_retries": MAX_RETRIES,
            "execution_note": (
                "受控模拟执行：脚本化模拟模型 + 真实本地工具（text_analyzer 成功；"
                "calculator 连续 3 次注入 TOOL_TIMEOUT）；未调用付费 API"
            ),
            "failure_observed": True,
            "status_decision": "失败工具步骤 3 次 + 最终回答声明失败（与 evaluation_engine 规则一致）",
            "engine_version": "tracepilot-55bc",
        }
    )

    collector = TraceCollector(
        task_id=trace.task_id,
        trace_id=trace.trace_id,
        run_id=trace.run_id,
        started_at=trace.started_at,
        traces_dir=DATA_TRACES,
    )
    failed_trace = collector.build(
        raw_steps,
        status=TraceStatus.FAILED,
        ended_at=trace.ended_at,
        metadata=metadata,
    )
    path = collector.save(failed_trace)

    reloaded = load_trace(failed_trace.trace_id)
    dag = build_dag(reloaded)
    propagation = analyze_failure_propagation(reloaded)
    evidence = {
        "trace_id": reloaded.trace_id,
        "file": str(path.relative_to(PROJECT_ROOT)),
        "status": reloaded.status.value,
        "step_count": len(reloaded.steps),
        "time_order": [
            {
                "step_id": s.step_id,
                "step_type": s.step_type.value,
                "status": s.status.value,
                "tool": s.tool_name,
                "attempt": (s.metadata or {}).get("attempt"),
                "retry_of": (s.metadata or {}).get("retry_of"),
                "error_type": s.error.error_type if s.error else None,
            }
            for s in reloaded.steps
        ],
        "dag": {
            "nodes": dag.node_count,
            "edges": dag.edge_count,
            "root_nodes": dag.root_nodes,
            "leaf_nodes": dag.leaf_nodes,
        },
        "propagation_fault_nodes": propagation["summary"]["confirmed_fault"],
        "source_class": classify_trace_record(reloaded)["source_class"],
        "final_answer": result.result,
        "paid_api_called": False,
    }
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
