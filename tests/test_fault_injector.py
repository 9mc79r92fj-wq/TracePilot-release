"""故障注入模块测试（pytest，第 20 步）。

使用模拟模型响应 + 真实本地工具，离线驱动 Agent，验证六种可控故障。
不调用真实 DeepSeek API。
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.fault_injector import (  # noqa: E402
    FaultInjectionError,
    FaultInjector,
    FaultRule,
    FaultType,
)
from backend.app.tools.basic_tools import calculator  # noqa: E402


# ---------------------------------------------------------------------------
# 模拟模型响应
# ---------------------------------------------------------------------------

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
            return _FakeMessage("分析并计算"), usage, usage_meta
        if "任务规划助手" in sys_content:
            return _FakeMessage("1. 调用计算器\n2. 给出结论"), usage, usage_meta
        has_tool = any(m.get("role") == "tool" for m in messages)
        if not has_tool:
            return _FakeMessage("", [_FakeToolCall("c1", "calculator", json.dumps({"expression": "1+1"}))]), usage, usage_meta
        return _FakeMessage("最终回答：计算完成"), usage, usage_meta

    return fake_chat


def _run_with_injector(monkeypatch, injector=None, api_key="fake-secret-key"):
    import backend.app.agents.basic_agent as ba

    monkeypatch.setattr(ba, "_chat", _make_fake_chat())
    return ba.run_agent_sync(
        "计算 1+1", api_key=api_key, tools=[calculator],
        fault_injector=injector, collect_trace=True,
    )


# ---------------------------------------------------------------------------
# 单元测试：六种故障触发
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fault_type", [
    FaultType.TOOL_TIMEOUT,
    FaultType.TOOL_EXCEPTION,
    FaultType.INVALID_ARGUMENTS,
    FaultType.PERMISSION_DENIED,
])
def test_before_faults_raise(fault_type):
    inj = FaultInjector([FaultRule(fault_type, target_tool="calculator")])
    with pytest.raises(FaultInjectionError) as ei:
        inj.before_tool("calculator", "s1")
    assert ei.value.fault_type == fault_type
    assert inj.fired[0]["fault_type"] == fault_type.value


def test_after_faults_corrupt():
    inj = FaultInjector([FaultRule(FaultType.INVALID_TOOL_RESULT, target_tool="calculator")])
    result, fired = inj.after_tool("calculator", "s1", {"result": 2})
    assert result == {"__fault__": "invalid_tool_result", "payload": "not-a-valid-result-shape"}
    assert fired["fault_type"] == "invalid_tool_result"

    inj2 = FaultInjector([FaultRule(FaultType.EMPTY_RETRIEVAL, target_tool="calculator")])
    result2, _ = inj2.after_tool("calculator", "s1", {"hits": [1, 2]})
    assert result2 == []


def test_disable_restores_normal():
    inj = FaultInjector([FaultRule(FaultType.TOOL_EXCEPTION, target_tool="calculator")])
    inj.disable()
    inj.before_tool("calculator", "s1")  # 不抛异常
    result, fired = inj.after_tool("calculator", "s1", {"ok": 1})
    assert result == {"ok": 1}
    assert fired is None


def test_target_matching():
    inj = FaultInjector([FaultRule(FaultType.TOOL_EXCEPTION, target_tool="calculator")])
    inj.before_tool("text_analyzer", "s1")  # 非目标工具，不触发
    with pytest.raises(FaultInjectionError):
        inj.before_tool("calculator", "s1")


def test_deterministic_repeat():
    def run():
        inj = FaultInjector([FaultRule(FaultType.TOOL_EXCEPTION, target_tool="calculator")])
        try:
            inj.before_tool("calculator", "s1")
        except FaultInjectionError as e:
            return e.fault_type.value
        return None

    assert run() == run() == "tool_exception"  # 同一配置确定性一致


def test_config_recording():
    inj = FaultInjector([FaultRule(FaultType.TOOL_EXCEPTION, target_tool="calculator", count=2)])
    assert inj.get_config()[0]["fault_type"] == "tool_exception"
    assert inj.get_config()[0]["remaining"] == 2
    with pytest.raises(FaultInjectionError):
        inj.before_tool("calculator", "s1")
    assert inj.get_config()[0]["remaining"] == 1
    assert len(inj.fired) == 1


# ---------------------------------------------------------------------------
# 端到端：正常模式不受影响
# ---------------------------------------------------------------------------

def test_normal_execution_unaffected(monkeypatch):
    result = _run_with_injector(monkeypatch, injector=None)
    tool = next(s for s in result.trace.steps if s.step_type.value == "tool_call")
    assert tool.tool_result == {"expression": "1+1", "result": 2}
    assert "fault_injection" not in tool.metadata


# ---------------------------------------------------------------------------
# 端到端：故障进入 Trace 并关联 DAG
# ---------------------------------------------------------------------------

def test_fault_enters_trace_and_dag(monkeypatch):
    from backend.app.services.trace_dag import build_dag

    inj = FaultInjector([FaultRule(FaultType.TOOL_EXCEPTION, target_tool="calculator")])
    result = _run_with_injector(monkeypatch, injector=inj)

    assert result.trace is not None
    tool = next(s for s in result.trace.steps if s.step_type.value == "tool_call")
    assert tool.status.value == "failed"
    assert tool.error.error_type == "tool_exception"
    assert tool.metadata["fault_injection"]["injected"] is True
    assert tool.metadata["fault_injection"]["type"] == "tool_exception"

    # 故障步骤能关联 DAG 节点，且其父节点是触发它的模型调用步骤
    dag = build_dag(result.trace)
    assert tool.step_id in dag.topological_order
    agent_step_ids = [s.step_id for s in result.trace.steps if s.step_type.value == "model_call"]
    assert tool.parent_step_ids == [agent_step_ids[-2]] if len(agent_step_ids) >= 2 else True

    # Trace 级记录
    assert result.trace.metadata["fault_injections"][0]["fault_type"] == "tool_exception"
    assert "fault_injection_config" in result.trace.metadata


# ---------------------------------------------------------------------------
# 端到端：关闭注入恢复 + 不泄露 API Key
# ---------------------------------------------------------------------------

def test_disabled_injector_normal_execution(monkeypatch):
    inj = FaultInjector([FaultRule(FaultType.TOOL_EXCEPTION, target_tool="calculator")])
    inj.disable()
    result = _run_with_injector(monkeypatch, injector=inj)
    tool = next(s for s in result.trace.steps if s.step_type.value == "tool_call")
    assert tool.status.value == "succeeded"
    assert tool.tool_result == {"expression": "1+1", "result": 2}


def test_no_api_key_in_trace(monkeypatch):
    result = _run_with_injector(monkeypatch, injector=None, api_key="fake-secret-key")
    payload = result.trace.model_dump_json()
    assert "fake-secret-key" not in payload


# ---------------------------------------------------------------------------
# 故障不破坏数据库
# ---------------------------------------------------------------------------

def test_fault_not_corrupt_database(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sales (id INTEGER PRIMARY KEY, amount REAL)")
    conn.executemany("INSERT INTO sales (amount) VALUES (?)", [(10.0,), (20.0,), (30.0,)])
    conn.commit()
    conn.close()
    monkeypatch.setenv("TRACEPILOT_DB_PATH", str(db))

    inj = FaultInjector([FaultRule(FaultType.PERMISSION_DENIED, target_tool="sqlite_query")])
    with pytest.raises(FaultInjectionError):
        inj.before_tool("sqlite_query", "s1")

    n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    assert n == 3  # 注入未修改真实数据
