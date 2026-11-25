#!/usr/bin/env python
"""TracePilot 本地/CI 质量门禁运行器（第 51 步）。

用途：在没有 GitHub 远程仓库的环境里也能完整跑通 CI 链路：
1. 加载固定评测集（仅白名单数据集，受控确定性 Mock 执行器，不调用付费模型）；
2. 分别执行 baseline / candidate 两次独立评测（`force_new` 关闭去重，保证同为独立运行）；
3. 生成版本对比报告（第 49 步）；
4. 运行回归检测（第 50 步）；
5. 执行质量门禁（第 51 步）；
6. 输出机器可读 JSON + 人工可读 Markdown，并打印 CI 摘要；
7. 按门禁结果返回明确退出码。

退出码：
    0  PASS（WARN 默认也返回 0，可通过配置 `warn_exit_code` 改为非 0）
    1  BLOCK
    2  INCONCLUSIVE
    3  配置/执行异常（禁止异常默认放行）

安全：
- 只使用白名单数据集与受控确定性执行器，不发起真实模型调用、不消耗付费额度；
- 不打印任何环境变量内容/密钥；报告只包含 ID、指标与规则结论。

用法：
    python scripts/ci_quality_gate.py
    python scripts/ci_quality_gate.py --dataset tracepilot-fault-v1 --max-cases 8
    python scripts/ci_quality_gate.py --config config/quality_gate.yaml \
        --report-dir ci-reports/quality-gate --ci-run-id local-001
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# CI 日志固定 UTF-8：Windows 默认 cp936 时中文输出在 UTF-8 解析器（GitHub Actions /
# 测试子进程）中会变成乱码，导致日志与断言不稳定。仅调整输出编码，不改变退出码语义。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):  # pragma: no cover - 非文本流兜底
        pass


EXIT_PASS = 0
EXIT_BLOCK = 1
EXIT_INCONCLUSIVE = 2
EXIT_ERROR = 3

DEFAULT_DATASET = "tracepilot-eval-v1"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "quality_gate.yaml"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "ci-reports" / "quality-gate"


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"必须是整数: {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须 >= 1")
    return parsed


def _wait_run(store, run_id: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = store.get(run_id)
        if record.get("status") in {"succeeded", "failed"}:
            return record
        time.sleep(0.1)
    raise TimeoutError(f"评测运行 {run_id} 在 {timeout:g}s 内未完成")


def _run_deterministic_evaluation(
    store,
    dataset_id: str,
    max_cases: int | None,
    timeout: float,
    label: str,
) -> dict:
    """执行一次受控确定性评测（judge_mode=none，无付费 API 调用）。"""
    started = time.monotonic()
    record = store.submit(dataset_id, max_cases, judge_mode="none", dedup=False)
    record = _wait_run(store, record["run_id"], timeout)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if record.get("status") != "succeeded":
        raise RuntimeError(
            f"{label} 评测运行失败（run_id={record.get('run_id')}）: {record.get('error')}"
        )
    print(
        f"[ci] {label} 评测完成: run_id={record['run_id']} "
        f"dataset={dataset_id} cases={record.get('report', {}).get('executed_cases')} "
        f"elapsed={elapsed_ms}ms"
    )
    return {"run_id": record["run_id"], "report": record.get("report") or {}}


def _write_reports(report: dict, report_dir: Path) -> tuple[Path, Path]:
    """写出机器可读 JSON 与人工可读 Markdown；同时保存到门禁默认报告目录。"""
    from backend.app.services.quality_gate import render_markdown

    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "quality-gate-report.json"
    md_path = report_dir / "quality-gate-report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(report)
    md_path.write_text(markdown, encoding="utf-8")
    return json_path, md_path


def _write_github_step_summary(markdown: str) -> Path | str | None:
    """GitHub Actions 摘要（仅当运行在 CI 且提供 GITHUB_STEP_SUMMARY 时）。"""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return None
    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(markdown)
            handle.write("\n")
        return summary_path
    except OSError:
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TracePilot CI 质量门禁（确定性评测 + 版本对比 + 回归检测 + 门禁）",
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help="固定评测集 ID（白名单）")
    parser.add_argument(
        "--max-cases",
        type=_positive_int,
        default=None,
        help="每个运行的案例上限（限制 CI 资源消耗；默认全量）",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="质量门禁 YAML 配置路径（默认 config/quality_gate.yaml）",
    )
    parser.add_argument(
        "--report-dir",
        default=str(DEFAULT_REPORT_DIR),
        help="报告输出目录（默认 ci-reports/quality-gate）",
    )
    parser.add_argument(
        "--ci-run-id",
        default=None,
        help="CI 运行标识（默认 GITHUB_RUN_ID 或本地生成）",
    )
    parser.add_argument(
        "--baseline-run-id",
        default=None,
        help="复用已有 baseline 运行（仅同一进程内有效；默认新建）",
    )
    parser.add_argument(
        "--candidate-run-id",
        default=None,
        help="复用已有 candidate 运行（仅同一进程内有效；默认新建）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="整体执行超时秒数（默认 300，防止 CI 卡死）",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="只输出 JSON 报告路径，不打印 Markdown 摘要",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.monotonic()
    deadline = started + args.timeout  # 与 time.monotonic() 同源，避免墙钟跳变

    try:
        from backend.app.services.evaluation_store import (
            ALLOWED_DATASETS,
            get_evaluation_store,
        )
        from backend.app.services.quality_gate import (
            QualityGateConfigError,
            evaluate_gate,
            load_gate_config,
        )

        if args.dataset not in ALLOWED_DATASETS:
            raise QualityGateConfigError(
                f"不支持的评测集: {args.dataset!r}（仅允许: {', '.join(sorted(ALLOWED_DATASETS))}）"
            )
        if not Path(args.config).exists():
            raise QualityGateConfigError(f"门禁配置文件不存在: {args.config}")
        # 先校验配置再执行评测：配置错误立即失败，避免浪费 CI 时间
        load_gate_config(args.config)

        ci_run_id = (
            args.ci_run_id
            or os.environ.get("GITHUB_RUN_ID")
            or f"local-{uuid.uuid4().hex[:12]}"
        )
        store = get_evaluation_store()

        print(f"[ci] TracePilot Quality Gate · ci_run_id={ci_run_id}")
        print(
            f"[ci] dataset={args.dataset} max_cases={args.max_cases or 'ALL'} "
            f"config={args.config}"
        )
        print("[ci] 步骤 1/5：加载固定评测集并执行确定性 Mock 评测（judge_mode=none，无付费 API）")

        if args.baseline_run_id and args.candidate_run_id:
            baseline = {"run_id": args.baseline_run_id, "report": {}}
            candidate = {"run_id": args.candidate_run_id, "report": {}}
            print("[ci] 复用调用方提供的 baseline/candidate 运行")
        else:
            baseline = _run_deterministic_evaluation(
                store, args.dataset, args.max_cases, args.timeout, "baseline"
            )
            if time.monotonic() > deadline:
                raise TimeoutError("执行超时（baseline 完成后）")
            print("[ci] 步骤 2/5：执行 candidate 评测")
            candidate = _run_deterministic_evaluation(
                store, args.dataset, args.max_cases, args.timeout, "candidate"
            )

        if time.monotonic() > deadline:
            raise TimeoutError("执行超时（评测阶段）")

        print("[ci] 步骤 3/5：生成版本对比报告并运行回归检测")
        print("[ci] 步骤 4/5：执行质量门禁（确定性规则）")
        report = evaluate_gate(
            baseline_run_id=baseline["run_id"],
            candidate_run_id=candidate["run_id"],
            config_path=args.config,
            ci_run_id=ci_run_id,
            persist=True,
        )

        if time.monotonic() > deadline:
            raise TimeoutError("执行超时（门禁阶段）")

        print("[ci] 步骤 5/5：保存 JSON / Markdown 报告")
        report_dir = Path(args.report_dir)
        json_path, md_path = _write_reports(report, report_dir)
        markdown = md_path.read_text(encoding="utf-8")
        summary_path = _write_github_step_summary(markdown)

        print(f"[ci] JSON 报告: {json_path}")
        print(f"[ci] Markdown 报告: {md_path}")
        if summary_path:
            print(f"[ci] GitHub Step Summary: {summary_path}")
        print(
            f"[ci] 结果: gate_status={report['gate_status']} "
            f"exit_code={report['exit_code']} "
            f"triggered_rules={[r['rule_id'] for r in report['triggered_rules']]}"
        )
        if report["gate_status"] == "WARN":
            print(
                "[ci][WARN] 门禁存在警告（默认不阻断，exit_code="
                f"{report['exit_code']}）；严格门禁请在配置中设置 warn_exit_code。"
            )
        if not args.json_only:
            print("")
            print(markdown)

        if report["gate_status"] == "BLOCK":
            print("[ci] BLOCK：质量门禁未通过，CI 失败。", file=sys.stderr)
        elif report["gate_status"] == "INCONCLUSIVE":
            print(
                "[ci] INCONCLUSIVE：证据不足/版本不可比，门禁无法判定，按失败处理。",
                file=sys.stderr,
            )
        return int(report["exit_code"])
    except Exception as exc:  # noqa: BLE001 - CI 脚本必须兜底为非零退出码
        print(f"[ci][ERROR] 质量门禁执行失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return EXIT_ERROR


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
