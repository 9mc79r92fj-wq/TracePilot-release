"""术语防雷检查脚本的测试（同时保证仓库当前无禁用术语）。

测试文件本身不出现禁用词明文，避免"检查器把测试自己拦住"的循环依赖：
所有样例文本都由片段拼接生成。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import check_terminology as ct  # noqa: E402

RE_ACT = "Re" + "Act"
SANDBOX_CN = "沙" + "箱"
HARNESS = "Har" + "ness"
CONTROLLED = "controlled_" + "sand" + "box"


def test_repository_is_clean() -> None:
    """当前仓库（排除规则允许的规范文件）不得包含禁用术语。"""
    violations = ct.scan(ROOT)
    assert violations == [], [v.to_dict() for v in violations][:10]


def test_detects_all_rule_terms(tmp_path: Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text(
        f"{RE_ACT} 风格\n{SANDBOX_CN}环境\n{HARNESS} 工具\n{CONTROLLED}\n",
        encoding="utf-8",
    )
    violations = ct.scan(tmp_path)
    found = {v.term for v in violations}
    assert RE_ACT in found
    assert SANDBOX_CN in found
    assert HARNESS in found
    assert CONTROLLED in found


def test_react_framework_is_not_flagged(tmp_path: Path) -> None:
    """前端框架 React 与禁用缩写仅差大小写，检查器不能把 React 一起误伤。"""
    target = tmp_path / "frontend.tsx"
    target.write_text(
        "import React from 'react'\n"
        "// React 19 + react-router\n"
        "const node = <div>react-hooks</div>\n",
        encoding="utf-8",
    )
    assert ct.scan(tmp_path) == []


def test_allowlisted_terminology_doc_is_ignored(tmp_path: Path) -> None:
    doc = tmp_path / "docs" / "TERMINOLOGY.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(f"{RE_ACT} / {SANDBOX_CN} / {HARNESS}\n", encoding="utf-8")
    checker = tmp_path / "scripts" / "check_terminology.py"
    checker.parent.mkdir(parents=True)
    checker.write_text(f"{RE_ACT}\n", encoding="utf-8")
    assert ct.scan(tmp_path) == []


def test_skips_runtime_and_backup_dirs(tmp_path: Path) -> None:
    for rel in (
        "step59_backup_20260101/file.md",
        "data/traces/trace-x.json",
        "node_modules/pkg/readme.md",
        "frontend/dist/assets/app.js",
    ):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{SANDBOX_CN}\n", encoding="utf-8")
    assert ct.scan(tmp_path) == []


def test_cli_exit_codes(tmp_path: Path, capsys) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("Plan-and-Execute\n", encoding="utf-8")
    assert ct.main(["--root", str(tmp_path)]) == 0
    assert "通过" in capsys.readouterr().out

    dirty = tmp_path / "dirty.md"
    dirty.write_text(f"{HARNESS}\n", encoding="utf-8")
    assert ct.main(["--root", str(tmp_path), "--json"]) == 1
    assert '"ok": false' in capsys.readouterr().out.replace("False", "false").replace("false", "false")


def test_rules_have_suggestions() -> None:
    for rule in ct.RULES:
        assert rule.suggestion
        assert rule.pattern.pattern
