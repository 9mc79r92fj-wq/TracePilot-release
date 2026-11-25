"""Trace 列表缓存测试（性能修复：数千 Trace 时避免每次全量解析）。

验证：
- 目录签名未变化时复用解析结果（不重复解析 JSON）
- 新增 / 删除 Trace 文件会自动使缓存失效
- `clear_trace_list_cache()` 可强制重新解析
- 损坏文件仍被跳过，不影响整体列表
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services import trace_store as ts  # noqa: E402

TRACE_DIR = PROJECT_ROOT / "data" / "traces"


def _sample_trace_text() -> str:
    for p in sorted(TRACE_DIR.glob("trace-*.json")):
        return p.read_text(encoding="utf-8")
    pytest.skip("data/traces 中没有可用示例 Trace")


@pytest.fixture()
def counted_loader(monkeypatch):
    calls = {"n": 0}
    original = ts._load_one

    def counted(path):
        calls["n"] += 1
        return original(path)

    monkeypatch.setattr(ts, "_load_one", counted)
    return calls


@pytest.fixture(autouse=True)
def _clean_cache():
    ts.clear_trace_list_cache()
    yield
    ts.clear_trace_list_cache()


def test_cache_reuses_parse_until_directory_changes(tmp_path, counted_loader):
    text = _sample_trace_text()
    (tmp_path / "trace-a.json").write_text(text, encoding="utf-8")

    first = ts.list_traces(tmp_path)
    second = ts.list_traces(tmp_path)
    assert len(first) == len(second) == 1
    assert first[0].trace_id == second[0].trace_id
    assert counted_loader["n"] == 1  # 第二次调用命中缓存，不再解析

    # 摘要与记录共享缓存：不重复解析，也不重复构建
    summaries = ts.list_trace_summaries(tmp_path)
    assert len(summaries) == 1
    assert summaries[0]["trace_id"] == first[0].trace_id
    assert counted_loader["n"] == 1

    # 新增文件 → 签名变化 → 自动增量解析（只解析新文件）
    (tmp_path / "trace-b.json").write_text(text, encoding="utf-8")
    third = ts.list_traces(tmp_path)
    assert len(third) == 2
    assert counted_loader["n"] == 2

    # 删除文件 → 同样失效（删除不需要重新解析剩余文件）
    (tmp_path / "trace-b.json").unlink()
    fourth = ts.list_traces(tmp_path)
    assert len(fourth) == 1
    assert counted_loader["n"] == 2

    # 手动清空缓存 → 强制重新解析
    ts.clear_trace_list_cache()
    fifth = ts.list_traces(tmp_path)
    assert len(fifth) == 1
    assert counted_loader["n"] == 3


def test_corrupt_file_is_skipped_and_cached(tmp_path, counted_loader):
    text = _sample_trace_text()
    (tmp_path / "trace-good.json").write_text(text, encoding="utf-8")
    (tmp_path / "trace-bad.json").write_text("{not-json", encoding="utf-8")

    first = ts.list_traces(tmp_path)
    second = ts.list_traces(tmp_path)
    assert len(first) == len(second) == 1
    assert first[0].trace_id
    assert counted_loader["n"] == 2  # good + corrupt 各尝试解析一次；第二次命中缓存


def test_cache_is_per_directory(tmp_path, counted_loader):
    text = _sample_trace_text()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "trace-a.json").write_text(text, encoding="utf-8")
    (dir_b / "trace-b.json").write_text(text, encoding="utf-8")

    assert len(ts.list_traces(dir_a)) == 1
    assert len(ts.list_traces(dir_b)) == 1
    assert counted_loader["n"] == 2  # 两个目录各自解析一次
    ts.list_traces(dir_a)
    ts.list_traces(dir_b)
    assert counted_loader["n"] == 2  # 均命中缓存
