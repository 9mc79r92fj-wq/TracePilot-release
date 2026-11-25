"""Agent Evaluation Engine 测试（pytest，第 35 步）。确定性模拟，不调用付费 API。"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.schemas.trace import TraceRecord  # noqa: E402
from backend.app.services.evaluation_engine import (  # noqa: E402
    FAIL,
    NOT_APPLICABLE,
    PASS,
    UNKNOWN,
    EvaluationError,
    build_evaluation_report,
    evaluate_trace,
    execute_case,
    load_dataset,
    run_evaluation,
)

TS = "2026-11-18T12:00:00+00:00"


def _dataset():
    return load_dataset()


def _case(case_id: str):
    return next(c for c in _dataset()["cases"] if c["case_id"] == case_id)


def _simple_trace(answer: str, tool_name="calculator", tool_status="succeeded",
                  tool_result=None, extra_steps=None, started=TS, ended=TS) -> TraceRecord:
    steps = [
        {"step_id": "t-s001", "trace_id": "t", "parent_step_ids": [], "step_type": "planning",
         "timestamp": TS, "status": "succeeded"},
        {"step_id": "t-s002", "trace_id": "t", "parent_step_ids": ["t-s001"], "step_type": "tool_call",
         "timestamp": TS, "status": tool_status, "tool_name": tool_name,
         "tool_result": tool_result if tool_result is not None else {"result": 2}},
        {"step_id": "t-s003", "trace_id": "t", "parent_step_ids": ["t-s002"], "step_type": "model_call",
         "timestamp": TS, "status": "succeeded", "output": answer,
         "token_usage": {"input_tokens": 0, "output_tokens": 0}},
    ]
    if extra_steps:
        steps.extend(extra_steps)
    data = {
        "trace_id": "t", "task_id": "task", "run_id": "run",
        "started_at": started, "status": "succeeded", "steps": steps, "metadata": {},
    }
    if ended:
        data["ended_at"] = ended
    return TraceRecord.model_validate(data)


# 1. 精确匹配正确
def test_exact_match():
    case = _case("case-13-safety-constraints")  # numeric 2
    t = _simple_trace("计算结果为 2")
    r = evaluate_trace(t, case)
    assert r["metrics"]["task_success"]["status"] == PASS

    # 错误答案 -> FAIL（不能把错误判为通过）
    t_bad = _simple_trace("计算结果为 99")
    r_bad = evaluate_trace(t_bad, case)
    assert r_bad["metrics"]["task_success"]["status"] == FAIL
    assert r_bad["overall_status"] == FAIL
    assert r_bad["is_bad_case"] is True


# 2. 数值容差正确
def test_numeric_tolerance():
    case = _case("case-02-calc-tolerance")  # 0.3 ± 0.0001
    ok = evaluate_trace(_simple_trace("结果为 0.30000000000000004"), case)
    assert ok["metrics"]["task_success"]["status"] == PASS

    bad = evaluate_trace(_simple_trace("结果为 0.31"), case)
    assert bad["metrics"]["task_success"]["status"] == FAIL


# 3. 工具调用检查正确
def test_tool_call_checks():
    case = _case("case-01-calc-exact")
    ok = evaluate_trace(_simple_trace("计算结果为 2", tool_name="calculator"), case)
    assert ok["metrics"]["tool_correctness"]["status"] == PASS

    # 缺少必需工具
    wrong_tool = evaluate_trace(_simple_trace("计算结果为 2", tool_name="text_analyzer"), case)
    assert wrong_tool["metrics"]["tool_correctness"]["status"] == FAIL

    # 调用禁止工具
    case_forbid = {
        **case,
        "expected_tool_behavior": {**case["expected_tool_behavior"], "forbidden_tools": ["calculator"]},
    }
    forbid = evaluate_trace(_simple_trace("计算结果为 2"), case_forbid)
    assert forbid["metrics"]["tool_correctness"]["status"] == FAIL


# 4. 安全规则正确
def test_safety_rules():
    case = _case("case-13-safety-constraints")
    clean = evaluate_trace(_simple_trace("计算结果为 2"), case)
    assert clean["metrics"]["safety"]["status"] == PASS

    # 危险工具 -> FAIL
    dangerous = evaluate_trace(_simple_trace("计算结果为 2", tool_name="subprocess"), case)
    assert dangerous["metrics"]["safety"]["status"] == FAIL

    # 敏感串 -> FAIL
    leak = evaluate_trace(
        _simple_trace("计算结果为 2", tool_result={"api_key": "DEEPSEEK_API_KEY=abcdefgh12345678"}),
        case,
    )
    assert leak["metrics"]["safety"]["status"] == FAIL


# 5. 缺失数据返回 UNKNOWN
def test_missing_data_unknown():
    case = _case("case-01-calc-exact")
    # 无结束时间 -> efficiency 不可得
    t = _simple_trace("计算结果为 2", ended=None)
    r = evaluate_trace(t, case)
    assert r["metrics"]["efficiency"]["status"] == UNKNOWN
    assert r["metrics"]["efficiency"]["metric_value"] is None, "缺失指标不得填 0"

    # 答案缺失 -> task_success UNKNOWN
    steps = [
        {"step_id": "t-s001", "trace_id": "t", "parent_step_ids": [], "step_type": "planning",
         "timestamp": TS, "status": "succeeded"},
        {"step_id": "t-s002", "trace_id": "t", "parent_step_ids": ["t-s001"], "step_type": "tool_call",
         "timestamp": TS, "status": "succeeded", "tool_name": "calculator"},
    ]
    t2 = TraceRecord.model_validate({
        "trace_id": "t2", "task_id": "task", "run_id": "run",
        "started_at": TS, "ended_at": TS, "status": "succeeded",
        "steps": steps, "metadata": {},
    })
    r2 = evaluate_trace(t2, case)
    assert r2["metrics"]["task_success"]["status"] == UNKNOWN


# 6. 评测集能够读取
def test_dataset_loadable():
    data = _dataset()
    assert data["dataset_id"] == "tracepilot-eval-v1"
    assert data["data_source"] == "synthetic"
    assert len(data["cases"]) >= 12
    for c in data["cases"]:
        for key in ("case_id", "task_description", "expected_outcome",
                    "expected_tool_behavior", "evaluation_rules",
                    "scenario_type", "data_source"):
            assert key in c, f"{c.get('case_id')} 缺少 {key}"
        assert c["data_source"] in {"synthetic", "recorded", "real_api"}


def test_dataset_invalid_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"dataset_id": "x", "dataset_version": "1",
                               "data_source": "unknown", "cases": []}), encoding="utf-8")
    with pytest.raises(EvaluationError):
        load_dataset(bad)


# 7/8. 批量评测执行 + 每案例独立结果
def test_batch_evaluation():
    report = run_evaluation()
    assert report["total_cases"] >= 12
    assert report["executed_cases"] == report["total_cases"]
    assert report["execution_error_cases"] == 0
    assert len(report["case_results"]) == report["executed_cases"]
    assert len(report["trace_ids"]) == report["executed_cases"]
    # 每个案例独立 trace
    assert len(set(report["trace_ids"])) == len(report["trace_ids"])
    # 六维度齐备
    for r in report["case_results"]:
        assert set(r["metrics"].keys()) == {
            "task_success", "tool_correctness", "process_quality",
            "recovery", "efficiency", "safety",
        }
        for m in r["metrics"].values():
            for key in ("metric_name", "metric_value", "evaluation_method",
                        "evidence", "confidence_or_limitations", "status"):
                assert key in m
            assert m["status"] in {PASS, FAIL, UNKNOWN, NOT_APPLICABLE}


# 9. Bad Case 能够识别
def test_bad_case_identified():
    data = _dataset()
    # 篡改一个案例的期望值，使其必然失败
    mutated = json.loads(json.dumps(data))
    for c in mutated["cases"]:
        if c["case_id"] == "case-01-calc-exact":
            c["expected_outcome"]["answer_match"]["expected"] = 999
    report = run_evaluation(mutated)
    assert report["bad_cases"], "应识别出 Bad Case"
    bad_ids = {b["case_id"] for b in report["bad_cases"]}
    assert "case-01-calc-exact" in bad_ids
    assert report["failed_cases"] >= 1


# 10. 汇总指标计算正确
def test_summary_metrics():
    report = run_evaluation()
    m = report["metrics"]["task_success_rate"]
    assert m["denominator"] == report["executed_cases"]
    assert m["numerator"] == report["passed_cases"]
    assert abs(m["value"] - report["passed_cases"] / report["executed_cases"]) < 1e-9
    assert "denominator_definition" in m
    assert m["denominator_definition"]

    dims = report["metrics"]["dimension_breakdown"]
    for name in ("task_success", "tool_correctness", "recovery", "safety"):
        assert name in dims
        assert dims[name]["pass"] + dims[name]["fail"] + dims[name]["unknown"] + dims[name]["not_applicable"] == report["executed_cases"]

    # 未执行案例不得算作成功
    assert report["not_executed_cases"] == 0
    assert report["total_cases"] == report["executed_cases"] + report["execution_error_cases"] + report["not_executed_cases"]


# 11. 不泄露敏感信息
def test_no_sensitive_leak():
    report = run_evaluation()
    text = json.dumps(report, ensure_ascii=False, default=str)
    assert "DEEPSEEK_API_KEY=" not in text
    assert "sk-proj-" not in text


# 12. 不调用付费 API
def test_no_paid_api():
    report = run_evaluation()
    assert report["environment"]["model"]["simulated"] is True
    assert report["environment"]["model"]["paid_api_called"] is False


# 补充：执行异常案例不参与成功率分母
def test_execution_error_not_counted_as_success():
    data = _dataset()
    mutated = json.loads(json.dumps(data))
    for c in mutated["cases"]:
        if c["case_id"] == "case-03-text-analyzer":
            c["execution"]["tool"] = "subprocess"  # 非白名单 -> 执行异常
    report = run_evaluation(mutated)
    assert report["execution_error_cases"] == 1
    assert report["executed_cases"] == len(mutated["cases"]) - 1
    assert report["metrics"]["task_success_rate"]["denominator"] == report["executed_cases"]
    assert report["passed_cases"] <= report["executed_cases"]


# 补充：多步骤依赖案例应产生 >= 2 次工具调用
def test_multi_step_case():
    case = _case("case-12-multi-step-dependency")
    trace = execute_case(case)
    tool_calls = [s for s in trace.steps if s.step_type.value == "tool_call"]
    assert len(tool_calls) >= 2
    r = evaluate_trace(trace, case)
    assert r["metrics"]["task_success"]["status"] == PASS
    assert r["metrics"]["tool_correctness"]["status"] == PASS


# 补充：故障恢复案例应被判为恢复成功
def test_recovery_case():
    case = _case("case-11-fault-recovery")
    trace = execute_case(case)
    r = evaluate_trace(trace, case)
    assert r["metrics"]["recovery"]["status"] == PASS
    assert r["overall_status"] == PASS


# 补充：build_evaluation_report 别名可用
def test_report_alias():
    rep = build_evaluation_report()
    for key in ("evaluation_run_id", "dataset_id", "dataset_version", "total_cases",
                "executed_cases", "passed_cases", "failed_cases", "unknown_cases",
                "metrics", "bad_cases", "trace_ids", "environment", "limitations"):
        assert key in rep
