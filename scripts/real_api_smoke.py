"""真实 DeepSeek API 冒烟测试（独立运行，不进入默认 pytest / CI）。

用途：在受控预算下验证「真实 API 可用 + 预算扣减 + Agent 真实执行」这条链路，
而不是让默认测试套件依赖付费凭据。

用法（会产生真实费用，默认不执行调用）::

    .venv\\Scripts\\python.exe scripts/real_api_smoke.py --check-only
    .venv\\Scripts\\python.exe scripts/real_api_smoke.py --yes
    .venv\\Scripts\\python.exe scripts/real_api_smoke.py --yes --with-agent --max-calls 8

退出码：0 成功；2 前置条件不满足（未配置/未授权/预算不足）；3 调用失败。

安全约束：
- 必须显式 `--yes` 才会发起付费调用；
- 执行前检查持久化预算剩余额度，不足直接拒绝；
- 不打印 API Key；报告只写模型名、Token、耗时与预算快照。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许直接以脚本方式运行
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from backend.app.services import real_llm  # noqa: E402
from backend.app.services.agent_runner import (  # noqa: E402
    DEFAULT_REAL_MODEL,
    real_api_readiness,
    run_agent_task,
)
from backend.app.services.real_api_budget import budget_status  # noqa: E402

REPORT_DIR = ROOT / "data" / "v2_real_runs"

# 一次 smoke 的预估上游调用数：JSON 调用 1 次；Agent 最多 4 段 model_call（与 RunLimits 对齐）。
JSON_CALLS = 1
AGENT_MAX_CALLS = 4


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _models_probe() -> dict:
    """列出可用模型；该接口不产生 Token 计费。"""
    client = real_llm._client()  # noqa: SLF001 - smoke 脚本复用统一客户端配置
    response = client.models.list()
    return {"models": [m.id for m in response.data]}


def _run(json_only: bool, with_agent: bool, model: str | None) -> dict:
    result: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model or DEFAULT_REAL_MODEL,
        "json_call": None,
        "agent_run": None,
        "errors": [],
    }
    try:
        result["models_probe"] = _models_probe()
    except Exception as exc:  # noqa: BLE001 - smoke 需要把失败落盘而非崩溃
        result["errors"].append(f"models_probe: {type(exc).__name__}: {exc}")

    try:
        call = real_llm.call_json(
            [
                {
                    "role": "system",
                    "content": "你是冒烟测试响应器，只输出 JSON 对象，不要多余文字。",
                },
                {
                    "role": "user",
                    "content": '返回 JSON：{"status":"ok","checked":["json_mode"]}',
                },
            ],
            model=model,
            max_tokens=200,
            timeout=60.0,
            kind="smoke_json",
        )
        result["json_call"] = {
            "data": call["data"],
            "usage": call["usage"],
            "latency_ms": call["latency_ms"],
            "model": call["model"],
        }
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"json_call: {type(exc).__name__}: {exc}")

    if not json_only and with_agent and not result["errors"]:
        try:
            run = run_agent_task(
                "计算 125 乘以 48，并说明使用的工具结果",
                mode="real_api",
                confirm_real=True,
                model=model,
            )
            result["agent_run"] = {
                "run_id": run.run_id,
                "trace_id": run.trace_id,
                "status": run.status,
                "data_source": run.data_source,
                "model": run.model,
                "token_usage": run.token_usage,
                "elapsed_ms": run.elapsed_ms,
                "limit_violations": run.limit_violations,
            }
            if run.status != "succeeded":
                result["errors"].append(f"agent_run: status={run.status!r}")
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"agent_run: {type(exc).__name__}: {exc}")

    result["budget_after"] = budget_status()
    result["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result["ok"] = not result["errors"] and bool(result["json_call"])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实 DeepSeek API 冒烟测试（会产生费用）")
    parser.add_argument("--yes", action="store_true", help="确认发起真实付费调用")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只检查配置与预算，不发起任何调用（默认行为）",
    )
    parser.add_argument("--with-agent", action="store_true", help="额外执行一次真实 Agent 任务")
    parser.add_argument("--model", default=None, help="指定模型；默认读 DEEPSEEK_MODEL")
    parser.add_argument(
        "--max-calls",
        type=int,
        default=JSON_CALLS + AGENT_MAX_CALLS,
        help="允许消耗的上游调用数上限（默认 5）",
    )
    args = parser.parse_args(argv)

    readiness = real_api_readiness()
    budget = budget_status()
    planned = JSON_CALLS + (AGENT_MAX_CALLS if args.with_agent else 0)
    precheck = {
        "readiness": readiness,
        "budget_before": budget,
        "planned_calls": planned,
        "max_calls": args.max_calls,
    }

    if not readiness.get("api_key_configured"):
        print(json.dumps({"ok": False, "reason": "DEEPSEEK_API_KEY 未配置", **precheck},
                         ensure_ascii=False, indent=2))
        return 2
    if not readiness.get("available"):
        print(json.dumps({"ok": False, "reason": "真实 API 当前不可用", **precheck},
                         ensure_ascii=False, indent=2))
        return 2
    remaining = budget.get("total_calls_remaining")
    if remaining is not None and remaining < planned:
        print(json.dumps(
            {"ok": False, "reason": f"预算不足：剩余 {remaining}，计划 {planned}", **precheck},
            ensure_ascii=False, indent=2),)
        return 2
    if planned > args.max_calls:
        print(json.dumps(
            {"ok": False, "reason": f"计划调用 {planned} 超过 --max-calls {args.max_calls}",
             **precheck}, ensure_ascii=False, indent=2))
        return 2

    if args.check_only or not args.yes:
        print(json.dumps({"ok": True, "mode": "check_only", **precheck},
                         ensure_ascii=False, indent=2))
        return 0

    report = _run(json_only=not args.with_agent, with_agent=args.with_agent, model=args.model)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"real_api_smoke_{_utc_stamp()}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(path.relative_to(ROOT))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
