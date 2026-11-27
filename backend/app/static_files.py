"""同源部署扩展：由 FastAPI 在单端口同时托管前端 SPA 与 /api（ModelScope 部署副本专用）。

设计要点：
- 仅在环境变量 ``TRACEPILOT_STATIC_DIR`` 指向含 ``index.html`` 的目录时启用；
  本地开发（Vite dev server 5173 + 后端 8000）不设置该变量，行为与主仓库一致。
- 先注册 API 路由、最后挂载静态资源（见 ``backend/app/main.py`` 末尾），
  因此 ``/api/*``、``/docs``、``/openapi.json`` 不会被静态托管遮蔽。
- BrowserRouter 的深链接（如 ``/traces/xxx``、``/dag``）刷新时回退到
  ``index.html``，避免 404；``/api/*`` 下未知路径仍返回 JSON 404。
- ``index.html`` 加 ``Cache-Control: no-cache``，避免网关缓存旧版本入口；
  带哈希的 ``/assets/*`` 由 Starlette StaticFiles 提供 ETag/Last-Modified。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

__all__ = ["SPAStaticFiles", "mount_frontend"]

logger = logging.getLogger("tracepilot.static")


class SPAStaticFiles(StaticFiles):
    """静态文件 + SPA 回退的 StaticFiles 实现。"""

    def __init__(
        self,
        directory: str | Path,
        *,
        fallback: str = "index.html",
        api_prefix: str = "api/",
    ) -> None:
        super().__init__(directory=directory, html=True, check_dir=True)
        self._fallback = fallback
        self._api_prefix = api_prefix

    async def get_response(self, path: str, scope):  # type: ignore[override]
        # Starlette 在 Windows 上会用 os.path.normpath 把挂载相对路径变成反斜杠，
        # 统一成 POSIX 形式后再判断 /api 前缀与入口 HTML。
        normalized = path.lstrip("/").replace("\\", "/")
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            is_spa_get = scope.get("method") in ("GET", "HEAD")
            if exc.status_code != 404 or not is_spa_get:
                raise
            if normalized.startswith(self._api_prefix):
                # /api/* 下的未知路径保持 JSON 语义，不被 SPA 回退成 HTML
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            logger.debug("SPA 回退: /%s -> %s", normalized, self._fallback)
            response = await super().get_response(self._fallback, scope)

        # 入口 HTML 不缓存，确保重新部署后能拿到新的 asset 哈希
        if normalized in ("", ".", self._fallback):
            response.headers["Cache-Control"] = "no-cache"
        return response


def mount_frontend(app: FastAPI, static_dir: str | Path | None = None) -> bool:
    """按需把前端构建产物挂载到根路径（返回是否启用）。"""
    raw = static_dir if static_dir is not None else os.environ.get("TRACEPILOT_STATIC_DIR", "")
    if not raw:
        return False

    root = Path(raw).expanduser()
    try:
        root = root.resolve()
    except OSError:
        pass
    index = root / "index.html"
    if not index.is_file():
        logger.warning(
            "TRACEPILOT_STATIC_DIR=%s 未找到 index.html，跳过前端托管（仅提供 /api）", root
        )
        return False

    app.mount("/", SPAStaticFiles(directory=root), name="spa")
    logger.info("已挂载前端 SPA: %s（同源 /api，单端口）", root)
    return True
