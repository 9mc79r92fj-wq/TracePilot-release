"""把测试运行写入生产 Trace 目录的「污染」文件隔离到可回溯目录（第 55-A 步）。

背景：完整测试套件会通过生产默认路径写入 Trace（AgentRunner / 评测执行器），
使 data/traces 与 data/evaluation/traces 的数量、状态与来源统计被测试数据稀释。
本脚本以第 55-A 步备份为基线，只移动「基线中不存在」的 Trace 文件到
data/governance/quarantine/ 下（不删除，保留 manifest 以支持恢复）。

用法：
    python scripts/quarantine_test_pollution.py            # 只预览（默认 dry-run）
    python scripts/quarantine_test_pollution.py --apply    # 实际移动
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.data_governance import (  # noqa: E402
    TRACE_DIRS,
    classify_metadata,
)

DEFAULT_BACKUP_ROOT = PROJECT_ROOT / "backups"
DEFAULT_QUARANTINE_ROOT = PROJECT_ROOT / "data" / "governance" / "quarantine"


def _latest_backup() -> Path:
    candidates = sorted(
        (p for p in DEFAULT_BACKUP_ROOT.glob("step55a_*") if p.is_dir()),
        key=lambda p: p.name,
    )
    if not candidates:
        raise SystemExit(f"未找到备份目录: {DEFAULT_BACKUP_ROOT}/step55a_*")
    return candidates[-1]


def _backup_trace_dirs(backup_dir: Path) -> dict[Path, Path]:
    """Trace 目录 → 备份内对应目录。"""
    return {
        TRACE_DIRS[0]: backup_dir / "traces" / "traces",
        TRACE_DIRS[1]: backup_dir / "traces" / "evaluation_traces",
    }


def _describe(path: Path) -> dict[str, Any]:
    """读取污染文件的轻量元数据，写入 manifest 供审计（不修改文件）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 坏文件也照常隔离
        return {"task_id": None, "status": None, "source_class": "UNKNOWN", "note": "不可解析"}
    metadata = data.get("metadata") or {}
    return {
        "task_id": data.get("task_id"),
        "status": data.get("status"),
        "data_source": metadata.get("data_source"),
        "source_class": classify_metadata(metadata)["source_class"],
        "task": (data.get("task") or "")[:80],
    }


def build_plan(backup_dir: Path, keep: set[str] | None = None) -> list[dict[str, Any]]:
    """keep 中的 trace_id（无需 .json 后缀）视为有意保留的证据文件，不进入隔离计划。"""
    keep = keep or set()
    plan: list[dict[str, Any]] = []
    for current_dir, backup_dir_for_traces in _backup_trace_dirs(backup_dir).items():
        if not current_dir.exists():
            continue
        baseline = {p.name for p in backup_dir_for_traces.glob("*.json")}
        for path in sorted(current_dir.glob("*.json")):
            if path.name in baseline or path.stem in keep:
                continue
            plan.append(
                {
                    "source": str(path.relative_to(PROJECT_ROOT)),
                    "target_rel": str(path.relative_to(PROJECT_ROOT / "data")),
                    "size": path.stat().st_size,
                    **{"meta": _describe(path)},
                }
            )
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description="隔离测试写入生产 Trace 目录的文件")
    parser.add_argument("--apply", action="store_true", help="实际移动文件（默认只预览）")
    parser.add_argument("--backup-dir", type=Path, default=None, help="基线备份目录")
    parser.add_argument("--quarantine-dir", type=Path, default=None, help="隔离输出目录")
    parser.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar="TRACE_ID",
        help="有意保留的证据 Trace ID（可重复），不会被隔离",
    )
    args = parser.parse_args()

    backup_dir = args.backup_dir or _latest_backup()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    quarantine_dir = args.quarantine_dir or (DEFAULT_QUARANTINE_ROOT / f"step55a_{stamp}")

    plan = build_plan(backup_dir, keep=set(args.keep))
    by_source_class: dict[str, int] = {}
    for item in plan:
        cls = item["meta"]["source_class"]
        by_source_class[cls] = by_source_class.get(cls, 0) + 1
    print(f"基线备份: {backup_dir.relative_to(PROJECT_ROOT).as_posix()}")
    print(f"待隔离 Trace: {len(plan)} 个（数据来源分类: {by_source_class}）")
    if not args.apply:
        print("DRY-RUN：未移动任何文件；加 --apply 执行")
        return 0
    if not plan:
        print("无需隔离")
        return 0

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "test_pollution_quarantine",
        "backup_baseline": backup_dir.relative_to(PROJECT_ROOT).as_posix(),
        "quarantine_dir": quarantine_dir.relative_to(PROJECT_ROOT).as_posix(),
        "count": len(plan),
        "by_source_class": by_source_class,
        "note": "测试运行写入生产 Trace 目录的文件；移动而非删除，可按 source 恢复",
        "files": plan,
    }
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for item in plan:
        source = PROJECT_ROOT / item["source"]
        target = quarantine_dir / item["target_rel"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        moved += 1
    manifest_path = quarantine_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已隔离 {moved} 个文件 → {quarantine_dir.relative_to(PROJECT_ROOT).as_posix()}")
    print(f"manifest: {manifest_path.relative_to(PROJECT_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
