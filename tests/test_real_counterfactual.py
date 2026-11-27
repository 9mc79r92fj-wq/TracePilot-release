"""真实反事实执行服务的单元测试（使用注入的假执行器，不发起付费调用）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.agents.basic_agent import AgentResult  # noqa: E402
from backend.app.schemas.trace import TraceRecord  # noqa: E402
from backend.app.services.real_counterfactual import (  # noqa: E402
    RealCounterfactualError,
    UnsupportedRealIntervention,
    run_real_counterfactual,
    verify_repair_candidate_real,
)

FIXTURE_TRACE = Path(__file__).resolve().parent / "fixtures" / "traces" / "trace-bf1f2e2690f743379fadc80d22e03adf.json"


def _source() -> TraceRecord:
    return TraceRecord.model_validate_json(FIXTURE_TRACE.read_text(encoding="utf-8"))


def _fake_executor(task, **kwargs):
    """返回一条明确标记 real_api 的最小 Trace（测试用，不代表真实 API 调用）。"""
    source = _source()
    trace = source.model_copy(
        deep=True,
        update={
            "trace_id": "trace-realcf-test-001",
            "run_id": "run-realcf-test-001",
            "metadata": {
                "data_source": "real_api",
                "simulated": False,
                "model": "deepseek-flash",
                "test_stub": True,
            },
        },
    )
    return AgentResult(
        task=task,
        understanding="测试",
        plan=["执行"],
        result="测试结果",
        model="deepseek-flash",
        elapsed_ms=12,
        tool_call_count=0,
        tool_executions=[],
        trace=trace,
        trace_path=None,
        token_total=15,
    )


def test_unsupported_intervention_rejected():
    with pytest.raises(UnsupportedRealIntervention):
        run_real_counterfactual(_source(), {"type": "replace_tool_result"}, executor=_fake_executor)


def test_invalid_replays_rejected():
    with pytest.raises(RealCounterfactualError):
        run_real_counterfactual(_source(), {"type": "remove_fault"}, replays=9, executor=_fake_executor)


def test_remove_fault_real_execution_report():
    report = run_real_counterfactual(
        _source(), {"type": "remove_fault"}, replays=2, executor=_fake_executor
    )
    assert report["execution_mode"] == "real_api"
    assert report["simulated"] is False
    assert report["source_trace_id"] == "trace-bf1f2e2690f743379fadc80d22e03adf"
    assert report["replays_completed"] == 2
    assert all(item["is_real_api"] for item in report["intervention_outcomes"])
    assert report["original_trace_modified"] is False
    assert report["causal_evidence_level"] in {"weak", "moderate"}
    # 假执行器没有真实费用，但报告仍标明真实模式；真实运行时由预算模块扣减
    assert report["paid_api_called"] is True


def test_executor_must_return_real_api_trace():
    def fake_but_simulated(task, **kwargs):
        result = _fake_executor(task, **kwargs)
        trace = result.trace.model_copy(update={"metadata": {"data_source": "mock", "simulated": True}})
        return AgentResult(**{**result.__dict__, "trace": trace})

    report = run_real_counterfactual(
        _source(), {"type": "remove_fault"}, executor=fake_but_simulated
    )
    assert report["replays_completed"] == 0
    assert report["errors"] and "不是真实 API Trace" in report["errors"][0]
    assert report["paid_api_called"] is False


def test_correct_arguments_requires_note():
    with pytest.raises(UnsupportedRealIntervention):
        run_real_counterfactual(_source(), {"type": "correct_arguments"}, executor=_fake_executor)


def test_verify_repair_candidate_maps_supported_types():
    class Candidate:
        repair_type = "INCREASE_TIMEOUT"
        modification = {"timeout_ms": 5000}

    report = verify_repair_candidate_real(_source(), Candidate(), executor=_fake_executor)
    assert report["intervention"]["type"] == "adjust_timeout_retry"
    assert report["replays_completed"] == 1
