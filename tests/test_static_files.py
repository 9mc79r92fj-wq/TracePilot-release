"""单端口同源部署扩展（backend/app/static_files.py）的行为测试。

覆盖：
- 未配置 TRACEPILOT_STATIC_DIR 时完全不启用（本地开发行为不变）；
- 配置了含 index.html 的目录时，根路径与深链接回退到 index.html；
- /api/* 未知路径仍返回 JSON 404，不被 SPA 回退成 HTML；
- entry HTML 带 no-cache，静态资源可正常返回。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.static_files import mount_frontend


def _build_app(static_dir: Path | None) -> FastAPI:
    app = FastAPI()

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    mount_frontend(app, static_dir=static_dir)
    return app


def _make_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>TracePilot SPA</body></html>", encoding="utf-8")
    (dist / "assets" / "app-abc123.js").write_text("console.log('ok')", encoding="utf-8")
    return dist


def test_disabled_without_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TRACEPILOT_STATIC_DIR", raising=False)
    app = _build_app(None)
    client = TestClient(app)
    assert client.get("/api/health").json() == {"status": "ok"}
    # 未挂载 SPA 时根路径不应返回 HTML
    assert client.get("/").status_code == 404


def test_mounts_when_index_exists(tmp_path: Path) -> None:
    dist = _make_dist(tmp_path)
    assert mount_frontend(FastAPI(), static_dir=dist) is True

    app = _build_app(dist)
    client = TestClient(app)

    root = client.get("/")
    assert root.status_code == 200
    assert "TracePilot SPA" in root.text
    assert root.headers.get("cache-control") == "no-cache"

    deep = client.get("/traces/trace-abc")
    assert deep.status_code == 200
    assert "TracePilot SPA" in deep.text

    asset = client.get("/assets/app-abc123.js")
    assert asset.status_code == 200
    assert "console.log" in asset.text

    # API 路由不被静态托管遮蔽
    assert client.get("/api/health").json() == {"status": "ok"}
    # /api/* 未知路径保持 JSON 语义
    unknown_api = client.get("/api/not-exists")
    assert unknown_api.status_code == 404
    assert unknown_api.headers.get("content-type", "").startswith("application/json")


def test_skip_when_index_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert mount_frontend(FastAPI(), static_dir=empty) is False

    app = _build_app(empty)
    client = TestClient(app)
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 404
