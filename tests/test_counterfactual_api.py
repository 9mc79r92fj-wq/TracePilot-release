"""Counterfactual Lab API 测试（pytest，第 32 步）。"""

import glob
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402

client = TestClient(app)
DATA_TRACES = PROJECT_ROOT / "data" / "traces"


def _fault_trace_id() -> str:
    from fixture_data import STEP21_FAULT_ID

    return STEP21_FAULT_ID


def _wait(experiment_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/counterfactual/experiments/{experiment_id}")
        assert r.status_code == 200
        if r.json()["status"] in {"succeeded", "failed"}:
            return r.json()
        time.sleep(0.05)
    pytest.fail(f"实验 {experiment_id} 超时未完成")


def test_create_and_query_experiment():
    tid = _fault_trace_id()
    r = client.post(
        "/api/counterfactual/experiments",
        json={"trace_id": tid, "intervention": {"type": "remove_fault"}, "replays": 2},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["experiment_id"].startswith("exp-")
    assert body["status"] in {"running", "succeeded"}
    assert body["source_trace_id"] == tid
    assert "links" in body

    final = _wait(body["experiment_id"])
    assert final["status"] == "succeeded"
    assert final["replays"] == 2


def test_results_endpoint():
    tid = _fault_trace_id()
    r = client.post(
        "/api/counterfactual/experiments",
        json={"trace_id": tid, "intervention": {"type": "remove_fault"}, "replays": 2},
    )
    eid = r.json()["experiment_id"]
    _wait(eid)

    res = client.get(f"/api/counterfactual/experiments/{eid}/results")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "succeeded"
    assert data["observed_outcome"]["executed"] is False
    assert data["intervention_outcome"]["simulated"] is True
    assert "tool_timeout" in data["observed_difference"]["fault_types_removed"]
    assert data["causal_evidence_level"] in {"none", "weak", "moderate"}
    assert data["causal_evidence_level"] != "strong"
    assert data["original_trace_modified"] is False
    assert data["environment"]["model"]["simulated"] is True
    assert data["control_conditions"] and data["confounders"] and data["limitations"]
    assert data["links"]["source_trace"].endswith(tid)


def test_original_trace_not_overwritten():
    tid = _fault_trace_id()
    before = client.get(f"/api/traces/{tid}").json()["trace"]
    r = client.post(
        "/api/counterfactual/experiments",
        json={"trace_id": tid, "intervention": {"type": "remove_fault"}},
    )
    _wait(r.json()["experiment_id"])
    after = client.get(f"/api/traces/{tid}").json()["trace"]
    assert before == after


def test_result_matches_backend_report():
    """API 结果与引擎直接调用结果的关键字段一致（不伪造）。"""
    from backend.app.services.counterfactual_engine import run_counterfactual_experiment
    from backend.app.services.trace_store import load_trace

    tid = _fault_trace_id()
    direct = run_counterfactual_experiment(
        load_trace(tid), {"type": "remove_fault"}, replays=2
    )
    r = client.post(
        "/api/counterfactual/experiments",
        json={"trace_id": tid, "intervention": {"type": "remove_fault"}, "replays": 2},
    )
    eid = r.json()["experiment_id"]
    _wait(eid)
    api = client.get(f"/api/counterfactual/experiments/{eid}/results").json()

    assert api["observed_outcome"]["fault_types"] == direct["observed_outcome"]["fault_types"]
    assert api["intervention_outcome"]["status"] == direct["intervention_outcome"]["status"]
    assert (
        api["observed_difference"]["fault_types_removed"]
        == direct["observed_difference"]["fault_types_removed"]
    )


@pytest.mark.parametrize(
    "bad_intervention",
    [
        {"type": "run_shell"},
        {"type": "exec_code"},
        {"type": "replace_tool_result", "tool": "subprocess", "value": "x"},
        {"type": "adjust_timeout_retry", "max_retries": 999},
    ],
)
def test_unsupported_intervention_rejected(bad_intervention):
    tid = _fault_trace_id()
    r = client.post(
        "/api/counterfactual/experiments",
        json={"trace_id": tid, "intervention": bad_intervention},
    )
    assert r.status_code == 400


def test_invalid_trace_and_missing_experiment():
    r = client.post(
        "/api/counterfactual/experiments",
        json={"trace_id": "trace-00000000000000000000000000000000", "intervention": {"type": "remove_fault"}},
    )
    assert r.status_code == 404

    r2 = client.get("/api/counterfactual/experiments/exp-doesnotexist")
    assert r2.status_code == 404

    r3 = client.get("/api/counterfactual/experiments/exp-doesnotexist/results")
    assert r3.status_code == 404


def test_invalid_payload_rejected():
    tid = _fault_trace_id()
    # 缺少 intervention
    assert client.post("/api/counterfactual/experiments", json={"trace_id": tid}).status_code == 422
    # replays 越界
    assert (
        client.post(
            "/api/counterfactual/experiments",
            json={"trace_id": tid, "intervention": {"type": "remove_fault"}, "replays": 99},
        ).status_code
        == 422
    )


def test_deduplication():
    tid = _fault_trace_id()
    payload = {"trace_id": tid, "intervention": {"type": "remove_fault"}, "replays": 1}
    a = client.post("/api/counterfactual/experiments", json=payload).json()
    _wait(a["experiment_id"])
    b = client.post("/api/counterfactual/experiments", json=payload).json()
    assert b["experiment_id"] == a["experiment_id"], "窗口内相同配置应复用同一实验"


def test_no_sensitive_leak():
    tid = _fault_trace_id()
    r = client.post(
        "/api/counterfactual/experiments",
        json={"trace_id": tid, "intervention": {"type": "remove_fault"}},
    )
    eid = r.json()["experiment_id"]
    _wait(eid)
    text = client.get(f"/api/counterfactual/experiments/{eid}/results").text
    assert "DEEPSEEK_API_KEY" not in text
    assert "sk-proj-" not in text


def test_list_experiments():
    tid = _fault_trace_id()
    client.post(
        "/api/counterfactual/experiments",
        json={"trace_id": tid, "intervention": {"type": "adjust_timeout_retry", "max_retries": 2}},
    )
    r = client.get("/api/counterfactual/experiments")
    assert r.status_code == 200
    assert r.json()["count"] >= 1
