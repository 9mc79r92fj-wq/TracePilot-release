"""Repair Studio API 测试（pytest，第 34 步）。"""

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


def _wait_report(report_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/repairs/reports/{report_id}")
        assert r.status_code == 200
        if r.json()["status"] in {"succeeded", "failed"}:
            return r.json()
        time.sleep(0.1)
    pytest.fail("修复报告超时未完成")


# 1. 能获取真实候选
def test_get_candidates():
    tid = _fault_trace_id()
    r = client.get(f"/api/repairs/candidates/{tid}")
    assert r.status_code == 200
    data = r.json()
    assert data["trace_id"] == tid
    assert data["candidate_count"] >= 1
    assert len(data["candidates"]) == data["candidate_count"]
    assert data["fault_steps"], "应返回真实故障节点"
    for c in data["candidates"]:
        assert c["repair_id"].startswith("repair-")
        assert c["verification_status"] == "not_tested"
        assert c["modification"] and c["expected_effect"]
        assert c["applicable_conditions"] and c["risks"]
    assert "尚未验证" in data["note"]


# 2/3. 能提交并查询安全修复验证
def test_submit_and_query_validation():
    tid = _fault_trace_id()
    r = client.post("/api/repairs/validate", json={"trace_id": tid, "max_candidates": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["report_id"].startswith("report-")
    assert body["status"] in {"running", "succeeded"}

    final = _wait_report(body["report_id"])
    assert final["status"] == "succeeded"
    res = final["result"]
    assert res["source_trace_id"] == tid
    assert res["candidate_count"] >= 1


# 4. 修复后生成独立 Trace
def test_repair_generates_independent_trace():
    tid = _fault_trace_id()
    r = client.post("/api/repairs/validate", json={"trace_id": tid})
    res = _wait_report(r.json()["report_id"])["result"]
    verified = [c for c in res["candidates"] if c["verification_status"] == "verified"]
    assert verified, "至少一个候选应通过受控验证"
    for c in verified:
        assert c["repair_trace_id"]
        assert c["repair_trace_id"] != tid
        assert c["experiment_id"]


# 5. 前后对比数据正确
def test_before_after_comparison():
    tid = _fault_trace_id()
    r = client.post("/api/repairs/validate", json={"trace_id": tid})
    res = _wait_report(r.json()["report_id"])["result"]
    c = res["candidates"][0]
    assert c["outcome_before"] is not None
    assert c["outcome_after"] is not None
    assert c["outcome_before"]["trace_id"] == tid
    assert c["outcome_after"]["trace_id"] == c["repair_trace_id"]
    assert c["cost_delta"] is not None
    assert "duration_before_ms" in c["cost_delta"]
    assert res["task_result_change"]["after_status_by_candidate"]


# 6. 无效候选不会被推荐为成功
def test_invalid_candidate_not_recommended():
    tid = _fault_trace_id()
    r = client.post("/api/repairs/validate", json={"trace_id": tid})
    res = _wait_report(r.json()["report_id"])["result"]
    if res["recommended"]:
        rec_id = res["recommended"]["repair_id"]
        rec = next(c for c in res["candidates"] if c["repair_id"] == rec_id)
        assert rec["verification_status"] == "verified", "推荐项必须是通过验证的候选"
    # failed 候选不得出现在推荐中
    for c in res["candidates"]:
        if c["verification_status"] == "failed":
            assert not res["recommended"] or res["recommended"]["repair_id"] != c["repair_id"]


# 7. 未验证候选有明确标记
def test_unverified_marked():
    tid = _fault_trace_id()
    r = client.post("/api/repairs/validate", json={"trace_id": tid})
    res = _wait_report(r.json()["report_id"])["result"]
    not_tested = [c for c in res["candidates"] if c["verification_status"] == "not_tested"]
    for c in not_tested:
        assert c["verification_error"], "未验证候选必须说明原因"
        assert c["repair_trace_id"] is None
    assert res["unverified"], "报告必须列出未验证事项"


# 8. 页面能够处理空数据（无故障 Trace -> 空候选）
def test_empty_candidates_for_clean_trace(tmp_path, monkeypatch):
    from backend.app.schemas.trace import TraceRecord
    from backend.app.services import trace_store

    ts = "2026-11-18T12:00:00+00:00"
    clean = TraceRecord.model_validate({
        "trace_id": "clean-syn-001", "task_id": "task", "run_id": "run",
        "started_at": ts, "ended_at": ts, "status": "succeeded",
        "steps": [{
            "step_id": "clean-syn-001-s001", "trace_id": "clean-syn-001",
            "parent_step_ids": [], "step_type": "planning", "timestamp": ts,
            "status": "succeeded",
        }],
        "metadata": {},
    })
    tmp = tmp_path
    (tmp / "clean-syn-001.json").write_text(clean.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(trace_store, "DEFAULT_TRACES_DIR", tmp)
    monkeypatch.setattr(trace_store, "_traces_dir", lambda d=None: tmp.resolve())

    r = client.get("/api/repairs/candidates/clean-syn-001")
    assert r.status_code == 200
    data = r.json()
    assert data["candidate_count"] == 0
    assert data["candidates"] == []


# 9. 后端错误处理
def test_error_handling():
    # 不存在 Trace
    assert client.get("/api/repairs/candidates/trace-00000000000000000000000000000000").status_code == 404
    # 不存在报告
    assert client.get("/api/repairs/reports/report-doesnotexist").status_code == 404
    # 非法 trace_id
    assert client.get("/api/repairs/candidates/bad..name").status_code == 400
    # Pydantic 校验
    assert client.post("/api/repairs/validate", json={}).status_code == 422
    assert client.post("/api/repairs/validate", json={"trace_id": "x", "max_candidates": 99}).status_code == 422


# 10. 原始 Trace 不被修改
def test_original_trace_unchanged():
    tid = _fault_trace_id()
    before = client.get(f"/api/traces/{tid}").json()["trace"]
    r = client.post("/api/repairs/validate", json={"trace_id": tid})
    _wait_report(r.json()["report_id"])
    after = client.get(f"/api/traces/{tid}").json()["trace"]
    assert before == after


# 11. 不泄露敏感信息
def test_no_sensitive_leak():
    tid = _fault_trace_id()
    r = client.post("/api/repairs/validate", json={"trace_id": tid})
    text = _wait_report(r.json()["report_id"])["result"].__str__()
    assert "DEEPSEEK_API_KEY" not in text
    assert "sk-proj-" not in text


# 12. 去重与报告列表
def test_dedup_and_list():
    tid = _fault_trace_id()
    payload = {"trace_id": tid, "max_candidates": 2}
    a = client.post("/api/repairs/validate", json=payload).json()
    _wait_report(a["report_id"])
    b = client.post("/api/repairs/validate", json=payload).json()
    assert b["report_id"] == a["report_id"], "窗口内相同配置应复用报告"
