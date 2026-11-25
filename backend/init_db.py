"""TracePilot 数据库初始化入口。

用法：
    python backend/init_db.py               # 使用环境变量 / 默认路径
    python backend/init_db.py --path xx.db  # 指定路径（调试用）

安全：只创建缺失的表（IF NOT EXISTS），不删除、不覆盖任何已有数据，可重复执行。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.database import init_database, list_tables  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="init_db",
        description="初始化 TracePilot SQLite 数据库（只建缺失的表，不覆盖数据）",
    )
    parser.add_argument("--path", default=None, help="数据库路径（默认读 TRACEPILOT_DB_PATH / 内置默认）")
    args = parser.parse_args(argv)

    db_path = init_database(args.path)
    tables = list_tables(db_path)
    print(f"数据库就绪: {db_path}")
    print(f"业务表: {tables}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
