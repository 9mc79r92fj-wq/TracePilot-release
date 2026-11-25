"""创建一条「真实本地执行」的 MOCK 失败 Trace（第 55-A 步验收证据）。

执行链路（全部本地、零网络、零付费 API）：
- Mock Agent：backend.app.agents.basic_agent.run_agent_sync，模型调用替换为脚本化
  fake `_chat`（不创建网络请求）；
- 真实本地工具：text_analyzer 真实执行并成功返回；calculator 在受控故障注入
  （TOOL_TIMEOUT）下失败；
- 最终回答明确声明任务失败 → Trace 状态 FAILED（判定规则与 evaluation_engine 一致）。

metadata 标记 data_source=mock / simulated=true / simulated_model=true，按来源
分类规则归为 MOCK（不是 REAL_API，也不是 SIMULATED / REPLAY）。

只写入 data/traces/ 下的 1 个新文件；若同一 scenario_id 已存在则不再重复创建。
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
from backend.app.services.data_governance import (  # noqa: E402
    classify_trace_record,
    trace_eligibility,
)
from backend.app.services.fault_injector import (  # noqa: E402
    FaultInjector,
    FaultRule,
    FaultType,
)
from backend.app.services.trace_collector import TraceCollector  # noqa: E402
from backend.app.services.trace_store import load_trace  # noqa: E402
from backend.app.tools.basic_tools import calculator, text_analyzer  # noqa: E402

DATA_TRACES = PROJECT_ROOT / "data" / "traces"
SCENARIO_ID = "scenario-mock-tool-timeout-failure-01"


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
    """脚本化 Mock 模型：先调用真实本地 text_analyzer，再调用 calculator（被注入故障）。"""

    async def fake_chat(client, model, messages, max_tokens=None, tools=None):
        del client, model, max_tokens, tools
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        usage_meta = {
            "provider": "local-simulation",
            "model": "mock-scripted-model",
            "usage_source": "SIMULATED",
            "raw_usage": None,
        }
        system = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
        if "任务分析助手" in system:
            return _FakeMessage("先分析文本，再计算表达式"), usage, usage_meta
        if "任务规划助手" in system:
            return _FakeMessage("1. 调用文本分析\n2. 调用计算器\n3. 汇总结论"), usage, usage_meta
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        if len(tool_messages) == 0:
            return (
                _FakeMessage(
                    "",
                    [
                        _FakeToolCall(
                            "call-text-1",
                            "text_analyzer",
                            json.dumps({"text": "TracePilot 失败场景验证"}),
                        )
                    ],
                ),
                usage,
                usage_meta,
            )
        if len(tool_messages) == 1:
            return (
                _FakeMessage(
                    "",
                    [
                        _FakeToolCall(
                            "call-calc-1",
                            "calculator",
                            json.dumps({"expression": "6*7"}),
                        )
                    ],
                ),
                usage,
                usage_meta,
            )
        return _FakeMessage("任务失败：calculator 工具持续超时，无法得到确定性计算结果。"), usage, usage_meta

    return fake_chat


def _existing_trace() -> str | None:
    for path in DATA_TRACES.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 跳过坏文件
            continue
        if (data.get("metadata") or {}).get("scenario_id") == SCENARIO_ID:
            return str(data.get("trace_id"))
    return None


def main() -> int:
    existing = _existing_trace()
    if existing:
        print(f"SCENARIO_EXISTS trace_id={existing}")
        return 0

    task = "分析文本「TracePilot 失败场景验证」并计算 6*7 的结果"
    injector = FaultInjector(
        [FaultRule(FaultType.TOOL_TIMEOUT, target_tool="calculator", count=1)]
    )

    original_chat = ba._chat
    ba._chat = _make_fake_chat()  # 脚本化本地模型：不发起网络请求
    try:
        result = ba.run_agent_sync(
            task,
            api_key="mock-local-key",
            model="mock-scripted-model",
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

    tool_steps = [s for s in trace.steps if s.step_type.value == "tool_call"]
    failed_steps = [s for s in tool_steps if s.status.value == "failed"]
    ok_steps = [s for s in tool_steps if s.status.value == "succeeded"]
    if not failed_steps or not ok_steps:
        raise SystemExit(
            f"场景异常：需要至少 1 个成功工具步骤 + 1 个失败工具步骤，"
            f"实际成功 {len(ok_steps)} / 失败 {len(failed_steps)}"
        )
    if "失败" not in (result.result or ""):
        raise SystemExit(f"最终回答未声明失败：{result.result!r}")

    metadata = dict(trace.metadata or {})
    metadata.update(
        {
            "data_source": "mock",
            "simulated": True,
            "simulated_model": True,
            "model_provider": "local-simulation",
            "model": "mock-scripted-model",
            "mock_script": "controlled_failure_scenario_v1",
            "scenario_id": SCENARIO_ID,
            "execution_note": (
                "本地真实执行：Mock Agent（脚本化本地模型，无网络）+ 真实本地工具"
                "（text_analyzer 成功、calculator 受控故障失败）；未调用付费 API"
            ),
            "fault_injection_scenario": (
                "text_analyzer 成功执行后，calculator 注入 TOOL_TIMEOUT 导致失败，"
                "模型给出明确失败结论"
            ),
            "failure_observed": True,
            "status_decision": (
                "存在失败工具步骤且最终回答明确声明失败（与 evaluation_engine 判定规则一致）"
            ),
            "engine_version": "tracepilot-55a",
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
        [step.model_dump(mode="json") for step in trace.steps],
        status=TraceStatus.FAILED,
        ended_at=trace.ended_at,
        metadata=metadata,
    )
    path = collector.save(failed_trace)

    # 复验：重新从磁盘读取，确认存储内容与分类/资格判断
    reloaded = load_trace(failed_trace.trace_id)
    classification = classify_trace_record(reloaded)
    eligibility = trace_eligibility(reloaded)
    evidence = {
        "trace_id": reloaded.trace_id,
        "file": str(path.relative_to(PROJECT_ROOT)),
        "status": reloaded.status.value,
        "step_count": len(reloaded.steps),
        "step_types": [s.step_type.value for s in reloaded.steps],
        "tool_steps": [
            {
                "step_id": s.step_id,
                "tool_name": s.tool_name,
                "status": s.status.value,
                "error_type": (s.error.error_type if s.error else None),
                "fault_injection": (s.metadata or {}).get("fault_injection"),
                "result": s.tool_result,
            }
            for s in reloaded.steps
            if s.step_type.value == "tool_call"
        ],
        "final_answer": result.result,
        "source_class": classification["source_class"],
        "source_reasons": classification["reasons"],
        "eligibility": {
            key: value["eligible"] for key, value in eligibility["capabilities"].items()
        },
        "eligibility_reasons": {
            key: value["reasons"]
            for key, value in eligibility["capabilities"].items()
            if not value["eligible"]
        },
        "paid_api_called": False,
    }
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
