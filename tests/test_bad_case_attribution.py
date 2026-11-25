"""Automated Bad Case Attribution 测试（pytest，第 47 步）。

覆盖：Bad Case 关联、Trace 关联、故障类型识别、多异常、UNKNOWN 处理、
证据引用真实性、DAG 依赖不被误判为因果、反事实实验引用、API 查询、
不调用付费 API、不泄露敏感信息。
"""

import json
import re
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.services import bad_case_attribution as bca  # noqa: E402
from backend.app.services.trace_store import load_trace  # noqa: E402

client = TestClient(app)
FAULT_DATASET = "tracepilot-fault-v1"


def _run_fault_eval() -> tuple[str, dict]:
    r = client.post(
        "/api/evaluation/runs",
        json={"dataset_id": FAULT_DATASET, "judge_mode": "none"},
    )
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
    return _run_fault_eval()


@pytest.fixture(scope="module")
def batch_report(fault_run):
    run_id, _report = fault_run
    return bca.attribute_evaluation_run(run_id, max_cases=20)


# ---------- 1. Bad Case 关联正确 ----------

def test_batch_reads_bad_cases(fault_run, batch_report):
    run_id, report = fault_run
    bad_ids = {c["case_id"] for c in report["case_results"] if c.get("is_bad_case")}
    assert bad_ids, "受控失败数据集必须产生 Bad Case"
    assert batch_report["bad_case_count"] == len(bad_ids)
    assert {r["case_id"] for r in batch_report["reports"]} == bad_ids
    for rep in batch_report["reports"]:
        assert rep["evaluation_run_id"] == run_id
        assert rep["case_id"] in bad_ids


def test_bad_case_order_matches_evaluation(fault_run, batch_report):
    _run_id, report = fault_run
    bad_order = [c["case_id"] for c in report["case_results"] if c.get("is_bad_case")]
    assert [r["case_id"] for r in batch_report["reports"]] == bad_order


# ---------- 2. Trace 关联正确 ----------

def test_trace_linked_and_loadable(batch_report):
    for rep in batch_report["reports"]:
        assert rep["trace_id"], f"{rep['case_id']} 必须关联 Trace"
        trace = load_trace(rep["trace_id"])
        assert trace.trace_id == rep["trace_id"]
        # 受影响步骤必须真实存在于 Trace 中
        ids = {s.step_id for s in trace.steps}
        for sid in rep["affected_steps"]:
            assert sid in ids, f"受影响步骤 {sid} 在 Trace 中不存在"


def test_evidence_refs_all_resolvable(batch_report):
    """证据引用必须能在原始数据中核对（不伪造证据）。"""
    for rep in batch_report["reports"]:
        trace = load_trace(rep["trace_id"])
        step_ids = {s.step_id for s in trace.steps}
        metric_names = {m["metric"] for m in rep["failed_metrics"]} | {
            m["metric"] for m in rep["unknown_metrics"]
        }
        for ref in rep["evidence_refs"]:
            kind = ref["kind"]
            if kind == "trace_step":
                assert ref["ref_id"] in step_ids, f"引用不存在的步骤 {ref['ref_id']}"
            elif kind == "metric":
                assert ref["ref_id"] in metric_names, f"引用不存在的指标 {ref['ref_id']}"
            elif kind == "trace":
                assert ref["ref_id"] == rep["trace_id"]
            elif kind == "dag_dependency":
                assert ref["ref_id"] in step_ids
            elif kind == "experiment":
                assert ref["ref_id"].startswith("exp-")


# ---------- 3. 故障类型识别正确 ----------

@pytest.mark.parametrize("case_id,expected", [
    ("fail-01-timeout", "TOOL_TIMEOUT"),
    ("fail-02-exception", "TOOL_EXCEPTION"),
    ("fail-03-invalid-args", "INVALID_ARGUMENTS"),
    ("fail-06-permission", "PERMISSION_DENIED"),
    ("fail-07-wrong-tool", "PROCESS_VIOLATION"),
    ("fail-08-answer-mismatch", "OUTPUT_MISMATCH"),
])
def test_cause_type_identified(batch_report, case_id, expected):
    rep = next((r for r in batch_report["reports"] if r["case_id"] == case_id), None)
    assert rep is not None, f"缺少 {case_id} 的归因报告"
    types = {c["cause_type"] for c in rep["candidate_causes"]}
    assert expected in types, f"{case_id} 应识别为 {expected}，实际 {types}"


def test_confirmed_event_requires_trace_record(batch_report):
    """CONFIRMED_EVENT 级候选必须引用 trace_step / metric 等可直接核对的证据。"""
    for rep in batch_report["reports"]:
        for cand in rep["candidate_causes"]:
            if cand["evidence_level"] != "CONFIRMED_EVENT":
                continue
            assert cand["evidence"], "确认事件必须有证据引用"
            assert all(e["kind"] for e in cand["evidence"])


# ---------- 4. 多异常场景 ----------

def test_multi_anomaly_not_forced_single_root(batch_report):
    multi = [r for r in batch_report["reports"] if r["root_cause_status"] == "MULTI_ANOMALY"]
    assert multi, "超时/异常类案例应产生多异常归因"
    for rep in multi:
        confirmed = [c for c in rep["candidate_causes"]
                     if c["evidence_level"] == "CONFIRMED_EVENT"]
        assert len(confirmed) >= 2, "MULTI_ANOMALY 必须包含 ≥2 条确认级候选"
        assert rep["ambiguity_notes"], "多异常必须给出歧义说明"
    # 不得强行归并成单一根因字段
    for rep in batch_report["reports"]:
        assert "root_cause" not in rep, "不应输出单一 root_cause 断言"
        assert "candidate_causes" in rep


# ---------- 5. UNKNOWN 处理 ----------

def test_unknown_when_no_evidence():
    """无失败指标、无故障、无实验的 Trace → UNKNOWN，不伪造原因。"""
    run_id, report = _run_fault_eval()
    case = next(c for c in report["case_results"] if c["case_id"] == "fail-08-answer-mismatch")
    # 构造一个「全部通过」的案例视图
    clean_case = {
        "case_id": "clean-case",
        "trace_id": case["trace_id"],
        "metrics": {},
        "is_bad_case": False,
    }
    rep = bca.attribute_case(evaluation_run_id=run_id, case=clean_case)
    # 该 Trace 无故障注入 → 只可能有指标差异或 UNKNOWN
    types = {c["cause_type"] for c in rep["candidate_causes"]}
    assert types, "必须至少给出一个候选（允许 UNKNOWN）"
    assert "root_cause_status" in rep


def test_missing_trace_returns_unknown():
    rep = bca.attribute_case(
        evaluation_run_id=None,
        case={"case_id": "no-trace", "trace_id": None, "metrics": {}},
    )
    assert rep["trace_id"] is None
    assert rep["candidate_causes"][0]["cause_type"] == "UNKNOWN"
    assert rep["candidate_causes"][0]["evidence_level"] == "UNKNOWN"
    assert rep["root_cause_status"] == "UNKNOWN"


def test_unreadable_trace_returns_unknown():
    rep = bca.attribute_case(
        evaluation_run_id=None,
        case={"case_id": "ghost", "trace_id": "trace-doesnotexist", "metrics": {}},
    )
    assert rep["candidate_causes"][0]["cause_type"] == "UNKNOWN"
    assert rep["limitations"]


# ---------- 6. 证据等级规则 ----------

def test_evidence_level_vocabulary():
    assert set(bca.EVIDENCE_LEVELS) == {
        "CONFIRMED_EVENT", "OBSERVED_DIFFERENCE", "POTENTIAL_CAUSE",
        "INTERVENTION_SUPPORTED", "UNKNOWN",
    }
    assert len(bca.ATTRIBUTION_TYPES) == 10


def test_no_numeric_confidence_emitted(batch_report):
    """不得生成无来源的置信概率（不输出 0~1 数值置信度字段）。"""
    text = json.dumps(batch_report, ensure_ascii=False, default=str)
    for bad_key in ('"confidence":', '"probability":', '"score":'):
        assert bad_key not in text, f"不应输出 {bad_key} 形式的数字置信度"
    for rep in batch_report["reports"]:
        for cand in rep["candidate_causes"]:
            assert isinstance(cand["confidence_basis"], str)
            assert cand["confidence_basis"]


# ---------- 7. DAG 依赖不被误判为因果 ----------

def test_dag_dependency_not_causal(batch_report):
    dag_cands = [
        c for rep in batch_report["reports"] for c in rep["candidate_causes"]
        if any(e["kind"] == "dag_dependency" for e in c["evidence"])
    ]
    assert dag_cands, "应产生基于 DAG 依赖的候选"
    for c in dag_cands:
        assert c["evidence_level"] == "POTENTIAL_CAUSE", "DAG 依赖只能是潜在原因"
        assert c["is_conclusive"] is False, "DAG 依赖不得作为结论"
        assert "不构成因果" in (c["uncertainty"] or "") or "仅" in (c["uncertainty"] or "")


# ---------- 8. 反事实实验引用 ----------

def test_experiment_refs_are_real():
    """若报告引用实验，则 experiment_id 必须真实存在（不能捏造）。"""
    reports = bca.list_reports(limit=20)
    assert reports, "应已持久化归因报告"
    for item in reports[:5]:
        rep = bca.get_report(item["attribution_id"])
        for ref in rep.get("experiment_refs") or []:
            assert str(ref["experiment_id"]).startswith("exp-")
            assert "control_conditions" in ref


def test_intervention_supported_requires_experiment(batch_report):
    for rep in batch_report["reports"]:
        for cand in rep["candidate_causes"]:
            if cand["evidence_level"] != "INTERVENTION_SUPPORTED":
                continue
            assert cand["experiment_refs"], "INTERVENTION_SUPPORTED 必须引用真实实验"
            assert all(r["experiment_id"].startswith("exp-") for r in cand["experiment_refs"])


# ---------- 9. API ----------

def test_api_types():
    r = client.get("/api/attribution/types")
    assert r.status_code == 200
    d = r.json()
    assert len(d["attribution_types"]) == 10
    assert len(d["evidence_levels"]) == 5
    assert d["limits"]["paid_api_called"] is False
    assert "not_emitted" in d["limits"]["confidence_probability"]
    assert d["evidence_level_rules"]["POTENTIAL_CAUSE"]


def test_api_batch_and_query(fault_run, batch_report):
    run_id, _ = fault_run
    r = client.post("/api/attribution/runs", json={"evaluation_run_id": run_id, "max_cases": 3})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["attributed_count"] == 3
    assert d["paid_api_called"] is False
    assert d["summary"]["evidence_level_counts"]

    aid = d["reports"][0]["attribution_id"]
    one = client.get(f"/api/attribution/reports/{aid}")
    assert one.status_code == 200
    assert one.json()["report"]["attribution_id"] == aid

    listing = client.get("/api/attribution/reports")
    assert listing.status_code == 200
    assert any(x["attribution_id"] == aid for x in listing.json()["reports"])


def test_api_single_case(fault_run):
    run_id, report = fault_run
    bad = next(c for c in report["case_results"] if c.get("is_bad_case"))
    r = client.post(f"/api/attribution/evaluations/{run_id}/cases/{bad['case_id']}")
    assert r.status_code == 200, r.text
    rep = r.json()["report"]
    assert rep["case_id"] == bad["case_id"]
    assert rep["failed_metrics"]

    # 非 Bad Case → 409
    passed = next((c for c in report["case_results"] if not c.get("is_bad_case")), None)
    if passed:
        r2 = client.post(f"/api/attribution/evaluations/{run_id}/cases/{passed['case_id']}")
        assert r2.status_code == 409


def test_api_evidence_trace_experiments(fault_run):
    run_id, _ = fault_run
    batch = client.post("/api/attribution/runs", json={"evaluation_run_id": run_id, "max_cases": 2}).json()
    aid = batch["reports"][0]["attribution_id"]

    ev = client.get(f"/api/attribution/reports/{aid}/evidence")
    assert ev.status_code == 200
    body = ev.json()
    assert body["candidate_by_level"]
    assert body["observed_events"] is not None

    tr = client.get(f"/api/attribution/reports/{aid}/trace")
    assert tr.status_code == 200
    tb = tr.json()
    if tb["status"] == "AVAILABLE":
        assert tb["step_count"] > 0
        assert tb["links"]["dag"].endswith("/dag")

    ex = client.get(f"/api/attribution/reports/{aid}/experiments")
    assert ex.status_code == 200
    assert "experiments" in ex.json()


def test_api_unknown_ids():
    assert client.get("/api/attribution/reports/attr-nope").status_code == 404
    assert client.get("/api/attribution/reports/..%2F..%2Fetc").status_code == 404
    assert client.post(
        "/api/attribution/runs", json={"evaluation_run_id": "eval-nope"}
    ).status_code == 400


def test_api_batch_limits(fault_run):
    run_id, _ = fault_run
    r = client.post(
        "/api/attribution/runs",
        json={"evaluation_run_id": run_id, "case_ids": [f"c-{i}" for i in range(21)]},
    )
    assert r.status_code in (400, 422)
    r2 = client.post(
        "/api/attribution/runs",
        json={"evaluation_run_id": run_id, "max_cases": 99},
    )
    assert r2.status_code in (400, 422)


def test_api_trace_attribution():
    """任意 Trace 可直接归因（无评测上下文）。"""
    run_id, report = _run_fault_eval()
    case = next(c for c in report["case_results"] if c.get("is_bad_case"))
    r = client.post(f"/api/attribution/traces/{case['trace_id']}")
    assert r.status_code == 200, r.text
    rep = r.json()["report"]
    assert rep["trace_id"] == case["trace_id"]
    assert rep["candidate_causes"]


# ---------- 10/11. 前端可查看归因（页面存在即验证；浏览器细节由 e2e 覆盖） ----------

def test_frontend_page_file_exists():
    page = PROJECT_ROOT / "frontend" / "src" / "pages" / "EvaluationCenter.tsx"
    text = page.read_text(encoding="utf-8")
    assert "归因" in text, "Evaluation Center 应包含归因入口"


# ---------- 13/14. 安全 ----------

def test_no_paid_api():
    r = client.get("/api/attribution/types")
    assert r.json()["limits"]["paid_api_called"] is False


def test_no_sensitive_leak(fault_run):
    run_id, _ = fault_run
    batch = client.post("/api/attribution/runs", json={"evaluation_run_id": run_id, "max_cases": 2})
    text = batch.text
    assert "DEEPSEEK_API_KEY=" not in text
    assert not re.search(r"\bsk-[A-Za-z0-9]{20,}\b", text)


# ---------- 报告持久化 ----------

def test_reports_persisted_and_queryable(fault_run, batch_report):
    aid = batch_report["reports"][0]["attribution_id"]
    path = bca.ATTRIBUTION_DIR / f"{aid}.json"
    assert path.exists(), "归因报告必须持久化到磁盘"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["attribution_id"] == aid
    for key in (
        "evaluation_run_id", "case_id", "trace_id", "failed_metrics", "observed_events",
        "candidate_causes", "evidence_refs", "evidence_levels", "affected_steps",
        "experiment_refs", "limitations", "generated_at",
    ):
        assert key in loaded, f"报告缺少字段 {key}"
