"""持久化真实模型调用预算（V2）。

设计目标：
- 每次真实上游模型调用（Agent 的每次 chat.completions、Judge 的每次评审）
  在发出请求前原子扣减一次；不按外部 HTTP 请求扣减，避免 Function Calling
  一轮请求内多次上游调用绕过预算；
- 总调用上限与访客每日上限双重约束，持久化到 JSON 文件，服务重启不清零；
- 记录 token 用量与按可配置单价估算的费用（明确标注为估算，不是 Provider 账单）；
- 状态文件默认位于数据目录，ModelScope 部署时随持久卷保留。

安全边界：
- 未配置 `TRACEPILOT_REAL_TOTAL_CALL_LIMIT`（或为 0）时拒绝一切真实调用；
- 不读取/不保存 API Key；
- 任何异常都按“拒绝调用”处理（fail closed），绝不静默放行。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "RealCallBudgetExceeded",
    "reserve_real_call",
    "record_real_usage",
    "budget_status",
    "budget_limits",
    "reset_budget_for_tests",
    "set_total_limit",
]

TOTAL_LIMIT_ENV = "TRACEPILOT_REAL_TOTAL_CALL_LIMIT"
VISITOR_LIMIT_ENV = "TRACEPILOT_REAL_VISITOR_DAILY_LIMIT"
BUDGET_PATH_ENV = "TRACEPILOT_REAL_BUDGET_PATH"
INPUT_PRICE_ENV = "TRACEPILOT_PRICE_INPUT_CNY_PER_M"
OUTPUT_PRICE_ENV = "TRACEPILOT_PRICE_OUTPUT_CNY_PER_M"

DEFAULT_VISITOR_DAILY_LIMIT = 12
DEFAULT_INPUT_PRICE_CNY_PER_M = 1.0
DEFAULT_OUTPUT_PRICE_CNY_PER_M = 2.0
MAX_EVENTS = 200

_LOCK = threading.RLock()


class RealCallBudgetExceeded(RuntimeError):
    """真实调用预算不足（拒绝调用，未产生上游请求）。"""

    def __init__(self, message: str, *, code: str = "REAL_CALL_BUDGET_EXCEEDED", status: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.status = status or {}


def _settings_data_dir() -> Path:
    """项目数据目录（与 trace_store 等模块的路径约定一致）。"""
    return Path(__file__).resolve().parents[3] / "data"


def _state_path() -> Path:
    raw = os.environ.get(BUDGET_PATH_ENV, "").strip()
    if raw:
        return Path(raw)
    return _settings_data_dir() / "real_api_budget.json"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def budget_limits() -> dict[str, Any]:
    return {
        "total_calls": _int_env(TOTAL_LIMIT_ENV, 0),
        "visitor_daily_calls": _int_env(VISITOR_LIMIT_ENV, DEFAULT_VISITOR_DAILY_LIMIT),
        "price_input_cny_per_million": _float_env(INPUT_PRICE_ENV, DEFAULT_INPUT_PRICE_CNY_PER_M),
        "price_output_cny_per_million": _float_env(OUTPUT_PRICE_ENV, DEFAULT_OUTPUT_PRICE_CNY_PER_M),
        "price_estimate_note": "按环境变量配置的人民币单价估算，不是 Provider 实际账单",
    }


def _empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_calls_used": 0,
        "total_tokens": {"input": 0, "output": 0},
        "estimated_cost_cny": 0.0,
        "visitors": {},
        "events": [],
    }


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _visitor_hash(value: str | None) -> str:
    raw = (value or "unknown").strip() or "unknown"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _read_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 损坏的预算文件不能当作“未使用”放行，保留现状并返回 0 可用额度
        raise RealCallBudgetExceeded(
            "真实调用预算状态文件损坏，已拒绝调用",
            code="REAL_BUDGET_STATE_CORRUPT",
        )
    if not isinstance(data, dict) or data.get("version") != 1:
        raise RealCallBudgetExceeded(
            "真实调用预算状态版本不受支持，已拒绝调用",
            code="REAL_BUDGET_STATE_INVALID",
        )
    data.setdefault("total_calls_used", 0)
    data.setdefault("total_tokens", {"input": 0, "output": 0})
    data.setdefault("estimated_cost_cny", 0.0)
    data.setdefault("visitors", {})
    data.setdefault("events", [])
    return data


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _visitor_used(state: dict[str, Any], key: str) -> int:
    item = state["visitors"].get(key) or {}
    if item.get("date") != _today():
        return 0
    try:
        return int(item.get("calls") or 0)
    except (TypeError, ValueError):
        return 0


def _effective_total_limit(state: dict[str, Any], limits: dict[str, Any]) -> int:
    """管理员在持久化状态中设置的覆盖值优先于环境变量。"""
    override = state.get("override_total_calls")
    if isinstance(override, int) and override >= 0:
        return override
    return int(limits["total_calls"] or 0)


def _status_from_state(state: dict[str, Any], limits: dict[str, Any], visitor_key: str | None = None) -> dict[str, Any]:
    used = int(state.get("total_calls_used") or 0)
    total = _effective_total_limit(state, limits)
    remaining = max(0, total - used) if total > 0 else 0
    result: dict[str, Any] = {
        "enabled": total > 0,
        "total_calls_limit": total,
        "total_calls_used": used,
        "total_calls_remaining": remaining,
        "visitor_daily_limit": int(limits["visitor_daily_calls"]),
        "visitor_day": _today(),
        "total_tokens": dict(state.get("total_tokens") or {}),
        "estimated_cost_cny": round(float(state.get("estimated_cost_cny") or 0.0), 6),
        "price_estimate_note": limits["price_estimate_note"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if visitor_key is not None:
        key = _visitor_hash(visitor_key)
        result["visitor_calls_used"] = _visitor_used(state, key)
        result["visitor_calls_remaining"] = max(0, int(limits["visitor_daily_calls"]) - result["visitor_calls_used"])
    return result


def reserve_real_call(
    *,
    kind: str,
    model: str,
    visitor_key: str | None = None,
) -> dict[str, Any]:
    """在真实上游调用前扣减一次预算。

    Raises:
        RealCallBudgetExceeded: 未配置总预算、总量耗尽或访客日额度耗尽。
    """
    limits = budget_limits()
    visitor_limit = int(limits["visitor_daily_calls"])
    key = _visitor_hash(visitor_key)
    today = _today()

    with _LOCK:
        state = _read_state()
        total_limit = _effective_total_limit(state, limits)
        if total_limit <= 0:
            raise RealCallBudgetExceeded(
                f"真实模型调用未启用：需设置 {TOTAL_LIMIT_ENV} 为正整数",
                code="REAL_CALL_BUDGET_NOT_CONFIGURED",
                status={"enabled": False, **budget_limits()},
            )
        used = int(state.get("total_calls_used") or 0)
        if used >= total_limit:
            raise RealCallBudgetExceeded(
                f"真实模型调用累计预算已用尽（{used}/{total_limit}），未发起上游请求",
                status=_status_from_state(state, limits, visitor_key),
            )
        v_used = _visitor_used(state, key)
        if v_used >= visitor_limit:
            raise RealCallBudgetExceeded(
                f"当前访客今日真实调用额度已用尽（{v_used}/{visitor_limit}），未发起上游请求",
                code="VISITOR_REAL_CALL_LIMIT_REACHED",
                status=_status_from_state(state, limits, visitor_key),
            )

        state["total_calls_used"] = used + 1
        state["visitors"][key] = {"date": today, "calls": v_used + 1}
        state["events"].append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "kind": str(kind)[:64],
                "model": str(model)[:64],
                "visitor": key,
            }
        )
        state["events"] = state["events"][-MAX_EVENTS:]
        _write_state(state)
        return _status_from_state(state, limits, visitor_key)


def record_real_usage(
    *,
    kind: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    visitor_key: str | None = None,
) -> dict[str, Any]:
    """记录一次真实调用返回的 Token 与估算费用（只做统计，不改变调用计数）。"""
    limits = budget_limits()
    inp = max(0, int(input_tokens or 0))
    out = max(0, int(output_tokens or 0))
    price_in = float(limits["price_input_cny_per_million"])
    price_out = float(limits["price_output_cny_per_million"])
    cost = inp / 1_000_000 * price_in + out / 1_000_000 * price_out

    with _LOCK:
        state = _read_state()
        tokens = state.setdefault("total_tokens", {"input": 0, "output": 0})
        tokens["input"] = int(tokens.get("input") or 0) + inp
        tokens["output"] = int(tokens.get("output") or 0) + out
        state["estimated_cost_cny"] = round(float(state.get("estimated_cost_cny") or 0.0) + cost, 8)
        state["events"].append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "kind": str(kind)[:64],
                "model": str(model)[:64],
                "usage": {"input_tokens": inp, "output_tokens": out},
                "estimated_cost_cny": round(cost, 8),
                "visitor": _visitor_hash(visitor_key),
            }
        )
        state["events"] = state["events"][-MAX_EVENTS:]
        _write_state(state)
        return _status_from_state(state, limits, visitor_key)


def budget_status(visitor_key: str | None = None) -> dict[str, Any]:
    limits = budget_limits()
    try:
        state = _read_state()
    except RealCallBudgetExceeded as exc:
        return {
            "enabled": int(limits["total_calls"] or 0) > 0,
            "error": str(exc),
            "code": exc.code,
            **limits,
        }
    return _status_from_state(state, limits, visitor_key)


def set_total_limit(limit: int) -> dict[str, Any]:
    """管理员调整持久化总调用上限（覆盖环境变量）。limit=0 表示暂停真实调用。"""
    value = int(limit)
    if value < 0 or value > 100_000:
        raise ValueError("total_calls_limit 必须在 0..100000")
    limits = budget_limits()
    with _LOCK:
        state = _read_state()
        state["override_total_calls"] = value
        state.setdefault("events", []).append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "kind": "admin_budget_update",
                "total_calls_limit": value,
            }
        )
        state["events"] = state["events"][-MAX_EVENTS:]
        _write_state(state)
        return _status_from_state(state, limits)


def reset_budget_for_tests() -> None:
    """仅测试使用：删除预算状态文件（生产代码不应调用）。"""
    path = _state_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
