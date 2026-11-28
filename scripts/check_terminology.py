"""术语防雷检查：阻止错误术语重新进入代码库/文档。

背景：部分行业术语在本项目中有严格的准确写法，混用会造成面试/评审时的"术语雷"。
本脚本是 CI 与本地测试共用的唯一检查入口；规则与例外见 ``docs/TERMINOLOGY.md``。

用法::

    python scripts/check_terminology.py                # 扫描整个仓库
    python scripts/check_terminology.py README.md docs # 只扫描指定路径
    python scripts/check_terminology.py --json         # 机器可读输出

退出码：0 = 通过；1 = 发现问题；2 = 路径错误。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".css",
    ".html",
    ".txt",
    ".sh",
    ".example",
    ".sql",
}
SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    "backups",
    "test-results",
    "playwright-report",
    "htmlcov",
    "ci-reports",
    "data",
    ".mypy_cache",
    ".ruff_cache",
}
SKIP_DIR_PREFIXES = ("step59_backup_", "TracePilot-v2-backup")
SKIP_FILE_PATTERNS = (
    "package-lock.json",
    "pnpm-lock.yaml",
    "e2e-results.json",
    "* copy.tsx",
    "*.backup.txt",
)
# 规则定义文件自身必须能写出"禁用词"，因此显式放行（其余文件一律不允许出现）。
ALLOWLIST_FILES = {
    "scripts/check_terminology.py",
    "docs/TERMINOLOGY.md",
}


@dataclass(frozen=True)
class Rule:
    term: str
    pattern: re.Pattern[str]
    suggestion: str
    note: str = ""

    def matches(self, text: str) -> bool:
        return bool(self.pattern.search(text))


RULES: tuple[Rule, ...] = (
    Rule(
        term="ReAct",
        # 只匹配大小写严格的 ReAct；绝不能误伤 React 前端框架（不同术语）。
        pattern=re.compile(r"(?<![A-Za-z])ReAct(?![A-Za-z])"),
        suggestion="Plan-and-Execute",
        note="本项目的 Agent 执行范式是 Plan-and-Execute；React 是前端框架，不受影响",
    ),
    Rule(
        term="沙箱",
        pattern=re.compile(r"沙箱"),
        suggestion="受控执行（工具白名单 + 有界步数）",
    ),
    Rule(
        term="Harness",
        pattern=re.compile(r"(?<![A-Za-z])[Hh]arness(?![A-Za-z])"),
        suggestion="评测流水线（评测编排语境）/ 智能体开发工具（开发工具语境）/ 被测 Agent（运行时语境）",
        note="禁止泛指；按语境选择准确术语，见 docs/TERMINOLOGY.md",
    ),
    Rule(
        term="controlled_sandbox",
        pattern=re.compile(r"controlled_sandbox"),
        suggestion="controlled_execution",
    ),
    Rule(
        term="sandbox",
        pattern=re.compile(r"(?<![A-Za-z])[Ss]andbox(?![A-Za-z])"),
        suggestion="controlled_execution / 受控执行",
    ),
)


@dataclass(frozen=True)
class Violation:
    file: str
    line: int
    term: str
    suggestion: str
    snippet: str

    def to_dict(self) -> dict[str, object]:
        return {
            "file": self.file,
            "line": self.line,
            "term": self.term,
            "suggestion": self.suggestion,
            "snippet": self.snippet,
        }


def _is_skipped_file(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    if rel in ALLOWLIST_FILES:
        return True
    for pattern in SKIP_FILE_PATTERNS:
        if path.match(pattern):
            return True
    return path.suffix.lower() not in DEFAULT_EXTENSIONS


def iter_files(root: Path, paths: list[Path] | None = None) -> list[Path]:
    if paths:
        candidates: list[Path] = []
        for item in paths:
            p = item if item.is_absolute() else root / item
            if p.is_file():
                candidates.append(p)
            elif p.is_dir():
                candidates.extend(p.rglob("*"))
            else:
                raise FileNotFoundError(f"路径不存在：{p}")
        return sorted(
            p
            for p in candidates
            if p.is_file() and not _is_skipped_file(p, root) and not _in_skipped_dir(p, root)
        )
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or _in_skipped_dir(p, root):
            continue
        if _is_skipped_file(p, root):
            continue
        files.append(p)
    return files


def _in_skipped_dir(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    for part in parts[:-1]:
        if part in SKIP_DIR_NAMES:
            return True
        if part.startswith(SKIP_DIR_PREFIXES):
            return True
    return False


def scan(root: Path | None = None, paths: list[Path] | None = None) -> list[Violation]:
    base = (root or PROJECT_ROOT).resolve()
    violations: list[Violation] = []
    for path in iter_files(base, paths):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(base).as_posix()
        for index, line in enumerate(text.splitlines(), 1):
            for rule in RULES:
                if rule.matches(line):
                    violations.append(
                        Violation(
                            file=rel,
                            line=index,
                            term=rule.term,
                            suggestion=rule.suggestion,
                            snippet=line.strip()[:160],
                        )
                    )
    return violations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TracePilot 术语防雷检查")
    parser.add_argument("paths", nargs="*", help="只检查指定文件/目录（默认整个仓库）")
    parser.add_argument("--root", default=None, help="仓库根目录（默认脚本所在仓库）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--quiet", action="store_true", help="只输出结果计数")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root) if args.root else PROJECT_ROOT
    try:
        violations = scan(root, [Path(p) for p in args.paths] or None)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    payload = {
        "ok": not violations,
        "violations": [v.to_dict() for v in violations],
        "checked_root": str(root),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif violations:
        print(f"发现 {len(violations)} 处错误术语：", file=sys.stderr)
        for v in violations:
            print(f"  {v.file}:{v.line}  {v.term} -> {v.suggestion}\n    {v.snippet}", file=sys.stderr)
        print("\n按 docs/TERMINOLOGY.md 修正后重跑；该检查已接入 CI。", file=sys.stderr)
    elif not args.quiet:
        print("术语检查通过：未发现禁用术语。")
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
