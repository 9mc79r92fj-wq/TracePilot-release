"""Evaluation API 测试（第 35 步引擎 API，已适配第 36 步异步运行契约）。"""

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


def _run(max_cases: int | None = None) -> dict:
    """提交评测运行并轮询至完成（第 36 步起为异步契约）。"""
    payload: dict = {"dataset_id": "tracepilot-eval-v1"}
    if max_cases is not None:
        payload["max_cases"] = max_cases
    created = client.post("/api/evaluation/runs", json=payload)
    assert created.status_code == 200
    run_id = created.json()["run_id"]

    deadline = time.time() + 120
    while time.time() < deadline:
        r = client.get(f"/api/evaluation/runs/{run_id}")
        assert r.status_code == 200
        if r.json()["status"] in {"succeeded", "failed"}:
            body = r.json()
            assert body["status"] == "succeeded", body.get("error")
            return body["report"]
        time.sleep(0.1)
    pytest.fail("评测运行超时未完成")


def test_list_datasets():
    r = client.get("/api/evaluation/datasets")
    assert r.status_code == 200
    ds = r.json()["datasets"]
    by_id = {d["dataset_id"]: d for d in ds}
    # 列表来自白名单（第 47 步起不再硬编码单个数据集）
    assert "tracepilot-eval-v1" in by_id
    assert "tracepilot-fault-v1" in by_id
    assert len(ds) == len(by_id) >= 2

    main = by_id["tracepilot-eval-v1"]
    assert main["case_count"] >= 12
    assert main["data_source"] == "synthetic"

    # 受控失败数据集：明确标记 synthetic，且案例数与其定义一致
    fault = by_id["tracepilot-fault-v1"]
    assert fault["case_count"] >= 6
    assert fault["data_source"] == "synthetic"
    assert "synthetic" in (fault["data_source_note"] or "")


def test_run_full_evaluation():
    rep = _run()
    assert rep["evaluation_run_id"].startswith("eval-")
    assert rep["total_cases"] >= 12
    assert rep["executed_cases"] == rep["total_cases"]
    assert rep["passed_cases"] + rep["failed_cases"] + rep["unknown_cases"] + rep["not_applicable_cases"] == rep["executed_cases"]
    assert rep["execution_error_cases"] == 0
    assert len(rep["case_results"]) == rep["executed_cases"]
    assert rep["dataset_data_source"] == "synthetic"
    assert rep["environment"]["model"]["simulated"] is True
    assert rep["environment"]["model"]["paid_api_called"] is False
    assert rep["limitations"]


def test_run_limited_cases():
    rep = _run(3)
    assert rep["total_cases"] == 3
    assert rep["executed_cases"] == 3


@pytest.mark.parametrize("bad", [0, 51, 999])
def test_invalid_max_cases(bad):
    r = client.post("/api/evaluation/runs", json={"max_cases": bad, "dataset_id": "tracepilot-eval-v1"})
    assert r.status_code == 422


def test_metrics_have_denominators():
    rep = _run(6)
    m = rep["metrics"]["task_success_rate"]
    assert m["denominator"] == rep["executed_cases"]
    assert m["denominator_definition"]
    assert "dimension_breakdown" in rep["metrics"]
    assert set(rep["metrics"]["dimension_breakdown"].keys()) == {
        "task_success", "tool_correctness", "process_quality",
        "recovery", "efficiency", "safety",
    }


def test_no_sensitive_leak_in_api():
    rep = _run(5)
    text = str(rep)
    assert "DEEPSEEK_API_KEY=" not in text
    assert "sk-proj-" not in text
