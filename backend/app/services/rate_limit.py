"""公共演示环境限流（进程内滑动窗口，按 IP 区分普通/高成本接口）。

设计：
- 默认关闭；仅当 `TRACEPILOT_RATE_LIMIT_ENABLED=1` 时启用（Render 生产环境显式开启）；
- 普通请求 120 次/分钟/IP；高成本 POST（Agent 执行 / 评测 / 流水线 / 归因等）10 次/分钟/IP；
- 仅信任 `X-Forwarded-For` 当 `TRACEPILOT_TRUST_PROXY=1`（Render 反向代理场景）；
- 429 返回结构化 JSON 与 Retry-After，便于前端/监控识别；
- 单进程内存实现，足以保护单实例公开演示；多实例部署需改用 Redis/边缘限流。
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

__all__ = [
    "RateLimitMiddleware",
    "reset_rate_limits",
    "HEAVY_POST_PATHS",
    "rate_limit_enabled",
]

WINDOW_SECONDS = 60.0
GENERAL_LIMIT = 120
HEAVY_LIMIT = 10
_ENABLED_ENV = "TRACEPILOT_RATE_LIMIT_ENABLED"
_TRUST_PROXY_ENV = "TRACEPILOT_TRUST_PROXY"
_EXEMPT_PATHS = {"/api/health"}

# 高成本/有副作用 POST 前缀（成本低的只读请求不受 heavy 限制）
HEAVY_POST_PATHS: tuple[str, ...] = (
    "/api/agent/runs",
    "/api/evaluation/runs",
    "/api/evaluation/calibration/runs",
    "/api/evaluation/multi-judge/runs",
    "/api/pipelines",
    "/api/attribution/runs",
    "/api/failure-patterns/runs",
    "/api/red-team/runs",
    "/api/redteam/runs",
    "/api/repairs",
    "/api/repair-studio",
    "/api/counterfactual",
    "/api/quality-gate",
    "/api/version-comparison",
    "/api/regression",
    "/api/pareto",
    "/api/hitl",
)

_LOCK = threading.Lock()
_BUCKETS: dict[str, deque[float]] = {}


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def rate_limit_enabled() -> bool:
    return _env_bool(_ENABLED_ENV)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _limit_for(method: str, path: str) -> tuple[str, int]:
    method = method.upper()
    if method in {"POST", "PUT", "PATCH", "DELETE"} and path.startswith(HEAVY_POST_PATHS):
        return "heavy", _int_env("TRACEPILOT_RATE_LIMIT_HEAVY_PER_MINUTE", HEAVY_LIMIT)
    return "general", _int_env("TRACEPILOT_RATE_LIMIT_GENERAL_PER_MINUTE", GENERAL_LIMIT)


def _client_ip(request: Request) -> str:
    if _env_bool(_TRUST_PROXY_ENV):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
    client = request.client
    return client.host if client and client.host else "unknown"


def _check(ip: str, kind: str, limit: int) -> tuple[bool, int, int]:
    """返回 (allowed, remaining, retry_after_seconds)。"""
    now = time.monotonic()
    key = f"{ip}:{kind}"
    with _LOCK:
        bucket = _BUCKETS.setdefault(key, deque())
        while bucket and now - bucket[0] >= WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, int(WINDOW_SECONDS - (now - bucket[0])) + 1)
            return False, 0, retry_after
        bucket.append(now)
        return True, max(0, limit - len(bucket)), 0


def reset_rate_limits() -> None:
    """清空限流桶（测试/重启用）。"""
    with _LOCK:
        _BUCKETS.clear()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """公开演示限流中间件（默认关闭，生产环境显式开启）。"""

    async def dispatch(self, request: Request, call_next: Any):
        if not rate_limit_enabled():
            return await call_next(request)
        path = request.url.path
        if request.method.upper() == "OPTIONS" or path in _EXEMPT_PATHS:
            return await call_next(request)

        kind, limit = _limit_for(request.method, path)
        ip = _client_ip(request)
        allowed, remaining, retry_after = _check(ip, kind, limit)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": (
                        "公开演示环境请求过于频繁，请稍后重试。"
                        "真实付费 API 未开放；Mock 演示功能仍受此限流保护。"
                    ),
                    "retry_after_seconds": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
