"""TracePilot SQLite 数据库初始化（最小实现）。

设计依据（不自行发明表结构）：
- 表结构严格来自 backend/app/schemas/trace.py 中实际定义的数据模型
  （TraceRecord / TraceStep）与 docs/ARCHITECTURE.md 第 7 节的 traces / steps 设计；
- ARCHITECTURE 第 7 节中的 tasks / runs / experiments / reports 表尚无对应
  代码模型，暂不创建，待对应模块开发时补充。

安全保证：
- 全部使用 CREATE TABLE IF NOT EXISTS，只建缺失的表；
- 不执行任何 DROP / DELETE / UPDATE，不删除、不覆盖已有数据；
- 可重复执行（幂等）。

数据库路径解析与 backend/app/tools/basic_tools.py 的 sqlite_query 工具保持一致：
环境变量 TRACEPILOT_DB_PATH 优先；默认 backend/data/tracepilot.db。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from backend.app.schemas.trace import SCHEMA_VERSION

__all__ = ["DEFAULT_DB_PATH", "init_database", "list_tables"]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = PROJECT_ROOT / "backend" / "data" / "tracepilot.db"

_ENV_DB_PATH = "TRACEPILOT_DB_PATH"

# traces：完整 TraceRecord 的文档存储 + 常用查询列（对应 TraceRecord 字段）
_TRACES_DDL = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id       TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    status         TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    schema_version TEXT NOT NULL DEFAULT '%s',
    trace_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_task_id ON traces(task_id);
""" % SCHEMA_VERSION

# steps：TraceStep 索引（对应 TraceStep 字段，parent_step_ids 以 JSON 数组存储）
_STEPS_DDL = """
CREATE TABLE IF NOT EXISTS steps (
    step_id         TEXT NOT NULL,
    trace_id        TEXT NOT NULL,
    run_id          TEXT,
    step_type       TEXT NOT NULL,
    status          TEXT NOT NULL,
    parent_step_ids TEXT NOT NULL DEFAULT '[]',
    error_type      TEXT,
    timestamp       TEXT NOT NULL,
    duration_ms     INTEGER,
    PRIMARY KEY (trace_id, step_id)
);
CREATE INDEX IF NOT EXISTS idx_steps_status ON steps(status);
"""


def _resolve_db_path(db_path: str | Path | None) -> Path:
    """解析数据库路径（与 sqlite_query 工具一致：env 优先，其次参数，最后默认）。"""
    if db_path is None:
        db_path = os.environ.get(_ENV_DB_PATH) or DEFAULT_DB_PATH
    path = Path(db_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def init_database(db_path: str | Path | None = None) -> Path:
    """初始化 SQLite 数据库：只创建缺失的表与索引，绝不删除或覆盖数据。

    Args:
        db_path: 数据库路径；None 时按环境变量 / 默认路径解析。

    Returns:
        实际使用的数据库文件绝对路径。
    """
    path = _resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    try:
        conn.executescript(_TRACES_DDL)
        conn.executescript(_STEPS_DDL)
        conn.commit()
    finally:
        conn.close()
    return path


def list_tables(db_path: str | Path | None = None) -> list[str]:
    """列出数据库中的业务表（只读检查用）。"""
    path = _resolve_db_path(db_path)
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]
