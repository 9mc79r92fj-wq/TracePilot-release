"""Trace 采集与 DAG 集成验证（pytest，第 17 步）。

使用模拟模型响应 + 真实本地工具，离线驱动完整 Agent 图，
验证产生的 Trace 能被现有 DAG 模块正确解析。不调用 DeepSeek API。
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.schemas.trace import TraceRecord, TraceStatus  # noqa: E402
from backend.app.services.trace_collector import TraceCollector  # noqa: E402
from backend.app.services.trace_dag import build_dag_from_steps  # noqa: E402
from backend.app.tools.basic_tools import calculator, text_analyzer  # noqa: E402


# ---------------------------------------------------------------------------
# 模拟模型响应（离线，不调用 DeepSeek API）
# ---------------------------------------------------------------------------

class _FakeMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.call_id = call_id
        self.name = name
        self.arguments = arguments

    def model_dump(self):
        return {
            "id": self.call_id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


def _make_fake_chat(expression: str):
    """构造一个按节点剧本应答的假 _chat，令 Agent 调用一次 calculator 后给最终回答。"""

    async def fake_chat(client, model, messages, max_tokens=None, tools=None):
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        usage_meta = {
            "provider": "deepseek",
            "model": "fake-model",
            "usage_source": "PROVIDER_REPORTED",
            "raw_usage": None,
            "recorded_at": "2026-11-19T00:00:00+00:00",
        }
        sys_content = (
            messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
        )
        if "任务分析助手" in sys_content:
            return _FakeMessage("分析并计算该表达式"), usage, usage_meta
        if "任务规划助手" in sys_content:
            return _FakeMessage("1. 调用计算器\n2. 给出结论"), usage, usage_meta
        # 执行助手：第一次请求工具，第二次给出最终回答
        has_tool = any(m.get("role") == "tool" for m in messages)
        if not has_tool:
            tc = _FakeToolCall("call_1", "calculator", json.dumps({"expression": expression}))
            return _FakeMessage("", [tc]), usage, usage_meta
        return _FakeMessage("最终回答：计算完成"), usage, usage_meta

    return fake_chat


def _raw_steps(trace_id: str = "trace-x") -> list[dict]:
    """合成原始步骤：含 model_call / tool_call（带敏感键）/ 失败步骤（带 error）。"""
    ts = "2026-11-18T12:00:00+00:00"
    return [
        {
            "step_id": f"{trace_id}-s001", "trace_id": trace_id, "parent_step_ids": [],
            "step_type": "planning", "timestamp": ts,
            "input": {"task": "t"}, "output": {"plan": ["a"]}, "status": "succeeded",
        },
        {
            "step_id": f"{trace_id}-s002", "trace_id": trace_id,
            "parent_step_ids": [f"{trace_id}-s001"], "step_type": "tool_call", "timestamp": ts,
            "tool_name": "sqlite_query",
            "tool_arguments": {"query": "SELECT 1", "api_key": "SECRET_KEY_123"},
            "tool_result": {"rows": []}, "status": "succeeded", "duration_ms": 3,
        },
        {
            "step_id": f"{trace_id}-s003", "trace_id": trace_id,
            "parent_step_ids": [f"{trace_id}-s002"], "step_type": "model_call", "timestamp": ts,
            "output": "done", "status": "failed",
            "error": {"error_type": "ModelCallError", "error_message": "boom", "recoverable": False},
            "token_usage": {"input_tokens": 1, "output_tokens": 1},
        },
    ]


# ---------------------------------------------------------------------------
# 1. TraceRecord 正确生成
# ---------------------------------------------------------------------------

def test_collector_generates_record():
    c = TraceCollector(task_id="task-x", trace_id="trace-x", run_id="run-x")
    record = c.build(_raw_steps("trace-x"), status="succeeded")

    assert record.trace_id == "trace-x"
    assert record.task_id == "task-x"
    assert record.run_id == "run-x"
    assert record.status == TraceStatus.SUCCEEDED
    assert len(record.steps) == 3
    assert record.ended_at >= record.started_at
    assert record.metadata["schema_version"]


# ---------------------------------------------------------------------------
# 2/3/4/5/6：端到端（模拟模型 + 真实 calculator 工具）
# ---------------------------------------------------------------------------

def test_end_to_end_trace_success(monkeypatch):
    import backend.app.agents.basic_agent as ba

    monkeypatch.setattr(ba, "_chat", _make_fake_chat("1+1"))
    result = ba.run_agent_sync(
        "计算 1+1", api_key="fake-key", tools=[calculator, text_analyzer]
    )

    assert result.trace is not None
    trace = result.trace

    # 步骤类型序列：任务理解(model_call) -> 规划(planning) -> agent(model_call) -> tool_call -> agent(model_call)
    types = [s.step_type.value for s in trace.steps]
    assert types == ["model_call", "planning", "model_call", "tool_call", "model_call"]

    # 工具调用与结果关联
    tool = next(s for s in trace.steps if s.step_type.value == "tool_call")
    assert tool.tool_name == "calculator"
    assert tool.tool_arguments == {"expression": "1+1"}
    assert tool.tool_result == {"expression": "1+1", "result": 2}
    assert tool.status.value == "succeeded"

    # 显式 parent_step_ids 依赖链（非时间推断）
    assert trace.steps[0].parent_step_ids == []
    assert trace.steps[1].parent_step_ids == [trace.steps[0].step_id]
    assert tool.parent_step_ids == [trace.steps[2].step_id]
    assert trace.steps[4].parent_step_ids == [tool.step_id]

    # 执行耗时与状态
    for s in trace.steps:
        assert s.duration_ms is not None and s.duration_ms >= 0
        assert s.status is not None

    # Token 用量符合 Schema
    model_calls = [s for s in trace.steps if s.step_type.value == "model_call"]
    assert len(model_calls) == 3
    for s in model_calls:
        assert s.token_usage is not None
        assert s.token_usage.total_tokens == 15


# ---------------------------------------------------------------------------
# 12：执行异常被记录
# ---------------------------------------------------------------------------

def test_end_to_end_tool_error_recorded(monkeypatch):
    import backend.app.agents.basic_agent as ba

    monkeypatch.setattr(ba, "_chat", _make_fake_chat("1/0"))
    result = ba.run_agent_sync(
        "计算 1/0", api_key="fake-key", tools=[calculator]
    )

    assert result.trace is not None
    tool = next(s for s in result.trace.steps if s.step_type.value == "tool_call")
    assert tool.status.value == "failed"
    assert tool.error is not None
    assert tool.error.error_type == "ToolError"
    assert "division by zero" in tool.error.error_message.lower()
    assert tool.error.recoverable is False


# ---------------------------------------------------------------------------
# 7：TokenUsage 自动补全 total
# ---------------------------------------------------------------------------

def test_token_usage_auto_total():
    c = TraceCollector(trace_id="trace-x")
    record = c.build(_raw_steps("trace-x"), status="succeeded")
    failed_step = record.steps[2]
    # 原始 token_usage 只给了 input/output，未给 total -> 自动相加
    assert failed_step.token_usage is not None
    assert failed_step.token_usage.total_tokens == 2


# ---------------------------------------------------------------------------
# 8：Trace JSON 保存与读取
# ---------------------------------------------------------------------------

def test_trace_json_save_and_read(tmp_path):
    c = TraceCollector(trace_id="trace-x", traces_dir=tmp_path)
    record = c.build(_raw_steps("trace-x"), status="succeeded")

    path = c.save(record)
    assert path.exists()
    assert path.suffix == ".json"
    assert path.name == "trace-x.json"

    loaded = TraceRecord.model_validate_json(path.read_text(encoding="utf-8"))
    assert loaded == record


# ---------------------------------------------------------------------------
# 11：API Key 不进入 Trace 文件
# ---------------------------------------------------------------------------

def test_api_key_redacted(tmp_path):
    c = TraceCollector(trace_id="trace-x", traces_dir=tmp_path)
    record = c.build(_raw_steps("trace-x"), status="succeeded")
    path = c.save(record)
    payload = path.read_text(encoding="utf-8")

    assert "SECRET_KEY_123" not in payload
    assert "[REDACTED]" in payload


# ---------------------------------------------------------------------------
# 9/10：Trace 可被 DAG 解析且无环
# ---------------------------------------------------------------------------

def test_trace_dag_parseable_and_acyclic():
    c = TraceCollector(trace_id="trace-x")
    record = c.build(_raw_steps("trace-x"), status="succeeded")

    dag = build_dag_from_steps(record.steps)
    assert dag.node_count == 3
    assert dag.edge_count == 2
    assert dag.root_nodes == ["trace-x-s001"]
    assert dag.leaf_nodes == ["trace-x-s003"]
    # 拓扑排序覆盖全部节点 => 无循环依赖
    assert len(dag.topological_order) == dag.node_count


def test_trace_error_fields_validated():
    """失败步骤的 error 被校验为 TraceError 且字段完整。"""
    c = TraceCollector(trace_id="trace-x")
    record = c.build(_raw_steps("trace-x"), status="failed")
    failed_step = record.steps[2]

    assert failed_step.status.value == "failed"
    assert failed_step.error.error_type == "ModelCallError"
    assert failed_step.error.error_message == "boom"
    assert failed_step.error.recoverable is False
