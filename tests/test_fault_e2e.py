"""故障场景端到端验收（pytest，第 21 步）。

使用模拟模型响应 + 真实本地 calculator 工具，生成正常基线 Trace 与
TOOL_TIMEOUT 故障 Trace，保存到 data/traces/，并验证故障注入、Trace 采集、
DAG 依赖的完整协作。模拟数据在 Trace metadata 中明确标记 simulated=true。
不调用付费 API。
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.fault_injector import (  # noqa: E402
    FaultInjector,
    FaultRule,
    FaultType,
)
from backend.app.services.trace_collector import TraceCollector  # noqa: E402
from backend.app.services.trace_dag import build_dag  # noqa: E402
from backend.app.tools.basic_tools import calculator  # noqa: E402

DATA_TRACES = PROJECT_ROOT / "data" / "traces"


class _FakeMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.call_id, self.name, self.arguments = call_id, name, arguments

    def model_dump(self):
        return {
            "id": self.call_id, "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


def _make_fake_chat():
    """模拟模型：第一次 agent 调用请求 calculator，第二次给出最终回答。"""

    async def fake_chat(client, model, messages, max_tokens=None, tools=None):
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        usage_meta = {
            "provider": "deepseek",
            "model": "fake-model",
            "usage_source": "PROVIDER_REPORTED",
            "raw_usage": None,
            "recorded_at": "2026-11-19T00:00:00+00:00",
        }
        sys_content = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
        if "任务分析助手" in sys_content:
            return _FakeMessage("分析并计算该表达式"), usage, usage_meta
        if "任务规划助手" in sys_content:
            return _FakeMessage("1. 调用计算器\n2. 给出结论"), usage, usage_meta
        has_tool = any(m.get("role") == "tool" for m in messages)
        if not has_tool:
            return _FakeMessage("", [_FakeToolCall("c1", "calculator", json.dumps({"expression": "1+1"}))]), usage, usage_meta
        return _FakeMessage("最终回答：计算完成"), usage, usage_meta

    return fake_chat


def _mark_simulated(record) -> None:
    """在 Trace metadata 中明确标记为模拟数据（模型响应为模拟，工具为真实本地执行）。"""
    record.metadata["simulated"] = True
    record.metadata["simulation_note"] = "模型响应为模拟，工具执行为真实本地工具"


def test_e2e_normal_and_fault(monkeypatch):
    import backend.app.agents.basic_agent as ba

    monkeypatch.setattr(ba, "_chat", _make_fake_chat())
    task = "请使用计算器工具计算 1+1"

    # 1) 正常基线
    normal = ba.run_agent_sync(task, api_key="fake-key", tools=[calculator], collect_trace=True)
    _mark_simulated(normal.trace)
    TraceCollector(traces_dir=DATA_TRACES).save(normal.trace)

    # 2) 注入 TOOL_TIMEOUT 故障
    inj = FaultInjector([FaultRule(FaultType.TOOL_TIMEOUT, target_tool="calculator")])
    fault = ba.run_agent_sync(task, api_key="fake-key", tools=[calculator], fault_injector=inj, collect_trace=True)
    _mark_simulated(fault.trace)
    TraceCollector(traces_dir=DATA_TRACES).save(fault.trace)

    # 3) 关闭故障注入
    inj_off = FaultInjector([FaultRule(FaultType.TOOL_TIMEOUT, target_tool="calculator")])
    inj_off.disable()
    off = ba.run_agent_sync(task, api_key="fake-key", tools=[calculator], fault_injector=inj_off, collect_trace=True)

    # --- 正常基线 ---
    ntool = next(s for s in normal.trace.steps if s.step_type.value == "tool_call")
    assert ntool.status.value == "succeeded"
    assert ntool.tool_result == {"expression": "1+1", "result": 2}
    assert "fault_injection" not in ntool.metadata
    assert normal.trace.status.value == "succeeded"

    # --- 故障位置 / 错误类型 / 执行状态 ---
    ftool = next(s for s in fault.trace.steps if s.step_type.value == "tool_call")
    assert ftool.status.value == "failed"
    assert ftool.error.error_type == "tool_timeout"
    assert ftool.metadata["fault_injection"]["injected"] is True
    assert ftool.metadata["fault_injection"]["type"] == "tool_timeout"

    # --- 故障与 DAG 节点关联 ---
    dag = build_dag(fault.trace)
    assert ftool.step_id in dag.topological_order
    agent_ids = [s.step_id for s in fault.trace.steps if s.step_type.value == "model_call"]
    assert list(ftool.parent_step_ids) == [agent_ids[-2]]

    # --- Agent 恢复行为与最终状态 ---
    assert fault.trace.metadata["recovery_observed"] is True
    assert fault.trace.status.value == "succeeded"  # 故障后仍给出最终回答

    # --- 关闭注入后正常 ---
    oftool = next(s for s in off.trace.steps if s.step_type.value == "tool_call")
    assert oftool.status.value == "succeeded"
    assert oftool.tool_result == {"expression": "1+1", "result": 2}

    # --- 模拟标记 ---
    assert normal.trace.metadata["simulated"] is True
    assert fault.trace.metadata["simulated"] is True

    # 输出验收关键信息
    print("NORMAL_TRACE_ID=" + normal.trace.trace_id)
    print("FAULT_TRACE_ID=" + fault.trace.trace_id)
    print("FAULT_NODE=" + ftool.step_id)
    print("FAULT_ERROR=" + ftool.error.error_type)
    print("RECOVERY_OBSERVED=" + str(fault.trace.metadata["recovery_observed"]))
    print("FINAL_STATUS=" + fault.trace.status.value)
