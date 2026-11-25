"""LLM Judge API 测试（pytest，第 38 步）。全部使用 Mock，不触发付费 API。"""

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
    pytest.fail("评测运行超时")


def _start(**payload) -> dict:
    body = {"dataset_id": "tracepilot-eval-v1", "max_cases": 2, **payload}
    r = client.post("/api/evaluation/runs", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --- Rubric 接口 ---

def test_rubrics_list():
    r = client.get("/api/evaluation/judge/rubrics")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 4
    ids = {x["rubric_id"] for x in data["rubrics"]}
    assert {"answer_quality", "tool_usage", "factual_consistency", "instruction_following"} <= ids
    for x in data["rubrics"]:
        assert x["version"]
        assert x["dimension_count"] >= 3


def test_rubric_detail():
    r = client.get("/api/evaluation/judge/rubrics/answer_quality")
    assert r.status_code == 200
    body = r.json()
    rb = body["rubric"]
    assert rb["rubric_id"] == "answer_quality"
    assert rb["scale"] == {"min": 1, "max": 5}
    assert len(rb["dimensions"]) >= 6
    assert rb["scoring_rules"] and rb["evidence_requirements"] and rb["unknown_conditions"]
    assert "allowed_dimensions" in body
    assert "reasoning_quality" in body["allowed_dimensions"]


def test_rubric_404_and_traversal():
    assert client.get("/api/evaluation/judge/rubrics/nope").status_code == 404
    assert client.get("/api/evaluation/judge/rubrics/..%2Fetc").status_code in (400, 404)


# --- Judge 信息接口 ---

def test_judge_info():
    r = client.get("/api/evaluation/judge/info")
    assert r.status_code == 200
    info = r.json()
    assert info["default_mode"] == "none"
    assert info["judge_engine_version"]
    assert info["prompt_version"]
    modes = {m["mode"]: m for m in info["modes"]}
    assert modes["none"]["available"] is True
    assert modes["mock"]["available"] is True
    assert modes["mock"]["is_mock"] is True
    assert modes["deepseek"]["available"] is False, "测试环境不得默认启用真实 Judge"
    assert info["paid_api_called"] is False
    assert "api_key" not in str(info).lower()


# --- 默认不启用 Judge ---

def test_run_without_judge():
    rid = _start()["run_id"]
    rep = _wait_run(rid)["report"]
    assert rep["metrics"].get("judge_breakdown") is None
    assert "judge" not in rep["case_results"][0]


# --- Mock Judge 运行 ---

def test_run_with_mock_judge():
    created = _start(judge_mode="mock")
    assert created["run_id"].startswith("eval-")
    rep = _wait_run(created["run_id"])["report"]

    jb = rep["metrics"]["judge_breakdown"]
    assert jb["enabled"] is True
    assert jb["provider_kind"] == "mock"
    assert jb["is_mock"] is True
    assert jb["simulated"] is True
    assert jb["rubric_id"] == "answer_quality"
    assert jb["rubric_version"] and jb["prompt_version"] and jb["judge_engine_version"]
    assert jb["judged_cases"] == 2
    assert jb["denominator_definition"]
    assert jb["note"]
    for dim, stat in jb["dimensions"].items():
        assert stat["scored"] + stat["unknown"] == jb["judged_cases"]
        if stat["avg"] is not None:
            assert 1 <= stat["avg"] <= 5

    # 单案例明细可审计
    j0 = rep["case_results"][0]["judge"]
    assert j0["is_mock"] is True
    assert j0["model_name"] == "mock-rule-based-judge"
    assert j0["provider_kind"] == "mock"
    assert len(j0["scores"]) == 6
    for s in j0["scores"]:
        assert s["status"] in {"SCORED", "UNKNOWN"}
        if s["status"] == "SCORED":
            assert 1 <= s["score"] <= 5
            assert s["evidence"]
        else:
            assert s["score"] is None
    assert j0["limitations"] and j0["disclaimer"]

    # 确定性指标未受影响
    assert rep["total_cases"] == 2
    assert rep["executed_cases"] == 2


def test_run_with_custom_rubric():
    created = _start(judge_mode="mock", judge_rubric_id="tool_usage")
    rep = _wait_run(created["run_id"])["report"]
    jb = rep["metrics"]["judge_breakdown"]
    assert jb["rubric_id"] == "tool_usage"
    assert set(jb["dimensions"].keys()) == {
        "task_relevance", "instruction_following", "factual_consistency", "reasoning_quality",
    }


# --- 安全 ---

def test_real_judge_not_authorized_via_api(monkeypatch):
    """未授权时请求 deepseek 模式：运行失败或返回错误，绝不产生付费调用。"""
    monkeypatch.delenv("TRACEPILOT_JUDGE_ALLOW_REAL", raising=False)
    created = _start(judge_mode="deepseek")
    final = _wait_run(created["run_id"])
    assert final["status"] == "failed"
    assert "RealJudgeDisabledError" in (final.get("error") or "")


def test_invalid_judge_mode_rejected():
    r = client.post(
        "/api/evaluation/runs",
        json={"dataset_id": "tracepilot-eval-v1", "judge_mode": "gpt4"},
    )
    # store 层拒绝 → 400；或 Pydantic 通过后由 store 抛错
    assert r.status_code in (400, 422)


def test_judge_mode_too_long_rejected():
    r = client.post(
        "/api/evaluation/runs",
        json={"dataset_id": "tracepilot-eval-v1", "judge_mode": "x" * 40},
    )
    assert r.status_code == 422


def test_no_sensitive_leak_in_judge_responses():
    rid = _start(judge_mode="mock")["run_id"]
    text = client.get(f"/api/evaluation/runs/{rid}").text
    assert "DEEPSEEK_API_KEY=" not in text
    assert "sk-proj-" not in text
    info = client.get("/api/evaluation/judge/info").text
    assert "DEEPSEEK_API_KEY=" not in info
