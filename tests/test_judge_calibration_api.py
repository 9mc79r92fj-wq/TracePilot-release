"""Judge Calibration API 测试（pytest，第 39 步）。Mock 模式，不触发付费 API。"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402

client = TestClient(app)


def test_calibration_datasets_endpoint():
    r = client.get("/api/evaluation/calibration/datasets")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    ds = data["datasets"][0]
    assert ds["case_count"] >= 20
    assert ds["annotation_source"] == "synthetic_reference"
    assert ds["annotation_note"]
    assert ds["scored_cases"] + ds["unknown_cases"] == ds["case_count"]
    assert len(ds["categories"]) >= 8


def test_calibration_run_mock():
    r = client.post(
        "/api/evaluation/calibration/runs",
        json={"dataset_id": "calibration_set_v1", "judge_mode": "mock", "repeats": 2},
    )
    assert r.status_code == 200, r.text
    rep = r.json()["report"]

    # 数据与版本
    assert rep["dataset"]["calibration_id"] == "judge-calibration-v1"
    assert rep["dataset"]["annotation_source"] == "synthetic_reference"
    assert rep["dataset"]["evaluated_cases"] == rep["dataset"]["total_cases"]
    assert rep["judge"]["provider_kind"] == "mock"
    assert rep["judge"]["is_mock"] is True
    assert rep["judge"]["rubric_version"]
    assert rep["judge"]["prompt_version"]
    assert rep["calibration_engine_version"]

    # 指标
    m = rep["metrics"]
    assert m["evaluable_cases"] > 0
    assert 0.0 <= m["exact_agreement"] <= 1.0
    assert m["mae"] >= 0
    assert m["within_1_agreement"] >= m["exact_agreement"]
    assert set(m["confusion_matrix"].keys()) == {"1", "2", "3", "4", "5"}

    # 差异与稳定性
    assert len(rep["differences"]) == rep["dataset"]["evaluated_cases"]
    assert rep["stability"]["repeats"] == 2
    assert rep["stability"]["mean_within_case_std"] == 0.0
    assert len(rep["bias"]["overestimated"]) + len(rep["bias"]["underestimated"]) + len(
        rep["bias"]["aligned"]
    ) == m["evaluable_cases"]

    # UNKNOWN 与限制
    assert len(rep["unknown"]["reference_unknown_cases"]) >= 1
    assert rep["unknown"]["execution_errors"] == []
    assert rep["limitations"]
    assert "不得冒充真实模型校准结果" in rep["disclaimer"]
    assert rep["paid_api_called"] is False


def test_calibration_max_cases():
    r = client.post(
        "/api/evaluation/calibration/runs",
        json={"dataset_id": "calibration_set_v1", "judge_mode": "mock", "max_cases": 5, "repeats": 1},
    )
    assert r.status_code == 200
    assert r.json()["report"]["dataset"]["evaluated_cases"] == 5


def test_calibration_real_judge_forbidden(monkeypatch):
    """未授权时请求真实 Judge 必须 403，且绝不产生付费调用。"""
    monkeypatch.delenv("TRACEPILOT_JUDGE_ALLOW_REAL", raising=False)
    r = client.post(
        "/api/evaluation/calibration/runs",
        json={"dataset_id": "calibration_set_v1", "judge_mode": "deepseek"},
    )
    assert r.status_code == 403
    assert "真实 Judge 未启用" in r.json()["detail"]


def test_calibration_unknown_dataset_404():
    r = client.post(
        "/api/evaluation/calibration/runs",
        json={"dataset_id": "no_such_set", "judge_mode": "mock"},
    )
    assert r.status_code == 404


def test_calibration_path_traversal_rejected():
    r = client.post(
        "/api/evaluation/calibration/runs",
        json={"dataset_id": "../../etc/passwd", "judge_mode": "mock"},
    )
    assert r.status_code in (400, 404, 422)


@pytest.mark.parametrize(
    "payload",
    [
        {"dataset_id": "calibration_set_v1", "repeats": 0},
        {"dataset_id": "calibration_set_v1", "repeats": 9},
        {"dataset_id": "calibration_set_v1", "max_cases": 0},
        {"dataset_id": "calibration_set_v1", "max_cases": 99},
        {"dataset_id": "calibration_set_v1", "judge_mode": "x" * 40},
    ],
)
def test_calibration_invalid_params(payload):
    r = client.post("/api/evaluation/calibration/runs", json=payload)
    assert r.status_code == 422


def test_no_sensitive_leak():
    r = client.post(
        "/api/evaluation/calibration/runs",
        json={"dataset_id": "calibration_set_v1", "judge_mode": "mock", "max_cases": 3},
    )
    text = r.text
    assert "DEEPSEEK_API_KEY=" not in text
    assert "sk-proj-" not in text
