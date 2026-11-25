"""Minimal Repair API 测试（第 33 步）。"""

import glob
import sys
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
    from backend.app.schemas.trace import TraceRecord

    for p in sorted(glob.glob(str(DATA_TRACES / "*.json"))):
        r = TraceRecord.model_validate_json(Path(p).read_text(encoding="utf-8"))
        if any(
            isinstance(s.metadata.get("fault_injection"), dict)
            and s.metadata["fault_injection"].get("injected") is True
            for s in r.steps
        ):
            return r.trace_id
    pytest.skip("缺少故障 Trace 数据")


def test_repair_types_endpoint():
    r = client.get("/api/repair/types")
    assert r.status_code == 200
    assert r.json()["repair_types"] == [
        "INCREASE_TIMEOUT",
        "LIMITED_RETRY",
        "FALLBACK_RESULT",
        "ARGUMENT_CORRECTION",
        "SKIP_OPTIONAL_STEP",
        "SAFE_TOOL_ALTERNATIVE",
    ]


def test_repair_report_created():
    tid = _fault_trace_id()
    r = client.post("/api/repair/report", json={"trace_id": tid})
    assert r.status_code == 200
    rep = r.json()["report"]
    assert rep["source_trace_id"] == tid
    assert rep["fault_type"] is not None
    assert rep["original_trace_modified"] is False
    assert rep["candidate_count"] >= 1
    assert len(rep["candidates"]) == rep["candidate_count"]
    for c in rep["candidates"]:
        assert c["repair_id"].startswith("repair-")
        assert c["verification_status"] in {"verified", "failed", "not_tested"}
        assert c["score"] is not None
        assert c["score_breakdown"]["disclaimer"]


def test_repair_verified_candidate_has_trace():
    tid = _fault_trace_id()
    rep = client.post("/api/repair/report", json={"trace_id": tid}).json()["report"]
    verified = [c for c in rep["candidates"] if c["verification_status"] == "verified"]
    for c in verified:
        assert c["repair_trace_id"]
        assert c["repair_trace_id"] != tid
        assert c["experiment_id"]


def test_original_trace_unchanged():
    tid = _fault_trace_id()
    before = client.get(f"/api/traces/{tid}").json()["trace"]
    client.post("/api/repair/report", json={"trace_id": tid})
    after = client.get(f"/api/traces/{tid}").json()["trace"]
    assert before == after


def test_verify_false_all_not_tested():
    tid = _fault_trace_id()
    rep = client.post(
        "/api/repair/report", json={"trace_id": tid, "verify": False}
    ).json()["report"]
    assert all(c["verification_status"] == "not_tested" for c in rep["candidates"])
    assert rep["recommended"] is None


def test_invalid_trace_rejected():
    r = client.post(
        "/api/repair/report",
        json={"trace_id": "trace-00000000000000000000000000000000"},
    )
    assert r.status_code == 404


@pytest.mark.parametrize("max_candidates", [0, 6, 99])
def test_invalid_max_candidates(max_candidates):
    tid = _fault_trace_id()
    r = client.post(
        "/api/repair/report", json={"trace_id": tid, "max_candidates": max_candidates}
    )
    assert r.status_code == 422


def test_no_sensitive_leak():
    tid = _fault_trace_id()
    text = client.post("/api/repair/report", json={"trace_id": tid}).text
    assert "DEEPSEEK_API_KEY" not in text
    assert "sk-proj-" not in text


def test_reproducible_api():
    tid = _fault_trace_id()
    a = client.post("/api/repair/report", json={"trace_id": tid}).json()["report"]
    b = client.post("/api/repair/report", json={"trace_id": tid}).json()["report"]
    sa = [(c["repair_type"], c["verification_status"]) for c in a["candidates"]]
    sb = [(c["repair_type"], c["verification_status"]) for c in b["candidates"]]
    assert sa == sb
