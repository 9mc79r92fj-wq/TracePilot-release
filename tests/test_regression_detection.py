"""第 50 步：Regression Detection（回归检测）测试。

覆盖：
- 默认 8 条规则 + 项目默认阈值的透明标注
- 每条规则的触发 / 不触发 / 无法求值 / 不适用 / 显式禁用 语义
- 风险等级（LOW/MEDIUM/HIGH/CRITICAL/UNKNOWN）与门禁
  （PASS/WARN/BLOCK/INCONCLUSIVE），INCONCLUSIVE 绝不等于 PASS
- 指标型、案例型、失败模式型三类规则
- 可比性不足 → INCONCLUSIVE；证据缺失 → WARN（不放行）
- 自定义阈值 / 禁用规则 / 非法配置
- 报告持久化、证据引用、API 契约
- 全程不调用付费 API
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient  # noqa: E402

import eval_fixtures as fx  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.services.evaluation_version_comparison import compare_runs  # noqa: E402
from backend.app.services.regression_detector import (  # noqa: E402
    DEFAULTS_LABEL,
    DEFAULT_RULES,
    RegressionError,
    detect_regressions,
    get_regression_report,
    list_regression_reports,
)

client = TestClient(app)
RULE_IDS = [r.rule_id for r in DEFAULT_RULES]


# ---------------------------------------------------------------------------
# 场景构造
# ---------------------------------------------------------------------------

def _complete_metrics(
    *,
    task_success: tuple[int, int] = (8, 8),
    tool_correctness: tuple[int, int] = (8, 8),
    safety: tuple[int, int] = (8, 8),
    recovery: tuple[int, int] = (1, 1),
    average_duration_ms: float = 100.0,
    total_tokens: float = 1000.0,
) -> dict:
    return fx.make_metrics(
        task_success=task_success,
        tool_correctness=tool_correctness,
        safety=safety,
        recovery=recovery,
        average_duration_ms=average_duration_ms,
        total_tokens=total_tokens,
    )


def _pair(
    *,
    baseline_metrics: dict,
    candidate_metrics: dict,
    baseline_statuses: list[str] | None = None,
    candidate_statuses: list[str] | None = None,
) -> tuple[str, str]:
    base_statuses = baseline_statuses or ["FAIL"] * 8
    cand_statuses = candidate_statuses or ["FAIL"] * 8
    return fx.simple_pair(
        baseline_statuses=base_statuses,
        candidate_statuses=cand_statuses,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
    )


def _run_pair(
    *,
    baseline_metrics: dict,
    candidate_metrics: dict,
    baseline_statuses: list[str] | None = None,
    candidate_statuses: list[str] | None = None,
    rules: list[dict] | None = None,
) -> dict:
    base, cand = _pair(
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
        baseline_statuses=baseline_statuses,
        candidate_statuses=candidate_statuses,
    )
    return detect_regressions(
        baseline_run_id=base, candidate_run_id=cand, rules=rules
    )


# ---------------------------------------------------------------------------
# 真实运行：无退化
# ---------------------------------------------------------------------------

def test_real_same_version_no_regression(real_fault_pair):
    run_a, run_b = real_fault_pair
    # 真实受控执行的耗时会抖动，这里显式禁用耗时/成本两条规则以隔离计时噪声；
    # 禁用行为本身也在断言范围内（必须如实列在 disabled_rules 中）。
    rules = [
        {"rule_id": "LATENCY_INCREASE", "enabled": False},
        {"rule_id": "COST_INCREASE", "enabled": False},
    ]
    rep = detect_regressions(baseline_run_id=run_a, candidate_run_id=run_b, rules=rules)
    assert rep["regression_report_id"].startswith("reg-")
    assert rep["comparability"]["verdict"] == "COMPARABLE"
    assert rep["risk_level"] == "LOW"
    assert rep["gate_status"] == "PASS"
    assert rep["triggered_rules"] == []
    assert rep["not_evaluable_rules"] == []
    assert [e["rule_id"] for e in rep["not_applicable_rules"]] == ["RECOVERY_RATE_DROP"]
    assert [e["rule_id"] for e in rep["disabled_rules"]] == [
        "LATENCY_INCREASE", "COST_INCREASE",
    ]
    assert "被配置禁用" in rep["gate_reason"]
    assert "不适用" in rep["gate_reason"]
    assert rep["threshold_source"] == DEFAULTS_LABEL
    assert rep["baseline_version"]["evaluation_run_id"] == run_a
    assert rep["candidate_version"]["evaluation_run_id"] == run_b
    assert rep["paid_api_called"] is False
    assert rep["data_source"] == "analysis_over_evaluation_runs"
    assert rep["evidence_refs"][0] == {
        "kind": "comparison_report",
        "ref_id": rep["comparison_id"],
        "detail": "comparability=COMPARABLE",
    }
    assert any("INCONCLUSIVE" in line and "PASS" in line for line in rep["limitations"])
    assert any("配置禁用" in line for line in rep["limitations"])


def test_all_rules_evaluable_pass():
    rep = _run_pair(
        baseline_metrics=_complete_metrics(),
        candidate_metrics=_complete_metrics(),
    )
    assert rep["risk_level"] == "LOW"
    assert rep["gate_status"] == "PASS"
    assert rep["risk_reason"] == "所有规则均可求值且无命中"
    assert rep["gate_reason"] == "无规则命中且证据完整"
    assert len(rep["rule_events"]) == 8
    assert {e["status"] for e in rep["rule_events"]} == {"NOT_TRIGGERED"}
    assert rep["not_evaluable_rules"] == rep["not_applicable_rules"] == []
    assert rep["disabled_rules"] == []


# ---------------------------------------------------------------------------
# 指标型规则
# ---------------------------------------------------------------------------

def test_task_success_drop_blocks():
    rep = _run_pair(
        baseline_metrics=_complete_metrics(task_success=(7, 8)),
        candidate_metrics=_complete_metrics(task_success=(4, 8)),
    )
    assert rep["risk_level"] == "HIGH"
    assert rep["gate_status"] == "BLOCK"
    assert [e["rule_id"] for e in rep["triggered_rules"]] == ["TASK_SUCCESS_DROP"]
    rule = rep["triggered_rules"][0]
    assert rule["severity"] == "HIGH"
    assert rule["blocking"] is True
    assert rule["observed"]["change_pp"] == -37.5
    assert rule["reason"].startswith("task_success_rate 变化 -37.50pp")
    assert "TASK_SUCCESS_DROP" in rep["gate_reason"]
    assert "BLOCK" not in rep["gate_reason"]
    assert rep["metric_deltas"]["task_success_rate"]["numerators_denominators"] == {
        "baseline": [7, 8], "candidate": [4, 8],
    }


def test_threshold_override_changes_verdict():
    # 同一对运行：默认阈值 5pp → 触发阻断；阈值 20pp → 不触发（可配置）
    base_metrics = _complete_metrics(task_success=(4, 8))
    cand_metrics = _complete_metrics(task_success=(3, 8))
    default = _run_pair(baseline_metrics=base_metrics, candidate_metrics=cand_metrics)
    assert default["gate_status"] == "BLOCK"
    assert [e["rule_id"] for e in default["triggered_rules"]] == ["TASK_SUCCESS_DROP"]

    relaxed = _run_pair(
        baseline_metrics=base_metrics,
        candidate_metrics=cand_metrics,
        rules=[{"rule_id": "TASK_SUCCESS_DROP", "threshold": 20.0}],
    )
    assert relaxed["gate_status"] == "PASS"
    assert relaxed["risk_level"] == "LOW"
    rule = next(r for r in relaxed["rules"] if r["rule_id"] == "TASK_SUCCESS_DROP")
    assert rule["threshold"] == 20.0
    assert rule["threshold_unit"] == "percentage_point"
    assert rule["source"] == DEFAULTS_LABEL
    event = next(e for e in relaxed["rule_events"] if e["rule_id"] == "TASK_SUCCESS_DROP")
    assert event["status"] == "NOT_TRIGGERED"
    assert event["observed"]["change_pp"] == -12.5


def test_new_safety_failure_is_critical_and_blocks():
    rep = _run_pair(
        baseline_metrics=_complete_metrics(safety=(8, 8)),
        candidate_metrics=_complete_metrics(safety=(7, 8)),
    )
    assert rep["risk_level"] == "CRITICAL"
    assert rep["gate_status"] == "BLOCK"
    assert [e["rule_id"] for e in rep["triggered_rules"]] == ["NEW_SAFETY_FAILURE"]
    assert rep["triggered_rules"][0]["severity"] == "CRITICAL"
    assert "NEW_SAFETY_FAILURE" in rep["gate_reason"]


def test_tool_correctness_drop_is_warn_not_block():
    rep = _run_pair(
        baseline_metrics=_complete_metrics(tool_correctness=(8, 8)),
        candidate_metrics=_complete_metrics(tool_correctness=(6, 8)),
    )
    assert [e["rule_id"] for e in rep["triggered_rules"]] == ["TOOL_CORRECTNESS_DROP"]
    assert rep["risk_level"] == "MEDIUM"
    assert rep["gate_status"] == "WARN"
    assert "存在非阻断退化" in rep["gate_reason"]


def test_latency_and_cost_increase():
    rep = _run_pair(
        baseline_metrics=_complete_metrics(average_duration_ms=100.0, total_tokens=1000.0),
        candidate_metrics=_complete_metrics(average_duration_ms=200.0, total_tokens=1400.0),
    )
    triggered = {e["rule_id"]: e for e in rep["triggered_rules"]}
    assert set(triggered) == {"LATENCY_INCREASE", "COST_INCREASE"}
    assert triggered["LATENCY_INCREASE"]["severity"] == "LOW"
    assert triggered["LATENCY_INCREASE"]["observed"]["relative_change"] == 1.0
    assert triggered["COST_INCREASE"]["severity"] == "MEDIUM"
    assert triggered["COST_INCREASE"]["observed"]["relative_change"] == 0.4
    assert rep["risk_level"] == "MEDIUM"
    assert rep["gate_status"] == "WARN"
    assert rep["metric_deltas"]["average_duration_ms"]["delta_unit"] == "absolute"


def test_latency_noise_suppressed_by_min_absolute():
    # +30% 相对上升但绝对增量仅 3ms < min_absolute 5ms → 噪声抑制，不触发
    rep = _run_pair(
        baseline_metrics=_complete_metrics(average_duration_ms=10.0, total_tokens=1000.0),
        candidate_metrics=_complete_metrics(average_duration_ms=13.0, total_tokens=1000.0),
    )
    event = next(e for e in rep["rule_events"] if e["rule_id"] == "LATENCY_INCREASE")
    assert event["status"] == "NOT_TRIGGERED"
    assert "噪声抑制" in event["reason"]
    assert event["observed"]["relative_change"] == 0.3
    assert rep["gate_status"] == "PASS"


# ---------------------------------------------------------------------------
# 案例型 / 失败模式型规则
# ---------------------------------------------------------------------------

def test_overall_improvement_still_blocks_on_case_regression():
    base_statuses = ["PASS"] + ["FAIL"] * 7
    cand_statuses = ["FAIL"] * 8
    base_metrics = _complete_metrics(task_success=(3, 8))
    cand_metrics = _complete_metrics(task_success=(6, 8))
    base, cand = fx.simple_pair(
        baseline_statuses=base_statuses,
        candidate_statuses=cand_statuses,
        baseline_metrics=base_metrics,
        candidate_metrics=cand_metrics,
    )
    rep = detect_regressions(baseline_run_id=base, candidate_run_id=cand)

    # 聚合指标在提升，但单案例 PASS→FAIL 仍触发阻断
    assert rep["metric_deltas"]["task_success_rate"]["status"] == "IMPROVED"
    assert rep["triggered_rules"][0]["rule_id"] == "CASE_REGRESSION"
    assert rep["triggered_rules"][0]["observed"]["regressed_cases"] == 1
    assert rep["risk_level"] == "HIGH"
    assert rep["gate_status"] == "BLOCK"
    assert [c["case_id"] for c in rep["regressed_cases"]] == ["case-01"]
    case_ref = next(
        ref for ref in rep["evidence_refs"] if ref["kind"] == "case"
    )
    assert case_ref["ref_id"] == "case-01"
    assert case_ref["detail"] == "PASS→FAIL"


def test_new_failure_pattern_from_injected_comparison():
    base, cand = _pair(
        baseline_metrics=_complete_metrics(),
        candidate_metrics=_complete_metrics(),
    )
    cmp = compare_runs(base, cand)
    assert cmp["failure_pattern_diff"]["status"] == "AVAILABLE"
    cmp = {
        **cmp,
        "failure_pattern_diff": {
            "status": "AVAILABLE",
            "baseline": {"pattern_report_id": "fp-a", "pattern_count": 0,
                         "denominators": {}},
            "candidate": {"pattern_report_id": "fp-b", "pattern_count": 1,
                          "denominators": {}},
            "rows": [{
                "fingerprint_id": "fp-new",
                "fingerprint_label": "TOOL_TIMEOUT | calculator | tool_correctness | "
                                     "not_recovered | tool_timeout",
                "failure_type": "TOOL_TIMEOUT",
                "cases_baseline": 0, "cases_candidate": 2, "delta": 2, "status": "NEW",
            }],
            "new_patterns": [{
                "fingerprint_id": "fp-new",
                "fingerprint_label": "TOOL_TIMEOUT | calculator | tool_correctness | "
                                     "not_recovered | tool_timeout",
                "failure_type": "TOOL_TIMEOUT",
                "cases_baseline": 0, "cases_candidate": 2, "delta": 2, "status": "NEW",
            }],
            "resolved_patterns": [],
            "note": "指纹相同不代表根因相同；分布变化只作为诊断线索",
        },
    }
    rep = detect_regressions(comparison=cmp)
    assert rep["comparison_id"] == cmp["comparison_id"]
    assert [e["rule_id"] for e in rep["triggered_rules"]] == ["NEW_FAILURE_PATTERN"]
    assert rep["triggered_rules"][0]["severity"] == "MEDIUM"
    assert rep["risk_level"] == "MEDIUM"
    assert rep["gate_status"] == "WARN"
    pattern_ref = next(
        ref for ref in rep["evidence_refs"] if ref["kind"] == "failure_pattern"
    )
    assert pattern_ref["ref_id"] == "fp-new"


# ---------------------------------------------------------------------------
# 不可比 / 证据缺失
# ---------------------------------------------------------------------------

def test_dataset_version_mismatch_inconclusive():
    base_metrics = _complete_metrics()
    cand_metrics = _complete_metrics()
    base_report = fx.build_report(
        fx.new_run_id(),
        [fx.make_case(f"case-{i + 1:02d}", "FAIL") for i in range(8)],
        metrics=base_metrics,
    )
    cand_report = fx.build_report(
        fx.new_run_id(),
        [fx.make_case(f"case-{i + 1:02d}", "FAIL") for i in range(8)],
        metrics=cand_metrics,
        dataset_version="9.9.9",
    )
    base = fx.inject_run(base_report)
    cand = fx.inject_run(cand_report)
    rep = detect_regressions(baseline_run_id=base, candidate_run_id=cand)
    assert rep["comparability"]["verdict"] == "NOT_COMPARABLE"
    assert rep["risk_level"] == "UNKNOWN"
    assert rep["gate_status"] == "INCONCLUSIVE"
    assert rep["risk_reason"].startswith("版本不可比")
    assert "不视为通过" in rep["gate_reason"]
    assert rep["gate_status"] != "PASS"
    assert any("DATASET_VERSION_MISMATCH" in line for line in rep["limitations"])


def test_unknown_metric_not_evaluable_yields_warn():
    unknown_rate = {
        "value": None,
        "numerator": 0,
        "denominator": 1,
        "denominator_definition": "恢复维度可判定案例数",
    }
    metrics = _complete_metrics(recovery=(1, 1))
    metrics["recovery_rate"] = dict(unknown_rate)
    rep = _run_pair(baseline_metrics=metrics, candidate_metrics=dict(metrics))
    assert rep["risk_level"] == "LOW"
    assert rep["gate_status"] == "WARN"
    assert [e["rule_id"] for e in rep["not_evaluable_rules"]] == ["RECOVERY_RATE_DROP"]
    event = next(e for e in rep["rule_events"] if e["rule_id"] == "RECOVERY_RATE_DROP")
    assert event["reason"] and "UNKNOWN" in event["reason"]
    assert "不视为通过" in rep["gate_reason"]
    assert "证据缺失" in " ".join(rep["limitations"])


def test_missing_latency_and_cost_metrics_are_not_evaluable():
    metrics = _complete_metrics()
    metrics.pop("average_duration_ms")
    metrics.pop("total_tokens")
    rep = _run_pair(baseline_metrics=metrics, candidate_metrics=dict(metrics))
    assert rep["risk_level"] == "LOW"
    assert rep["gate_status"] == "WARN"
    assert {e["rule_id"] for e in rep["not_evaluable_rules"]} == {
        "LATENCY_INCREASE", "COST_INCREASE",
    }
    assert rep["gate_status"] != "PASS"


# ---------------------------------------------------------------------------
# 规则配置 / 校验 / 持久化
# ---------------------------------------------------------------------------

def test_disabled_rules_are_reported_not_hidden():
    base, cand = _pair(
        baseline_metrics=_complete_metrics(),
        candidate_metrics=_complete_metrics(),
    )
    rep = detect_regressions(
        baseline_run_id=base,
        candidate_run_id=cand,
        rules=[{"rule_id": "CASE_REGRESSION", "enabled": False}],
    )
    assert rep["gate_status"] == "PASS"
    assert [e["rule_id"] for e in rep["disabled_rules"]] == ["CASE_REGRESSION"]
    event = next(e for e in rep["rule_events"] if e["rule_id"] == "CASE_REGRESSION")
    assert event["status"] == "DISABLED"
    assert "显式配置为禁用" in event["reason"]

    all_disabled = detect_regressions(
        baseline_run_id=base,
        candidate_run_id=cand,
        rules=[{"rule_id": rid, "enabled": False} for rid in RULE_IDS],
    )
    assert all_disabled["risk_level"] == "UNKNOWN"
    assert all_disabled["gate_status"] == "INCONCLUSIVE"
    assert "全部规则被配置为禁用" in all_disabled["gate_reason"]
    assert all_disabled["gate_status"] != "PASS"


def test_rule_validation_errors(real_fault_pair):
    run_a, run_b = real_fault_pair
    with pytest.raises(RegressionError):
        detect_regressions(baseline_run_id=run_a, candidate_run_id=run_b,
                           rules=[{"rule_id": "NOT_A_RULE"}])
    with pytest.raises(RegressionError):
        detect_regressions(baseline_run_id=run_a, candidate_run_id=run_b,
                           rules=[{"rule_id": "TASK_SUCCESS_DROP", "threshold": -1.0}])
    with pytest.raises(RegressionError):
        detect_regressions(baseline_run_id=run_a, candidate_run_id=run_b,
                           rules=[{"rule_id": "TASK_SUCCESS_DROP", "severity": "UNKNOWN"}])
    with pytest.raises(RegressionError):
        detect_regressions()
    with pytest.raises(RegressionError):
        detect_regressions(baseline_run_id="eval-does-not-exist",
                           candidate_run_id=run_b)


def test_persistence_and_lookup():
    base, cand = _pair(
        baseline_metrics=_complete_metrics(task_success=(8, 8)),
        candidate_metrics=_complete_metrics(task_success=(6, 8)),
    )
    rep = detect_regressions(baseline_run_id=base, candidate_run_id=cand)
    loaded = get_regression_report(rep["regression_report_id"])
    assert loaded["regression_report_id"] == rep["regression_report_id"]
    assert loaded["risk_level"] == rep["risk_level"] == "HIGH"
    assert loaded["gate_status"] == rep["gate_status"] == "BLOCK"
    items = list_regression_reports(limit=50)
    entry = next(
        i for i in items if i["regression_report_id"] == rep["regression_report_id"]
    )
    assert entry["triggered_rules"] == ["TASK_SUCCESS_DROP"]
    assert entry["comparison_id"] == rep["comparison_id"]

    with pytest.raises(RegressionError):
        get_regression_report("reg-nope")
    with pytest.raises(RegressionError):
        get_regression_report("../../etc/passwd")
    with pytest.raises(RegressionError):
        get_regression_report("not-a-report-id")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_api_rules_and_regression_flow():
    rules_resp = client.get("/api/regression/rules")
    assert rules_resp.status_code == 200
    body = rules_resp.json()
    assert body["rule_count"] == 8
    assert [r["rule_id"] for r in body["rules"]] == RULE_IDS
    assert body["threshold_source"] == DEFAULTS_LABEL
    assert "不代表行业统一标准" in body["threshold_note"]
    assert "INCONCLUSIVE" in body["gate_note"]
    assert body["rule_statuses"] == [
        "TRIGGERED", "NOT_TRIGGERED", "NOT_EVALUABLE", "NOT_APPLICABLE", "DISABLED",
    ]
    assert all(r["rationale"] for r in body["rules"])
    assert all(r["threshold_unit"] in {"percentage_point", "ratio", "count"}
               for r in body["rules"])

    base_cases = [fx.make_case("case-01", "PASS", trace_id="trace-test-base-01")] + [
        fx.make_case(f"case-{i:02d}", "FAIL") for i in range(2, 9)
    ]
    cand_cases = [fx.make_case("case-01", "FAIL", trace_id="trace-test-cand-01")] + [
        fx.make_case(f"case-{i:02d}", "FAIL") for i in range(2, 9)
    ]
    base = fx.inject_run(fx.build_report(
        fx.new_run_id(), base_cases,
        metrics=_complete_metrics(task_success=(3, 8)),
    ))
    cand = fx.inject_run(fx.build_report(
        fx.new_run_id(), cand_cases,
        metrics=_complete_metrics(task_success=(6, 8)),
    ))
    created = client.post("/api/regression/runs", json={
        "baseline_run_id": base, "candidate_run_id": cand,
    })
    assert created.status_code == 200, created.text
    rep = created.json()
    rid = rep["regression_report_id"]
    assert rep["risk_level"] == "HIGH"
    assert rep["gate_status"] == "BLOCK"
    assert [e["rule_id"] for e in rep["triggered_rules"]] == ["CASE_REGRESSION"]

    report = client.get(f"/api/regression/reports/{rid}")
    assert report.status_code == 200
    assert report.json()["report"]["regression_report_id"] == rid

    rules = client.get(f"/api/regression/reports/{rid}/rules")
    assert rules.status_code == 200
    rbody = rules.json()
    assert rbody["triggered_rules"][0]["rule_id"] == "CASE_REGRESSION"
    assert len(rbody["rule_events"]) == 8
    assert rbody["disabled_rules"] == []
    assert rbody["threshold_source"] == DEFAULTS_LABEL

    cases = client.get(f"/api/regression/reports/{rid}/cases")
    assert cases.status_code == 200
    assert cases.json()["count"] == 1
    assert cases.json()["regressed_cases"][0]["case_id"] == "case-01"

    risk = client.get(f"/api/regression/reports/{rid}/risk")
    assert risk.status_code == 200
    assert risk.json()["risk_level"] == "HIGH"
    assert risk.json()["triggered_rules"] == ["CASE_REGRESSION"]
    assert risk.json()["comparability"] == "COMPARABLE"

    gate = client.get(f"/api/regression/reports/{rid}/gate")
    assert gate.status_code == 200
    assert gate.json()["gate_status"] == "BLOCK"
    assert "INCONCLUSIVE 不等同于 PASS" in gate.json()["note"]

    evidence = client.get(f"/api/regression/reports/{rid}/evidence")
    assert evidence.status_code == 200
    ebody = evidence.json()
    assert ebody["comparison_id"] == rep["comparison_id"]
    assert ebody["evidence_refs"]
    entry = next(e for e in ebody["rule_evidence"] if e["rule_id"] == "CASE_REGRESSION")
    assert entry["status"] == "TRIGGERED"
    assert entry["evidence_refs"][0]["kind"] == "case"
    assert ebody["limitations"]

    trace = client.get(f"/api/regression/reports/{rid}/trace")
    assert trace.status_code == 200
    # 合成运行的 trace id 不存在 → 如实返回 NOT_AVAILABLE，不伪造
    assert trace.json()["count"] == 2
    assert {t["status"] for t in trace.json()["traces"]} == {"NOT_AVAILABLE"}
    assert {t["side"] for t in trace.json()["traces"]} == {"baseline", "candidate"}

    listing = client.get("/api/regression/reports", params={"limit": 50})
    assert listing.status_code == 200
    assert rid in {r["regression_report_id"] for r in listing.json()["reports"]}

    # 通过 comparison_id 复用已有对比报告
    via_comparison = client.post("/api/regression/runs", json={
        "comparison_id": rep["comparison_id"],
    })
    assert via_comparison.status_code == 200
    assert via_comparison.json()["triggered_rules"][0]["rule_id"] == "CASE_REGRESSION"

    # 规则覆盖经 API 生效
    relaxed = client.post("/api/regression/runs", json={
        "comparison_id": rep["comparison_id"],
        "rules": [{"rule_id": "CASE_REGRESSION", "enabled": False}],
    })
    assert relaxed.status_code == 200
    assert relaxed.json()["gate_status"] == "PASS"
    assert [e["rule_id"] for e in relaxed.json()["disabled_rules"]] == ["CASE_REGRESSION"]

    # 错误路径
    assert client.post("/api/regression/runs", json={}).status_code == 400
    assert client.post("/api/regression/runs", json={
        "baseline_run_id": base, "candidate_run_id": base,
    }).status_code == 400
    assert client.post("/api/regression/runs", json={
        "comparison_id": "cmp-nope",
    }).status_code == 404
    assert client.post("/api/regression/runs", json={
        "baseline_run_id": base, "candidate_run_id": cand,
        "rules": [{"rule_id": "NOT_A_RULE"}],
    }).status_code == 400
    assert client.post("/api/regression/runs", json={
        "baseline_run_id": base, "candidate_run_id": cand,
        "rules": [{"rule_id": "TASK_SUCCESS_DROP", "threshold": -5}],
    }).status_code == 422
    assert client.get("/api/regression/reports/reg-nope").status_code == 404
