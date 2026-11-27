"""真实模型持久化预算与管理员认证测试（V2）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services import real_api_budget as budget  # noqa: E402

LIMIT = "TRACEPILOT_REAL_TOTAL_CALL_LIMIT"
VISITOR = "TRACEPILOT_REAL_VISITOR_DAILY_LIMIT"


@pytest.fixture
def budget_env(tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    monkeypatch.setenv(budget.BUDGET_PATH_ENV, str(path))
    monkeypatch.setenv(LIMIT, "2")
    monkeypatch.setenv(VISITOR, "10")
    yield path
    budget.reset_budget_for_tests()


def test_reserve_persists_and_enforces_total_limit(budget_env):
    first = budget.reserve_real_call(kind="agent", model="deepseek-flash", visitor_key="ip-a")
    assert first["total_calls_used"] == 1
    assert first["total_calls_remaining"] == 1
    second = budget.reserve_real_call(kind="agent", model="deepseek-flash", visitor_key="ip-a")
    assert second["total_calls_remaining"] == 0
    with pytest.raises(budget.RealCallBudgetExceeded):
        budget.reserve_real_call(kind="agent", model="deepseek-flash", visitor_key="ip-a")
    # 状态已持久化（重启后读取同一文件）
    data = json.loads(budget_env.read_text(encoding="utf-8"))
    assert data["total_calls_used"] == 2


def test_budget_requires_explicit_limit(budget_env, monkeypatch):
    monkeypatch.delenv(LIMIT, raising=False)
    with pytest.raises(budget.RealCallBudgetExceeded) as exc:
        budget.reserve_real_call(kind="agent", model="deepseek-flash")
    assert exc.value.code == "REAL_CALL_BUDGET_NOT_CONFIGURED"


def test_visitor_daily_limit(budget_env, monkeypatch):
    monkeypatch.setenv(LIMIT, "100")
    monkeypatch.setenv(VISITOR, "1")
    budget.reserve_real_call(kind="agent", model="deepseek-flash", visitor_key="ip-a")
    with pytest.raises(budget.RealCallBudgetExceeded) as exc:
        budget.reserve_real_call(kind="agent", model="deepseek-flash", visitor_key="ip-a")
    assert exc.value.code == "VISITOR_REAL_CALL_LIMIT_REACHED"
    # 另一个访客仍可调用
    ok = budget.reserve_real_call(kind="agent", model="deepseek-flash", visitor_key="ip-b")
    assert ok["visitor_calls_used"] == 1


def test_usage_and_cost_estimation(budget_env):
    budget.reserve_real_call(kind="judge", model="deepseek-flash")
    status = budget.record_real_usage(
        kind="judge",
        model="deepseek-flash",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert status["total_tokens"] == {"input": 1_000_000, "output": 1_000_000}
    # 默认单价 1 CNY/M 输入 + 2 CNY/M 输出
    assert status["estimated_cost_cny"] == pytest.approx(3.0)


def test_corrupt_state_fails_closed(budget_env):
    budget_env.write_text("{not-json", encoding="utf-8")
    with pytest.raises(budget.RealCallBudgetExceeded) as exc:
        budget.reserve_real_call(kind="agent", model="deepseek-flash")
    assert exc.value.code == "REAL_BUDGET_STATE_CORRUPT"


def test_admin_override_limit(budget_env):
    status = budget.set_total_limit(5)
    assert status["total_calls_limit"] == 5
    # 覆盖值优先于环境变量（env=2）
    for _ in range(5):
        budget.reserve_real_call(kind="agent", model="deepseek-flash", visitor_key="ip-c")
    with pytest.raises(budget.RealCallBudgetExceeded):
        budget.reserve_real_call(kind="agent", model="deepseek-flash", visitor_key="ip-c")


def test_admin_login_cookie_and_protected_route(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from backend.app.main import app

    monkeypatch.setenv("TRACEPILOT_ADMIN_TOKEN", "a" * 48)
    monkeypatch.setenv("TRACEPILOT_ADMIN_REQUIRED", "1")
    monkeypatch.setenv(budget.BUDGET_PATH_ENV, str(tmp_path / "admin-budget.json"))
    monkeypatch.setenv(LIMIT, "10")
    client = TestClient(app)
    # 未登录不能修改预算/上传（上传依赖在单独模块中校验，这里只验证管理员预算端点）
    assert client.post("/api/admin/budget", json={"total_calls_limit": 3}).status_code == 401
    bad = client.post("/api/admin/login", json={"token": "wrong-token"})
    assert bad.status_code == 401
    good = client.post("/api/admin/login", json={"token": "a" * 48})
    assert good.status_code == 200
    assert good.json()["authenticated"] is True
    # 登录 Cookie 已由 TestClient 保存
    status = client.get("/api/admin/session")
    assert status.json()["authenticated"] is True
    assert client.post("/api/admin/budget", json={"total_calls_limit": 3}).status_code == 200
    assert client.post("/api/admin/logout").status_code == 200
    assert client.get("/api/admin/session").json()["authenticated"] is False
