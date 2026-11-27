"""请求级上下文（V2）：把访客标识传给模型调用预算与审计。

只在服务端进程内使用 ContextVar；不写入 Trace、不返回给客户端。
访客标识优先取反向代理注入的 X-Forwarded-For 首段，其次取客户端地址；
哈希后用于预算统计，原始 IP 不落盘。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

__all__ = ["CLIENT_KEY", "set_client_key", "get_client_key", "reset_client_key"]

CLIENT_KEY: ContextVar[str] = ContextVar("tracepilot_client_key", default="unknown")


def set_client_key(value: str | None) -> Token:
    return CLIENT_KEY.set((value or "unknown").strip() or "unknown")


def get_client_key() -> str:
    return CLIENT_KEY.get()


def reset_client_key(token: Token) -> None:
    try:
        CLIENT_KEY.reset(token)
    except (ValueError, RuntimeError):
        pass
