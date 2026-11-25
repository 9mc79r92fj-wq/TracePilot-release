"""Repair Report 登记与并发控制（第 34 步）。

为 Repair Studio 提供异步修复验证任务：
- 提交候选验证任务 → 返回 report_id，后台执行；
- 按 report_id 查询状态（running / succeeded / failed）与完整报告；
- 并发上限、执行超时、重复提交复用、内存上限。

安全：只保存 MinimalRepair 产出的 RepairReport（引擎已保证不含密钥、
不含任意代码执行能力、不修改原始 Trace）；结果不写入 data/traces。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from typing import Any

from backend.app.schemas.trace import TraceRecord
from backend.app.services.minimal_repair import build_repair_report

__all__ = [
    "RepairBusyError",
    "RepairNotFoundError",
    "RepairStore",
    "get_repair_store",
]

MAX_CONCURRENT_REPORTS = 2      # 并发上限
MAX_REPORT_SECONDS = 60.0       # 单次修复报告生成上限
DEDUP_WINDOW_SECONDS = 60.0     # 重复提交抑制窗口
MAX_STORED_REPORTS = 50         # 内存保留上限


class RepairBusyError(RuntimeError):
    """并发修复验证数已达上限。"""


class RepairNotFoundError(KeyError):
    """report_id 不存在。"""


def _fingerprint(trace_id: str, max_candidates: int, verify: bool, baseline: str | None) -> str:
    payload = json.dumps(
        {"t": trace_id, "m": max_candidates, "v": verify, "b": baseline},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RepairStore:
    """内存修复报告登记表 + 有界执行器。"""

    def __init__(
        self,
        max_concurrent: int = MAX_CONCURRENT_REPORTS,
        timeout_seconds: float = MAX_REPORT_SECONDS,
        dedup_window: float = DEDUP_WINDOW_SECONDS,
        max_stored: int = MAX_STORED_REPORTS,
    ) -> None:
        self._lock = threading.Lock()
        self._reports: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._running = 0
        self._max_concurrent = max_concurrent
        self._timeout = timeout_seconds
        self._dedup_window = dedup_window
        self._max_stored = max_stored
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent, thread_name_prefix="repair-report"
        )

    # ------------------------------------------------------------------
    def _evict_if_needed(self) -> None:
        while len(self._order) > self._max_stored:
            oldest = self._order.pop(0)
            self._reports.pop(oldest, None)

    def _find_duplicate(self, fingerprint: str) -> str | None:
        now = time.time()
        for rid, rep in self._reports.items():
            if rep.get("fingerprint") != fingerprint:
                continue
            if now - rep.get("created_ts", 0) > self._dedup_window:
                continue
            if rep.get("status") in {"running", "succeeded"}:
                return rid
        return None

    # ------------------------------------------------------------------
    def submit(
        self,
        trace: TraceRecord,
        *,
        baseline_trace: TraceRecord | None,
        max_candidates: int,
        verify: bool,
    ) -> dict[str, Any]:
        """登记并异步生成修复报告。

        Raises:
            RepairBusyError: 并发已达上限。
        """
        fp = _fingerprint(
            trace.trace_id, max_candidates, verify,
            baseline_trace.trace_id if baseline_trace else None,
        )
        with self._lock:
            dup = self._find_duplicate(fp)
            if dup is not None:
                return dict(self._reports[dup])
            if self._running >= self._max_concurrent:
                raise RepairBusyError(
                    f"并发修复验证数已达上限（{self._max_concurrent}），请稍后重试"
                )

            report_id = f"report-{uuid.uuid4().hex}"
            record: dict[str, Any] = {
                "report_id": report_id,
                "status": "running",
                "created_at": _now_iso(),
                "created_ts": time.time(),
                "source_trace_id": trace.trace_id,
                "max_candidates": max_candidates,
                "verify": verify,
                "fingerprint": fp,
                "result": None,
                "error": None,
            }
            self._reports[report_id] = record
            self._order.append(report_id)
            self._running += 1
            self._evict_if_needed()

            self._executor.submit(
                self._execute, report_id, trace, baseline_trace, max_candidates, verify
            )
            return dict(record)

    def _execute(
        self,
        report_id: str,
        trace: TraceRecord,
        baseline_trace: TraceRecord | None,
        max_candidates: int,
        verify: bool,
    ) -> None:
        started = time.monotonic()
        try:
            box: dict[str, Any] = {}

            def target() -> None:
                try:
                    box["value"] = build_repair_report(
                        trace,
                        baseline_trace=baseline_trace,
                        max_candidates=max_candidates,
                        verify=verify,
                    )
                except BaseException as exc:  # 交由调用方判定
                    box["error"] = exc

            t = threading.Thread(target=target, daemon=True)
            t.start()
            t.join(self._timeout)
            if t.is_alive():
                with self._lock:
                    self._reports[report_id].update(
                        status="failed",
                        error=f"修复报告生成超时（>{self._timeout:g}s）",
                    )
                return
            if "error" in box:
                exc = box["error"]
                with self._lock:
                    self._reports[report_id].update(
                        status="failed", error=f"{type(exc).__name__}: {exc}"
                    )
                return

            with self._lock:
                self._reports[report_id].update(
                    status="succeeded",
                    result=box["value"],
                    error=None,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
        finally:
            with self._lock:
                self._running = max(0, self._running - 1)

    # ------------------------------------------------------------------
    def get(self, report_id: str) -> dict[str, Any]:
        with self._lock:
            rep = self._reports.get(report_id)
            if rep is None:
                raise RepairNotFoundError(f"修复报告不存在: {report_id}")
            return dict(rep)

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "report_id": r["report_id"],
                    "status": r["status"],
                    "source_trace_id": r["source_trace_id"],
                    "created_at": r["created_at"],
                }
                for r in self._reports.values()
            ]


_store: RepairStore | None = None
_store_lock = threading.Lock()


def get_repair_store() -> RepairStore:
    """获取全局修复报告登记表（惰性单例）。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = RepairStore()
    return _store
