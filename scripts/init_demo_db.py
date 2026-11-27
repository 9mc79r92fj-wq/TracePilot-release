"""初始化本地演示用 SQLite 库（sales 表 + 3 行合成数据）。

`composite_sales_report` 等能力工具以只读方式查询根目录 ``tracepilot.db`` 的 ``sales`` 表。
该库是运行期文件（``*.db`` 已被 gitignore），新克隆仓库 / CI 环境需要先执行本脚本。

用法::

    python scripts/init_demo_db.py
    python scripts/init_demo_db.py --path /tmp/demo.db

安全：只创建缺失的表；表已有数据时不覆盖、不删除。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_ROWS = [(1, "手机", 2999.0), (2, "电脑", 6999.0), (3, "耳机", 399.0)]


def init_demo_db(path: Path) -> tuple[Path, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sales (id INTEGER PRIMARY KEY, product TEXT, amount REAL)"
        )
        count = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
        if count == 0:
            conn.executemany("INSERT INTO sales (id, product, amount) VALUES (?, ?, ?)", DEMO_ROWS)
            conn.commit()
            count = len(DEMO_ROWS)
    finally:
        conn.close()
    return path, count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初始化本地演示 sales SQLite 库（幂等）")
    parser.add_argument(
        "--path",
        default=None,
        help="数据库路径；默认取 TRACEPILOT_DB_PATH，否则 <项目根>/tracepilot.db",
    )
    args = parser.parse_args(argv)
    raw = args.path or os.environ.get("TRACEPILOT_DB_PATH") or str(ROOT / "tracepilot.db")
    path, rows = init_demo_db(Path(raw))
    print(f"演示库就绪: {path}（sales 行数: {rows}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
