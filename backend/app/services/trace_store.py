"""Trace 文件存储访问（第 24 步）。

为 API 层提供 Trace 的列表 / 加载能力，并做严格的安全校验：
- trace_id 必须是安全文件名（白名单字符，拒绝路径分隔符与 ".."）；
- 禁止客户端传入任意文件路径（仅接受 trace_id，文件路径由服务端解析）；
- 解析后用 resolve() 校验最终路径仍位于 traces 目录内（防路径穿越）。

列表缓存（性能修复）：Trace 文件达到数千条时，每次 `/api/traces` 全量解析需数秒
（实测 4300+ 文件约 4.1s，前端 15s 超时风险）。这里按目录签名
（文件数 + 总大小 + 最大 mtime）做进程内缓存：目录未变化时直接复用解析结果，
新增/删除/覆盖任何 Trace 文件都会使签名变化并自动失效，不牺牲正确性。
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from backend.app.schemas.trace import TraceRecord

__all__ = [
    "TraceNotFoundError",
    "InvalidTraceIdError",
    "list_traces",
    "list_trace_summaries",
    "load_trace",
    "clear_trace_list_cache",
    "DEFAULT_TRACES_DIR",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TRACES_DIR = PROJECT_ROOT / "data" / "traces"
# 评测运行产生的 Trace 存放目录（独立目录，不进入 Trace 列表，仅供按 ID 下钻查看）
EVAL_TRACES_DIR = PROJECT_ROOT / "data" / "evaluation" / "traces"

# 仅允许字母/数字/下划线/连字符/点，首字符为字母或数字；长度上限 128
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class TraceNotFoundError(KeyError):
    """指定 trace_id 的 Trace 不存在。"""


class InvalidTraceIdError(ValueError):
    """trace_id 不合法（含路径分隔符 / 穿越等）。"""


def _validate_trace_id(trace_id: str) -> str:
    if not isinstance(trace_id, str) or not _SAFE_ID.fullmatch(trace_id) or ".." in trace_id:
        raise InvalidTraceIdError(f"非法 trace_id: {trace_id!r}")
    return trace_id


def _traces_dir(traces_dir: str | Path | None) -> Path:
    d = Path(traces_dir) if traces_dir else DEFAULT_TRACES_DIR
    return d.resolve()


def _trace_file(trace_id: str, traces_dir: str | Path | None = None) -> Path:
    tid = _validate_trace_id(trace_id)
    d = _traces_dir(traces_dir)
    path = (d / f"{tid}.json").resolve()
    if path.parent != d:  # 防御：确保解析后仍在本目录
        raise InvalidTraceIdError(f"非法 trace_id: {trace_id!r}")
    return path


def _dir_signature(paths: list[Path]) -> tuple[int, int, int]:
    """目录签名：文件数 + 总字节数 + 最新 mtime（ns）。

    任一 Trace 新增、删除或覆盖都会改变签名；读取 stat 很便宜（不解析 JSON）。
    """
    count = 0
    total_bytes = 0
    latest_mtime_ns = 0
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        count += 1
        total_bytes += st.st_size
        latest_mtime_ns = max(latest_mtime_ns, st.st_mtime_ns)
    return (count, total_bytes, latest_mtime_ns)


def _load_one(path: Path) -> TraceRecord:
    """解析单个 Trace 文件（供增量缓存按文件复用）。"""
    return TraceRecord.model_validate_json(path.read_text(encoding="utf-8"))


def _load_records(files: list[Path]) -> list[TraceRecord]:
    """解析一组 Trace 文件（保留供测试/诊断使用；列表接口走增量 `_load_one`）。"""
    records: list[TraceRecord] = []
    for p in files:
        try:
            records.append(_load_one(p))
        except Exception:
            continue
    return records


# 进程内列表缓存：key = 解析后的目录绝对路径
# value = (目录签名, 记录元组, 摘要元组)
# 摘要一并缓存，避免每次 /api/traces 都为数千条 Trace 重建字典（JSON 序列化前的主要 CPU）。
_LIST_CACHE: dict[
    str, tuple[tuple[int, int, int], tuple[TraceRecord, ...], tuple[dict[str, Any], ...]]
] = {}
# 文件级缓存：目录 → (文件路径 → (mtime_ns/size, 已解析记录))
# 目录签名变化时只解析新增/修改的文件，避免数千条 Trace 全量重复反序列化。
_FILE_CACHE: dict[str, dict[str, tuple[tuple[int, int], TraceRecord]]] = {}
_LIST_CACHE_LOCK = threading.Lock()
# 单飞锁：并发请求同时发现缓存失效时只允许一个线程重解析，其余等待复用。
_PARSE_LOCK = threading.Lock()


def clear_trace_list_cache() -> None:
    """清空 Trace 列表缓存（测试或需要强制重新解析时使用）。"""
    with _LIST_CACHE_LOCK:
        _LIST_CACHE.clear()
        _FILE_CACHE.clear()


def _summarize(record: TraceRecord) -> dict[str, Any]:
    """轻量摘要（与列表接口字段一致；含数据来源分类，供前端筛选/标注）。"""
    from backend.app.services.data_governance import classify_metadata

    meta = record.metadata or {}
    classified = classify_metadata(meta)
    return {
        "trace_id": record.trace_id,
        "task_id": record.task_id,
        "run_id": record.run_id,
        "status": record.status.value,
        "step_count": len(record.steps),
        "fault_types": [
            f.get("fault_type") for f in meta.get("fault_injections", [])
        ],
        "started_at": record.started_at.isoformat(),
        "ended_at": record.ended_at.isoformat() if record.ended_at else None,
        "data_source": meta.get("data_source"),
        "simulated": meta.get("simulated") is True,
        "derivation": meta.get("derivation"),
        "source_class": classified["source_class"],
        "source_reasons": classified["reasons"],
    }


def _list_bundle(
    traces_dir: str | Path | None = None,
) -> tuple[list[TraceRecord], list[dict[str, Any]]]:
    """返回（记录列表, 摘要列表），二者共享同一份目录缓存。"""
    d = _traces_dir(traces_dir)
    if not d.exists():
        return [], []
    cache_key = str(d)

    files = sorted(d.glob("*.json"))
    signature = _dir_signature(files)
    with _LIST_CACHE_LOCK:
        cached = _LIST_CACHE.get(cache_key)
        if cached is not None and cached[0] == signature:
            return list(cached[1]), list(cached[2])

    with _PARSE_LOCK:
        # 等锁期间目录可能已变化：重新扫描与签名，二次检查缓存
        files = sorted(d.glob("*.json"))
        signature = _dir_signature(files)
        with _LIST_CACHE_LOCK:
            cached = _LIST_CACHE.get(cache_key)
            if cached is not None and cached[0] == signature:
                return list(cached[1]), list(cached[2])
            prev = dict(_FILE_CACHE.get(cache_key) or {})
        records: list[TraceRecord] = []
        next_files: dict[str, tuple[tuple[int, int], TraceRecord]] = {}
        for p in files:
            key = str(p)
            try:
                st = p.stat()
            except OSError:
                continue
            stat_key = (st.st_mtime_ns, st.st_size)
            entry = prev.get(key)
            if entry is not None and entry[0] == stat_key:
                record = entry[1]
            else:
                try:
                    record = _load_one(p)
                except Exception:
                    continue
            next_files[key] = (stat_key, record)
            records.append(record)
        summaries = tuple(_summarize(r) for r in records)
        with _LIST_CACHE_LOCK:
            _LIST_CACHE[cache_key] = (signature, tuple(records), summaries)
            _FILE_CACHE[cache_key] = next_files
        return list(records), list(summaries)


def list_traces(traces_dir: str | Path | None = None) -> list[TraceRecord]:
    """列出全部已保存 Trace（按文件名排序）。

    目录签名未变化时复用上次解析结果；文件有任何增删改都会自动重新解析。
    目录签名变化时按文件 mtime/size 只解析新增/变化的 Trace（增量），并用单飞锁
    避免并发页面请求重复全量解析（数千 Trace 时这是主要超时来源）。
    """
    return _list_bundle(traces_dir)[0]


def list_trace_summaries(traces_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """列出全部 Trace 的轻量摘要（与 `list_traces` 共享缓存，避免重复构建字典）。"""
    return _list_bundle(traces_dir)[1]


def load_trace(trace_id: str, traces_dir: str | Path | None = None) -> TraceRecord:
    """按 trace_id 加载并校验单条 Trace；不存在抛 TraceNotFoundError。

    查找顺序：调用方指定目录 → 主 Trace 目录 → 评测 Trace 目录。
    （评测 Trace 不在列表中展示，但可按 ID 下钻查看，保证证据链可追溯。）
    """
    candidates = [traces_dir, DEFAULT_TRACES_DIR, EVAL_TRACES_DIR]
    path: Path | None = None
    for d in candidates:
        try:
            candidate = _trace_file(trace_id, d)
        except InvalidTraceIdError:
            raise
        if candidate.exists():
            path = candidate
            break
    if path is None:
        raise TraceNotFoundError(f"Trace 不存在: {trace_id}")
    return TraceRecord.model_validate_json(path.read_text(encoding="utf-8"))
