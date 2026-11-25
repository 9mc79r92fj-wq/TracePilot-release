"""第 55 步：根因准确率指标（Top-1/Top-3）测试。

使用真实评测运行（受控故障数据集 `tracepilot-fault-v1`）对比数据集标注金标准与
Bad Case Attribution 输出，验证指标口径诚实：金标准缺失不计入分母、UNKNOWN 不当作
命中、修复成功率未验证时为 null、不调用付费 API。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.services import rootcause_metrics as rm  # noqa: E402
from backend.app.services.evaluation_store import get_evaluation_store  # noqa: E402

client = TestClient(app)


def _wait_run(run_id: str, timeout: float = 180.0) -> dict:
    store = get_evaluation_store()
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = store.get(run_id)
        if rec.get("status") in {"succeeded", "failed"}:
            return rec
        time.sleep(0.2)
    raise AssertionError(f"评测运行未完成: {run_id}")


@pytest.fixture(scope="module")
def fault_run() -> dict:
    store = get_evaluation_store()
    run = store.submit("tracepilot-fault-v1", 2, judge_mode="none", dedup=False)
    rec = _wait_run(run["run_id"])
    assert rec["status"] == "succeeded", rec.get("error")
    return rec


def test_metrics_computation(fault_run):
    report = rm.compute_metrics(fault_run["run_id"], max_cases=2)
    assert report["metrics_id"].startswith("rcm-")
    assert report["paid_api_called"] is False
    assert report["evaluated_cases"] == 2
    assert report["attributed_cases"] == 2
    metrics = report["metrics"]
    assert metrics["labeled_cases"] == 2
    # 指标要么是 0..1 的比率，要么在无分母时为 None（绝不伪造数字）
    for key in (
        "type_top1_accuracy",
        "type_top3_coverage",
        "misattribution_rate",
        "unknown_first_rate",
    ):
        value = metrics[key]
        assert value is None or 0.0 <= value <= 1.0
    assert metrics["type_top1_accuracy"] is not None
    assert metrics["type_top1_accuracy"] >= 0.5  # 受控故障集应能命中大部分标注
    assert metrics["average_diagnosis_ms"] is not None
    assert metrics["cost_status"] == "SIMULATED"
    assert metrics["average_cost"] == 0.0  # 模拟执行器无真实计费
    assert metrics["repair_success_rate"] is None  # 未开启修复验证
    assert metrics["repair_note"]
    assert metrics["baselines"]["majority_cause_type"]
    # 案例级证据可追溯
    for case in report["cases"]:
        assert case["gold_cause_type"]
        assert case["first_candidate_type"]
        assert case["attribution_id"].startswith("attr-")
    assert Path(PROJECT_ROOT / report["report_path"]).exists()
    # 查询持久化报告
    fetched = rm.get_metrics(report["metrics_id"])
    assert fetched["metrics_id"] == report["metrics_id"]
    listing = rm.list_metrics(limit=5)
    assert any(row["metrics_id"] == report["metrics_id"] for row in listing["reports"])


def test_metrics_include_repair_path(fault_run, monkeypatch):
    monkeypatch.setattr(rm, "_repair_success", lambda trace: (True, None, 5))
    report = rm.compute_metrics(fault_run["run_id"], max_cases=1, include_repair=True)
    metrics = report["metrics"]
    assert report["include_repair"] is True
    assert metrics["repair_attempted_cases"] == 1
    assert metrics["repair_success_rate"] == 1.0
    assert metrics["repair_note"] is None


def test_metrics_rejects_missing_and_invalid():
    with pytest.raises(rm.RootCauseMetricsError):
        rm.compute_metrics("eval-does-not-exist")
    with pytest.raises(rm.RootCauseMetricsError):
        rm.compute_metrics("x", max_cases=0)
    with pytest.raises(rm.RootCauseMetricsNotFoundError):
        rm.get_metrics("rcm-missing")
    with pytest.raises(rm.RootCauseMetricsNotFoundError):
        rm.get_metrics("bad-id")


def test_metrics_api_endpoints(fault_run):
    created = client.post(
        "/api/attribution/metrics",
        json={"evaluation_run_id": fault_run["run_id"], "max_cases": 1},
    )
    assert created.status_code == 200, created.text
    report = created.json()
    metrics_id = report["metrics_id"]
    assert report["metrics"]["labeled_cases"] == 1
    assert created.json()["paid_api_called"] is False

    fetched = client.get(f"/api/attribution/metrics/{metrics_id}")
    assert fetched.status_code == 200
    assert fetched.json()["metrics_id"] == metrics_id

    listing = client.get("/api/attribution/metrics")
    assert listing.status_code == 200
    assert listing.json()["count"] >= 1

    assert client.post("/api/attribution/metrics", json={}).status_code == 400
    assert client.get("/api/attribution/metrics/rcm-missing").status_code == 404
    bad_run = client.post(
        "/api/attribution/metrics", json={"evaluation_run_id": "eval-missing"}
    )
    assert bad_run.status_code == 400
