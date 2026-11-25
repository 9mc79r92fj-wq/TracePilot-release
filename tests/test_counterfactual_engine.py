"""Counterfactual Engine 测试（pytest，第 31 步）。全程确定性模拟，不调用付费 API。"""

import glob
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.schemas.trace import TraceRecord  # noqa: E402
from backend.app.services.counterfactual_engine import (  # noqa: E402
    INTERVENTION_TYPES,
    UnsupportedInterventionError,
    CounterfactualError,
    run_counterfactual_experiment,
)

DATA_TRACES = PROJECT_ROOT / "data" / "traces"


def _fault_trace() -> TraceRecord:
    for p in sorted(glob.glob(str(DATA_TRACES / "*.json"))):
        r = TraceRecord.model_validate_json(Path(p).read_text(encoding="utf-8"))
        if any(
            isinstance(s.metadata.get("fault_injection"), dict)
            and s.metadata["fault_injection"].get("injected") is True
            for s in r.steps
        ):
            return r
    pytest.skip("缺少故障 Trace 数据")


# 1. 原始 Trace 不被修改
def test_original_trace_unchanged():
    trace = _fault_trace()
    before = trace.model_dump_json()
    run_counterfactual_experiment(trace, {"type": "remove_fault"})
    assert trace.model_dump_json() == before


# 2. 干预运行生成独立 Trace
def test_independent_trace_generated():
    trace = _fault_trace()
    r = run_counterfactual_experiment(trace, {"type": "remove_fault"})
    assert r["experiment_id"].startswith("exp-")
    assert r["intervention_outcome"]["trace_id"].startswith("trace-cf-")
    assert r["intervention_outcome"]["trace_id"] != trace.trace_id
    assert r["intervention_outcome"]["run_id"].startswith("run-cf-")
    assert r["original_trace_modified"] is False


# 3. 移除故障后能够重新执行
def test_remove_fault_succeeds():
    trace = _fault_trace()
    r = run_counterfactual_experiment(trace, {"type": "remove_fault"})
    assert r["observed_outcome"]["fault_types"], "原始应有故障"
    assert r["intervention_outcome"]["fault_types"] == []
    assert r["observed_difference"]["fault_types_removed"]


# 4. 替换模拟工具结果有效
def test_replace_tool_result():
    trace = _fault_trace()
    r = run_counterfactual_experiment(
        trace,
        {"type": "replace_tool_result", "tool": "calculator", "value": {"expression": "1+1", "result": 2}},
    )
    assert r["intervention"]["tool"] == "calculator"
    assert r["observed_difference"]["fault_types_removed"] == ["tool_timeout"]


# 5. 不支持的干预被拒绝
@pytest.mark.parametrize(
    "bad",
    [
        {"type": "run_shell"},
        {"type": "exec_code"},
        {"type": "replace_tool_result", "tool": "subprocess", "value": "x"},
        {"type": "adjust_timeout_retry", "timeout_ms": 10**9},
        {"type": "adjust_timeout_retry", "max_retries": 999},
    ],
)
def test_unsupported_intervention_rejected(bad):
    trace = _fault_trace()
    with pytest.raises((UnsupportedInterventionError, CounterfactualError)):
        run_counterfactual_experiment(trace, bad)


# 6. 实验配置正确记录
def test_config_recorded():
    trace = _fault_trace()
    r = run_counterfactual_experiment(trace, {"type": "remove_fault"}, replays=2)
    assert r["engine_version"]
    assert r["environment"]["model"]["simulated"] is True
    assert r["environment"]["model"]["paid_api_called"] is False
    assert r["environment"]["model"]["model"] == "simulated-scripted-model"
    assert r["control_conditions"] and r["confounders"] and r["limitations"]
    assert r["observed_outcome"]["executed"] is False


# 7. 运行失败时保留错误证据
def test_failure_evidence_retained():
    trace = _fault_trace()
    # 不干预故障 -> 重跑仍应失败，并保留错误证据
    r = run_counterfactual_experiment(
        trace, {"type": "adjust_timeout_retry", "max_retries": 2}
    )
    assert r["intervention_outcome"]["fault_types"] == ["tool_timeout"]
    comparison = r["comparison"]
    assert isinstance(comparison["diff_nodes"], list)


# 8. 相同受控配置可复现
def test_reproducible():
    trace = _fault_trace()
    r = run_counterfactual_experiment(trace, {"type": "remove_fault"}, replays=3)
    assert r["reproducible"] is True


# 9. 不泄露敏感信息
def test_no_sensitive_leak():
    trace = _fault_trace()
    r = run_counterfactual_experiment(trace, {"type": "remove_fault"})
    payload = str(r)
    # 注意："task-" 含子串 "sk-"，因此使用更精确的真实密钥模式
    assert "DEEPSEEK_API_KEY" not in payload
    assert "sk-proj-" not in payload
    assert "sk-ant-" not in payload
    import re

    assert not re.search(r"\bsk-[A-Za-z0-9]{20,}\b", payload)


# 10. 不执行危险操作 + 因果等级保守
def test_no_dangerous_ops_and_conservative_level():
    trace = _fault_trace()
    r = run_counterfactual_experiment(trace, {"type": "remove_fault"}, replays=2)
    assert r["environment"]["tools"] == ["calculator", "text_analyzer"]
    assert set(INTERVENTION_TYPES) == {"remove_fault", "replace_tool_result", "adjust_timeout_retry"}
    assert r["causal_evidence_level"] in {"none", "weak", "moderate"}
    assert r["causal_evidence_level"] != "strong"
