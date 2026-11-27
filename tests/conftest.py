"""pytest 共用 fixture：为第 49/50 步测试提供真实评测运行。

真实运行由受控确定性执行器完成（零成本、无真实网络、无付费 API），
每个测试模块各自创建，避免跨模块相互依赖（EvaluationStore 只保留最近 20 条）。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval_fixtures import real_run  # noqa: E402
from fixture_data import install_fixture_traces, remove_fixture_traces  # noqa: E402

# 测试会通过生产默认路径写入 Trace（AgentRunner / 评测执行器）；这些目录是
# 产品统计口径的数据源，测试写入会污染统计。会话结束时把「会话内新增」的
# Trace 移到系统临时隔离目录（不删除、可回溯），并还原被测试重写的数据文件。
_TRACE_DIRS = (
    PROJECT_ROOT / "data" / "traces",
    PROJECT_ROOT / "data" / "evaluation" / "traces",
)
_MUTABLE_DATA_FILES = (
    PROJECT_ROOT / "data" / "evaluation" / "redteam" / "redteam_v1.json",
)


@pytest.fixture(scope="session", autouse=True)
def _install_fixture_traces():
    """把历史 fixture Trace 临时放入数据目录，供依赖 API/存储的测试读取。"""
    installed = install_fixture_traces()
    try:
        yield installed
    finally:
        remove_fixture_traces(installed)


@pytest.fixture(scope="session", autouse=True)
def _quarantine_test_data_pollution(_install_fixture_traces):
    """隔离测试对生产数据目录的写入（仅清理会话内新增/重写的文件）。"""
    trace_snapshot = {
        str(directory): {p.name for p in directory.glob("*.json")} if directory.exists() else set()
        for directory in _TRACE_DIRS
    }
    file_snapshot = {
        path: path.read_bytes() if path.exists() else None for path in _MUTABLE_DATA_FILES
    }
    try:
        yield
    finally:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        quarantine_root = Path(tempfile.gettempdir()) / "tracepilot_test_quarantine" / stamp
        moved = 0
        for directory in _TRACE_DIRS:
            if not directory.exists():
                continue
            stale = [
                p
                for p in directory.glob("*.json")
                if p.name not in trace_snapshot.get(str(directory), set())
            ]
            if not stale:
                continue
            target = quarantine_root / directory.relative_to(PROJECT_ROOT)
            target.mkdir(parents=True, exist_ok=True)
            for path in stale:
                shutil.move(str(path), str(target / path.name))
                moved += 1
        restored = 0
        for path, original in file_snapshot.items():
            if original is None:
                continue
            current = path.read_bytes() if path.exists() else b""
            if current != original:
                path.write_bytes(original)
                restored += 1
        if moved or restored:
            print(
                f"\n[pytest] 已隔离测试写入：Trace {moved} 个 → {quarantine_root}；"
                f"还原数据文件 {restored} 个"
            )


@pytest.fixture(scope="session", autouse=True)
def _keep_all_runs_for_test_session():
    """测试会话内扩大 EvaluationStore 的内存保留上限。

    EvaluationStore 生产默认只保留最近 20 条运行（内存上限）；完整测试套件会创建
    几十条运行，若不扩大保留量，早期 fixture 的运行可能被淘汰，导致跨模块用例
    偶发 404（与被测功能无关）。这里只调整测试进程内的保留量，结束后恢复。
    """
    from backend.app.services.evaluation_store import get_evaluation_store

    store = get_evaluation_store()
    original = store._max_stored  # noqa: SLF001 - 仅测试进程内调整
    store._max_stored = 500
    try:
        yield
    finally:
        store._max_stored = original


@pytest.fixture(scope="module")
def real_fault_pair() -> tuple[str, str]:
    """同一版本的两次独立真实运行（tracepilot-fault-v1）。"""
    return real_run("tracepilot-fault-v1"), real_run("tracepilot-fault-v1")


@pytest.fixture(scope="module")
def real_main_run() -> str:
    """主数据集（tracepilot-eval-v1）的一次真实运行，用于不可比场景。"""
    return real_run("tracepilot-eval-v1")
