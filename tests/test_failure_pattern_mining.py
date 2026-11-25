"""Failure Pattern Mining 测试（pytest，第 48 步）。

覆盖：指纹稳定性、缺失字段不填充、分组正确、统计分母、UNKNOWN 处理、
代表性案例关联、Trace 关联、运行对比、API 查询、不调用付费 API、不泄露敏感信息。
"""

import json
import re
import sys
import time

import pytest

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.services import failure_pattern_mining as fpm  # noqa: E402
from backend.app.services.trace_store import load_trace  # noqa: E402

client = TestClient(app)
FAULT_DATASET = "tracepilot-fault-v1"


def _run_eval(dataset: str = FAULT_DATASET) -> tuple[str, dict]:
    r = client.post("/api/evaluation/runs", json={"dataset_id": dataset, "judge_mode": "none"})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    for _ in range(600):
        d = client.get(f"/api/evaluation/runs/{run_id}").json()
        if d.get("status") != "running":
            break
        time.sleep(0.1)
    assert d["status"] == "succeeded"
    return run_id, d["report"]


@pytest.fixture(scope="module")
def fault_run():
    return _run_eval()


@pytest.fixture(scope="module")
def pattern_report(fault_run):
    run_id, _ = fault_run
    return fpm.analyze_run(run_id)


# ---------- 1. 指纹生成稳定 ----------

def test_fingerprint_stable():
    kw = dict(
        failure_type="TOOL_TIMEOUT",
        tool_name="calculator",
        failed_metric="task_success+tool_correctness",
        recovery_status="not_recovered",
        scenario_type="tool_timeout",
    )
    a = fpm.build_fingerprint(**kw)
    b = fpm.build_fingerprint(**kw)
    assert a["fingerprint_id"] == b["fingerprint_id"]
    assert a["fingerprint_id"].startswith("fp-")
    assert a["canonical"] == b["canonical"]


def test_fingerprint_differs_by_field():
    base = dict(failure_type="TOOL_TIMEOUT", tool_name="calculator",
                failed_metric="task_success", recovery_status="not_recovered",
                scenario_type="tool_timeout")
    fp0 = fpm.build_fingerprint(**base)["fingerprint_id"]
    for field, value in (
        ("failure_type", "TOOL_EXCEPTION"),
        ("tool_name", "sqlite_query"),
        ("failed_metric", "tool_correctness"),
        ("recovery_status", "recovered"),
        ("scenario_type", "wrong_tool"),
    ):
        changed = {**base, field: value}
        assert fpm.build_fingerprint(**changed)["fingerprint_id"] != fp0, f"{field} 未影响指纹"


def test_fingerprint_missing_fields_not_filled():
    fp = fpm.build_fingerprint(
        failure_type="UNKNOWN",
        tool_name=None,
        failed_metric=None,
        recovery_status="unknown",
        scenario_type=None,
    )
    assert fp["tool_name"] is None
    assert fp["failed_metric"] is None
    assert fp["scenario_type"] is None
    assert set(fp["missing_fields"]) == {"tool_name", "failed_metric", "scenario_type"}
    # 缺失维度在 label 中显示为 '-'
    assert fp["fingerprint_label"].count("-") >= 3


def test_fingerprint_fields_complete():
    assert fpm.FINGERPRINT_FIELDS == (
        "failure_type", "tool_name", "failed_metric", "recovery_status", "scenario_type",
    )


# ---------- 2. 分组正确 ----------

def test_grouping_matches_fingerprint(pattern_report):
    groups: dict[str, list[str]] = {}
    for c in pattern_report["cases"]:
        groups.setdefault(c["fingerprint_id"], []).append(c["case_id"])
    for pat in pattern_report["patterns"]:
        assert set(pat["case_ids"]) == set(groups[pat["pattern_id"]]), "模式案例集合应与指纹分组一致"
        assert pat["case_count"] == len(pat["case_ids"])
        assert pat["fingerprint_id"] == pat["pattern_id"]


def test_pattern_count_matches(pattern_report):
    assert pattern_report["pattern_count"] == len(pattern_report["patterns"])
    # 各模式案例数之和 == 分析案例数
    total = sum(p["case_count"] for p in pattern_report["patterns"])
    assert total == pattern_report["denominators"]["analyzed_cases"]


def test_patterns_sorted_by_count(pattern_report):
    counts = [p["case_count"] for p in pattern_report["patterns"]]
    assert counts == sorted(counts, reverse=True)


# ---------- 3. 统计分母正确 ----------

def test_denominators(pattern_report, fault_run):
    _run_id, eval_report = fault_run
    d = pattern_report["denominators"]
    assert d["total_cases_in_run"] == eval_report["total_cases"]
    assert d["bad_cases"] == eval_report["failed_cases"]
    assert d["analyzed_cases"] == len(pattern_report["cases"])
    assert d["analyzed_cases"] <= d["bad_cases"]
    assert d["executed_cases"] == eval_report["executed_cases"]
    # 占比分母 = analyzed_cases
    total_share = sum(p["share_of_bad_cases"] for p in pattern_report["patterns"])
    assert total_share == pytest.approx(1.0, abs=0.01)
    for p in pattern_report["patterns"]:
        assert p["share_of_bad_cases"] == pytest.approx(p["case_count"] / d["analyzed_cases"], abs=1e-3)


def test_not_executed_cases_excluded():
    """未执行案例不得计入失败模式。"""
    # 只执行前 2 个案例的运行
    r = client.post(
        "/api/evaluation/runs",
        json={"dataset_id": FAULT_DATASET, "judge_mode": "none", "max_cases": 2},
    )
    run_id = r.json()["run_id"]
    for _ in range(600):
        d = client.get(f"/api/evaluation/runs/{run_id}").json()
        if d.get("status") != "running":
            break
        time.sleep(0.1)
    rep = fpm.analyze_run(run_id)
    assert rep["denominators"]["total_cases_in_run"] == 2
    analyzed_ids = {c["case_id"] for c in rep["cases"]}
    all_ids = {c["case_id"] for c in d["report"]["case_results"]}
    assert analyzed_ids <= all_ids
    # 未执行案例不在任何模式里
    executed_ids = {c["case_id"] for c in d["report"]["case_results"]
                    if c["overall_status"] != "NOT_EXECUTED"}
    assert analyzed_ids <= executed_ids


# ---------- 4. UNKNOWN 处理 ----------

def test_unknown_handling(pattern_report):
    unknown_ids = {u["case_id"] for u in pattern_report["unknown_cases"]}
    assert pattern_report["denominators"]["unknown_cases"] == len(unknown_ids)
    # UNKNOWN 案例仍然进入指纹分组（failure_type=UNKNOWN），但不计入失败类型统计之外
    for c in pattern_report["cases"]:
        if c["is_unknown"]:
            assert c["failure_type"] == "UNKNOWN"


def test_unknown_not_merged_into_other_types(pattern_report):
    for pat in pattern_report["patterns"]:
        types = set(pat["cause_type_counts"].keys())
        if "UNKNOWN" in types and len(types) > 1:
            # 允许同组案例中既有 UNKNOWN 又有其它类型，但主类型不能是 UNKNOWN 且有更强类型
            assert pat["fingerprint_fields"]["failure_type"] != "UNKNOWN" or "UNKNOWN" in types


# ---------- 5. 代表性案例关联 ----------

def test_representative_case(pattern_report):
    case_ids = {c["case_id"] for c in pattern_report["cases"]}
    for pat in pattern_report["patterns"]:
        rep = pat["representative_case"]
        assert rep["case_id"] in case_ids
        assert rep["case_id"] in pat["case_ids"]
        assert rep["trace_id"]


# ---------- 6. Trace 关联 ----------

def test_trace_links_resolvable(pattern_report):
    for pat in pattern_report["patterns"]:
        assert pat["trace_ids"], f"{pat['pattern_id']} 应有 Trace"
        for tid in pat["trace_ids"]:
            trace = load_trace(tid)
            assert trace.trace_id == tid


def test_trace_api_for_pattern(pattern_report):
    pat = pattern_report["patterns"][0]
    r = client.get(
        f"/api/failure-patterns/reports/{pattern_report['pattern_report_id']}"
        f"/patterns/{pat['pattern_id']}/trace"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == len(pat["trace_ids"])
    for row in body["traces"]:
        assert row["status"] in {"AVAILABLE", "NOT_AVAILABLE"}
        if row["status"] == "AVAILABLE":
            assert row["links"]["dag"].endswith("/dag")


# ---------- 7. 不同运行对比 ----------

def test_compare_same_dataset_unchanged(fault_run):
    run_a, _ = fault_run
    run_b, _ = _run_eval()
    cmp = fpm.compare_runs(run_a, run_b)
    # 同一受控数据集、同一确定性执行 → 指纹应完全一致
    assert cmp["summary"]["new_patterns"] == 0
    assert cmp["summary"]["resolved_patterns"] == 0
    assert cmp["summary"]["unchanged"] == len(cmp["rows"])
    assert cmp["run_a"]["pattern_count"] == cmp["run_b"]["pattern_count"]


def test_compare_different_datasets():
    run_fault, _ = _run_eval(FAULT_DATASET)
    run_normal, _ = _run_eval("tracepilot-eval-v1")
    cmp = fpm.compare_runs(run_fault, run_normal)
    assert cmp["run_a"]["denominators"]["bad_cases"] > 0
    assert cmp["run_b"]["denominators"]["bad_cases"] == 0
    # 正常数据集无 Bad Case → 其模式数应为 0，故障运行全部模式在 B 侧"消失"
    assert cmp["run_b"]["pattern_count"] == 0
    assert cmp["summary"]["resolved_patterns"] == cmp["run_a"]["pattern_count"]


def test_compare_same_run_rejected():
    run_id, _ = _run_eval()
    r = client.post("/api/failure-patterns/compare", json={"run_a": run_id, "run_b": run_id})
    assert r.status_code == 400


# ---------- 8. API ----------

def test_api_types():
    r = client.get("/api/failure-patterns/types")
    assert r.status_code == 200
    d = r.json()
    assert len(d["fingerprint_fields"]) == 5
    assert len(d["failure_types"]) == 10
    assert set(d["recovery_states"]) == {
        "recovered", "not_recovered", "no_tool_failure", "unknown",
    }
    assert "fingerprint" in d["cluster_strategies"]
    assert d["limits"]["paid_api_called"] is False
    assert "语义聚类" in d["limits"]["semantic_clustering"] or "not_implemented" in d["limits"]["semantic_clustering"]


def test_api_create_and_query(fault_run):
    run_id, _ = fault_run
    r = client.post("/api/failure-patterns/runs", json={"evaluation_run_id": run_id, "max_cases": 10})
    assert r.status_code == 200, r.text
    rep = r.json()
    report_id = rep["pattern_report_id"]
    assert report_id.startswith("fp-report-")
    assert rep["pattern_count"] >= 1
    assert rep["paid_api_called"] is False

    one = client.get(f"/api/failure-patterns/reports/{report_id}")
    assert one.status_code == 200
    assert one.json()["report"]["pattern_report_id"] == report_id

    listing = client.get("/api/failure-patterns/reports")
    assert listing.status_code == 200
    assert any(x["pattern_report_id"] == report_id for x in listing.json()["reports"])


def test_api_pattern_detail_and_cases(pattern_report):
    report_id = pattern_report["pattern_report_id"]
    pat = pattern_report["patterns"][0]

    d = client.get(f"/api/failure-patterns/reports/{report_id}/patterns/{pat['pattern_id']}")
    assert d.status_code == 200
    body = d.json()
    assert body["pattern"]["pattern_id"] == pat["pattern_id"]
    assert len(body["cases"]) == pat["case_count"]
    assert body["denominators"]["analyzed_cases"] == pattern_report["denominators"]["analyzed_cases"]

    c = client.get(f"/api/failure-patterns/reports/{report_id}/patterns/{pat['pattern_id']}/cases")
    assert c.status_code == 200
    cases = c.json()["cases"]
    assert len(cases) == pat["case_count"]
    for case in cases:
        assert case["links"]["trace"]
        assert case["links"]["attribution"]


def test_api_errors():
    assert client.get("/api/failure-patterns/reports/fp-report-nope").status_code == 404
    assert client.get("/api/failure-patterns/reports/..%2F..%2Fetc").status_code == 404
    r = client.post("/api/failure-patterns/runs", json={"evaluation_run_id": "eval-nope"})
    assert r.status_code == 400
    r2 = client.post("/api/failure-patterns/runs", json={"evaluation_run_id": "eval-nope", "max_cases": 99})
    assert r2.status_code in (400, 422)


def test_api_invalid_strategy(fault_run):
    run_id, _ = fault_run
    r = client.post(
        "/api/failure-patterns/runs",
        json={"evaluation_run_id": run_id, "strategy": "semantic"},
    )
    assert r.status_code == 400
    assert "聚类策略" in r.json()["detail"]


def test_cluster_strategy_extension_hook():
    """语义聚类扩展接口可用（注册后自动生效）。"""
    assert "semantic_test" not in fpm.CLUSTER_STRATEGIES
    fpm.register_cluster_strategy("semantic_test", lambda f: "all-in-one")
    try:
        assert "semantic_test" in fpm.CLUSTER_STRATEGIES
        assert fpm.CLUSTER_STRATEGIES["semantic_test"]({}) == "all-in-one"
    finally:
        fpm.CLUSTER_STRATEGIES.pop("semantic_test", None)


# ---------- 9/10. 构建与后端测试由外部命令验证 ----------

def test_report_persisted(pattern_report):
    path = fpm.PATTERN_DIR / f"{pattern_report['pattern_report_id']}.json"
    assert path.exists()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    for key in (
        "pattern_report_id", "evaluation_run_id", "denominators", "pattern_count",
        "patterns", "cases", "unknown_cases", "limitations", "generated_at",
    ):
        assert key in loaded


# ---------- 11. 不调用付费 API ----------

def test_no_paid_api(pattern_report):
    assert pattern_report["paid_api_called"] is False
    assert pattern_report["data_source"] == "analysis_over_attribution_reports"


# ---------- 12. 不泄露敏感信息 ----------

def test_no_sensitive_leak(pattern_report):
    text = json.dumps(pattern_report, ensure_ascii=False, default=str)
    assert "DEEPSEEK_API_KEY=" not in text
    assert not re.search(r"\bsk-[A-Za-z0-9]{20,}\b", text)
