"""Agent 执行 API 测试（pytest，第 41 步）。全部 mock，不发起真实 API 调用。"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402

client = TestClient(app)
REAL_FLAG = "TRACEPILOT_ALLOW_REAL_API"


def test_agent_status():
    r = client.get("/api/agent/status")
    assert r.status_code == 200
    s = r.json()
    assert {m["mode"] for m in s["modes"]} == {"mock", "real_api"}
    modes = {m["mode"]: m for m in s["modes"]}
    assert modes["mock"]["available"] is True
    assert modes["mock"]["simulated"] is True
    assert s["allowed_models"] == ["deepseek-chat", "deepseek-reasoner"]
    assert set(s["data_sources"]) == {"real_api", "mock", "simulated"}
    # 工具白名单必须覆盖实际可用的本地安全工具，且不得包含系统命令/任意代码执行工具
    assert {"calculator", "text_analyzer", "sqlite_query"}.issubset(set(s["tool_whitelist"]))
    for forbidden in ("subprocess", "shell", "bash", "exec", "file_write", "http_request", "eval"):
        assert forbidden not in s["tool_whitelist"]
    # 执行预算由后端统一下发（本地/生产配置不同，字段必须存在）
    assert s["limits"]["agent_max_total_tokens"] >= 1000
    assert s["limits"]["agent_max_output_tokens"] >= 64
    assert s["limits"]["frontend_agent_timeout_ms"] > 0
    assert s["paid_api_called"] is False
    # 不得泄露密钥
    assert "DEEPSEEK_API_KEY=" not in r.text
    assert "sk-" not in r.text


def test_mock_run_created():
    r = client.post("/api/agent/runs", json={"task": "计算125乘以48", "mode": "mock"})
    assert r.status_code == 200, r.text
    run = r.json()["run"]
    assert run["status"] == "succeeded"
    assert run["mode"] == "mock"
    assert run["data_source"] == "mock"
    assert run["simulated"] is True
    assert "6000" in (run["final_answer"] or "")
    assert run["trace_id"]
    assert Path(run["trace_path"]).exists()
    assert run["limits"]["api_max_retries"] == 0


def test_run_detail_and_links():
    run = client.post("/api/agent/runs", json={"task": "计算125乘以48"}).json()["run"]
    r = client.get(f"/api/agent/runs/{run['run_id']}")
    assert r.status_code == 200
    d = r.json()["run"]
    assert d["run_id"] == run["run_id"]
    assert d["links"]["trace"].endswith(run["trace_id"])
    assert d["links"]["dag"].endswith("/dag")


def test_run_trace_and_dag_reachable():
    run = client.post("/api/agent/runs", json={"task": "计算125乘以48"}).json()["run"]
    t = client.get(f"/api/traces/{run['trace_id']}")
    assert t.status_code == 200
    trace = t.json()["trace"]
    assert trace["metadata"]["data_source"] == "mock"
    assert trace["metadata"]["simulated"] is True

    d = client.get(f"/api/traces/{run['trace_id']}/dag")
    assert d.status_code == 200
    dag = d.json()["dag"]
    assert len(dag["nodes"]) == len(trace["steps"])
    assert dag["topological_order"]


def test_runs_list():
    client.post("/api/agent/runs", json={"task": "计算125乘以48"})
    r = client.get("/api/agent/runs")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    assert all("data_source" in x for x in data["runs"])


def test_real_mode_forbidden_without_env(monkeypatch):
    monkeypatch.delenv(REAL_FLAG, raising=False)
    r = client.post(
        "/api/agent/runs",
        json={"task": "计算125乘以48", "mode": "real_api", "confirm_real": True},
    )
    assert r.status_code == 403
    assert "未获授权" in r.json()["detail"]


def test_real_mode_requires_confirm(monkeypatch):
    monkeypatch.setenv(REAL_FLAG, "1")
    r = client.post(
        "/api/agent/runs",
        json={"task": "计算125乘以48", "mode": "real_api", "confirm_real": False},
    )
    assert r.status_code == 403


def test_invalid_model_rejected(monkeypatch):
    monkeypatch.setenv(REAL_FLAG, "1")
    r = client.post(
        "/api/agent/runs",
        json={"task": "x", "mode": "real_api", "confirm_real": True, "model": "gpt-4o"},
    )
    assert r.status_code == 400
    assert "白名单" in r.json()["detail"]


def test_unknown_run_404():
    assert client.get("/api/agent/runs/run-doesnotexist").status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"task": ""},
        {"task": "x", "mode": "gpt"},
        {"task": "x", "max_model_calls": 0},
        {"task": "x", "max_total_tokens": 50},
        {"task": "x", "max_tool_calls": 99},
        {"task": "x", "max_wall_seconds": 1},
        {"task": "x" * 3000},
    ],
)
def test_invalid_payload(payload):
    r = client.post("/api/agent/runs", json=payload)
    assert r.status_code in (400, 422)


def test_no_sensitive_leak_in_run_responses():
    run = client.post("/api/agent/runs", json={"task": "计算125乘以48"}).json()["run"]
    text = client.get(f"/api/agent/runs/{run['run_id']}").text
    assert "DEEPSEEK_API_KEY=" not in text
    assert "sk-" not in text
