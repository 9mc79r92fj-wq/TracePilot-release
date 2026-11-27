"""测试专用历史 fixture Trace（不属于生产展示数据）。

这些 Trace 是早期步骤留下的固定实验样本，被多个测试用于验证对比、传播、
反事实、回放、Token 统计与能力解释。它们保存在 ``tests/fixtures/traces``，
只在本测试会话内复制到 ``data/traces``，会话结束后移除，避免污染正式数据。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import UUID

from backend.app.schemas.trace import TraceRecord  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_TRACES = Path(__file__).resolve().parent / "fixtures" / "traces"
PRODUCTION_TRACES = PROJECT_ROOT / "data" / "traces"

STEP21_NORMAL_ID = "trace-bf1f2e2690f743379fadc80d22e03adf"
STEP21_FAULT_ID = "trace-3071fea861694ee1b70731a400f28000"
REAL_API_TRACE_ID = "trace-4d3d82dc7a67496dbc2842cd3b31aa47"

FIXTURE_IDS = (STEP21_NORMAL_ID, STEP21_FAULT_ID, REAL_API_TRACE_ID)


def load_fixture_trace(trace_id: str) -> TraceRecord:
    """从 tests/fixtures 读取固定 Trace（绕过生产数据目录）。"""
    path = FIXTURE_TRACES / f"{trace_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"缺少测试 fixture Trace: {path}")
    return TraceRecord.model_validate_json(path.read_text(encoding="utf-8"))


def fixture_trace_id(value: str | UUID) -> str:
    return str(value)


def install_fixture_traces() -> list[Path]:
    """把历史 fixture Trace 临时复制到生产 Trace 目录，供 API 测试读取。"""
    PRODUCTION_TRACES.mkdir(parents=True, exist_ok=True)
    installed: list[Path] = []
    for trace_id in FIXTURE_IDS:
        source = FIXTURE_TRACES / f"{trace_id}.json"
        target = PRODUCTION_TRACES / f"{trace_id}.json"
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)
            installed.append(target)
    return installed


def remove_fixture_traces(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
