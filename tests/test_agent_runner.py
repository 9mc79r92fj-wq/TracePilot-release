"""Agent 执行链路测试（pytest，第 41 步）。全部 mock 模式，不发起真实 API 调用。"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.agent_runner import (  # noqa: E402
    ALLOWED_MODELS,
    DATA_SOURCES,
    REAL_API_ENV_FLAG,
    AgentRunError,
    InvalidModelError,
    RealApiNotAuthorized,
    RunLimits,
    _extract_expression,
    get_run,
    list_runs,
    run_agent_task,
)
from backend.app.services.trace_dag import build_dag  # noqa: E402
from backend.app.services.trace_store import load_trace  # noqa: E402


# ---------- 1. Mock 端到端 ----------

def test_mock_e2e_success():
    run = run_agent_task("计算125乘以48", mode="mock")
    assert run.status == "succeeded"
    assert run.mode == "mock"
    assert run.data_source == "mock"
    assert run.simulated is True
    assert run.final_answer
    assert "6000" in run.final_answer, f"实际答案: {run.final_answer}"
    assert run.trace_id and run.trace_path
    assert run.elapsed_ms is not None and run.elapsed_ms >= 0
    assert run.limit_violations == []


def test_mock_tool_call_recorded():
    run = run_agent_task("计算125乘以48", mode="mock")
    assert len(run.tool_calls) == 1
    tc = run.tool_calls[0]
    assert tc["tool_name"] == "calculator"
    assert tc["arguments"] == {"expression": "125*48"}
    assert tc["result"] == {"expression": "125*48", "result": 6000}
    assert tc["status"] == "succeeded"


def test_mock_token_usage_explicitly_zero_with_note():
    run = run_agent_task("计算125乘以48", mode="mock")
    assert run.token_usage is not None
    assert run.token_usage["total_tokens"] == 0
    assert "note" in run.token_usage, "mock 模式 0 token 必须有明确说明，避免被误读为真实计量"


def test_expression_extraction():
    assert _extract_expression("计算125乘以48") == "125*48"
    assert _extract_expression("125 * 48") == "125*48"
    assert _extract_expression("12加上8") == "12+8"
    assert _extract_expression("请分析销售数据") is None
    assert _extract_expression("1+2*3") == "1+2*3"


# ---------- 2. Trace 保存与读取 ----------

def test_trace_persisted_and_loadable():
    run = run_agent_task("计算125乘以48", mode="mock")
    assert Path(run.trace_path).exists()
    trace = load_trace(run.trace_id)
    assert trace.trace_id == run.trace_id
    assert len(trace.steps) >= 3


def test_trace_identity_consistent():
    """trace_id / run_id / metadata 必须一致，不得出现两套 run_id。"""
    run = run_agent_task("计算125乘以48", mode="mock")
    trace = load_trace(run.trace_id)
    assert trace.run_id == run.run_id, "TraceRecord.run_id 必须等于 Run.run_id"
    assert trace.metadata["run_id"] == run.run_id
    assert trace.metadata["data_source"] == "mock"
    assert trace.metadata["simulated"] is True
    assert trace.metadata["model"] == run.model


def test_trace_steps_have_required_fields():
    run = run_agent_task("计算125乘以48", mode="mock")
    trace = load_trace(run.trace_id)
    types = [s.step_type.value for s in trace.steps]
    assert "planning" in types
    assert "tool_call" in types
    assert types.count("model_call") >= 2

    for s in trace.steps:
        assert s.timestamp is not None
        assert s.status is not None
    tool = next(s for s in trace.steps if s.step_type.value == "tool_call")
    assert tool.tool_name == "calculator"
    assert tool.tool_result == {"expression": "125*48", "result": 6000}
    assert tool.parent_step_ids, "工具步骤必须记录父节点"


def test_trace_timestamps_present():
    run = run_agent_task("计算125乘以48", mode="mock")
    trace = load_trace(run.trace_id)
    assert trace.started_at is not None
    assert trace.ended_at is not None
    assert trace.ended_at >= trace.started_at


# ---------- 3. DAG 生成 ----------

def test_dag_from_run_trace():
    run = run_agent_task("计算125乘以48", mode="mock")
    trace = load_trace(run.trace_id)
    dag = build_dag(trace)
    assert dag.node_count == len(trace.steps)
    assert dag.edge_count >= 2
    assert len(dag.topological_order) == dag.node_count
    assert dag.root_nodes and dag.leaf_nodes
    assert dag.has_path(dag.root_nodes[0], dag.leaf_nodes[0]) is True


# ---------- 4. 数据来源标记 ----------

def test_data_source_values():
    assert set(DATA_SOURCES) == {"real_api", "mock", "simulated"}
    run = run_agent_task("计算125乘以48", mode="mock")
    assert run.data_source in DATA_SOURCES
    assert run.data_source != "real_api", "mock 模式绝不能标记为 real_api"


# ---------- 5. 真实模式授权（不产生付费调用） ----------

def test_real_mode_requires_authorization(monkeypatch):
    monkeypatch.delenv(REAL_API_ENV_FLAG, raising=False)
    with pytest.raises(RealApiNotAuthorized):
        run_agent_task("计算125乘以48", mode="real_api")
    # 即使请求确认，但服务端开关关闭也必须拒绝
    with pytest.raises(RealApiNotAuthorized):
        run_agent_task("计算125乘以48", mode="real_api", confirm_real=True)


def test_real_mode_requires_confirm_flag(monkeypatch):
    monkeypatch.setenv(REAL_API_ENV_FLAG, "1")
    with pytest.raises(RealApiNotAuthorized):
        run_agent_task("计算125乘以48", mode="real_api", confirm_real=False)


def test_invalid_model_rejected(monkeypatch):
    monkeypatch.setenv(REAL_API_ENV_FLAG, "1")
    with pytest.raises(InvalidModelError):
        run_agent_task(
            "计算125乘以48", mode="real_api", confirm_real=True, model="gpt-4o"
        )


def test_model_allowlist():
    assert ALLOWED_MODELS == ("deepseek-flash", "deepseek-v4-pro")


# ---------- 6. 错误处理 ----------

@pytest.mark.parametrize("task", ["", "   "])
def test_empty_task_rejected(task):
    with pytest.raises(AgentRunError):
        run_agent_task(task, mode="mock")


def test_invalid_mode_rejected():
    with pytest.raises(AgentRunError):
        run_agent_task("计算125乘以48", mode="gpt")


def test_get_run_and_list():
    run = run_agent_task("计算125乘以48", mode="mock")
    assert get_run(run.run_id).run_id == run.run_id
    assert any(r["run_id"] == run.run_id for r in list_runs())


def test_get_unknown_run():
    with pytest.raises(AgentRunError):
        get_run("run-doesnotexist")


# ---------- 7. 限制与超时 ----------

def test_limits_recorded():
    run = run_agent_task("计算125乘以48", mode="mock")
    lim = run.limits
    assert lim["max_model_calls"] >= 1
    assert lim["max_tool_calls"] >= 0
    assert lim["max_wall_seconds"] > 0
    assert lim["api_max_retries"] == 0, "真实模式必须禁止自动重试"


def test_limit_violation_flagging(monkeypatch):
    """人为把工具调用上限设为 0，应标记 limit_exceeded 而非静默通过。"""
    run = run_agent_task("计算125乘以48", mode="mock", limits=RunLimits(max_tool_calls=0))
    assert run.status == "limit_exceeded"
    assert run.limit_violations
    assert any("工具调用" in v for v in run.limit_violations)


def test_elapsed_not_zero_filled():
    run = run_agent_task("计算125乘以48", mode="mock")
    assert run.elapsed_ms is not None
    assert isinstance(run.elapsed_ms, int)


# ---------- 8. 安全 ----------

def test_no_sensitive_leak():
    import json

    run = run_agent_task("计算125乘以48", mode="mock")
    text = json.dumps(run.to_dict(), ensure_ascii=False, default=str)
    assert "DEEPSEEK_API_KEY=" not in text
    assert "sk-" not in text


def test_mock_does_not_call_paid_api():
    """mock 模式不产生任何网络调用（无 API Key 也应成功）。"""
    run = run_agent_task("计算125乘以48", mode="mock")
    assert run.simulated is True
    assert run.data_source == "mock"
    assert run.token_usage["total_tokens"] == 0

# ---------- 9. 真实路径 run_id 归一化（桩替换网络调用，零费用） ----------

def test_real_path_normalizes_run_id(monkeypatch):
    """验证真实执行路径会把 Trace.run_id 归一化为 Run.run_id（不发起网络调用）。"""
    from datetime import datetime, timezone

    import backend.app.agents.basic_agent as ba
    from backend.app.schemas.trace import (
        TokenUsage, TraceRecord, TraceStatus, TraceStep,
    )

    # 构造一个「basic_agent 生成的」Trace，run_id 故意与 Run 不同
    inner_trace = TraceRecord(
        trace_id="trace-stub-real",
        task_id="task-stub",
        run_id="run-INNER-different",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        status=TraceStatus.SUCCEEDED,
        steps=[
            TraceStep.model_validate({
                "step_id": "s1", "trace_id": "trace-stub-real", "parent_step_ids": [],
                "step_type": "planning", "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "succeeded",
            }),
            TraceStep.model_validate({
                "step_id": "s2", "trace_id": "trace-stub-real", "parent_step_ids": ["s1"],
                "step_type": "tool_call", "timestamp": datetime.now(timezone.utc).isoformat(),
                "tool_name": "calculator", "tool_arguments": {"expression": "125*48"},
                "tool_result": {"result": 6000}, "status": "succeeded",
            }),
            TraceStep.model_validate({
                "step_id": "s3", "trace_id": "trace-stub-real", "parent_step_ids": ["s2"],
                "step_type": "model_call", "timestamp": datetime.now(timezone.utc).isoformat(),
                "output": "计算结果为 6000", "status": "succeeded",
                "token_usage": TokenUsage(input_tokens=100, output_tokens=20),
            }),
        ],
        metadata={"model": "deepseek-chat"},
    )

    def fake_sync(task, **kwargs):
        return ba.AgentResult(
            task=task, understanding="理解", plan=["调用计算器"],
            result="计算结果为 6000", model="deepseek-chat", elapsed_ms=100,
            tool_call_count=1, tool_executions=[], trace=inner_trace,
            trace_path=None,
        )

    monkeypatch.setattr(ba, "run_agent_sync", fake_sync)
    monkeypatch.setenv("TRACEPILOT_ALLOW_REAL_API", "1")

    run = run_agent_task(
        "计算125乘以48", mode="real_api", confirm_real=True, model="deepseek-chat"
    )
    assert run.status == "succeeded"
    assert run.data_source == "real_api"
    assert run.simulated is False
    assert run.model == "deepseek-flash"
    # Token 来自 Trace 真实记录（桩里是 120）
    assert run.token_usage["total_tokens"] == 120

    trace = load_trace(run.trace_id)
    assert trace.run_id == run.run_id, "真实路径也必须归一化 run_id"
    assert trace.metadata["run_id"] == run.run_id
    assert trace.metadata["data_source"] == "real_api"
    assert trace.metadata["simulated"] is False

    # 清理：桩数据不是真实 API 产物，绝不能作为 real_api 证据留在 data/traces
    try:
        Path(run.trace_path).unlink(missing_ok=True)
    except OSError:
        pass

# ---------- 10. 真实调用证据标记（Provider usage → real_api_verified） ----------

def _stub_real_trace(*, with_provider_usage: bool):
    """构造 basic_agent 形态的桩 Trace（不发起任何网络调用）。"""
    from datetime import datetime, timezone

    from backend.app.schemas.trace import (
        TokenUsage, TraceRecord, TraceStatus, TraceStep,
    )

    meta = {}
    if with_provider_usage:
        meta = {
            "usage_meta": {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "usage_source": "PROVIDER_REPORTED",
                "raw_usage": {"total_tokens": 321},
            }
        }
    trace_id = "trace-stub-evidence-" + ("yes" if with_provider_usage else "no")
    return TraceRecord(
        trace_id=trace_id,
        task_id="task-stub-evidence",
        run_id="run-inner-evidence",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        status=TraceStatus.SUCCEEDED,
        steps=[
            TraceStep.model_validate({
                "step_id": "s1", "trace_id": trace_id, "parent_step_ids": [],
                "step_type": "model_call", "timestamp": datetime.now(timezone.utc).isoformat(),
                "output": "答案 2", "status": "succeeded",
                "token_usage": TokenUsage(input_tokens=200, output_tokens=121),
                "metadata": meta,
            }),
        ],
        metadata={"model": "deepseek-chat"},
    )


def _run_stub_real(monkeypatch, *, with_provider_usage: bool):
    import backend.app.agents.basic_agent as ba

    inner_trace = _stub_real_trace(with_provider_usage=with_provider_usage)

    def fake_sync(task, **kwargs):
        return ba.AgentResult(
            task=task, understanding="理解", plan=["调用计算器"],
            result="答案 2", model="deepseek-chat", elapsed_ms=100,
            tool_call_count=0, tool_executions=[], trace=inner_trace, trace_path=None,
        )

    monkeypatch.setattr(ba, "run_agent_sync", fake_sync)
    monkeypatch.setenv(REAL_API_ENV_FLAG, "1")
    return run_agent_task(
        "计算1+1", mode="real_api", confirm_real=True, model="deepseek-chat"
    )


def _cleanup_run_trace(run) -> None:
    try:
        Path(run.trace_path).unlink(missing_ok=True)
    except OSError:
        pass


def test_real_provider_usage_marks_verified(monkeypatch):
    """Provider 上报 usage 时写入 real_api_verified，治理分类为 REAL_API。"""
    from backend.app.services.data_governance import classify_metadata

    run = _run_stub_real(monkeypatch, with_provider_usage=True)
    try:
        trace = load_trace(run.trace_id)
        assert trace.metadata["real_api_verified"] is True
        evidence = trace.metadata["real_api_verification"]
        assert evidence["provider"] == "deepseek"
        assert evidence["usage_source"] == "PROVIDER_REPORTED"
        assert evidence["model_call_steps_with_usage"] == 1
        assert evidence["total_tokens"] == 321
        assert classify_metadata(trace.metadata)["source_class"] == "REAL_API"
    finally:
        _cleanup_run_trace(run)


def test_real_without_provider_usage_not_verified(monkeypatch):
    """缺少 Provider usage 证据时绝不写入 real_api_verified（治理保持 UNKNOWN）。"""
    from backend.app.services.data_governance import classify_metadata

    run = _run_stub_real(monkeypatch, with_provider_usage=False)
    try:
        trace = load_trace(run.trace_id)
        assert "real_api_verified" not in trace.metadata
        assert classify_metadata(trace.metadata)["source_class"] == "UNKNOWN"
        assert any("real_api_verified" in item for item in run.limitations)
    finally:
        _cleanup_run_trace(run)
