"""反事实实验登记与并发控制（第 32 步）。

为 Counterfactual Lab 提供：
- 实验登记表（内存存储）：按 experiment_id 查询状态与结果；
- 并发上限：通过固定大小线程池限制同时执行的实验数，超出返回 429；
- 执行超时：单次实验最长执行时间，超时标记 failed；
- 重复提交抑制：同一 (trace_id, intervention, scenario, replays) 指纹在
  时间窗口内重复提交时直接复用已有实验，避免重复消耗；
- 敏感信息不外泄：只保存实验报告本身（引擎已保证不含密钥）。

注意：实验结果仅驻留内存，不写入 data/traces，不覆盖原始 Trace；
进程重启后登记表清空（属于受控开发环境的已知限制）。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeout

from backend.app.schemas.trace import TraceRecord
from backend.app.services.counterfactual_engine import run_counterfactual_experiment

__all__ = [
    "ExperimentBusyError",
    "ExperimentNotFoundError",
    "ExperimentStore",
    "get_store",
]

# ---- 有界参数 ----
MAX_CONCURRENT_EXPERIMENTS = 2      # 并发上限
MAX_EXPERIMENT_SECONDS = 20.0       # 单次实验执行上限
DEDUP_WINDOW_SECONDS = 60.0         # 重复提交抑制窗口
MAX_STORED_EXPERIMENTS = 50         # 内存保留上限（防无界增长）


class ExperimentBusyError(RuntimeError):
    """并发实验数已达上限。"""


class ExperimentNotFoundError(KeyError):
    """experiment_id 不存在。"""


def _fingerprint(trace_id: str, intervention: dict, scenario: str | None, replays: int) -> str:
    payload = json.dumps(
        {"t": trace_id, "i": intervention, "s": scenario, "r": replays},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ExperimentStore:
    """内存实验登记表 + 有界执行器。"""

    def __init__(
        self,
        max_concurrent: int = MAX_CONCURRENT_EXPERIMENTS,
        timeout_seconds: float = MAX_EXPERIMENT_SECONDS,
        dedup_window: float = DEDUP_WINDOW_SECONDS,
        max_stored: int = MAX_STORED_EXPERIMENTS,
    ) -> None:
        self._lock = threading.Lock()
        self._experiments: dict[str, dict] = {}
        self._order: list[str] = []
        self._futures: dict[str, Future] = {}
        self._running = 0
        self._max_concurrent = max_concurrent
        self._timeout = timeout_seconds
        self._dedup_window = dedup_window
        self._max_stored = max_stored
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent, thread_name_prefix="cf-experiment"
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _evict_if_needed(self) -> None:
        while len(self._order) > self._max_stored:
            oldest = self._order.pop(0)
            self._experiments.pop(oldest, None)
            self._futures.pop(oldest, None)

    def _find_duplicate(self, fingerprint: str) -> str | None:
        now = time.time()
        for exp_id, exp in self._experiments.items():
            if exp.get("fingerprint") != fingerprint:
                continue
            if now - exp.get("created_ts", 0) > self._dedup_window:
                continue
            if exp.get("status") in {"running", "succeeded"}:
                return exp_id
        return None

    # ------------------------------------------------------------------
    # 创建实验
    # ------------------------------------------------------------------

    def submit(
        self,
        trace: TraceRecord,
        intervention: dict,
        scenario: str | None,
        replays: int,
    ) -> dict:
        """登记并异步执行一次实验，返回实验登记记录。

        Raises:
            ExperimentBusyError: 并发已达上限。
        """
        fingerprint = _fingerprint(trace.trace_id, intervention, scenario, replays)

        with self._lock:
            dup = self._find_duplicate(fingerprint)
            if dup is not None:
                return dict(self._experiments[dup])

            if self._running >= self._max_concurrent:
                raise ExperimentBusyError(
                    f"并发实验数已达上限（{self._max_concurrent}），请稍后重试"
                )

            experiment_id = f"exp-{uuid.uuid4().hex}"
            record = {
                "experiment_id": experiment_id,
                "status": "running",
                "created_at": _now_iso(),
                "created_ts": time.time(),
                "source_trace_id": trace.trace_id,
                "intervention": intervention,
                "scenario": scenario,
                "replays": replays,
                "result": None,
                "error": None,
                "fingerprint": fingerprint,
                "deduplicated": False,
            }
            self._experiments[experiment_id] = record
            self._order.append(experiment_id)
            self._running += 1
            self._evict_if_needed()

            future = self._executor.submit(
                self._execute, experiment_id, trace, intervention, scenario, replays
            )
            self._futures[experiment_id] = future
            return dict(record)

    def _execute(
        self,
        experiment_id: str,
        trace: TraceRecord,
        intervention: dict,
        scenario: str | None,
        replays: int,
    ) -> None:
        started = time.monotonic()
        try:
            try:
                future_result = _run_with_timeout(
                    trace, intervention, scenario, replays, self._timeout,
                    experiment_id=experiment_id,
                )
            except FuturesTimeout:
                with self._lock:
                    self._experiments[experiment_id].update(
                        status="failed",
                        error=f"实验执行超时（>{self._timeout:g}s）",
                    )
                return
            except Exception as exc:  # 引擎拒绝 / 执行异常
                with self._lock:
                    self._experiments[experiment_id].update(
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                return

            with self._lock:
                self._experiments[experiment_id].update(
                    status="succeeded",
                    result=future_result,
                    error=None,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
        finally:
            with self._lock:
                self._running = max(0, self._running - 1)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, experiment_id: str) -> dict:
        with self._lock:
            exp = self._experiments.get(experiment_id)
            if exp is None:
                raise ExperimentNotFoundError(f"实验不存在: {experiment_id}")
            return dict(exp)

    def get_results(self, experiment_id: str) -> dict:
        exp = self.get(experiment_id)
        if exp["status"] == "running":
            return {"experiment_id": experiment_id, "status": "running", "result": None}
        if exp["status"] == "failed":
            return {
                "experiment_id": experiment_id,
                "status": "failed",
                "error": exp["error"],
                "result": None,
            }
        return {
            "experiment_id": experiment_id,
            "status": "succeeded",
            "result": exp["result"],
        }

    def list_all(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "experiment_id": e["experiment_id"],
                    "status": e["status"],
                    "source_trace_id": e["source_trace_id"],
                    "intervention": e["intervention"],
                    "created_at": e["created_at"],
                }
                for e in self._experiments.values()
            ]


def _run_with_timeout(
    trace: TraceRecord,
    intervention: dict,
    scenario: str | None,
    replays: int,
    timeout: float,
    experiment_id: str | None = None,
) -> dict:
    """在独立线程中执行引擎并施加超时上限（引擎本身为纯计算，无 IO）。"""
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = run_counterfactual_experiment(
                trace,
                intervention,
                scenario=scenario,
                replays=replays,
                experiment_id=experiment_id,
            )
        except BaseException as exc:  # 交由调用方判定
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise FuturesTimeout()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


_store: ExperimentStore | None = None
_store_lock = threading.Lock()


def get_store() -> ExperimentStore:
    """获取全局实验登记表（惰性单例）。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ExperimentStore()
    return _store
