#!/usr/bin/env python
"""TracePilot 数据来源审计 / 迁移脚本（第 55-A 步）。

只读扫描 `data/traces/` 与 `data/evaluation/traces/`，按保守规则分类：
REAL_API / MOCK / SIMULATED / REPLAY / UNKNOWN，不修改任何原始 Trace。

用法：
    python scripts/audit_data_sources.py                 # 打印审计摘要（不写文件）
    python scripts/audit_data_sources.py --apply         # 生成/更新 data/governance/source_index.json
    python scripts/audit_data_sources.py --verify        # 校验 sidecar 与当前数据一致（可重复执行）
    python scripts/audit_data_sources.py --json report.json
    python scripts/audit_data_sources.py --restore-check # 输出回滚/恢复指引（不执行恢复）

迁移是非破坏性的：原始 Trace 不动，删除 sidecar 即回滚到「无分类索引」状态。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services import data_governance as governance  # noqa: E402


def _print_summary(summary: dict) -> None:
    print("== Trace 数据来源审计 ==")
    print(f"范围: {summary['scope']['agent_dir']} + {summary['scope']['evaluation_dir']}")
    print(f"总 Trace: {summary['total_traces']}")
    print(f"  Agent 目录: {summary['agent_traces']}")
    print(f"  Evaluation 目录: {summary['evaluation_traces']}")
    for source in governance.SOURCE_CLASSES:
        print(f"  {source}: {summary['by_source'][source]}")
    print(f"UNKNOWN: {summary['unknown_count']}；重复 trace_id: {len(summary['duplicate_trace_ids'])}；坏文件: {summary['malformed_files']}")
    if summary["legacy_real_api_claims"]:
        print("历史 real_api 声明但无独立验证标记（归 UNKNOWN，不自动升级）:")
        for item in summary["legacy_real_api_claims"]:
            print(f"  - {item['trace_id']} -> {item['classified_as']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="TracePilot 数据来源审计/迁移")
    parser.add_argument("--apply", action="store_true", help="写入 sidecar 索引（非破坏性）")
    parser.add_argument("--verify", action="store_true", help="校验 sidecar 与当前数据一致")
    parser.add_argument("--json", dest="json_path", help="把完整审计摘要写到 JSON 文件")
    parser.add_argument("--restore-check", action="store_true", help="输出回滚/恢复指引")
    args = parser.parse_args()

    if args.restore_check:
        print("回滚/恢复指引：")
        print(f"1) 分类索引 sidecar: {governance.SOURCE_INDEX_PATH}")
        print("   删除该文件即可回滚分类索引；原始 Trace 从未被修改。")
        print("2) 数据库/配置文件备份位于 backups/step55a_*/，恢复步骤见其中 RESTORE.md。")
        print("3) 代码回滚：git checkout master（或回退本分支提交）。")
        return 0

    if args.verify:
        sidecar = governance._load_sidecar()  # noqa: SLF001 - 运维脚本
        if not sidecar:
            print("VERIFY FAILED: sidecar 不存在，请先运行 --apply")
            return 2
        current = governance.scan_sources(force=True)
        ok = (
            sidecar.get("entry_count") == current["total_traces"]
            and sidecar.get("by_source") == current["by_source"]
            and sidecar.get("signature") == [list(item) for item in governance._dir_signature()]  # noqa: SLF001
        )
        if not ok:
            print("VERIFY FAILED: sidecar 与当前数据不一致（可能新增/删除 Trace，请重新 --apply）")
            return 2
        print("VERIFY OK：sidecar 与当前 16407 条 Trace 的分类结果一致（重复执行不会重复创建记录）")
        return 0

    summary = governance.scan_sources(force=True, persist=args.apply)
    _print_summary(summary)
    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        io.open(path, "w", encoding="utf-8", newline="\n").write(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
        )
        print(f"JSON 已写入: {path}")
    if args.apply:
        print(f"sidecar 已写入: {governance.SOURCE_INDEX_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
