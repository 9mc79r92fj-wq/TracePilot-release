"""Evaluation Center API 测试（pytest，第 36 步）。"""

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


def _wait_run(run_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/evaluation/runs/{run_id}")
        assert r.status_code == 200
        if r.json()["status"] in {"succeeded", "failed"}:
            return r.json()
        time.sleep(0.1)
    pytest.fail("评测运行超时未完成")


def _start_run(max_cases: int | None = 5) -> dict:
    payload: dict = {"dataset_id": "tracepilot-eval-v1"}
    if max_cases is not None:
        payload["max_cases"] = max_cases
    r = client.post("/api/evaluation/runs", json=payload)
    assert r.status_code == 200
    return r.json()


# 1. 数据集接口正常
def test_datasets_list_and_detail():
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

    d = client.get("/api/evaluation/datasets/tracepilot-eval-v1")
    assert d.status_code == 200
    body = d.json()
    assert body["dataset_version"]
    assert body["case_count"] == by_id["tracepilot-eval-v1"]["case_count"]
    assert len(body["cases"]) == body["case_count"]
    assert body["scenario_types"]
    for c in body["cases"]:
        assert set(c.keys()) == {"case_id", "task_description", "scenario_type", "data_source"}
        assert c["data_source"] in {"synthetic", "recorded", "real_api"}

    # 非白名单数据集 -> 404
    assert client.get("/api/evaluation/datasets/etc-passwd").status_code == 404


# 2. 评测运行接口正常
def test_run_creation_and_status():
    created = _start_run(3)
    assert created["run_id"].startswith("eval-")
    assert created["status"] in {"running", "succeeded"}
    assert created["dataset_id"] == "tracepilot-eval-v1"
    assert "links" in created

    final = _wait_run(created["run_id"])
    assert final["status"] == "succeeded"
    assert final["report"]["evaluation_run_id"] == created["run_id"]


# 3. 评测报告能够查询
def test_report_query():
    run_id = _start_run(4)["run_id"]
    final = _wait_run(run_id)
    rep = final["report"]
    assert rep["total_cases"] == 4
    assert rep["executed_cases"] == 4
    assert len(rep["case_results"]) == 4
    assert rep["dataset_data_source"] == "synthetic"
    assert rep["environment"]["model"]["simulated"] is True
    assert rep["environment"]["model"]["paid_api_called"] is False
    assert rep["limitations"]


# 4. Bad Case 数据正确
def test_bad_cases_endpoint():
    run_id = _start_run(5)["run_id"]
    _wait_run(run_id)
    r = client.get(f"/api/evaluation/runs/{run_id}/bad-cases")
    assert r.status_code == 200
    body = r.json()
    assert "bad_cases" in body
    assert "unknown_cases" in body
    assert "limitations" in body
    for b in body["bad_cases"]:
        assert b["trace_id"]
        assert b["failed_dimensions"], "Bad Case 必须标注失败维度"


# 5. 指标计算与后端一致
def test_metrics_consistent():
    from backend.app.services.evaluation_engine import load_dataset, run_evaluation

    direct = run_evaluation(load_dataset(), max_cases=3)
    run_id = _start_run(3)["run_id"]
    api = _wait_run(run_id)["report"]

    # 同配置下计数应一致（模拟环境确定性）
    assert api["total_cases"] == direct["total_cases"]
    assert api["executed_cases"] == direct["executed_cases"]
    assert api["passed_cases"] == direct["passed_cases"]
    assert api["failed_cases"] == direct["failed_cases"]
    assert (
        api["metrics"]["task_success_rate"]["denominator"]
        == direct["metrics"]["task_success_rate"]["denominator"]
    )
    assert (
        api["metrics"]["task_success_rate"]["numerator"]
        == direct["metrics"]["task_success_rate"]["numerator"]
    )


# 6/7. 指标可追溯到案例、案例可追溯到 Trace
def test_drilldown_chain():
    run_id = _start_run(5)["run_id"]
    report = _wait_run(run_id)["report"]

    # 指标 -> 案例：dimension_breakdown 计数应等于 case_results 统计
    dims = report["metrics"]["dimension_breakdown"]
    for dim, stat in dims.items():
        statuses = [r["metrics"][dim]["status"] for r in report["case_results"]]
        assert stat["pass"] == statuses.count("PASS")
        assert stat["fail"] == statuses.count("FAIL")
        assert stat["unknown"] == statuses.count("UNKNOWN")
        assert stat["not_applicable"] == statuses.count("NOT_APPLICABLE")

    # 案例 -> Trace -> DAG
    case0 = report["case_results"][0]
    r = client.get(f"/api/evaluation/runs/{run_id}/cases/{case0['case_id']}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["case"]["trace_id"] == case0["trace_id"]
    assert detail["linked_trace_id"] == case0["trace_id"]
    assert detail["links"]["trace"].endswith(case0["trace_id"])
    assert detail["links"]["dag"].endswith("/dag")


# 8. Trace 可跳转 DAG（trace_ids 与报告一致）
def test_trace_ids_linkage():
    run_id = _start_run(3)["run_id"]
    report = _wait_run(run_id)["report"]
    assert len(report["trace_ids"]) == report["executed_cases"]
    assert len(set(report["trace_ids"])) == len(report["trace_ids"])
    for c in report["case_results"]:
        assert c["trace_id"] in report["trace_ids"]


# 9. 错误处理
def test_error_handling():
    # 非白名单数据集
    r = client.post("/api/evaluation/runs", json={"dataset_id": "evil-dataset"})
    assert r.status_code == 404
    # 运行的 run_id 不存在
    assert client.get("/api/evaluation/runs/eval-doesnotexist").status_code == 404
    assert client.get("/api/evaluation/runs/eval-doesnotexist/bad-cases").status_code == 404
    # Pydantic 校验
    assert client.post("/api/evaluation/runs", json={"max_cases": 0}).status_code == 422
    assert client.post("/api/evaluation/runs", json={"max_cases": 999}).status_code == 422
    # 案例不存在
    run_id = _start_run(2)["run_id"]
    _wait_run(run_id)
    assert client.get(f"/api/evaluation/runs/{run_id}/cases/nope").status_code == 404


# 10. 重复提交复用（防资源耗尽）
def test_dedup():
    payload = {"dataset_id": "tracepilot-eval-v1", "max_cases": 2}
    a = client.post("/api/evaluation/runs", json=payload).json()
    _wait_run(a["run_id"])
    b = client.post("/api/evaluation/runs", json=payload).json()
    assert b["run_id"] == a["run_id"]


# 11. 不泄露 API Key
def test_no_sensitive_leak():
    run_id = _start_run(3)["run_id"]
    _wait_run(run_id)
    text = client.get(f"/api/evaluation/runs/{run_id}").text
    assert "DEEPSEEK_API_KEY=" not in text
    assert "sk-proj-" not in text


# 12. 不触发付费模型
def test_no_paid_model():
    run_id = _start_run(2)["run_id"]
    rep = _wait_run(run_id)["report"]
    assert rep["environment"]["model"]["paid_api_called"] is False
    assert rep["environment"]["model"]["simulated"] is True
