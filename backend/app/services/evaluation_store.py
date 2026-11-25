"""Evaluation Run 登记与并发控制（第 36 步）。

为 Evaluation Center 提供异步批量评测：
- 提交评测运行 → 立即返回 run_id，后台执行；
- 按 run_id 查询状态（running / succeeded / failed）与完整报告；
- 并发上限、执行超时、重复提交复用、内存上限；
- 只允许受控数据集（白名单 dataset_id），不接受任意文件路径。

安全：结果仅驻留内存，不写入 data/traces；引擎保证不调用付费 API、
不执行任意代码、不访问任意路径。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from backend.app.services.evaluation_engine import (
    DATASET_DIR,
    EvaluationError,
    load_dataset,
    run_evaluation,
)
from backend.app.services.llm_judge import (
    DEFAULT_RUBRIC_ID,
    RealJudgeDisabledError,
    build_judge,
)
from backend.app.services.execution_config import get_execution_limits

_EXEC_LIMITS = get_execution_limits()

__all__ = [
    "EvaluationBusyError",
    "EvaluationNotFoundError",
    "EvaluationStore",
    "get_evaluation_store",
    "ALLOWED_DATASETS",
]

MAX_CONCURRENT_RUNS = 1          # 并发上限（评测为 CPU 密集型批处理）
MAX_RUN_SECONDS = _EXEC_LIMITS.evaluation_max_run_seconds  # 单次评测执行上限（统一配置）
DEDUP_WINDOW_SECONDS = 60.0      # 重复提交抑制窗口
MAX_STORED_RUNS = 20             # 内存保留上限
PERSIST_ENV_FLAG = "TRACEPILOT_PERSIST_EVAL_RUNS"
RUNS_DIR = Path(__file__).resolve().parents[3] / "data" / "evaluation" / "runs"

# 受控数据集白名单：dataset_id -> 文件名（仅允许目录内的固定文件）
ALLOWED_DATASETS: dict[str, str] = {
    "tracepilot-eval-v1": "dataset_v1.json",
    # 归因模块使用的受控失败数据集（synthetic，案例被设计为可预期失败）
    "tracepilot-fault-v1": "dataset_fault_v1.json",
}

# 允许的 Judge 模式：none（默认，不启用）/ mock（规则化模拟，零成本）
# deepseek 需显式配置授权，绝不由本层自动启用
ALLOWED_JUDGE_MODES = ("none", "mock", "deepseek")


class EvaluationBusyError(RuntimeError):
    """并发评测数已达上限。"""


class EvaluationNotFoundError(KeyError):
    """run_id 不存在。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(
    dataset_id: str, max_cases: int | None, judge_mode: str = "none", judge_rubric_id: str = ""
) -> str:
    payload = json.dumps(
        {"d": dataset_id, "m": max_cases, "j": judge_mode, "r": judge_rubric_id},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EvaluationStore:
    """内存评测运行登记表 + 有界执行器。"""

    def __init__(
        self,
        max_concurrent: int = MAX_CONCURRENT_RUNS,
        timeout_seconds: float = MAX_RUN_SECONDS,
        dedup_window: float = DEDUP_WINDOW_SECONDS,
        max_stored: int = MAX_STORED_RUNS,
    ) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._running = 0
        self._max_concurrent = max_concurrent
        self._timeout = timeout_seconds
        self._dedup_window = dedup_window
        self._max_stored = max_stored
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent, thread_name_prefix="eval-run"
        )
        self._load_persisted()

    # ------------------------------------------------------------------
    def _persistence_enabled(self) -> bool:
        return os.environ.get(PERSIST_ENV_FLAG, "").strip().lower() in {"1", "true", "yes"}

    def _persist(self, record: dict[str, Any]) -> None:
        if not self._persistence_enabled():
            return
        try:
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            target = RUNS_DIR / f"{record['run_id']}.json"
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            tmp.replace(target)
        except Exception:  # noqa: BLE001
            pass

    def _load_persisted(self) -> None:
        if not self._persistence_enabled() or not RUNS_DIR.exists():
            return
        try:
            files = sorted(RUNS_DIR.glob("eval-*.json"), key=lambda f: f.stat().st_mtime)
        except OSError:
            return
        for path in files[-self._max_stored:]:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                run_id = str(record["run_id"])
            except Exception:  # noqa: BLE001
                continue
            if run_id in self._runs:
                continue
            record.setdefault("created_ts", time.time())
            self._runs[run_id] = record
            self._order.append(run_id)
    def _evict_if_needed(self) -> None:
        while len(self._order) > self._max_stored:
            oldest = self._order.pop(0)
            self._runs.pop(oldest, None)

    def _find_duplicate(self, fp: str) -> str | None:
        now = time.time()
        for rid, run in self._runs.items():
            if run.get("fingerprint") != fp:
                continue
            if now - run.get("created_ts", 0) > self._dedup_window:
                continue
            if run.get("status") in {"running", "succeeded"}:
                return rid
        return None

    # ------------------------------------------------------------------
    def submit(
        self,
        dataset_id: str,
        max_cases: int | None,
        judge_mode: str = "none",
        judge_rubric_id: str = DEFAULT_RUBRIC_ID,
        *,
        dedup: bool = True,
    ) -> dict[str, Any]:
        """登记并异步执行一次评测。

        Args:
            dedup: 是否启用重复提交抑制。版本对比需要"同版本的两次独立运行"，
                此时传 False 强制创建新运行（第 49 步新增）。

        Raises:
            EvaluationError: dataset_id 不在受控白名单 / judge_mode 非法。
            EvaluationBusyError: 并发已达上限。
        """
        if dataset_id not in ALLOWED_DATASETS:
            raise EvaluationError(
                f"不支持的评测集: {dataset_id!r}（仅允许: {', '.join(sorted(ALLOWED_DATASETS))}）"
            )
        if judge_mode not in ALLOWED_JUDGE_MODES:
            raise EvaluationError(
                f"不支持的 judge_mode: {judge_mode!r}"
                f"（仅允许: {', '.join(ALLOWED_JUDGE_MODES)}）"
            )
        fp = _fingerprint(dataset_id, max_cases, judge_mode, judge_rubric_id)

        with self._lock:
            dup = self._find_duplicate(fp) if dedup else None
            if dup is not None:
                return dict(self._runs[dup])
            if self._running >= self._max_concurrent:
                raise EvaluationBusyError(
                    f"并发评测运行数已达上限（{self._max_concurrent}），请稍后重试"
                )

            run_id = f"eval-{uuid.uuid4().hex}"
            record: dict[str, Any] = {
                "run_id": run_id,
                "status": "running",
                "created_at": _now_iso(),
                "created_ts": time.time(),
                "dataset_id": dataset_id,
                "max_cases": max_cases,
                "judge_mode": judge_mode,
                "judge_rubric_id": judge_rubric_id,
                "fingerprint": fp,
                "report": None,
                "error": None,
            }
            self._runs[run_id] = record
            self._order.append(run_id)
            self._running += 1
            self._evict_if_needed()

            self._executor.submit(
                self._execute, run_id, dataset_id, max_cases, judge_mode, judge_rubric_id
            )
            return dict(record)

    def _execute(
        self,
        run_id: str,
        dataset_id: str,
        max_cases: int | None,
        judge_mode: str = "none",
        judge_rubric_id: str = DEFAULT_RUBRIC_ID,
    ) -> None:
        started = time.monotonic()
        try:
            # 仅从受控目录内的白名单文件加载，绝不接受客户端传入路径
            path = DATASET_DIR / ALLOWED_DATASETS[dataset_id]
            box: dict[str, Any] = {}

            def target() -> None:
                try:
                    dataset = load_dataset(path)
                    judge = None
                    if judge_mode != "none":
                        # deepseek 模式要求显式授权；未授权时 build_judge 抛
                        # RealJudgeDisabledError，绝不会产生付费调用
                        judge = build_judge(judge_mode)
                    box["value"] = run_evaluation(
                        dataset,
                        max_cases=max_cases,
                        run_id=run_id,
                        persist_traces=True,  # 供前端「案例 → Trace → DAG」下钻
                        judge=judge,
                        judge_rubric_id=judge_rubric_id,
                    )
                except BaseException as exc:  # 交由调用方判定
                    box["error"] = exc

            t = threading.Thread(target=target, daemon=True)
            t.start()
            t.join(self._timeout)
            if t.is_alive():
                with self._lock:
                    self._runs[run_id].update(
                        status="failed", error=f"评测执行超时（>{self._timeout:g}s）"
                    )
                    snapshot = dict(self._runs[run_id])
                self._persist(snapshot)
                return
            if "error" in box:
                exc = box["error"]
                with self._lock:
                    self._runs[run_id].update(
                        status="failed", error=f"{type(exc).__name__}: {exc}"
                    )
                    snapshot = dict(self._runs[run_id])
                self._persist(snapshot)
                return

            with self._lock:
                self._runs[run_id].update(
                    status="succeeded",
                    report=box["value"],
                    error=None,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                snapshot = dict(self._runs[run_id])
            self._persist(snapshot)
        finally:
            with self._lock:
                self._running = max(0, self._running - 1)

    # ------------------------------------------------------------------
    # 外部已完成报告登记（第 52 步 Pipeline 复用）
    # ------------------------------------------------------------------
    def register_report(
        self,
        dataset_id: str,
        report: dict[str, Any],
        *,
        judge_mode: str = "none",
        judge_rubric_id: str = DEFAULT_RUBRIC_ID,
        max_cases: int | None = None,
    ) -> dict[str, Any]:
        """登记一份已在当前进程内完成的历史评测报告（**不重新执行评测**）。

        供 Evaluation Pipeline 复用：Pipeline 分阶段执行 Agent / Trace 采集 /
        确定性评测后，把真实产生的报告登记进运行表，使版本对比、回归检测、
        质量门禁等下游模块可以像普通评测运行一样按 run_id 读取。

        Raises:
            EvaluationError: 数据集不在白名单 / 报告缺少 run_id。
        """
        if dataset_id not in ALLOWED_DATASETS:
            raise EvaluationError(
                f"不支持的评测集: {dataset_id!r}（仅允许: {', '.join(sorted(ALLOWED_DATASETS))}）"
            )
        if judge_mode not in ALLOWED_JUDGE_MODES:
            raise EvaluationError(f"不支持的 judge_mode: {judge_mode!r}")
        run_id = report.get("evaluation_run_id")
        if not isinstance(run_id, str) or not run_id:
            raise EvaluationError("报告缺少 evaluation_run_id，无法登记")

        record: dict[str, Any] = {
            "run_id": run_id,
            "status": "succeeded",
            "created_at": _now_iso(),
            "created_ts": time.time(),
            "dataset_id": dataset_id,
            "max_cases": max_cases,
            "judge_mode": judge_mode,
            "judge_rubric_id": judge_rubric_id,
            "fingerprint": f"pipeline-registered-{run_id}",
            "report": report,
            "error": None,
            "elapsed_ms": 0,
            "registered_by": "evaluation_pipeline",
        }
        with self._lock:
            self._runs[run_id] = record
            if run_id not in self._order:
                self._order.append(run_id)
            self._evict_if_needed()
        self._persist(record)
        return dict(record)

    def update_report(self, run_id: str, report: dict[str, Any]) -> dict[str, Any]:
        """更新已登记运行的报告（用于 Judge 等可选阶段补充结果）。"""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise EvaluationNotFoundError(f"评测运行不存在: {run_id}")
            run["report"] = report
            run["status"] = "succeeded"
            snapshot = dict(run)
        self._persist(snapshot)
        return dict(run)

    # ------------------------------------------------------------------
    def get(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise EvaluationNotFoundError(f"评测运行不存在: {run_id}")
            return dict(run)

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "run_id": r["run_id"],
                    "status": r["status"],
                    "dataset_id": r["dataset_id"],
                    "judge_mode": r.get("judge_mode", "none"),
                    "created_at": r["created_at"],
                }
                for r in self._runs.values()
            ]


_store: EvaluationStore | None = None
_store_lock = threading.Lock()


def get_evaluation_store() -> EvaluationStore:
    """获取全局评测运行登记表（惰性单例）。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = EvaluationStore()
    return _store
