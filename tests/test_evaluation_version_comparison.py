"""第 49 步：Evaluation Version Comparison（评测版本对比）测试。

覆盖：
- 版本元数据来自真实运行记录（不伪造 agent_version / Rubric）
- 可比性检查（COMPARABLE / COMPARABLE_WITH_WARNINGS / NOT_COMPARABLE）
- 指标差异：分子分母保留、pp 与相对变化分开、UNKNOWN 不当 0
- Case-level Diff：FAIL→PASS / PASS→FAIL / PASS→PASS / FAIL→FAIL / UNKNOWN
- 缺失案例单独列出、不参与配对统计
- 报告持久化与查询、API 契约
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
from backend.app.services.evaluation_version_comparison import (  # noqa: E402
    VersionComparisonError,
    build_version_descriptor,
    check_comparability,
    compare_runs,
    get_comparison,
    list_comparisons,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# 版本元数据
# ---------------------------------------------------------------------------

def test_build_version_descriptor_from_real_run(real_fault_pair):
    run_a, run_b = real_fault_pair
    desc = build_version_descriptor(run_a)
    assert desc["evaluation_version_id"].startswith("ev-ver-")
    assert desc["evaluation_run_id"] == run_a
    assert desc["dataset_id"] == "tracepilot-fault-v1"
    assert desc["dataset_version"] == "1.0.0"
    assert len(desc["dataset_fingerprint"]) == 64
    assert desc["dataset_case_count"] == 8
    assert desc["evaluator_version"] == "0.1.0"
    assert desc["judge_mode"] == "none"
    # 受控执行器没有独立 agent_version / prompt：保持 None 并写明原因，不伪造
    assert desc["agent_version"] is None
    assert "未伪造" in desc["agent_version_note"]
    assert desc["prompt_version"] is None
    assert desc["prompt_version_note"]
    assert desc["rubric_id"] is None and desc["rubric_version"] is None
    assert desc["model_name"] == "local-simulation"
    assert desc["model_simulated"] is True
    assert desc["tool_config_version"] and len(desc["tool_config_version"]) == 12
    assert desc["execution_config"] == {
        "data_source": "synthetic",
        "judge_mode": "none",
        "judge_rubric_id": None,
        "max_cases": None,
        "execution_engine": "controlled_simulation",
        "paid_api_called": False,
    }

    again = build_version_descriptor(run_a)
    assert again["evaluation_version_id"] == desc["evaluation_version_id"]
    assert again["identity_hash"] == desc["identity_hash"]

    # 同一版本的两次独立运行：运行 ID 不同，但版本 ID / 身份哈希相同
    other = build_version_descriptor(run_b)
    assert other["evaluation_run_id"] != desc["evaluation_run_id"]
    assert other["evaluation_version_id"] == desc["evaluation_version_id"]
    assert other["identity_hash"] == desc["identity_hash"]


def test_build_version_descriptor_unknown_run_raises():
    with pytest.raises(VersionComparisonError):
        build_version_descriptor("eval-does-not-exist")


# ---------------------------------------------------------------------------
# 可比性检查
# ---------------------------------------------------------------------------

def test_check_comparability_levels(real_fault_pair):
    run_a, run_b = real_fault_pair
    da = build_version_descriptor(run_a)
    db = build_version_descriptor(run_b)

    same = check_comparability(da, db)
    assert same["verdict"] == "COMPARABLE"
    assert same["is_comparable"] is True
    assert same["issue_count"] == 0
    # 双方都未启用 Judge → Rubric 不适用（跳过），而不是"元数据缺失"警告
    assert [s["code"] for s in same["skipped_checks"]] == ["RUBRIC_CHECK_SKIPPED"]

    warn = check_comparability(da, {**db, "tool_config_version": "ffffffffffff"})
    assert warn["verdict"] == "COMPARABLE_WITH_WARNINGS"
    assert warn["is_comparable"] is True
    assert [i["code"] for i in warn["warning_issues"]] == ["TOOL_CONFIG_MISMATCH"]

    warn2 = check_comparability(da, {**db, "evaluator_version": "9.9.9"})
    assert warn2["verdict"] == "COMPARABLE_WITH_WARNINGS"
    assert [i["code"] for i in warn2["warning_issues"]] == ["EVALUATOR_VERSION_MISMATCH"]

    judge_a = {
        **da,
        "judge_mode": "mock",
        "rubric_id": "rubric-default",
        "rubric_version": "1.0.0",
        "execution_config": {**da["execution_config"], "judge_mode": "mock",
                             "judge_rubric_id": "rubric-default"},
    }
    judge_b = {
        **judge_a,
        "rubric_id": "rubric-other",
        "rubric_version": "2.0.0",
        "execution_config": {**judge_a["execution_config"],
                             "judge_rubric_id": "rubric-other"},
    }
    bad = check_comparability(judge_a, judge_b)
    assert bad["verdict"] == "NOT_COMPARABLE"
    assert bad["is_comparable"] is False
    codes = {i["code"] for i in bad["critical_issues"]}
    assert {"RUBRIC_ID_MISMATCH", "RUBRIC_VERSION_MISMATCH"} <= codes
    assert bad["note"]

    # 不同执行模式（模拟 / 真实）不可比
    mode = check_comparability(
        da, {**db, "execution_config": {**db["execution_config"],
                                        "execution_engine": "real_api"}},
    )
    assert mode["verdict"] == "NOT_COMPARABLE"
    assert [i["code"] for i in mode["critical_issues"]] == ["EXECUTION_MODE_MISMATCH"]


# ---------------------------------------------------------------------------
# 真实运行对比
# ---------------------------------------------------------------------------

def test_compare_real_same_version(real_fault_pair):
    run_a, run_b = real_fault_pair
    cmp = compare_runs(run_a, run_b)
    assert cmp["comparison_id"].startswith("cmp-")
    assert cmp["baseline_version"]["evaluation_run_id"] == run_a
    assert cmp["candidate_version"]["evaluation_run_id"] == run_b
    assert cmp["comparability"]["verdict"] == "COMPARABLE"
    assert cmp["comparability"]["issue_count"] == 0

    metrics = {m["metric"]: m for m in cmp["metric_deltas"]}
    assert {"task_success_rate", "tool_correctness_rate", "safety_pass_rate"} <= set(metrics)
    for name in ("task_success_rate", "tool_correctness_rate", "safety_pass_rate"):
        m = metrics[name]
        assert m["delta"] == 0.0
        assert m["delta_unit"] == "percentage_point"
        assert m["status"] == "UNCHANGED"
        assert m["baseline"]["numerator"] == m["candidate"]["numerator"]
        assert m["baseline"]["denominator"] == m["candidate"]["denominator"] == 8
        assert "案例数" in m["denominator_definition"]
        # 基线为 0 时相对变化无定义 → None（不是 0%）
        if m["baseline"]["value"] == 0:
            assert m["relative_change"] is None
        else:
            assert m["relative_change"] == 0.0

    # 标量指标没有分子分母：如实置 None，不编造
    duration = metrics["average_duration_ms"]
    assert duration["delta_unit"] == "absolute"
    assert duration["shape"] == "scalar"
    assert duration["baseline"]["numerator"] is None
    assert duration["baseline"]["denominator"] is None

    counts = cmp["cases"]["counts"]
    assert counts == {
        "common": 8,
        "improved": 0,
        "regressed": 0,
        "stable_pass": 0,
        "stable_fail": 6,
        "unknown_involved": 2,
        "only_in_baseline": 0,
        "only_in_candidate": 0,
    }
    assert cmp["cases"]["regressed_cases"] == []
    assert cmp["cases"]["missing_cases"] == {"only_in_baseline": [], "only_in_candidate": []}
    row = next(r for r in cmp["cases"]["rows"] if r["status_baseline"] == "FAIL")
    assert row["transition"] == "FAIL_TO_FAIL"
    assert row["is_regression"] is False
    assert row["links"]["trace_baseline"].startswith("/api/traces/")
    assert row["links"]["trace_candidate"].startswith("/api/traces/")
    assert row["links"]["dag_candidate"].endswith("/dag")
    assert set(row["dimension_status"]) == set(fx.CASE_DIMENSIONS)

    # 失败模式分布变化复用第 48 步分析结果
    fp = cmp["failure_pattern_diff"]
    assert fp["status"] == "AVAILABLE"
    assert fp["baseline"]["denominators"]["bad_cases"] == 6
    assert fp["candidate"]["denominators"]["bad_cases"] == 6
    assert fp["baseline"]["denominators"]["analyzed_cases"] == 6
    assert len(fp["rows"]) >= 1
    assert fp["new_patterns"] == [] and fp["resolved_patterns"] == []

    assert cmp["bad_case_diff"]["baseline"]["failed_cases"] == 6
    assert cmp["bad_case_diff"]["baseline"]["unknown_cases"] == 2
    assert cmp["bad_case_diff"]["delta_failed_cases"] == 0
    assert cmp["paid_api_called"] is False
    assert cmp["baseline_version"]["execution_config"]["paid_api_called"] is False
    assert len(cmp["limitations"]) >= 5
    assert cmp["limits"]["compared_cases"] == 8


def test_compare_different_dataset_not_comparable(real_fault_pair, real_main_run):
    run_a, _ = real_fault_pair
    cmp = compare_runs(run_a, real_main_run)
    assert cmp["comparability"]["verdict"] == "NOT_COMPARABLE"
    assert cmp["comparability"]["is_comparable"] is False
    criticals = {i["code"] for i in cmp["comparability"]["critical_issues"]}
    assert {"DATASET_ID_MISMATCH", "DATASET_FINGERPRINT_MISMATCH",
            "CASE_COUNT_MISMATCH"} <= criticals
    counts = cmp["cases"]["counts"]
    assert counts["common"] == 0
    assert counts["only_in_baseline"] == 8
    assert counts["only_in_candidate"] == 13
    assert cmp["cases"]["rows"] == []
    # 不可比时仍保留指标差异（仅供诊断），但报告必须明确不可解释为提升
    assert cmp["metric_deltas"]
    assert any("不可比" in line for line in cmp["limitations"])
    assert cmp["comparability"]["note"]


# ---------------------------------------------------------------------------
# 指标差异计算
# ---------------------------------------------------------------------------

def _breakdown(**dims) -> dict:
    """构造 dimension_breakdown（每维保留 pass/fail/unknown/适用分母）。"""
    return {
        name: {
            "pass": spec["pass"],
            "fail": spec["fail"],
            "unknown": spec["unknown"],
            "not_applicable": spec["not_applicable"],
            "applicable_denominator": spec["pass"] + spec["fail"] + spec["unknown"],
            "pass_rate": round(
                spec["pass"] / (spec["pass"] + spec["fail"] + spec["unknown"]), 6
            ) if spec["pass"] + spec["fail"] + spec["unknown"] else None,
        }
        for name, spec in dims.items()
    }


def test_metric_delta_pp_relative_and_scalar():
    statuses = ["FAIL"] * 8
    base, cand = fx.simple_pair(
        baseline_statuses=statuses,
        candidate_statuses=statuses,
        baseline_metrics=fx.make_metrics(
            task_success=(4, 8), tool_correctness=(8, 8), safety=(8, 8),
            recovery=(2, 2), average_duration_ms=200.0, total_tokens=1000,
            dimension_breakdown=_breakdown(
                task_success={"pass": 4, "fail": 4, "unknown": 0, "not_applicable": 0},
                safety={"pass": 8, "fail": 0, "unknown": 0, "not_applicable": 0},
            ),
        ),
        candidate_metrics=fx.make_metrics(
            task_success=(2, 8), tool_correctness=(8, 8), safety=(8, 8),
            recovery=(2, 2), average_duration_ms=100.0, total_tokens=1500,
            dimension_breakdown=_breakdown(
                task_success={"pass": 2, "fail": 6, "unknown": 0, "not_applicable": 0},
                safety={"pass": 8, "fail": 0, "unknown": 0, "not_applicable": 0},
            ),
        ),
    )
    cmp = compare_runs(base, cand)
    assert cmp["comparability"]["verdict"] == "COMPARABLE"
    metrics = {m["metric"]: m for m in cmp["metric_deltas"]}

    ts = metrics["task_success_rate"]
    assert ts["baseline"] == {"value": 0.5, "numerator": 4, "denominator": 8}
    assert ts["candidate"] == {"value": 0.25, "numerator": 2, "denominator": 8}
    assert ts["delta"] == -25.0                     # 百分点
    assert ts["delta_unit"] == "percentage_point"
    assert ts["relative_change"] == -0.5            # 相对基线比例
    assert ts["relative_change_unit"] == "ratio_of_baseline"
    assert ts["status"] == "REGRESSED"
    assert ts["note"] is None

    dur = metrics["average_duration_ms"]
    assert dur["delta"] == -100.0
    assert dur["delta_unit"] == "absolute"
    assert dur["relative_change"] == -0.5
    assert dur["status"] == "IMPROVED"              # 越低越好

    tokens = metrics["total_tokens"]
    assert tokens["delta"] == 500.0
    assert tokens["delta_unit"] == "absolute"
    assert tokens["status"] == "REGRESSED"          # 成本越高越差

    # 维度差异同样保留通过/失败数与适用分母
    dims = {d["dimension"]: d for d in cmp["dimension_deltas"]}
    assert dims["task_success"]["baseline"]["pass"] == 4
    assert dims["task_success"]["candidate"]["pass"] == 2
    assert dims["task_success"]["delta"] == -25.0
    assert dims["task_success"]["status"] == "REGRESSED"
    assert dims["safety"]["status"] == "UNCHANGED"


def test_unknown_metric_not_treated_as_zero():
    statuses = ["FAIL"] * 8
    base, cand = fx.simple_pair(
        baseline_statuses=statuses,
        candidate_statuses=statuses,
        baseline_metrics=fx.make_metrics(
            task_success=(4, 8), recovery=(1, 2), average_duration_ms=100.0,
        ),
        candidate_metrics=fx.make_metrics(task_success=(4, 8), recovery=(0, 0)),
    )
    cmp = compare_runs(base, cand)
    metrics = {m["metric"]: m for m in cmp["metric_deltas"]}

    recovery = metrics["recovery_rate"]
    assert recovery["baseline"]["value"] == 0.5
    assert recovery["candidate"]["value"] is None
    assert recovery["candidate"]["denominator"] == 0
    assert recovery["delta"] is None
    assert recovery["relative_change"] is None
    assert recovery["status"] == "UNKNOWN"
    assert "不当作 0" in recovery["note"]

    duration = metrics["average_duration_ms"]
    assert duration["candidate"]["value"] is None
    assert duration["delta"] is None
    assert duration["status"] == "UNKNOWN"

    # 两侧都不存在的指标不进入差异列表（不臆造缺失指标）
    assert "safety_pass_rate" not in metrics


# ---------------------------------------------------------------------------
# Case-level Diff
# ---------------------------------------------------------------------------

def test_case_level_diff_transitions():
    base_statuses = ["FAIL", "PASS", "PASS", "FAIL", "UNKNOWN", "PASS", "UNKNOWN",
                     "NOT_APPLICABLE"]
    cand_statuses = ["PASS", "FAIL", "PASS", "FAIL", "PASS", "UNKNOWN", "UNKNOWN",
                     "PASS"]
    case_ids = [f"case-{i + 1:02d}" for i in range(8)]
    base = fx.inject_run(fx.build_report(
        fx.new_run_id(),
        [fx.make_case("case-01", "FAIL", trace_id="trace-b-01")] +
        [fx.make_case(cid, st) for cid, st in zip(case_ids[1:], base_statuses[1:])],
        metrics=fx.make_metrics(task_success=(4, 8), tool_correctness=(8, 8),
                                safety=(8, 8), recovery=(1, 1)),
    ))
    cand = fx.inject_run(fx.build_report(
        fx.new_run_id(),
        [fx.make_case("case-01", "PASS", trace_id="trace-c-01")] +
        [fx.make_case(cid, st) for cid, st in zip(case_ids[1:], cand_statuses[1:])],
        metrics=fx.make_metrics(task_success=(4, 8), tool_correctness=(8, 8),
                                safety=(8, 8), recovery=(1, 1)),
    ))
    cmp = compare_runs(base, cand)
    counts = cmp["cases"]["counts"]
    assert counts["common"] == 8
    assert counts["improved"] == 1
    assert counts["regressed"] == 1
    assert counts["stable_pass"] == 1
    assert counts["stable_fail"] == 1
    assert counts["unknown_involved"] == 4
    assert counts["only_in_baseline"] == counts["only_in_candidate"] == 0

    rows = {r["case_id"]: r for r in cmp["cases"]["rows"]}
    assert rows["case-01"]["transition"] == "FAIL_TO_PASS"
    assert rows["case-01"]["is_improvement"] is True
    assert rows["case-02"]["transition"] == "PASS_TO_FAIL"
    assert rows["case-02"]["is_regression"] is True
    assert rows["case-03"]["transition"] == "PASS_TO_PASS"
    assert rows["case-04"]["transition"] == "FAIL_TO_FAIL"
    assert rows["case-05"]["transition"] == "UNKNOWN_INVOLVED"
    assert rows["case-06"]["transition"] == "UNKNOWN_INVOLVED"
    assert rows["case-08"]["transition"] == "UNKNOWN_INVOLVED"

    assert [c["case_id"] for c in cmp["cases"]["regressed_cases"]] == ["case-02"]
    assert [c["case_id"] for c in cmp["cases"]["improved_cases"]] == ["case-01"]
    assert rows["case-01"]["trace_id_baseline"] == "trace-b-01"
    assert rows["case-01"]["links"]["trace_candidate"] == "/api/traces/trace-c-01"
    assert "不参与状态变化统计" in cmp["cases"]["note"]


def test_missing_cases_excluded_from_pairing():
    case_ids = [f"case-{i + 1:02d}" for i in range(8)]
    base, cand = fx.simple_pair(
        case_ids=case_ids,
        baseline_statuses=["FAIL"] * 8,
        candidate_statuses=["FAIL"] * 7,
        baseline_metrics=fx.make_metrics(task_success=(0, 8), tool_correctness=(0, 8),
                                         safety=(8, 8), recovery=(1, 1)),
        candidate_metrics=fx.make_metrics(task_success=(0, 7), tool_correctness=(0, 7),
                                          safety=(7, 7), recovery=(1, 1)),
    )
    cmp = compare_runs(base, cand)
    # 只有案例集合不同：警告级（共同案例子集仍可诊断），不是 NOT_COMPARABLE
    assert cmp["comparability"]["verdict"] == "COMPARABLE_WITH_WARNINGS"
    assert "CASE_SET_MISMATCH" in {i["code"] for i in cmp["comparability"]["warning_issues"]}
    counts = cmp["cases"]["counts"]
    assert counts["common"] == 7
    assert counts["only_in_baseline"] == 1
    assert counts["only_in_candidate"] == 0
    assert cmp["cases"]["missing_cases"] == {
        "only_in_baseline": ["case-08"], "only_in_candidate": [],
    }
    assert "case-08" not in {r["case_id"] for r in cmp["cases"]["rows"]}


# ---------------------------------------------------------------------------
# 校验与持久化
# ---------------------------------------------------------------------------

def test_comparison_validation_and_persistence(real_fault_pair):
    run_a, run_b = real_fault_pair
    with pytest.raises(VersionComparisonError):
        compare_runs(run_a, run_a)
    with pytest.raises(VersionComparisonError):
        compare_runs(run_a, run_b, max_cases=0)
    with pytest.raises(VersionComparisonError):
        compare_runs(run_a, run_b, max_cases=51)

    cmp = compare_runs(run_a, run_b, max_cases=5)
    assert cmp["limits"] == {"max_cases": 5, "compared_cases": 5}
    assert len(cmp["cases"]["rows"]) == 5

    loaded = get_comparison(cmp["comparison_id"])
    assert loaded["comparison_id"] == cmp["comparison_id"]
    assert loaded["cases"]["counts"] == cmp["cases"]["counts"]
    assert loaded["metric_deltas"] == cmp["metric_deltas"]

    items = list_comparisons(limit=50)
    assert any(i["comparison_id"] == cmp["comparison_id"] for i in items)
    entry = next(i for i in items if i["comparison_id"] == cmp["comparison_id"])
    assert entry["baseline_run_id"] == run_a
    assert entry["candidate_run_id"] == run_b
    assert entry["comparability"] == "COMPARABLE"

    with pytest.raises(VersionComparisonError):
        get_comparison("cmp-does-not-exist")
    with pytest.raises(VersionComparisonError):
        get_comparison("../../etc/passwd")
    with pytest.raises(VersionComparisonError):
        get_comparison("not-a-comparison-id")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_api_version_comparison_flow(real_fault_pair):
    run_a, run_b = real_fault_pair

    runs = client.get("/api/version-comparison/runs", params={"limit": 50})
    assert runs.status_code == 200
    body = runs.json()
    assert body["count"] == len(body["runs"]) >= 2
    listed = {r["evaluation_run_id"]: r for r in body["runs"]}
    assert run_a in listed and run_b in listed
    assert listed[run_a]["evaluation_version_id"].startswith("ev-ver-")
    assert listed[run_a]["dataset_id"] == "tracepilot-fault-v1"

    created = client.post("/api/version-comparison/runs", json={
        "baseline_run_id": run_a,
        "candidate_run_id": run_b,
        "max_cases": 5,
    })
    assert created.status_code == 200, created.text
    cmp = created.json()
    cid = cmp["comparison_id"]
    assert cmp["comparability"]["verdict"] == "COMPARABLE"
    assert cmp["limits"]["compared_cases"] == 5

    report = client.get(f"/api/version-comparison/reports/{cid}")
    assert report.status_code == 200
    assert report.json()["report"]["comparison_id"] == cid

    metrics = client.get(f"/api/version-comparison/reports/{cid}/metrics")
    assert metrics.status_code == 200
    mbody = metrics.json()
    assert mbody["metric_deltas"]
    assert mbody["comparability"]["verdict"] == "COMPARABLE"
    assert mbody["bad_case_diff"]["baseline"]["failed_cases"] == 6

    cases = client.get(f"/api/version-comparison/reports/{cid}/cases")
    assert cases.status_code == 200
    assert cases.json()["count"] == 5
    filtered = client.get(
        f"/api/version-comparison/reports/{cid}/cases",
        params={"transition": "FAIL_TO_FAIL"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["count"] >= 1
    assert all(r["transition"] == "FAIL_TO_FAIL" for r in filtered.json()["rows"])
    empty = client.get(
        f"/api/version-comparison/reports/{cid}/cases",
        params={"transition": "PASS_TO_FAIL"},
    )
    assert empty.json()["count"] == 0 and empty.json()["rows"] == []

    patterns = client.get(f"/api/version-comparison/reports/{cid}/failure-patterns")
    assert patterns.status_code == 200
    assert patterns.json()["failure_pattern_diff"]["status"] == "AVAILABLE"

    traces = client.get(f"/api/version-comparison/reports/{cid}/traces")
    assert traces.status_code == 200
    tbody = traces.json()
    # max_cases=5 截断后的 5 行 × 基线/候选两侧
    assert tbody["count"] == 5 * 2
    assert all(t["status"] == "AVAILABLE" for t in tbody["traces"])
    assert all(t["step_count"] >= 1 for t in tbody["traces"])

    reports = client.get("/api/version-comparison/reports", params={"limit": 50})
    assert reports.status_code == 200
    assert cid in {r["comparison_id"] for r in reports.json()["reports"]}

    # 错误路径
    same = client.post("/api/version-comparison/runs", json={
        "baseline_run_id": run_a, "candidate_run_id": run_a,
    })
    assert same.status_code == 400
    unknown = client.post("/api/version-comparison/runs", json={
        "baseline_run_id": run_a, "candidate_run_id": "eval-nope",
    })
    assert unknown.status_code == 400
    assert client.get("/api/version-comparison/reports/cmp-nope").status_code == 404
    assert client.get("/api/version-comparison/reports/..%2F..%2Fetc").status_code == 404
    assert client.get("/api/version-comparison/runs", params={"limit": 0}).status_code == 422
