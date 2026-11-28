"""只读公网部署健康检查（用于发布前/后门禁，绝不修改线上数据）。

检查项：
1. ``/api/health`` 200 且返回 status=ok；
2. ``/api/v2/status`` 200：真实 API 公开开关关闭、预算 fail-closed、Benchmark 与 Judge 归档可用；
3. ``/api/interop/queryforge/schema`` 200（协议入口可用）；
4. 首页 HTML 200 且包含前端 bundle 引用。

用法::

    python scripts/verify_public_deploy.py
    python scripts/verify_public_deploy.py --base https://luis123xi394-tracepilot.ms.show --json

退出码：0 = 全部通过；1 = 存在失败。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE = "https://luis123xi394-tracepilot.ms.show"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _request(url: str, timeout: float = 30.0) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def _json(url: str) -> tuple[int, dict[str, Any]]:
    status, body = _request(url)
    return status, json.loads(body.decode("utf-8"))


def check(base: str) -> list[dict[str, Any]]:
    base = base.rstrip("/")
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        status, health = _json(base + "/api/health")
        record(
            "GET /api/health",
            status == 200 and health.get("status") == "ok",
            f"http={status} trace_count={health.get('trace_count')}",
        )
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        record("GET /api/health", False, f"{type(exc).__name__}: {exc}")

    try:
        status, v2 = _json(base + "/api/v2/status")
        real_api = v2.get("real_api") or {}
        budget = v2.get("budget") or {}
        benchmark = v2.get("benchmark") or {}
        judge = v2.get("judge_calibration") or {}
        record(
            "GET /api/v2/status",
            status == 200 and benchmark.get("available") is True,
            f"http={status} benchmark={benchmark.get('available')} cases={benchmark.get('case_count')}",
        )
        record(
            "公开空间真实 API 关闭",
            real_api.get("available") is False and real_api.get("public_real_enabled") is False,
            f"available={real_api.get('available')} public={real_api.get('public_real_enabled')}",
        )
        record(
            "真实调用预算 fail-closed",
            budget.get("enabled") is False and int(budget.get("total_calls_limit") or 0) == 0,
            f"enabled={budget.get('enabled')} limit={budget.get('total_calls_limit')}",
        )
        record(
            "Judge 校准归档可用",
            judge.get("available") is True,
            f"available={judge.get('available')}",
        )
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        record("GET /api/v2/status", False, f"{type(exc).__name__}: {exc}")

    try:
        status, schema = _json(base + "/api/interop/queryforge/schema")
        record(
            "GET /api/interop/queryforge/schema",
            status == 200 and schema.get("protocol_version") == "1.0",
            f"http={status} protocol={schema.get('protocol_version')}",
        )
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        record("GET /api/interop/queryforge/schema", False, f"{type(exc).__name__}: {exc}")

    try:
        status, body = _request(base + "/")
        html = body.decode("utf-8", errors="ignore")
        record(
            "GET /（前端入口）",
            status == 200 and ("/assets/" in html or "id=\"app\"" in html),
            f"http={status} bytes={len(body)}",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        record("GET /（前端入口）", False, f"{type(exc).__name__}: {exc}")
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TracePilot 公网部署只读健康检查")
    parser.add_argument("--base", default=DEFAULT_BASE, help="公网基地址")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)

    checks = check(args.base)
    ok = all(item["ok"] for item in checks)
    if args.json:
        print(json.dumps({"ok": ok, "base": args.base, "checks": checks}, ensure_ascii=False, indent=2))
    else:
        print(f"公网部署检查：{args.base}")
        for item in checks:
            print(f"  {'PASS' if item['ok'] else 'FAIL'}  {item['name']}: {item['detail']}")
        print("结果：全部通过" if ok else "结果：存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
