"""真实 API 可用性诊断测试（零付费调用，不发真实模型请求）。

覆盖第 63 步修复：
- 服务端开关 + API Key 同时具备时 real_api.available=true；
- 缺少任一条件时返回具体阻断原因，而不是笼统提示；
- `/api/agent/status` 永远不返回密钥内容；
- 不在白名单的模型 ID 作为警告（运行时会回退到 deepseek-chat）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.services.agent_runner import real_api_readiness  # noqa: E402

client = TestClient(app)
FLAG = "TRACEPILOT_ALLOW_REAL_API"
KEY = "DEEPSEEK_API_KEY"
BUDGET_LIMIT = "TRACEPILOT_REAL_TOTAL_CALL_LIMIT"
BUDGET_PATH = "TRACEPILOT_REAL_BUDGET_PATH"


@pytest.fixture(autouse=True)
def _temp_budget(tmp_path, monkeypatch):
    """readiness 现在要求配置持久化调用预算；测试使用临时文件，不污染项目 data/。"""
    monkeypatch.setenv(BUDGET_LIMIT, "10")
    monkeypatch.setenv(BUDGET_PATH, str(tmp_path / "budget.json"))
    yield


def _real_mode(payload: dict) -> dict:
    return next(m for m in payload["modes"] if m["mode"] == "real_api")


def test_readiness_blocked_without_flag_and_key(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.delenv(KEY, raising=False)
    readiness = real_api_readiness()
    assert readiness["available"] is False
    assert readiness["allow_flag"] is False
    assert readiness["api_key_configured"] is False
    joined = "；".join(readiness["blocking_reasons"])
    assert FLAG in joined, "缺少服务端开关时必须给出具体原因"
    assert KEY in joined, "缺少 API Key 时必须给出具体原因"


def test_readiness_available_with_flag_and_key(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(KEY, "sk-test-only-not-a-real-key-000000")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    readiness = real_api_readiness()
    assert readiness["available"] is True
    assert readiness["allow_flag"] is True
    assert readiness["api_key_configured"] is True
    assert readiness["configured_model_valid"] is True
    assert readiness["blocking_reasons"] == []


def test_status_missing_key_explains_reason(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.delenv(KEY, raising=False)
    r = client.get("/api/agent/status")
    assert r.status_code == 200
    payload = r.json()
    real = _real_mode(payload)
    assert real["available"] is False
    assert real["api_key_configured"] is False
    assert any(KEY in reason for reason in real["blocking_reasons"])
    assert payload["real_api_ready"] is False
    assert payload["real_api_blocking_reasons"] == real["blocking_reasons"]


def test_status_missing_flag_explains_reason(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.setenv(KEY, "sk-test-only-not-a-real-key-111111")
    r = client.get("/api/agent/status")
    payload = r.json()
    real = _real_mode(payload)
    assert real["available"] is False
    assert any(FLAG in reason for reason in real["blocking_reasons"])


def test_status_available_and_never_leaks_key(monkeypatch):
    secret = "sk-test-secret-should-never-leak-0123456789"
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(KEY, secret)
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    r = client.get("/api/agent/status")
    assert r.status_code == 200
    payload = r.json()
    real = _real_mode(payload)
    assert real["available"] is True
    assert real["api_key_configured"] is True
    assert payload["real_api_ready"] is True
    assert payload["real_api_blocking_reasons"] == []
    # 密钥内容绝不能出现在响应中
    assert secret not in r.text
    assert "sk-" not in r.text
    assert KEY + "=" not in r.text


def test_invalid_model_is_warning_not_blocker(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(KEY, "sk-test-only-not-a-real-key-222222")
    monkeypatch.setenv("DEEPSEEK_MODEL", "gpt-4o")
    readiness = real_api_readiness()
    # 运行时存在回退逻辑，因此无效模型是警告而不是阻断
    assert readiness["available"] is True
    assert readiness["configured_model_valid"] is False
    assert any("deepseek-flash" in w for w in readiness["warnings"])
