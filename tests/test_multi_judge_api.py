"""Multi-Judge API 测试（pytest，第 40 步）。全部 Mock，不触发付费 API。"""

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


def _wait(run_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/evaluation/multi-judge/runs/{run_id}")
        assert r.status_code == 200
        if r.json()["status"] in {"succeeded", "failed"}:
            return r.json()
        time.sleep(0.1)
    pytest.fail("多 Judge 评测超时")


def _create(**payload) -> dict:
    body = {"dataset_id": "tracepilot-eval-v1", "config_id": "mock-mixed-variants",
            "max_cases": 3, "strategy": "median", **payload}
    r = client.post("/api/evaluation/multi-judge/runs", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_configs_endpoint():
    r = client.get("/api/evaluation/multi-judge/configs")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 3
    ids = {c["config_id"] for c in data["configs"]}
    assert {"mock-mixed-variants", "mock-resampling", "mock-dual"} <= ids
    for c in data["configs"]:
        assert c["judge_count"] >= 2
        assert c["limitation"], "配置组必须声明限制"
        assert c["expected_relationship"]


def test_create_and_status():
    created = _create()
    assert created["run_id"].startswith("mj-")
    assert created["judge_count"] == 3
    assert created["case_count"] == 3
    assert created["strategy"] == "median"
    assert "links" in created

    final = _wait(created["run_id"])
    assert final["status"] == "succeeded"
    assert final["rubric"]["rubric_id"] == "answer_quality"
    assert len(final["judges"]) == 3
    assert final["independence"]["level"] == "mock_prompt_variants"
    assert final["independence"]["not_independent"] is True
    assert final["case_count"] == 3
    assert final["summary"]["conclusive_count"] + final["summary"]["inconclusive_count"] == 3
    assert final["paid_api_called"] is False
    assert final["engine_version"]
    assert final["limitations"]


def test_scores_endpoint():
    run_id = _create()["run_id"]
    _wait(run_id)
    r = client.get(f"/api/evaluation/multi-judge/runs/{run_id}/scores")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "succeeded"
    assert len(data["judges"]) == 3
    assert len(data["cases"]) == 3
    for c in data["cases"]:
        assert len(c["judges"]) == 3
        for jid, v in c["judges"].items():
            assert v["status"] in {"SCORED", "UNKNOWN", "ERROR"}
            assert "evidence" in v
            assert "reason" in v
            assert "model_name" in v
            assert "prompt_version" in v


def test_disagreement_endpoint():
    run_id = _create()["run_id"]
    _wait(run_id)
    r = client.get(f"/api/evaluation/multi-judge/runs/{run_id}/disagreement")
    assert r.status_code == 200
    d = r.json()["disagreement"]
    assert d["threshold"] >= 0
    assert d["severe_threshold"] >= d["threshold"]
    assert d["case_count"] == 3
    for row in d["cases"]:
        assert "max_diff" in row
        assert "stdev" in row
        assert "variance" in row
        assert "distribution" in row
        assert "agreement" in row
        assert "disagreement_flagged" in row
        assert "evidence_conflicts" in row
        assert "reason_summary" in row
        assert "human_review_required" in row
    assert d["aggregate"]["denominator_definition"]


def test_arbitration_endpoint():
    run_id = _create()["run_id"]
    _wait(run_id)
    r = client.get(f"/api/evaluation/multi-judge/runs/{run_id}/arbitration")
    assert r.status_code == 200
    a = r.json()["arbitration"]
    assert a["strategy"] == "median"
    s = a["summary"]
    assert s["conclusive_count"] + s["inconclusive_count"] == 3
    assert s["denominator_definition"]
    for row in a["cases"]:
        assert row["status"] in {"SCORED", "INCONCLUSIVE"}
        if row["status"] == "INCONCLUSIVE":
            assert row["aggregated_score"] is None
        else:
            assert 1 <= row["aggregated_score"] <= 5


def test_resampling_config_correlated():
    created = _create(config_id="mock-resampling")
    final = _wait(created["run_id"])
    assert final["independence"]["level"] == "correlated"
    assert final["independence"]["not_independent"] is True


def test_mean_strategy():
    created = _create(strategy="mean")
    final = _wait(created["run_id"])
    assert final["summary"]["strategy"] == "mean"


def test_calibration_weighted_without_calibration_fails():
    """加权聚合缺少校准依据时不得编造权重 → 运行失败并报错。"""
    created = _create(strategy="calibration_weighted")
    final = _wait(created["run_id"])
    assert final["status"] == "failed"
    assert "MissingCalibrationError" in (final.get("error") or "")


def test_real_judge_forbidden(monkeypatch):
    monkeypatch.delenv("TRACEPILOT_JUDGE_ALLOW_REAL", raising=False)
    r = client.post(
        "/api/evaluation/multi-judge/runs",
        json={
            "dataset_id": "tracepilot-eval-v1",
            "config_id": None,
            "judges": [{"judge_id": "real", "mode": "deepseek"}],
            "max_cases": 1,
        },
    )
    assert r.status_code == 403
    assert "真实 Judge 未启用" in r.json()["detail"]


def test_unknown_config_404():
    r = client.post(
        "/api/evaluation/multi-judge/runs",
        json={"dataset_id": "tracepilot-eval-v1", "config_id": "no-such-config"},
    )
    assert r.status_code == 404


def test_unknown_dataset_404():
    r = client.post(
        "/api/evaluation/multi-judge/runs",
        json={"dataset_id": "evil", "config_id": "mock-dual"},
    )
    assert r.status_code == 404


def test_unknown_run_404():
    assert client.get("/api/evaluation/multi-judge/runs/mj-doesnotexist").status_code == 404
    assert client.get("/api/evaluation/multi-judge/runs/mj-doesnotexist/scores").status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"max_cases": 0},
        {"max_cases": 99},
        {"threshold": -1},
        {"severe_threshold": 9},
        {"config_id": None, "judges": []},
        {"judges": [{"judge_id": f"j{i}"} for i in range(6)]},
    ],
)
def test_invalid_payload(payload):
    r = client.post(
        "/api/evaluation/multi-judge/runs",
        json={"dataset_id": "tracepilot-eval-v1", **payload},
    )
    assert r.status_code in (400, 422)


def test_runs_list():
    r = client.get("/api/evaluation/multi-judge/runs")
    assert r.status_code == 200
    assert "runs" in r.json()


def test_no_sensitive_leak():
    created = _create(max_cases=2)
    text = client.get(f"/api/evaluation/multi-judge/runs/{created['run_id']}").text
    assert "DEEPSEEK_API_KEY=" not in text
    assert "sk-proj-" not in text
