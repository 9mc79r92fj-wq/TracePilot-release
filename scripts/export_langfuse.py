"""TracePilot → LangFuse 增量导出 CLI（OTLP/HTTP，零新增依赖）。

示例::

    # 审阅模式（默认安全姿态，不发网络请求）
    python scripts/export_langfuse.py --dry-run --limit 10 --out ./tmp/otlp

    # 真实导出到自托管 LangFuse（必须显式 --yes）
    LANGFUSE_HOST=http://localhost:3000 \\
    LANGFUSE_PUBLIC_KEY=pk-... LANGFUSE_SECRET_KEY=sk-... \\
    python scripts/export_langfuse.py --yes

    # 查看只读状态（不请求网络、不打印密钥）
    python scripts/export_langfuse.py --status --json

退出码：0 成功；2 可预期失败（配置/网络/状态/锁）；1 未预期异常。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # 本地 .env 是可选的；生产环境通常直接注入环境变量
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001 - 缺少 dotenv 不应阻断导出工具
    pass

from backend.app.services.langfuse_export import (  # noqa: E402
    ExportError,
    ExportLockError,
    ExportStateError,
    build_export_config,
    describe_export_status,
    export_to_langfuse,
)
from backend.app.services.langfuse_sdk_export import (  # noqa: E402
    export_traces_via_sdk,
    sdk_available,
    sdk_version,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TracePilot Trace → LangFuse OTLP 增量导出")
    parser.add_argument("--dry-run", action="store_true", help="只生成 OTLP 负载，不发网络请求")
    parser.add_argument("--yes", action="store_true", help="确认真实发送（否则默认 dry-run）")
    parser.add_argument("--force", action="store_true", help="忽略增量状态，重新导出")
    parser.add_argument("--status", action="store_true", help="只输出只读状态")
    parser.add_argument("--limit", type=int, default=None, help="最多检查多少条 Trace")
    parser.add_argument("--trace-id", default=None, help="只导出指定 Trace")
    parser.add_argument("--out", default=None, help="dry-run 负载输出目录")
    parser.add_argument("--state-file", default=None, help="增量状态文件路径")
    parser.add_argument("--traces-dir", default=None, help="Trace 目录（默认 data/traces）")
    parser.add_argument("--endpoint", default=None, help="OTLP endpoint（覆盖 LANGFUSE_HOST 推导）")
    parser.add_argument("--langfuse-host", default=None, help="自托管 LangFuse 地址（如 http://localhost:3000）")
    parser.add_argument("--timeout", type=float, default=None, help="单次请求超时秒数")
    parser.add_argument("--max-retries", type=int, default=None, help="失败重试次数（默认 3）")
    parser.add_argument("--batch-traces", type=int, default=None, help="每个 OTLP 请求包含的 Trace 数")
    parser.add_argument("--max-attr-chars", type=int, default=None, help="单字段截断长度（默认 4000）")
    parser.add_argument(
        "--transport",
        choices=("auto", "sdk", "otlp"),
        default="auto",
        help="真实发送通道：sdk=LangFuse 官方 SDK；otlp=内置零依赖 OTLP；auto=有 SDK 时用 sdk",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return parser


def _export_via_sdk(*, config, force: bool, limit, trace_id, yes: bool):
    """SDK 通道：复用 OTLP 通道的状态/增量语义，但通过官方 SDK 发送。"""
    from backend.app.services.langfuse_export import (
        _StateLock,
        TraceExportState,
        load_traces_for_export,
    )

    if not yes and not config.enabled:
        raise ExportError(
            "真实导出需要显式确认：请加 --yes，或设置 TRACEPILOT_LANGFUSE_ENABLED=1（默认只做 dry-run）"
        )
    with _StateLock(config.state_path):
        state = TraceExportState.load(config.state_path)
        traces = load_traces_for_export(traces_dir=config.traces_dir, limit=limit, trace_id=trace_id)
        result = export_traces_via_sdk(traces, config=config, state=state, force=force)
        if result.failed:
            state.save(error=f"{result.failed} 条 Trace SDK 导出失败")
        else:
            state.save(mark_success=True)
        return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    host = args.langfuse_host
    if host is not None:
        import os

        os.environ["LANGFUSE_HOST"] = host
    config = build_export_config(
        endpoint=args.endpoint,
        state_path=args.state_file,
        traces_dir=args.traces_dir,
        timeout=args.timeout,
        max_retries=args.max_retries,
        batch_traces=args.batch_traces,
        max_attr_chars=args.max_attr_chars,
    )

    if args.status:
        status = describe_export_status(config)
        status["sdk"] = {"available": sdk_available(), "version": sdk_version()}
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    dry_run = True if args.dry_run else (False if args.yes else None)
    transport = args.transport
    if transport == "auto":
        transport = "sdk" if sdk_available() else "otlp"
    try:
        if transport == "sdk" and dry_run is not True:
            result = _export_via_sdk(config=config, force=args.force, limit=args.limit,
                                     trace_id=args.trace_id, yes=args.yes)
        else:
            result = export_to_langfuse(
            config=config,
            dry_run=dry_run,
            force=args.force,
            limit=args.limit,
            trace_id=args.trace_id,
            output_dir=Path(args.out) if args.out else None,
            yes=args.yes,
        )
    except (ExportError, ExportLockError, ExportStateError) as exc:
        payload = {"ok": False, "error": str(exc)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"[失败] {exc}", file=sys.stderr)
        return 2

    summary = {"ok": result.failed == 0, "transport": transport, **result.to_dict()}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        mode = "dry-run" if result.dry_run else "真实导出"
        print(
            f"[{mode}] scanned={result.scanned} eligible={result.eligible} exported={result.exported} "
            f"skipped={result.skipped} failed={result.failed} spans={result.spans} batches={result.batches}"
        )
        if result.dry_run and result.payloads:
            print(f"OTLP 负载输出目录示例：{result.payloads[0].parent}")
        if result.failures:
            for failure in result.failures[:5]:
                print(f"  - {failure['trace_id']}: {failure['error']}", file=sys.stderr)
        print(f"endpoint={result.endpoint or '(未配置)'} state={result.state_file}")
    return 0 if result.failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
