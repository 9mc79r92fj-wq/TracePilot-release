"""公网公开演示安全测试：真实 API 匿名禁用 + 请求限流 + CORS 白名单配置。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app, _cors_origins  # noqa: E402
from backend.app.services import evaluation_pipeline as ep  # noqa: E402
from backend.app.services.agent_runner import real_api_readiness  # noqa: E402
from backend.app.services.rate_limit import (  # noqa: E402
    RateLimitMiddleware,
    reset_rate_limits,
)

client = TestClient(app)
FLAG = "TRACEPILOT_ALLOW_REAL_API"
KEY = "DEEPSEEK_API_KEY"
PUBLIC_DEMO = "TRACEPILOT_PUBLIC_DEMO"
RATE_ENV = "TRACEPILOT_RATE_LIMIT_ENABLED"


@pytest.fixture(autouse=True)
def _clean_rate_limits():
    reset_rate_limits()
    yield
    reset_rate_limits()


def test_public_demo_forces_real_api_off_even_with_flag_and_key(monkeypatch):
    monkeypatch.setenv(PUBLIC_DEMO, "1")
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(KEY, "sk-test-public-demo-key-000000")
    readiness = real_api_readiness()
    assert readiness["public_demo"] is True
    assert readiness["available"] is False
    assert any("公共演示" in reason for reason in readiness["blocking_reasons"])

    # 即使请求显式确认，也必须 403 且不产生真实调用
    r = client.post(
        "/api/agent/runs",
        json={"task": "公开演示禁止真实调用", "mode": "real_api", "confirm_real": True},
    )
    assert r.status_code == 403
    assert "公共演示环境禁止真实 API 调用" in r.json()["detail"]


def test_public_demo_blocks_pipeline_real_api(monkeypatch):
    monkeypatch.setenv(PUBLIC_DEMO, "1")
    monkeypatch.setenv(FLAG, "1")
    with pytest.raises(ep.PipelineAuthError):
        ep.create_pipeline(
            dataset_id="tracepilot-eval-v1",
            execution_mode="real_api",
            confirm_real=True,
        )


def test_rate_limit_disabled_by_default(monkeypatch):
    monkeypatch.delenv(RATE_ENV, raising=False)
    for _ in range(30):
        assert client.get("/api/agent/status").status_code == 200


def test_heavy_endpoint_rate_limited(monkeypatch):
    monkeypatch.setenv(RATE_ENV, "1")
    monkeypatch.setenv("TRACEPILOT_RATE_LIMIT_HEAVY_PER_MINUTE", "2")
    monkeypatch.setenv("TRACEPILOT_RATE_LIMIT_GENERAL_PER_MINUTE", "1000")

    # 用非法请求体验证限流发生在业务处理前，且不产生任何 Agent 运行
    payload = {"task": "", "mode": "mock"}
    codes = [client.post("/api/agent/runs", json=payload).status_code for _ in range(3)]
    assert codes[:2] != [429, 429]
    assert codes[2] == 429
    r = client.post("/api/agent/runs", json=payload)
    assert r.status_code == 429
    assert r.headers.get("Retry-After", "").isdigit()
    body = r.json()
    assert body["error"] == "rate_limited"
    assert "真实付费 API 未开放" in body["message"]


def test_general_rate_limit_and_headers(monkeypatch):
    monkeypatch.setenv(RATE_ENV, "1")
    monkeypatch.setenv("TRACEPILOT_RATE_LIMIT_GENERAL_PER_MINUTE", "2")
    monkeypatch.setenv("TRACEPILOT_RATE_LIMIT_HEAVY_PER_MINUTE", "1000")
    first = client.get("/api/agent/status")
    second = client.get("/api/agent/status")
    third = client.get("/api/agent/status")
    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert first.headers.get("X-RateLimit-Limit") == "2"


def test_health_check_exempt_from_rate_limit(monkeypatch):
    monkeypatch.setenv(RATE_ENV, "1")
    monkeypatch.setenv("TRACEPILOT_RATE_LIMIT_GENERAL_PER_MINUTE", "1")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_trust_proxy_uses_forwarded_for(monkeypatch):
    monkeypatch.setenv(RATE_ENV, "1")
    monkeypatch.setenv("TRACEPILOT_TRUST_PROXY", "1")
    monkeypatch.setenv("TRACEPILOT_RATE_LIMIT_HEAVY_PER_MINUTE", "1")
    monkeypatch.setenv("TRACEPILOT_RATE_LIMIT_GENERAL_PER_MINUTE", "1000")
    payload = {"task": "", "mode": "mock"}
    first = client.post(
        "/api/agent/runs", json=payload, headers={"X-Forwarded-For": "198.51.100.7"}
    )
    second = client.post(
        "/api/agent/runs", json=payload, headers={"X-Forwarded-For": "198.51.100.8"}
    )
    # 不同访客 IP 不共享同一桶
    assert first.status_code != 429
    assert second.status_code != 429


def test_cors_origins_from_env(monkeypatch):
    monkeypatch.setenv(
        "TRACEPILOT_CORS_ORIGINS",
        "https://tracepilot-demo.vercel.app, https://tracepilot.example.com/",
    )
    origins = _cors_origins()
    assert origins == [
        "https://tracepilot-demo.vercel.app",
        "https://tracepilot.example.com",
    ]
    monkeypatch.delenv("TRACEPILOT_CORS_ORIGINS", raising=False)
    assert _cors_origins() == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_rate_limit_middleware_is_registered():
    names = [m.cls.__name__ for m in app.user_middleware]
    assert "RateLimitMiddleware" in names
