"""Agent Token 预算预检查 / 工具调用上限 / 执行器预算透传测试（全部离线替身）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import backend.app.agents.basic_agent as ba  # noqa: E402
from backend.app.schemas.trace import TokenUsage, TraceRecord, TraceStatus, TraceStep  # noqa: E402
from backend.app.tools.basic_tools import calculator  # noqa: E402


class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str):
        self.id = call_id
        self.function = _FakeFunction(name, arguments)

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


class _FakeMessage:
    def __init__(self, content: str = "", tool_calls: list | None = None):
        self.content = content
        self.tool_calls = tool_calls


def _install_fake_chat(monkeypatch, *, mode: str = "normal"):
    calls = {"n": 0}

    async def fake_chat(client, model, messages, max_tokens=1024, tools=None):
        calls["n"] += 1
        n = calls["n"]
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        usage_meta = {
            "provider": "deepseek",
            "model": model,
            "usage_source": "PROVIDER_REPORTED",
            "raw_usage": {},
        }
        if n == 1:
            return _FakeMessage("理解任务"), usage, usage_meta
        if n == 2:
            return _FakeMessage("1. 调用计算器\n2. 给出结果"), usage, usage_meta
        if mode == "toolcap":
            return (
                _FakeMessage(
                    "",
                    [_FakeToolCall(f"call_{n}", "calculator", json.dumps({"expression": "1+1"}))],
                ),
                usage,
                usage_meta,
            )
        if n == 3:
            return (
                _FakeMessage(
                    "",
                    [_FakeToolCall("call_1", "calculator", json.dumps({"expression": "1+1"}))],
                ),
                usage,
                usage_meta,
            )
        return _FakeMessage("结果是 2"), usage, usage_meta

    monkeypatch.setattr(ba, "_chat", fake_chat)
    return calls


def _run(monkeypatch, **kwargs):
    monkeypatch.setenv("TRACEPILOT_ENV", "local")
    return ba.run_agent_sync(
        "计算1+1",
        api_key="sk-test-offline-only",
        model="deepseek-chat",
        tools=[calculator],
        collect_trace=True,
        **kwargs,
    )


def test_token_usage_counted_once_with_budget_metadata(monkeypatch):
    calls = _install_fake_chat(monkeypatch)
    result = _run(monkeypatch, max_total_tokens=16000)
    assert calls["n"] == 4
    assert result.budget_exceeded is False
    assert result.token_total == 60  # 4 次调用 × 15，未重复统计
    step_tokens = [s.token_usage.total_tokens for s in result.trace.steps if s.token_usage]
    assert sum(step_tokens) == 60
    assert len(step_tokens) == 4
    budget_meta = [
        (s.metadata or {}).get("budget")
        for s in result.trace.steps
        if (s.metadata or {}).get("budget")
    ]
    assert [m["token_used_after"] for m in budget_meta] == [15, 30, 45, 60]
    assert result.trace.metadata["token_budget"] == 16000
    assert result.trace.metadata["token_total_estimate"] == 60


def test_budget_precheck_stops_before_overflow_and_keeps_partial_trace(monkeypatch):
    calls = _install_fake_chat(monkeypatch)
    result = _run(monkeypatch, max_total_tokens=400)
    # 第一次调用后，第二次调用前预算预检查即停止（不再发起模型请求）
    assert calls["n"] == 1
    assert result.budget_exceeded is True
    assert result.budget_reason and "预算预检查" in result.budget_reason
    assert len(result.trace.steps) == 1
    assert result.trace.steps[0].step_type.value == "model_call"
    assert result.trace.status.value == "failed"
    assert "Token 预算不足" in result.result
    # 部分 Trace 必须保留预算证据，且未被标记为成功
    assert result.trace.metadata["budget_exceeded"] is True
    assert result.trace.metadata["token_budget"] == 400


def test_tool_call_cap_prevents_extra_calls(monkeypatch):
    _install_fake_chat(monkeypatch, mode="toolcap")
    result = _run(monkeypatch, max_tool_calls=1, max_tool_rounds=6)
    tool_steps = [s for s in result.trace.steps if s.tool_name]
    assert len(tool_steps) == 1, "工具调用上限必须在执行前拦截，而不是事后才发现超限"
    assert result.tool_calls_limit_reached is True
    assert "工具调用达到本地上限" in result.result
    assert result.trace.status.value == "partial"
    assert result.trace.metadata["tool_calls_limit_reached"] is True


def test_executor_passes_budget_and_safe_tools_to_basic_agent(monkeypatch):
    import backend.app.agents.basic_agent as basic_agent
    from backend.app.services import agent_runner

    captured: dict = {}

    trace = TraceRecord(
        trace_id="trace-budget-passthrough",
        task_id="task-budget",
        run_id="run-inner",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:00+00:00",
        status=TraceStatus.SUCCEEDED,
        steps=[
            TraceStep.model_validate(
                {
                    "step_id": "s1",
                    "trace_id": "trace-budget-passthrough",
                    "parent_step_ids": [],
                    "step_type": "model_call",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "output": "ok",
                    "status": "succeeded",
                    "token_usage": TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                    "metadata": {
                        "usage_meta": {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "usage_source": "PROVIDER_REPORTED",
                        }
                    },
                }
            )
        ],
        metadata={"model": "deepseek-chat"},
    )

    def fake_sync(task, **kwargs):
        captured["kwargs"] = kwargs
        return basic_agent.AgentResult(
            task=task,
            understanding="",
            plan=[],
            result="ok",
            model="deepseek-chat",
            elapsed_ms=10,
            tool_call_count=0,
            tool_executions=[],
            trace=trace,
            trace_path=None,
            token_total=15,
        )

    monkeypatch.setattr(basic_agent, "run_agent_sync", fake_sync)
    monkeypatch.setenv("TRACEPILOT_ALLOW_REAL_API", "1")
    limits = agent_runner.RunLimits(
        max_model_calls=7,
        max_total_tokens=7777,
        max_tool_calls=5,
        max_wall_seconds=123.0,
        max_tool_rounds=2,
        api_max_retries=0,
        max_tokens_per_call=1024,
    )
    run = None
    try:
        run = agent_runner.run_agent_task(
            "计算1+1",
            mode="real_api",
            confirm_real=True,
            model="deepseek-chat",
            limits=limits,
        )
        kwargs = captured["kwargs"]
        assert kwargs["max_total_tokens"] == 7777
        assert kwargs["max_tool_calls"] == 5
        assert kwargs["max_tool_rounds"] == 2
        assert kwargs["max_tokens"] == 1024
        assert kwargs["overall_timeout_seconds"] == 123.0
        tool_names = {getattr(t, "name", "") for t in kwargs["tools"]}
        assert {"calculator", "text_analyzer", "sqlite_query"}.issubset(tool_names)
        for forbidden in ("subprocess", "shell", "bash", "exec", "file_write", "http_request", "eval"):
            assert forbidden not in tool_names
        assert run.token_usage["total_tokens"] == 15
    finally:
        trace_id = getattr(run, "trace_id", None)
        if trace_id:
            from backend.app.services.trace_store import DEFAULT_TRACES_DIR

            (DEFAULT_TRACES_DIR / f"{trace_id}.json").unlink(missing_ok=True)
