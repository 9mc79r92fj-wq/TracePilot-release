"""管理员认证（V2）。

目标：公网部署时，上传/更新知识文件、查看完整工具调用与管理调用预算必须由
服务端验证的管理员身份执行，不能只靠前端隐藏按钮。

设计：
- 管理员令牌只从服务端环境变量读取（`TRACEPILOT_ADMIN_TOKEN`），不落盘、不返回；
- 登录成功后签发 HMAC 签名的 HttpOnly Cookie（不依赖 Authorization 头，
  兼容 ModelScope 网关可能改写/剥离请求头的情况）；
- Cookie 同样可用于 API 客户端，另兼容 `Authorization: Bearer <token>` 与
  `X-Admin-Token`，便于脚本验收；
- `TRACEPILOT_ADMIN_REQUIRED=1` 时强制校验；未启用时本地开发/测试保持无感，
  但登录端点仍可用；
- 登录失败统一返回 401，不泄露“是否配置了令牌”。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Annotated

from fastapi import Cookie, Header, HTTPException, Request, Response

__all__ = [
    "ADMIN_COOKIE",
    "SESSION_TTL_SECONDS",
    "admin_required",
    "admin_configured",
    "admin_token",
    "verify_admin_token",
    "create_admin_session",
    "clear_admin_session",
    "require_admin",
    "admin_status",
    "is_admin_request",
]

ADMIN_COOKIE = "tp_admin"
SESSION_TTL_SECONDS = 8 * 3600
_ENV_TOKEN = "TRACEPILOT_ADMIN_TOKEN"
_ENV_REQUIRED = "TRACEPILOT_ADMIN_REQUIRED"
_ENV_SECRET = "TRACEPILOT_SESSION_SECRET"
_ENV_COOKIE_SECURE = "TRACEPILOT_SECURE_COOKIE"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def admin_token() -> str:
    return (os.environ.get(_ENV_TOKEN, "") or "").strip()


def admin_configured() -> bool:
    return bool(admin_token())


def admin_required() -> bool:
    return _env_bool(_ENV_REQUIRED, False) and admin_configured()


def _session_secret() -> bytes:
    """会话签名密钥：优先显式 secret，否则由管理员令牌派生（不落盘）。"""
    raw = (os.environ.get(_ENV_SECRET, "") or "").strip()
    if raw:
        return raw.encode("utf-8")
    return hashlib.sha256(("tracepilot-admin|" + admin_token()).encode("utf-8")).digest()


def _sign(expires: int) -> str:
    msg = f"admin:{expires}".encode("utf-8")
    return hmac.new(_session_secret(), msg, hashlib.sha256).hexdigest()


def verify_admin_token(token: str | None) -> bool:
    expected = admin_token()
    if not expected or not token:
        return False
    return hmac.compare_digest(str(token).encode("utf-8"), expected.encode("utf-8"))


def verify_session_cookie(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    raw_exp, sig = value.split(".", 1)
    try:
        expires = int(raw_exp)
    except ValueError:
        return False
    if expires < int(time.time()) or expires > int(time.time()) + SESSION_TTL_SECONDS + 60:
        return False
    return hmac.compare_digest(_sign(expires), sig)


def create_admin_session(response: Response, admin_token_input: str) -> None:
    expires = int(time.time()) + SESSION_TTL_SECONDS
    response.set_cookie(
        ADMIN_COOKIE,
        f"{expires}.{_sign(expires)}",
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_env_bool(_ENV_COOKIE_SECURE, False),
        samesite="lax",
        path="/",
    )


def clear_admin_session(response: Response) -> None:
    response.delete_cookie(ADMIN_COOKIE, path="/")


def _extract_credentials(
    request: Request,
    cookie: str | None,
    authorization: str | None,
    x_admin_token: str | None,
) -> bool:
    if verify_session_cookie(cookie):
        return True
    if x_admin_token and verify_admin_token(x_admin_token):
        return True
    if authorization and authorization.startswith("Bearer "):
        return verify_admin_token(authorization.removeprefix("Bearer "))
    return False


async def require_admin(
    request: Request,
    tp_admin: Annotated[str | None, Cookie()] = None,
    authorization: Annotated[str | None, Header()] = None,
    x_admin_token: Annotated[str | None, Header()] = None,
) -> None:
    """管理员依赖：仅在显式要求时强制；登录端点本身不依赖它。"""
    if not admin_required():
        return
    if not _extract_credentials(request, tp_admin, authorization, x_admin_token):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "ADMIN_AUTH_REQUIRED",
                "message": "需要管理员登录：请先调用 /api/admin/login 获取会话",
            },
        )


def is_admin_request(request: Request) -> bool:
    """当前请求是否带有效管理员会话/令牌（只读判断）。"""
    return _extract_credentials(
        request,
        request.cookies.get(ADMIN_COOKIE),
        request.headers.get("authorization"),
        request.headers.get("x-admin-token"),
    )


def admin_status(
    *,
    request: Request | None = None,
    cookie: str | None = None,
    authorization: str | None = None,
    x_admin_token: str | None = None,
) -> dict[str, object]:
    authenticated = False
    if request is not None:
        authenticated = _extract_credentials(
            request, cookie, authorization, x_admin_token
        )
    return {
        "configured": admin_configured(),
        "required": admin_required(),
        "authenticated": authenticated,
        "session_ttl_seconds": SESSION_TTL_SECONDS,
        "cookie_name": ADMIN_COOKIE,
        "note": "管理员令牌仅存于服务端环境变量；会话为 HttpOnly 签名 Cookie",
    }
