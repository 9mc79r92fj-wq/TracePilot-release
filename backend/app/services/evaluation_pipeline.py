"""Automated Evaluation Pipeline（第 52 步）：多阶段评测流水线编排。

目标：把已有引擎按依赖关系串成一条可复现、可观测、可取消的流水线，**复用而非重写**
已有能力：

| 阶段 | 复用的既有模块 |
|------|----------------|
| dataset_validation | `evaluation_engine.load_dataset` + 数据集白名单 |
| agent_execution | `evaluation_engine.execute_case`（Mock 受控执行器）/ `agent_runner.run_agent_task`（真实 API，需双重授权） |
| trace_collection | `evaluation_engine.persist_trace` + `trace_store.load_trace` |
| deterministic_evaluation | `evaluation_engine.evaluate_trace` + `build_evaluation_report_from_results` |
| judge_evaluation（可选） | `llm_judge.build_judge`（仅 Mock，零成本） |
| bad_case_attribution（可选） | `bad_case_attribution.attribute_evaluation_run` |
| failure_pattern_mining（可选） | `failure_pattern_mining.analyze_run` |
| version_comparison（可选） | `evaluation_version_comparison.compare_runs` |
| regression_detection（可选） | `regression_detector.detect_regressions` |
| quality_gate | `quality_gate.evaluate_gate` |
| report_generation | 本模块（JSON + Markdown 最终报告） |

设计原则（诚实性优先）：
1. **阶段状态严格区分**：PENDING / RUNNING / SUCCEEDED / FAILED / SKIPPED / BLOCKED / CANCELLED；
   SKIPPED 绝不标记为 SUCCEEDED，依赖失败的下游阶段标记 BLOCKED 而非 SKIPPED。
2. **可复现性显式记录**：pipeline_run_id / dataset_version / agent_version / prompt_version /
   evaluator_version / rubric_version / execution_mode / random_seed / config_fingerprint /
   created_at；缺失字段为 `null` 并写明原因（不伪造）。
3. **数据一致性**：所有下游引用（评测运行 / Agent 运行 / Trace / Bad Case / 归因报告 /
   失败模式报告 / 对比报告 / 回归报告 / 门禁报告）都记录真实 ID；不修改原始 Trace。
4. **运行安全**：默认 Mock 模式；真实 API 需 `confirm_real=True` + 服务端
   `TRACEPILOT_ALLOW_REAL_API=1` 双重授权；公开接口绝不自动触发付费调用；
   并发 / 队列 / 运行时长 / 重试 / 案例数 / 输入长度均有限制。
5. **执行控制**：创建 → 启动 → 状态查询 → 阶段查询 → 报告 → 取消 → 历史；
   失败不自动无限重试（最多 `max_retries` 次，硬上限 2）。
6. 失败必须给出明确状态与错误详情，不允许空白页面。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.app.services.execution_config import get_execution_limits, public_demo_enabled

_EXEC_LIMITS = get_execution_limits()

__all__ = [
    "PipelineError",
    "PipelineConfigError",
    "PipelineNotFoundError",
    "PipelineAuthError",
    "PipelineBusyError",
    "PipelineStateError",
    "PIPELINE_STATUSES",
    "STAGE_STATUSES",
    "STAGES",
    "MAX_CASES",
    "MAX_CONCURRENT_PIPELINES",
    "MAX_QUEUE_LENGTH",
    "MAX_RETRIES_LIMIT",
    "MAX_RUNTIME_SECONDS",
    "PIPELINE_DIR",
    "create_pipeline",
    "start_pipeline",
    "cancel_pipeline",
    "get_pipeline",
    "list_pipelines",
    "get_pipeline_stages",
    "get_pipeline_report",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PIPELINE_DIR = PROJECT_ROOT / "data" / "pipelines" / "reports"
ENGINE_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# 限制（安全 / 资源）
# ---------------------------------------------------------------------------
MAX_CASES = _EXEC_LIMITS.evaluation_max_cases  # 单次流水线最大案例数
MAX_CONCURRENT_PIPELINES = 1      # 同时运行的流水线数上限
MAX_QUEUE_LENGTH = 10             # 等待启动的流水线队列上限
MAX_RUNTIME_SECONDS = _EXEC_LIMITS.pipeline_max_runtime_seconds  # 单次流水线硬上限（秒）
DEFAULT_RUNTIME_SECONDS = _EXEC_LIMITS.pipeline_default_runtime_seconds  # 默认运行上限（秒）
MAX_RETRIES_LIMIT = 2             # 单阶段最大重试次数
MAX_LABEL_LENGTH = 120            # 输入长度上限
MAX_BASELINE_ID_LENGTH = 64

PIPELINE_STATUSES = (
    "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "SKIPPED", "BLOCKED", "CANCELLED",
)
STAGE_STATUSES = (
    "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "SKIPPED", "BLOCKED", "CANCELLED",
)

# 阶段定义（顺序 = 依赖顺序；required=False 表示可选阶段）
STAGES: tuple[dict[str, Any], ...] = (
    {
        "stage_id": "dataset_validation",
        "stage_name": "数据集验证",
        "required": True,
        "depends_on": (),
        "always_run": False,
    },
    {
        "stage_id": "agent_execution",
        "stage_name": "Agent 执行",
        "required": True,
        "depends_on": ("dataset_validation",),
        "always_run": False,
    },
    {
        "stage_id": "trace_collection",
        "stage_name": "Trace 采集",
        "required": True,
        "depends_on": ("agent_execution",),
        "always_run": False,
    },
    {
        "stage_id": "deterministic_evaluation",
        "stage_name": "确定性评测",
        "required": True,
        "depends_on": ("trace_collection",),
        "always_run": False,
    },
    {
        "stage_id": "judge_evaluation",
        "stage_name": "Judge 评测（可选）",
        "required": False,
        "depends_on": ("deterministic_evaluation",),
        "always_run": False,
    },
    {
        "stage_id": "bad_case_attribution",
        "stage_name": "Bad Case 归因（可选）",
        "required": False,
        "depends_on": ("deterministic_evaluation",),
        "always_run": False,
    },
    {
        "stage_id": "failure_pattern_mining",
        "stage_name": "失败模式挖掘（可选）",
        "required": False,
        "depends_on": ("bad_case_attribution",),
        "always_run": False,
    },
    {
        "stage_id": "version_comparison",
        "stage_name": "版本对比（可选）",
        "required": False,
        "depends_on": ("deterministic_evaluation",),
        "always_run": False,
    },
    {
        "stage_id": "regression_detection",
        "stage_name": "回归检测（可选）",
        "required": False,
        "depends_on": ("version_comparison",),
        "always_run": False,
    },
    {
        "stage_id": "quality_gate",
        "stage_name": "质量门禁",
        "required": True,
        "depends_on": ("deterministic_evaluation", "version_comparison", "regression_detection"),
        "always_run": False,
    },
    {
        "stage_id": "report_generation",
        "stage_name": "报告生成",
        "required": True,
        "depends_on": (),
        "always_run": True,   # 发生失败/取消/超时也要生成最终报告
    },
)

_STAGE_BY_ID = {s["stage_id"]: s for s in STAGES}


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class PipelineError(ValueError):
    """流水线通用错误。"""


class PipelineConfigError(PipelineError):
    """流水线配置非法（数据集/参数/可选阶段组合等）。"""


class PipelineNotFoundError(PipelineError):
    """流水线不存在。"""


class PipelineAuthError(PipelineError):
    """真实 API 模式未获授权。"""


class PipelineBusyError(PipelineError):
    """并发/队列超过限制。"""


class PipelineStateError(PipelineError):
    """流水线状态不允许该操作。"""


class _PipelineCancelled(PipelineError):
    """内部：用户取消。"""


class _PipelineTimeout(PipelineError):
    """内部：执行超时。"""


# ---------------------------------------------------------------------------
# 运行登记（内存 + 磁盘快照）
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_PIPELINES: dict[str, dict[str, Any]] = {}
_ORDER: list[str] = []
_RUNNING = 0
_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_PIPELINES, thread_name_prefix="pipeline"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deep_copy(record: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(record, ensure_ascii=False))


def _persist(record: dict[str, Any]) -> dict[str, str]:
    """把流水线快照写入磁盘（页面刷新/历史查询可恢复）。"""
    PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
    path = PIPELINE_DIR / f"{record['pipeline_run_id']}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"json": f"data/pipelines/reports/{record['pipeline_run_id']}.json"}


def _persist_markdown(record: dict[str, Any], markdown: str) -> dict[str, str]:
    PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
    path = PIPELINE_DIR / f"{record['pipeline_run_id']}-report.md"
    path.write_text(markdown, encoding="utf-8")
    return {"markdown": f"data/pipelines/reports/{record['pipeline_run_id']}-report.md"}


def _load_from_disk(pipeline_run_id: str) -> dict[str, Any] | None:
    path = PIPELINE_DIR / f"{pipeline_run_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _get_record(pipeline_run_id: str) -> dict[str, Any]:
    with _LOCK:
        record = _PIPELINES.get(pipeline_run_id)
        if record is None:
            disk = _load_from_disk(pipeline_run_id)
            if disk is None:
                raise PipelineNotFoundError(f"流水线不存在: {pipeline_run_id}")
            # 从磁盘快照恢复后缓存到内存，保证后续状态更新作用于同一对象
            _PIPELINES[pipeline_run_id] = disk
            if pipeline_run_id not in _ORDER:
                _ORDER.append(pipeline_run_id)
            return disk
        return record


def _count_status(status: str) -> int:
    with _LOCK:
        return sum(1 for r in _PIPELINES.values() if r.get("status") == status)


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

def _validate_dataset_id(dataset_id: str) -> str:
    from backend.app.services.evaluation_store import ALLOWED_DATASETS

    if not isinstance(dataset_id, str) or dataset_id not in ALLOWED_DATASETS:
        raise PipelineConfigError(
            f"不支持的数据集: {dataset_id!r}（仅允许: {', '.join(sorted(ALLOWED_DATASETS))}）"
        )
    return dataset_id


def _real_api_allowed(confirm_real: bool) -> bool:
    if public_demo_enabled():
        return False
    flag = os.environ.get("TRACEPILOT_ALLOW_REAL_API", "").strip().lower()
    return bool(confirm_real and flag in {"1", "true", "yes"})


def _config_fingerprint(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "pipe-cfg-" + hashlib.sha256(payload).hexdigest()[:12]


def _new_stage_state(meta: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage_id": meta["stage_id"],
        "stage_name": meta["stage_name"],
        "required": meta["required"],
        "status": "PENDING",
        "started_at": None,
        "finished_at": None,
        "duration_ms": None,
        "input_refs": [],
        "output_refs": [],
        "error_details": None,
        "skip_reason": None,
        "attempts": 0,
        "max_retries": config["max_retries"],
    }


def _build_reproducibility(
    *,
    pipeline_run_id: str,
    config: dict[str, Any],
    config_fingerprint: str,
    created_at: str,
) -> dict[str, Any]:
    from backend.app.services.evaluation_engine import ENGINE_VERSION as EVAL_ENGINE_VERSION

    return {
        "pipeline_run_id": pipeline_run_id,
        "dataset_version": None,
        "agent_version": None,
        "prompt_version": None,
        "evaluator_version": EVAL_ENGINE_VERSION,
        "rubric_version": None,
        "execution_mode": config["execution_mode"],
        "random_seed": config["random_seed"],
        "config_fingerprint": config_fingerprint,
        "created_at": created_at,
        "notes": {
            "dataset_version": "待 dataset_validation 阶段从数据集文件读取",
            "agent_version": "受控执行器无独立 agent_version 字段，未伪造",
            "prompt_version": "仅启用 Mock Judge 时存在 prompt_version；确定性验证器无 prompt",
            "rubric_version": "仅启用 Mock Judge 时存在 Rubric 版本",
            "random_seed": "确定性执行器不依赖随机种子；字段用于记录实验配置，便于复现",
        },
    }


# ---------------------------------------------------------------------------
# 创建 / 启动 / 取消 / 查询
# ---------------------------------------------------------------------------

def create_pipeline(
    *,
    dataset_id: str,
    max_cases: int | None = None,
    execution_mode: str = "mock",
    confirm_real: bool = False,
    judge_mode: str = "none",
    judge_rubric_id: str = "answer_quality",
    baseline_run_id: str | None = None,
    enable_attribution: bool = True,
    enable_failure_patterns: bool = True,
    enable_version_comparison: bool = True,
    enable_regression: bool = True,
    enable_quality_gate: bool = True,
    random_seed: int = 42,
    label: str | None = None,
    max_runtime_seconds: float | None = None,
    max_retries: int = 0,
) -> dict[str, Any]:
    """创建流水线（PENDING 状态，不自动执行）。"""
    dataset_id = _validate_dataset_id(dataset_id)

    if execution_mode not in {"mock", "real_api"}:
        raise PipelineConfigError(
            f"不支持的执行模式: {execution_mode!r}（仅允许 mock / real_api）"
        )
    if execution_mode == "real_api":
        if not _real_api_allowed(confirm_real):
            raise PipelineAuthError(
                "真实 API 模式未获授权：需请求显式 confirm_real=True 且服务端设置 "
                "TRACEPILOT_ALLOW_REAL_API=1；本次未发起任何付费调用"
            )

    if judge_mode not in {"none", "mock"}:
        raise PipelineConfigError(
            f"不支持的 judge_mode: {judge_mode!r}（Pipeline 仅支持 none / mock；"
            "真实 Judge 需单独授权路径，当前未接入）"
        )
    if judge_mode == "mock":
        from backend.app.services.llm_judge import load_rubric

        try:
            load_rubric(judge_rubric_id)
        except Exception as exc:
            raise PipelineConfigError(
                f"Rubric 不可用: {judge_rubric_id!r}（{type(exc).__name__}: {exc}）"
            ) from exc

    if max_cases is not None:
        if not isinstance(max_cases, int) or isinstance(max_cases, bool) or not (
            1 <= max_cases <= MAX_CASES
        ):
            raise PipelineConfigError(f"max_cases 必须为 1..{MAX_CASES} 的整数")

    if not isinstance(random_seed, int) or isinstance(random_seed, bool):
        raise PipelineConfigError("random_seed 必须是整数")
    if not (0 <= random_seed <= 2**31 - 1):
        raise PipelineConfigError("random_seed 必须在 0..2147483647")

    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or not (
        0 <= max_retries <= MAX_RETRIES_LIMIT
    ):
        raise PipelineConfigError(f"max_retries 必须为 0..{MAX_RETRIES_LIMIT} 的整数")

    runtime = DEFAULT_RUNTIME_SECONDS if max_runtime_seconds is None else float(max_runtime_seconds)
    if not (1.0 <= runtime <= MAX_RUNTIME_SECONDS):
        raise PipelineConfigError(
            f"max_runtime_seconds 必须在 1..{MAX_RUNTIME_SECONDS:g} 秒"
        )

    if label is not None and (not isinstance(label, str) or len(label) > MAX_LABEL_LENGTH):
        raise PipelineConfigError(f"label 必须是不超过 {MAX_LABEL_LENGTH} 字符的字符串")

    if baseline_run_id is not None:
        if not isinstance(baseline_run_id, str) or not baseline_run_id:
            raise PipelineConfigError("baseline_run_id 必须是非空字符串")
        if len(baseline_run_id) > MAX_BASELINE_ID_LENGTH:
            raise PipelineConfigError(
                f"baseline_run_id 长度不能超过 {MAX_BASELINE_ID_LENGTH}"
            )
        from backend.app.services.evaluation_store import (
            EvaluationNotFoundError,
            get_evaluation_store,
        )

        try:
            get_evaluation_store().get(baseline_run_id)
        except EvaluationNotFoundError as exc:
            raise PipelineConfigError(
                f"基线运行不存在或已过期（运行记录仅内存保留）: {baseline_run_id}"
            ) from exc

    with _LOCK:
        pending = sum(1 for r in _PIPELINES.values() if r.get("status") == "PENDING")
        if pending >= MAX_QUEUE_LENGTH:
            raise PipelineBusyError(
                f"等待启动的流水线队列已满（{MAX_QUEUE_LENGTH}），请稍后重试"
            )

    pipeline_run_id = f"pipe-{uuid.uuid4().hex}"
    created_at = _now_iso()
    config: dict[str, Any] = {
        "dataset_id": dataset_id,
        "max_cases": max_cases,
        "execution_mode": execution_mode,
        "judge_mode": judge_mode,
        "judge_rubric_id": judge_rubric_id if judge_mode != "none" else None,
        "baseline_run_id": baseline_run_id,
        "enable_attribution": bool(enable_attribution),
        "enable_failure_patterns": bool(enable_failure_patterns),
        "enable_version_comparison": bool(enable_version_comparison),
        "enable_regression": bool(enable_regression),
        "enable_quality_gate": bool(enable_quality_gate),
        "random_seed": random_seed,
        "label": label,
        "max_runtime_seconds": runtime,
        "max_retries": max_retries,
    }
    fingerprint = _config_fingerprint(config)
    record: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "pipeline_run_id": pipeline_run_id,
        "status": "PENDING",
        "created_at": created_at,
        "started_at": None,
        "finished_at": None,
        "duration_ms": None,
        "config": config,
        "reproducibility": _build_reproducibility(
            pipeline_run_id=pipeline_run_id,
            config=config,
            config_fingerprint=fingerprint,
            created_at=created_at,
        ),
        "stages": [_new_stage_state(meta, config) for meta in STAGES],
        "refs": {
            "evaluation_run_id": None,
            "baseline_run_id": baseline_run_id,
            "agent_run_ids": [],
            "trace_ids": [],
            "bad_case_ids": [],
            "attribution_report_ids": [],
            "failure_pattern_report_id": None,
            "comparison_id": None,
            "regression_report_id": None,
            "quality_gate_report_id": None,
        },
        "final_report": None,
        "final_report_paths": None,
        "error": None,
        "cancel_requested": False,
        "limits": {
            "max_cases": MAX_CASES,
            "max_concurrent_pipelines": MAX_CONCURRENT_PIPELINES,
            "max_queue_length": MAX_QUEUE_LENGTH,
            "max_runtime_seconds": MAX_RUNTIME_SECONDS,
            "max_retries_limit": MAX_RETRIES_LIMIT,
            "max_label_length": MAX_LABEL_LENGTH,
        },
        "paid_api_called": False,
        "limitations": [
            "默认 Mock 模式：受控确定性模拟执行器，不调用真实模型，Token 恒为 0",
            "Mock 模式以评测案例为单位执行，不产生独立 Agent Run 记录；real_api 模式会记录 agent_run_ids",
            "评测运行/流水线记录仅内存保留（磁盘仅有快照），重启后部分证据 ID 不可再查询",
            "流水线不修改任何原始 Trace；所有输出引用均为真实产生的工件 ID",
            "SKIPPED 表示该阶段未执行（无基线、无 Bad Case 或未启用），不等于成功",
        ],
    }
    with _LOCK:
        _PIPELINES[pipeline_run_id] = record
        _ORDER.append(pipeline_run_id)
    paths = _persist(record)
    record["snapshot_path"] = paths["json"]
    return _deep_copy(record)


def start_pipeline(pipeline_run_id: str) -> dict[str, Any]:
    """启动流水线（仅 PENDING 可启动）。执行在后台线程中进行。"""
    global _RUNNING
    with _LOCK:
        record = _get_record(pipeline_run_id)
        if record.get("status") != "PENDING":
            raise PipelineStateError(
                f"流水线状态为 {record.get('status')}，只有 PENDING 可以启动"
            )
        if _RUNNING >= MAX_CONCURRENT_PIPELINES:
            raise PipelineBusyError(
                f"并发流水线已达上限（{MAX_CONCURRENT_PIPELINES}），请稍后重试"
            )
        _RUNNING += 1
        record["status"] = "RUNNING"
        record["started_at"] = _now_iso()
        _PIPELINES[pipeline_run_id] = record
    _persist(record)
    _EXECUTOR.submit(_run_pipeline_safe, pipeline_run_id)
    return _deep_copy(record)


def cancel_pipeline(pipeline_run_id: str) -> dict[str, Any]:
    """取消尚未完成的流水线。

    - PENDING：立即取消（所有阶段标记 CANCELLED，记录取消原因）；
    - RUNNING：设置取消标记，执行线程在阶段/案例边界检查后停止；
    - 已结束：返回 409（PipelineStateError）。
    """
    with _LOCK:
        record = _get_record(pipeline_run_id)
        status = record.get("status")
        if status == "PENDING":
            record["cancel_requested"] = True
            record["status"] = "CANCELLED"
            record["finished_at"] = _now_iso()
            record["error"] = {"type": "CANCELLED", "message": "用户在启动前取消"}
            for stage in record["stages"]:
                if stage["status"] == "PENDING":
                    stage["status"] = "CANCELLED"
                    stage["skip_reason"] = "用户取消流水线"
            record["final_report"] = _build_final_report(record, {})
            _persist(record)
            return _deep_copy(record)
        if status == "RUNNING":
            record["cancel_requested"] = True
            _persist(record)
            return _deep_copy(record)
        raise PipelineStateError(f"流水线状态为 {status}，无法取消（已结束）")


def get_pipeline(pipeline_run_id: str) -> dict[str, Any]:
    """查询流水线完整状态。"""
    with _LOCK:
        return _deep_copy(_get_record(pipeline_run_id))


def get_pipeline_stages(pipeline_run_id: str) -> dict[str, Any]:
    """查询阶段状态列表。"""
    record = get_pipeline(pipeline_run_id)
    return {
        "pipeline_run_id": pipeline_run_id,
        "status": record.get("status"),
        "stage_count": len(record.get("stages") or []),
        "stages": record.get("stages") or [],
    }


def get_pipeline_report(pipeline_run_id: str) -> dict[str, Any]:
    """查询流水线最终报告（未完成时返回当前状态与已完成阶段，不伪造结论）。"""
    record = get_pipeline(pipeline_run_id)
    report = record.get("final_report")
    return {
        "pipeline_run_id": pipeline_run_id,
        "status": record.get("status"),
        "report_available": report is not None,
        "report": report,
        "refs": record.get("refs"),
        "reproducibility": record.get("reproducibility"),
        "error": record.get("error"),
        "limitations": record.get("limitations"),
    }


def list_pipelines(limit: int = 20) -> list[dict[str, Any]]:
    """列出历史流水线（内存 + 磁盘快照合并，按创建时间倒序）。"""
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    with _LOCK:
        records = list(_PIPELINES.values())
    for record in records:
        seen.add(record["pipeline_run_id"])
        items.append(_summary(record))
    if PIPELINE_DIR.exists():
        for path in sorted(
            PIPELINE_DIR.glob("pipe-*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            pid = path.stem
            if pid in seen:
                continue
            disk = _load_from_disk(pid)
            if disk:
                items.append(_summary(disk))
            if len(items) >= limit * 2:
                break
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items[:limit]


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    stages = record.get("stages") or []
    return {
        "pipeline_run_id": record.get("pipeline_run_id"),
        "status": record.get("status"),
        "created_at": record.get("created_at"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "duration_ms": record.get("duration_ms"),
        "dataset_id": (record.get("config") or {}).get("dataset_id"),
        "dataset_version": (record.get("reproducibility") or {}).get("dataset_version"),
        "execution_mode": (record.get("config") or {}).get("execution_mode"),
        "label": (record.get("config") or {}).get("label"),
        "stage_count": len(stages),
        "succeeded_stages": sum(1 for s in stages if s.get("status") == "SUCCEEDED"),
        "failed_stages": sum(1 for s in stages if s.get("status") == "FAILED"),
        "skipped_stages": sum(1 for s in stages if s.get("status") == "SKIPPED"),
        "blocked_stages": sum(1 for s in stages if s.get("status") == "BLOCKED"),
        "cancelled_stages": sum(1 for s in stages if s.get("status") == "CANCELLED"),
        "evaluation_run_id": (record.get("refs") or {}).get("evaluation_run_id"),
        "quality_gate_report_id": (record.get("refs") or {}).get("quality_gate_report_id"),
        "quality_gate_status": (record.get("gate_summary") or {}).get("gate_status"),
    }


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------

def _run_pipeline_safe(pipeline_run_id: str) -> None:
    global _RUNNING
    try:
        _execute_pipeline(pipeline_run_id)
    except Exception as exc:  # noqa: BLE001 - 后台线程必须记录而非静默
        with _LOCK:
            record = _PIPELINES.get(pipeline_run_id)
            if record is not None:
                record["status"] = "FAILED"
                record["finished_at"] = _now_iso()
                record["error"] = {
                    "type": type(exc).__name__,
                    "message": f"流水线执行器异常: {exc}",
                }
                _persist(record)
    finally:
        with _LOCK:
            _RUNNING = max(0, _RUNNING - 1)


def _stage_by_id(record: dict[str, Any], stage_id: str) -> dict[str, Any] | None:
    for stage in record.get("stages") or []:
        if stage["stage_id"] == stage_id:
            return stage
    return None


def _stage_status(record: dict[str, Any], stage_id: str) -> str | None:
    stage = _stage_by_id(record, stage_id)
    return stage.get("status") if stage else None


def _check_cancel(record: dict[str, Any]) -> None:
    if record.get("cancel_requested"):
        raise _PipelineCancelled("用户取消流水线")


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise _PipelineTimeout("流水线超出最大运行时间限制")


def _dependency_block_reason(record: dict[str, Any], meta: dict[str, Any]) -> str | None:
    """返回 BLOCKED/SKIPPED 原因；依赖全部正常时返回 None。"""
    for dep in meta.get("depends_on", ()):
        status = _stage_status(record, dep)
        if status == "SUCCEEDED" or status == "SKIPPED":
            # SKIPPED 依赖通常表示"该可选能力未提供"，下游按未执行处理（SKIPPED）
            continue
        if status == "FAILED":
            return f"依赖阶段 {dep} 失败（FAILED）"
        if status == "BLOCKED":
            return f"依赖阶段 {dep} 被阻塞（BLOCKED）"
        if status == "CANCELLED":
            return f"依赖阶段 {dep} 已取消（CANCELLED）"
        return f"依赖阶段 {dep} 未完成（{status}）"
    return None


def _skippable_due_dep(record: dict[str, Any], meta: dict[str, Any]) -> bool:
    """依赖被 SKIPPED ⇒ 当前阶段也 SKIPPED（能力未提供），而非 BLOCKED。"""
    for dep in meta.get("depends_on", ()):
        if _stage_status(record, dep) == "SKIPPED":
            return True
    return False


def _cancel_remaining(record: dict[str, Any], *, reason: str = "用户取消流水线") -> None:
    for meta in STAGES:
        if meta.get("always_run"):
            continue  # 报告生成阶段仍需运行（生成取消/失败的最终报告）
        stage = _stage_by_id(record, meta["stage_id"])
        if stage is not None and stage["status"] == "PENDING":
            stage["status"] = "CANCELLED"
            stage["skip_reason"] = reason
            stage["finished_at"] = _now_iso()
            stage["duration_ms"] = 0


def _block_remaining(record: dict[str, Any], *, reason: str) -> None:
    for meta in STAGES:
        if meta.get("always_run"):
            continue
        stage = _stage_by_id(record, meta["stage_id"])
        if stage is not None and stage["status"] == "PENDING":
            stage["status"] = "BLOCKED"
            stage["skip_reason"] = reason
            stage["finished_at"] = _now_iso()
            stage["duration_ms"] = 0


def _compute_pipeline_status(record: dict[str, Any]) -> str:
    stages = record.get("stages") or []
    statuses = [s.get("status") for s in stages]
    if record.get("cancel_requested") or "CANCELLED" in statuses:
        return "CANCELLED"
    if "FAILED" in statuses:
        return "FAILED"
    if "BLOCKED" in statuses:
        return "BLOCKED"
    return "SUCCEEDED"


def _execute_pipeline(pipeline_run_id: str) -> None:
    with _LOCK:
        record = _get_record(pipeline_run_id)
    deadline = time.monotonic() + float(record["config"]["max_runtime_seconds"])
    ctx: dict[str, Any] = {
        "pipeline_run_id": pipeline_run_id,
        "record": record,
        "deadline": deadline,
        "dataset": None,
        "cases": [],
        "executed_pairs": [],
        "traces": [],
        "execution_errors": [],
        "results": [],
        "bad_cases": [],
        "report": None,
        "real_agent_runs": [],
        "judge_note": None,
        "attribution": None,
        "patterns": None,
        "comparison": None,
        "regression": None,
        "gate": None,
    }

    stage_runner = {
        "dataset_validation": _stage_dataset_validation,
        "agent_execution": _stage_agent_execution,
        "trace_collection": _stage_trace_collection,
        "deterministic_evaluation": _stage_deterministic_evaluation,
        "judge_evaluation": _stage_judge_evaluation,
        "bad_case_attribution": _stage_bad_case_attribution,
        "failure_pattern_mining": _stage_failure_pattern_mining,
        "version_comparison": _stage_version_comparison,
        "regression_detection": _stage_regression_detection,
        "quality_gate": _stage_quality_gate,
        "report_generation": _stage_report_generation,
    }

    for meta in STAGES:
        stage = _stage_by_id(record, meta["stage_id"])
        assert stage is not None

        if stage["status"] in {"CANCELLED", "SKIPPED", "BLOCKED", "SUCCEEDED", "FAILED"}:
            continue

        # 取消/超时在每个阶段边界检查
        if record.get("cancel_requested") and not meta.get("always_run"):
            stage["status"] = "CANCELLED"
            stage["skip_reason"] = "用户取消流水线"
            stage["finished_at"] = _now_iso()
            _cancel_remaining(record)
            with _LOCK:
                _PIPELINES[pipeline_run_id] = record
            _persist(record)
            break

        # 依赖检查（report_generation 为 always_run，不被依赖状态阻塞）
        block_reason = None if meta.get("always_run") else _dependency_block_reason(record, meta)
        if block_reason is not None:
            if _skippable_due_dep(record, meta):
                stage["status"] = "SKIPPED"
                stage["skip_reason"] = "依赖的可选阶段未执行（未启用或无需执行）"
            else:
                stage["status"] = "BLOCKED"
                stage["skip_reason"] = block_reason
                record["error"] = record.get("error") or {
                    "type": "BLOCKED",
                    "stage_id": stage["stage_id"],
                    "message": block_reason,
                }
            stage["finished_at"] = _now_iso()
            with _LOCK:
                _PIPELINES[pipeline_run_id] = record
            _persist(record)
            continue

        # 可选阶段未启用 → SKIPPED（明确原因，绝不标记 SUCCEEDED）
        skip_reason = _optional_skip_reason(record, stage) or _dynamic_skip_reason(
            record, ctx, stage
        )
        if skip_reason is not None:
            stage["status"] = "SKIPPED"
            stage["skip_reason"] = skip_reason
            stage["finished_at"] = _now_iso()
            stage["duration_ms"] = 0
            with _LOCK:
                _PIPELINES[pipeline_run_id] = record
            _persist(record)
            continue

        attempts = 0
        final_outcome = "SUCCEEDED"
        while True:
            attempts += 1
            stage["attempts"] = attempts
            stage["status"] = "RUNNING"
            stage["started_at"] = _now_iso()
            stage["finished_at"] = None
            stage["duration_ms"] = None
            stage["error_details"] = None
            with _LOCK:
                _PIPELINES[pipeline_run_id] = record
            _persist(record)
            stage_started = time.monotonic()
            try:
                _check_cancel(record)
                if not meta.get("always_run"):
                    _check_deadline(deadline)
                outcome = stage_runner[meta["stage_id"]](record, ctx, stage) or {}
                stage["duration_ms"] = int((time.monotonic() - stage_started) * 1000)
                stage["finished_at"] = _now_iso()
                stage["status"] = "SUCCEEDED"
                stage["output_refs"] = list(outcome.get("output_refs") or [])
                if outcome.get("input_refs"):
                    stage["input_refs"] = list(outcome["input_refs"])
                _apply_stage_refs(record, ctx, meta["stage_id"], outcome)
                final_outcome = "SUCCEEDED"
                break
            except _PipelineCancelled as exc:
                stage["duration_ms"] = int((time.monotonic() - stage_started) * 1000)
                stage["finished_at"] = _now_iso()
                stage["status"] = "CANCELLED"
                stage["error_details"] = {"type": "CANCELLED", "message": str(exc)}
                record["cancel_requested"] = True
                _cancel_remaining(record)
                final_outcome = "CANCELLED"
                break
            except _PipelineTimeout as exc:
                stage["duration_ms"] = int((time.monotonic() - stage_started) * 1000)
                stage["finished_at"] = _now_iso()
                stage["status"] = "FAILED"
                stage["error_details"] = {"type": "TIMEOUT", "message": str(exc)}
                record["error"] = {
                    "type": "TIMEOUT",
                    "stage_id": stage["stage_id"],
                    "message": str(exc),
                }
                _block_remaining(record, reason=f"流水线超时（阶段 {stage['stage_id']} 失败）")
                final_outcome = "FAILED"
                break
            except Exception as exc:  # noqa: BLE001 - 阶段失败必须记录明确错误
                stage["duration_ms"] = int((time.monotonic() - stage_started) * 1000)
                stage["finished_at"] = _now_iso()
                stage["error_details"] = {
                    "type": type(exc).__name__,
                    "message": str(exc)[:2000],
                    "attempt": attempts,
                    "max_retries": record["config"]["max_retries"],
                }
                if attempts <= record["config"]["max_retries"]:
                    # 有限重试（不无限重试）：保持 PENDING 等待下一轮尝试
                    stage["status"] = "PENDING"
                    stage["started_at"] = None
                    stage["finished_at"] = None
                    stage["duration_ms"] = None
                    record.setdefault("retries", []).append(
                        {
                            "stage_id": stage["stage_id"],
                            "attempt": attempts,
                            "error": stage["error_details"]["message"],
                            "at": _now_iso(),
                        }
                    )
                    with _LOCK:
                        _PIPELINES[pipeline_run_id] = record
                    _persist(record)
                    continue
                stage["status"] = "FAILED"
                record["error"] = {
                    "type": type(exc).__name__,
                    "stage_id": stage["stage_id"],
                    "message": str(exc)[:2000],
                }
                final_outcome = "FAILED"
                break

        with _LOCK:
            _PIPELINES[pipeline_run_id] = record
        _persist(record)
        if final_outcome in {"FAILED", "CANCELLED"}:
            # 继续循环以便 always_run 的 report_generation 仍能生成失败报告；
            # 其余 PENDING 阶段会因依赖失败被标记 BLOCKED / CANCELLED。
            continue

    _finalize_pipeline(record, ctx)


def _finalize_pipeline(record: dict[str, Any], ctx: dict[str, Any]) -> None:
    status = _compute_pipeline_status(record)
    with _LOCK:
        # 注意：终态 status 在最终路径写入后再发布，避免查询方在 finalize
        # 尚完成时看到"已成功但 final_report_paths 为空"的中间态。
        record["finished_at"] = _now_iso()
        if record.get("started_at"):
            try:
                started = datetime.fromisoformat(record["started_at"])
                finished = datetime.fromisoformat(record["finished_at"])
                record["duration_ms"] = int((finished - started).total_seconds() * 1000)
            except ValueError:
                record["duration_ms"] = None
        record["cancel_requested"] = record.get("cancel_requested", False)
        record["refs"] = _merge_refs(record, ctx)
        record["reproducibility"] = _merge_reproducibility(record, ctx)
        record["gate_summary"] = _gate_summary(ctx)
        record["final_report"] = _build_final_report(record, ctx)
        record["paid_api_called"] = False
        _PIPELINES[record["pipeline_run_id"]] = record
    paths = _persist(record)
    record["snapshot_path"] = paths["json"]
    # 报告生成阶段负责的 Markdown 由 report_generation 写入；此处兜底重写最终版
    md_paths = _persist_markdown(
        record, _render_markdown(record.get("final_report") or {})
    )
    record["final_report_paths"] = {
        "snapshot": paths["json"],
        "markdown": md_paths["markdown"],
    }
    with _LOCK:
        record["status"] = status
        record["final_report"]["status"] = status
        _PIPELINES[record["pipeline_run_id"]] = record
    _persist(record)


def _merge_refs(record: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    refs = dict(record.get("refs") or {})
    if ctx.get("trace_ids"):
        refs["trace_ids"] = list(ctx["trace_ids"])
    if ctx.get("evaluation_run_id"):
        refs["evaluation_run_id"] = ctx["evaluation_run_id"]
    if ctx.get("real_agent_runs"):
        refs["agent_run_ids"] = [r.get("run_id") for r in ctx["real_agent_runs"] if r.get("run_id")]
    if ctx.get("bad_cases"):
        refs["bad_case_ids"] = [c.get("case_id") for c in ctx["bad_cases"]]
    attribution = ctx.get("attribution") or {}
    if attribution.get("reports"):
        refs["attribution_report_ids"] = [
            r.get("attribution_id") for r in attribution["reports"] if r.get("attribution_id")
        ]
    patterns = ctx.get("patterns") or {}
    if patterns.get("pattern_report_id"):
        refs["failure_pattern_report_id"] = patterns["pattern_report_id"]
    comparison = ctx.get("comparison") or {}
    if comparison.get("comparison_id"):
        refs["comparison_id"] = comparison["comparison_id"]
    regression = ctx.get("regression") or {}
    if regression.get("regression_report_id"):
        refs["regression_report_id"] = regression["regression_report_id"]
    gate = ctx.get("gate") or {}
    if gate.get("gate_report_id"):
        refs["quality_gate_report_id"] = gate["gate_report_id"]
    return refs


def _merge_reproducibility(record: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repro = dict(record.get("reproducibility") or {})
    dataset = ctx.get("dataset") or {}
    if dataset.get("dataset_version"):
        repro["dataset_version"] = dataset["dataset_version"]
    report = ctx.get("report") or {}
    judge_breakdown = (report.get("metrics") or {}).get("judge_breakdown") or {}
    if judge_breakdown:
        repro["prompt_version"] = judge_breakdown.get("prompt_version")
        repro["rubric_version"] = judge_breakdown.get("rubric_version")
        repro["agent_version"] = None
    return repro


def _gate_summary(ctx: dict[str, Any]) -> dict[str, Any]:
    gate = ctx.get("gate") or {}
    regression = ctx.get("regression") or {}
    comparison = ctx.get("comparison") or {}
    if not gate:
        return {
            "gate_status": None,
            "exit_code": None,
            "gate_report_id": None,
            "reason": "质量门禁未执行（缺少基线/版本对比，或未启用）",
        }
    return {
        "gate_status": gate.get("gate_status"),
        "exit_code": gate.get("exit_code"),
        "gate_report_id": gate.get("gate_report_id"),
        "compared": bool(comparison),
        "regression_gate_status": regression.get("gate_status"),
        "triggered_rules": [
            r.get("rule_id") for r in gate.get("triggered_rules") or []
        ],
    }


def _apply_stage_refs(
    record: dict[str, Any], ctx: dict[str, Any], stage_id: str, outcome: dict[str, Any]
) -> None:
    if outcome.get("refs"):
        record.setdefault("refs", {}).update(outcome["refs"])


# ---------------------------------------------------------------------------
# 可选阶段跳过判定
# ---------------------------------------------------------------------------

def _optional_skip_reason(record: dict[str, Any], stage: dict[str, Any]) -> str | None:
    config = record["config"]
    sid = stage["stage_id"]
    if sid == "judge_evaluation" and config["judge_mode"] == "none":
        return "未启用 Judge（judge_mode=none），可选阶段跳过"
    if sid == "bad_case_attribution" and not config["enable_attribution"]:
        return "未启用 Bad Case 归因（enable_attribution=false）"
    if sid == "failure_pattern_mining" and not config["enable_failure_patterns"]:
        return "未启用失败模式挖掘（enable_failure_patterns=false）"
    if sid == "version_comparison":
        if not config["enable_version_comparison"]:
            return "未启用版本对比（enable_version_comparison=false）"
        if not config.get("baseline_run_id"):
            return "未提供基线版本（baseline_run_id 为空），版本对比无法执行"
    if sid == "regression_detection":
        if not config["enable_regression"]:
            return "未启用回归检测（enable_regression=false）"
        if not config.get("baseline_run_id"):
            return "未提供基线版本，回归检测无法执行"
    if sid == "quality_gate" and not config["enable_quality_gate"]:
        return "未启用质量门禁（enable_quality_gate=false）"
    return None


def _dynamic_skip_reason(
    record: dict[str, Any], ctx: dict[str, Any], stage: dict[str, Any]
) -> str | None:
    """运行时才知道的可选阶段跳过原因（不能仅凭配置判断）。"""
    sid = stage["stage_id"]
    config = record["config"]
    if sid in {"bad_case_attribution", "failure_pattern_mining"}:
        if not ctx.get("bad_cases"):
            return "无 Bad Case（全部案例通过），该可选阶段无需执行"
    if sid == "quality_gate" and not config.get("baseline_run_id"):
        return "未提供基线版本（baseline_run_id 为空），质量门禁无法执行"
    if sid == "quality_gate" and not config["enable_version_comparison"]:
        return "版本对比未启用，质量门禁无法执行"
    return None


# ---------------------------------------------------------------------------
# 阶段实现
# ---------------------------------------------------------------------------

def _stage_dataset_validation(
    record: dict[str, Any], ctx: dict[str, Any], stage: dict[str, Any]
) -> dict[str, Any]:
    from backend.app.services.evaluation_engine import load_dataset
    from backend.app.services.evaluation_store import ALLOWED_DATASETS, DATASET_DIR

    config = record["config"]
    dataset_id = config["dataset_id"]
    path = DATASET_DIR / ALLOWED_DATASETS[dataset_id]
    if not path.exists():
        raise PipelineError(f"数据集文件缺失: {path.name}")
    dataset = load_dataset(path)
    cases = dataset.get("cases") or []
    if not cases:
        raise PipelineError(f"数据集 {dataset_id} 没有案例")
    if len(cases) > MAX_CASES:
        raise PipelineConfigError(
            f"数据集案例数 {len(cases)} > 单次流水线上限 {MAX_CASES}"
        )
    seen: set[str] = set()
    for case in cases:
        case_id = case.get("case_id")
        if not case_id:
            raise PipelineError("数据集案例缺少 case_id")
        if case_id in seen:
            raise PipelineError(f"数据集 case_id 重复: {case_id}")
        seen.add(case_id)
        if not case.get("execution"):
            raise PipelineError(f"案例 {case_id} 缺少 execution 定义")
        if not case.get("task_description"):
            raise PipelineError(f"案例 {case_id} 缺少 task_description")
    max_cases = config.get("max_cases")
    selected = cases[:max_cases] if max_cases else cases
    ctx["dataset"] = dataset
    ctx["cases"] = selected
    ctx["dataset_path"] = str(path)
    record["reproducibility"]["dataset_version"] = dataset.get("dataset_version")
    record["dataset_id"] = dataset_id
    return {
        "input_refs": [f"dataset:{dataset_id}"],
        "output_refs": [
            f"dataset:{dataset_id}@{dataset.get('dataset_version')}",
            f"cases:{len(selected)}/{len(cases)}",
        ],
        "refs": {},
    }


def _stage_agent_execution(
    record: dict[str, Any], ctx: dict[str, Any], stage: dict[str, Any]
) -> dict[str, Any]:
    from backend.app.services.evaluation_engine import execute_case

    config = record["config"]
    cases = ctx["cases"]
    execution_errors: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    real_agent_runs: list[dict[str, Any]] = []
    if config["execution_mode"] == "real_api":
        if not _real_api_allowed(True):
            raise PipelineAuthError(
                "真实 API 模式未获授权（服务端 TRACEPILOT_ALLOW_REAL_API 未开启）"
            )
        from backend.app.services.agent_runner import run_agent_task

        for case in cases:
            _check_cancel(record)
            _check_deadline(ctx["deadline"])
            try:
                run = run_agent_task(
                    case["task_description"],
                    mode="real_api",
                    confirm_real=True,
                    task_id=case["case_id"],
                )
                real_agent_runs.append(run.to_dict())
                if not run.trace_id:
                    raise PipelineError("真实 API 运行未产生 Trace")
                pairs.append({"case": case, "trace_id": run.trace_id, "agent_run_id": run.run_id})
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001
                execution_errors.append(
                    {"case_id": case["case_id"], "error": f"{type(exc).__name__}: {exc}"}
                )
    else:
        for case in cases:
            _check_cancel(record)
            _check_deadline(ctx["deadline"])
            try:
                trace = execute_case(case)
                pairs.append({"case": case, "trace": trace, "trace_id": trace.trace_id})
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001
                execution_errors.append(
                    {"case_id": case["case_id"], "error": f"{type(exc).__name__}: {exc}"}
                )
    if not pairs:
        sample = execution_errors[0]["error"] if execution_errors else "无更多信息"
        raise PipelineError(
            f"Agent 执行全部失败（{len(execution_errors)} 个案例执行异常）：{sample}"
        )
    ctx["executed_pairs"] = pairs
    ctx["execution_errors"] = execution_errors
    ctx["real_agent_runs"] = real_agent_runs
    return {
        "input_refs": [f"dataset_validation:cases:{len(cases)}"],
        "output_refs": [
            f"agent_runs:{len(real_agent_runs)}" if config["execution_mode"] == "real_api"
            else f"executed_cases:{len(pairs)}",
            f"execution_errors:{len(execution_errors)}",
        ],
        "refs": {},
    }


def _stage_trace_collection(
    record: dict[str, Any], ctx: dict[str, Any], stage: dict[str, Any]
) -> dict[str, Any]:
    from backend.app.services.evaluation_engine import persist_trace
    from backend.app.services.trace_store import TraceNotFoundError, load_trace

    pairs = ctx["executed_pairs"]
    trace_ids: list[str] = []
    for index, pair in enumerate(pairs):
        _check_cancel(record)
        _check_deadline(ctx["deadline"])
        if "trace" in pair:
            trace = pair["trace"]
            try:
                persist_trace(trace)
            except Exception as exc:  # noqa: BLE001 - Trace 保存失败必须显式失败
                raise PipelineError(
                    f"Trace 保存失败（case={pair['case']['case_id']}）: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        trace_id = pair["trace_id"]
        try:
            persisted = load_trace(trace_id)
            pair["trace"] = persisted
        except TraceNotFoundError as exc:
            raise PipelineError(
                f"Trace 采集失败：{trace_id} 不可读取（case={pair['case']['case_id']}）"
            ) from exc
        trace_ids.append(trace_id)
    ctx["trace_ids"] = trace_ids
    record["reproducibility"]["dataset_version"] = (
        record["reproducibility"].get("dataset_version")
        or (ctx.get("dataset") or {}).get("dataset_version")
    )
    return {
        "input_refs": [f"agent_execution:executed_cases:{len(pairs)}"],
        "output_refs": [f"trace:{tid}" for tid in trace_ids],
        "refs": {"trace_ids": trace_ids},
    }


def _stage_deterministic_evaluation(
    record: dict[str, Any], ctx: dict[str, Any], stage: dict[str, Any]
) -> dict[str, Any]:
    from backend.app.services.evaluation_engine import (
        FAIL,
        UNKNOWN,
        build_evaluation_report_from_results,
        evaluate_trace,
    )
    from backend.app.services.evaluation_store import get_evaluation_store

    pairs = ctx["executed_pairs"]
    results: list[dict[str, Any]] = []
    bad_cases: list[dict[str, Any]] = []
    trace_ids: list[str] = []
    for pair in pairs:
        _check_cancel(record)
        _check_deadline(ctx["deadline"])
        trace = pair.get("trace")
        if trace is None:
            from backend.app.services.trace_store import load_trace

            trace = load_trace(pair["trace_id"])
            pair["trace"] = trace
        result = evaluate_trace(trace, pair["case"])
        results.append(result)
        trace_ids.append(result["trace_id"])
        if result.get("is_bad_case"):
            bad_cases.append(
                {
                    "case_id": result["case_id"],
                    "trace_id": result["trace_id"],
                    "failed_dimensions": [
                        k for k, m in result["metrics"].items() if m.get("status") == FAIL
                    ],
                    "unknown_dimensions": [
                        k for k, m in result["metrics"].items() if m.get("status") == UNKNOWN
                    ],
                }
            )
    run_id = f"eval-{uuid.uuid4().hex}"
    report = build_evaluation_report_from_results(
        ctx["dataset"],
        ctx["cases"],
        run_id=run_id,
        results=results,
        trace_ids=trace_ids,
        bad_cases=bad_cases,
        execution_errors=ctx["execution_errors"],
        not_executed=[],
    )
    report["pipeline_run_id"] = record["pipeline_run_id"]
    report["execution_mode"] = record["config"]["execution_mode"]
    get_evaluation_store().register_report(
        record["config"]["dataset_id"],
        report,
        judge_mode=record["config"]["judge_mode"],
        max_cases=record["config"].get("max_cases"),
    )
    ctx["report"] = report
    ctx["results"] = results
    ctx["bad_cases"] = bad_cases
    ctx["evaluation_run_id"] = run_id
    return {
        "input_refs": [f"trace_collection:traces:{len(trace_ids)}"],
        "output_refs": [
            f"evaluation_run:{run_id}",
            f"metrics:task_success_rate={report['metrics']['task_success_rate']['value']}",
            f"bad_cases:{len(bad_cases)}",
        ],
        "refs": {"evaluation_run_id": run_id, "bad_case_ids": [c["case_id"] for c in bad_cases]},
    }


def _stage_judge_evaluation(
    record: dict[str, Any], ctx: dict[str, Any], stage: dict[str, Any]
) -> dict[str, Any]:
    from backend.app.services.llm_judge import build_judge

    config = record["config"]
    _check_deadline(ctx["deadline"])
    if config["judge_mode"] == "none":  # 已在上层 SKIPPED；防御性检查
        raise PipelineError("judge_mode=none，不应执行 Judge 阶段")
    try:
        judge = build_judge(mode=config["judge_mode"])
    except Exception as exc:  # noqa: BLE001 - Judge 不可用必须明确失败
        raise PipelineError(f"Judge 不可用: {type(exc).__name__}: {exc}") from exc
    rubric_id = config["judge_rubric_id"] or "answer_quality"
    report = ctx["report"]
    results_by_case = {r["case_id"]: r for r in report.get("case_results") or []}
    judged = 0
    failed = 0
    for pair in ctx["executed_pairs"]:
        _check_cancel(record)
        _check_deadline(ctx["deadline"])
        case_id = pair["case"]["case_id"]
        try:
            judge_result = judge.judge(pair["trace"], rubric_id=rubric_id)
            results_by_case[case_id]["judge"] = judge_result.model_dump(mode="json")
            judged += 1
        except Exception as exc:  # noqa: BLE001 - 单案例失败降级为 UNKNOWN
            results_by_case[case_id]["judge"] = {
                "error": f"{type(exc).__name__}: {exc}",
                "is_mock": getattr(judge, "is_mock", None),
                "provider_kind": getattr(judge, "provider_kind", "unknown"),
                "status": "UNKNOWN",
                "limitations": ["Judge 执行失败，本案例无 Judge 评分（确定性指标不受影响）"],
            }
            failed += 1
    if judged == 0:
        raise PipelineError(
            f"Judge 不可用：{failed} 个案例全部评分失败，未产生任何 Judge 结果"
        )
    from backend.app.services.evaluation_engine import _judge_breakdown

    report["metrics"]["judge_breakdown"] = _judge_breakdown(list(results_by_case.values()))
    from backend.app.services.evaluation_store import get_evaluation_store

    get_evaluation_store().update_report(ctx["evaluation_run_id"], report)
    ctx["judge_note"] = f"Mock Judge 评分 {judged} 例（失败 {failed} 例降级为 UNKNOWN）"
    return {
        "input_refs": [f"deterministic_evaluation:{ctx['evaluation_run_id']}"],
        "output_refs": [
            f"judge_mode:{config['judge_mode']}",
            f"rubric:{rubric_id}",
            f"judged_cases:{judged}",
            f"failed_cases:{failed}",
        ],
        "refs": {},
    }


def _stage_bad_case_attribution(
    record: dict[str, Any], ctx: dict[str, Any], stage: dict[str, Any]
) -> dict[str, Any]:
    from backend.app.services.bad_case_attribution import attribute_evaluation_run

    bad_cases = ctx.get("bad_cases") or []
    if not bad_cases:
        # 由上层 optional skip 判定处理；正常情况不会到这里
        return {"output_refs": ["skipped: no bad cases"], "refs": {}}
    _check_cancel(record)
    _check_deadline(ctx["deadline"])
    try:
        attribution = attribute_evaluation_run(
            ctx["evaluation_run_id"], max_cases=min(len(bad_cases), 20)
        )
    except Exception as exc:  # noqa: BLE001 - 归因失败必须明确
        raise PipelineError(f"Bad Case 归因失败: {type(exc).__name__}: {exc}") from exc
    ctx["attribution"] = attribution
    reports = attribution.get("reports") or []
    return {
        "input_refs": [f"deterministic_evaluation:{ctx['evaluation_run_id']}"],
        "output_refs": [f"attribution:{r.get('attribution_id')}" for r in reports],
        "refs": {
            "attribution_report_ids": [
                r.get("attribution_id") for r in reports if r.get("attribution_id")
            ]
        },
    }


def _stage_failure_pattern_mining(
    record: dict[str, Any], ctx: dict[str, Any], stage: dict[str, Any]
) -> dict[str, Any]:
    from backend.app.services.failure_pattern_mining import analyze_run

    _check_cancel(record)
    _check_deadline(ctx["deadline"])
    try:
        patterns = analyze_run(ctx["evaluation_run_id"])
    except Exception as exc:  # noqa: BLE001
        raise PipelineError(f"失败模式挖掘失败: {type(exc).__name__}: {exc}") from exc
    ctx["patterns"] = patterns
    return {
        "input_refs": [f"deterministic_evaluation:{ctx['evaluation_run_id']}"],
        "output_refs": [
            f"failure_pattern_report:{patterns.get('pattern_report_id')}",
            f"patterns:{patterns.get('pattern_count')}",
        ],
        "refs": {"failure_pattern_report_id": patterns.get("pattern_report_id")},
    }


def _stage_version_comparison(
    record: dict[str, Any], ctx: dict[str, Any], stage: dict[str, Any]
) -> dict[str, Any]:
    from backend.app.services.evaluation_version_comparison import compare_runs

    _check_cancel(record)
    _check_deadline(ctx["deadline"])
    baseline = record["config"]["baseline_run_id"]
    if not baseline:
        raise PipelineError("未提供基线运行，版本对比无法执行")
    try:
        comparison = compare_runs(baseline, ctx["evaluation_run_id"])
    except Exception as exc:  # noqa: BLE001
        raise PipelineError(f"版本对比失败: {type(exc).__name__}: {exc}") from exc
    ctx["comparison"] = comparison
    verdict = (comparison.get("comparability") or {}).get("verdict")
    counts = (comparison.get("cases") or {}).get("counts") or {}
    return {
        "input_refs": [f"baseline:{baseline}", f"candidate:{ctx['evaluation_run_id']}"],
        "output_refs": [
            f"comparison:{comparison.get('comparison_id')}",
            f"verdict:{verdict}",
            f"regressed_cases:{counts.get('regressed')}",
        ],
        "refs": {"comparison_id": comparison.get("comparison_id")},
    }


def _stage_regression_detection(
    record: dict[str, Any], ctx: dict[str, Any], stage: dict[str, Any]
) -> dict[str, Any]:
    from backend.app.services.regression_detector import detect_regressions

    _check_cancel(record)
    _check_deadline(ctx["deadline"])
    comparison = ctx.get("comparison")
    if not comparison:
        raise PipelineError("缺少版本对比报告，回归检测无法执行")
    try:
        regression = detect_regressions(comparison=comparison)
    except Exception as exc:  # noqa: BLE001
        raise PipelineError(f"回归检测失败: {type(exc).__name__}: {exc}") from exc
    ctx["regression"] = regression
    return {
        "input_refs": [f"version_comparison:{comparison.get('comparison_id')}"],
        "output_refs": [
            f"regression_report:{regression.get('regression_report_id')}",
            f"risk_level:{regression.get('risk_level')}",
            f"gate_status:{regression.get('gate_status')}",
        ],
        "refs": {"regression_report_id": regression.get("regression_report_id")},
    }


def _stage_quality_gate(
    record: dict[str, Any], ctx: dict[str, Any], stage: dict[str, Any]
) -> dict[str, Any]:
    from backend.app.services.quality_gate import evaluate_gate

    _check_cancel(record)
    _check_deadline(ctx["deadline"])
    comparison = ctx.get("comparison")
    regression = ctx.get("regression")
    if not comparison or not comparison.get("comparison_id"):
        raise PipelineError("缺少版本对比报告，质量门禁无法执行")
    try:
        gate = evaluate_gate(
            comparison_id=comparison["comparison_id"],
            regression_report_id=(regression or {}).get("regression_report_id"),
            ci_run_id=f"pipeline-{record['pipeline_run_id']}",
        )
    except Exception as exc:  # noqa: BLE001
        raise PipelineError(f"质量门禁执行失败: {type(exc).__name__}: {exc}") from exc
    ctx["gate"] = gate
    return {
        "input_refs": [
            f"comparison:{comparison.get('comparison_id')}",
            f"regression:{(regression or {}).get('regression_report_id')}",
        ],
        "output_refs": [
            f"quality_gate:{gate.get('gate_report_id')}",
            f"gate_status:{gate.get('gate_status')}",
            f"exit_code:{gate.get('exit_code')}",
        ],
        "refs": {"quality_gate_report_id": gate.get("gate_report_id")},
    }


def _stage_report_generation(
    record: dict[str, Any], ctx: dict[str, Any], stage: dict[str, Any]
) -> dict[str, Any]:
    # 最终状态/报告由 _finalize_pipeline 统一生成（失败/取消也能生成）
    paths = _persist(record)
    return {
        "input_refs": ["pipeline:stages"],
        "output_refs": [f"pipeline_snapshot:{paths['json']}"],
        "refs": {},
    }


# ---------------------------------------------------------------------------
# 最终报告（JSON / Markdown）
# ---------------------------------------------------------------------------

def _evaluation_summary(ctx: dict[str, Any]) -> dict[str, Any] | None:
    report = ctx.get("report")
    if not report:
        return None
    metrics = report.get("metrics") or {}
    cases = []
    for result in report.get("case_results") or []:
        cases.append(
            {
                "case_id": result.get("case_id"),
                "scenario_type": result.get("scenario_type"),
                "overall_status": result.get("overall_status"),
                "trace_id": result.get("trace_id"),
                "is_bad_case": bool(result.get("is_bad_case")),
            }
        )
    return {
        "evaluation_run_id": report.get("evaluation_run_id"),
        "dataset_id": report.get("dataset_id"),
        "dataset_version": report.get("dataset_version"),
        "evaluator_version": report.get("engine_version"),
        "total_cases": report.get("total_cases"),
        "executed_cases": report.get("executed_cases"),
        "passed_cases": report.get("passed_cases"),
        "failed_cases": report.get("failed_cases"),
        "unknown_cases": report.get("unknown_cases"),
        "execution_error_cases": report.get("execution_error_cases"),
        "metrics": {
            name: {
                "value": (metrics.get(name) or {}).get("value"),
                "numerator": (metrics.get(name) or {}).get("numerator"),
                "denominator": (metrics.get(name) or {}).get("denominator"),
            }
            for name in (
                "task_success_rate",
                "tool_correctness_rate",
                "safety_pass_rate",
                "recovery_rate",
            )
        },
        "average_duration_ms": metrics.get("average_duration_ms"),
        "total_tokens": metrics.get("total_tokens"),
        "judge_breakdown": metrics.get("judge_breakdown"),
        "bad_case_count": len(ctx.get("bad_cases") or []),
        "bad_cases": [
            {
                "case_id": c.get("case_id"),
                "trace_id": c.get("trace_id"),
                "failed_dimensions": c.get("failed_dimensions"),
                "unknown_dimensions": c.get("unknown_dimensions"),
            }
            for c in (ctx.get("bad_cases") or [])
        ],
        "cases": cases,
        "limitations": report.get("limitations") or [],
    }


def _attribution_summary(ctx: dict[str, Any]) -> dict[str, Any] | None:
    attribution = ctx.get("attribution")
    if not attribution:
        return None
    reports = attribution.get("reports") or []
    summary = attribution.get("summary") or {}
    return {
        "batch_report_id": attribution.get("batch_report_id"),
        "attributed_count": attribution.get("attributed_count"),
        "bad_case_count": attribution.get("bad_case_count"),
        "reports": [
            {
                "attribution_id": r.get("attribution_id"),
                "case_id": r.get("case_id"),
                "root_cause_status": r.get("root_cause_status"),
                "candidate_types": [
                    c.get("cause_type") for c in r.get("candidate_causes") or []
                ],
            }
            for r in reports
        ],
        "evidence_level_counts": summary.get("evidence_level_counts"),
        "limitations": attribution.get("limitations") or [],
    }


def _patterns_summary(ctx: dict[str, Any]) -> dict[str, Any] | None:
    patterns = ctx.get("patterns")
    if not patterns:
        return None
    return {
        "pattern_report_id": patterns.get("pattern_report_id"),
        "pattern_count": patterns.get("pattern_count"),
        "denominators": patterns.get("denominators"),
        "patterns": [
            {
                "fingerprint_id": p.get("fingerprint_id"),
                "fingerprint_label": p.get("fingerprint_label"),
                "case_count": p.get("case_count"),
            }
            for p in (patterns.get("patterns") or [])
        ],
        "limitations": patterns.get("limitations") or [],
    }


def _comparison_summary(ctx: dict[str, Any]) -> dict[str, Any] | None:
    comparison = ctx.get("comparison")
    if not comparison:
        return None
    return {
        "comparison_id": comparison.get("comparison_id"),
        "comparability": comparison.get("comparability"),
        "metric_deltas": [
            {
                "metric": m.get("metric"),
                "baseline": (m.get("baseline") or {}).get("value"),
                "candidate": (m.get("candidate") or {}).get("value"),
                "delta": m.get("delta"),
                "delta_unit": m.get("delta_unit"),
                "status": m.get("status"),
            }
            for m in comparison.get("metric_deltas") or []
        ],
        "case_counts": (comparison.get("cases") or {}).get("counts"),
        "regressed_cases": [
            c.get("case_id")
            for c in (comparison.get("cases") or {}).get("regressed_cases") or []
        ],
        "limitations": comparison.get("limitations") or [],
    }


def _regression_summary(ctx: dict[str, Any]) -> dict[str, Any] | None:
    regression = ctx.get("regression")
    if not regression:
        return None
    return {
        "regression_report_id": regression.get("regression_report_id"),
        "risk_level": regression.get("risk_level"),
        "risk_reason": regression.get("risk_reason"),
        "gate_status": regression.get("gate_status"),
        "gate_reason": regression.get("gate_reason"),
        "triggered_rules": [
            r.get("rule_id") for r in regression.get("triggered_rules") or []
        ],
        "limitations": regression.get("limitations") or [],
    }


def _gate_final_summary(ctx: dict[str, Any]) -> dict[str, Any] | None:
    gate = ctx.get("gate")
    if not gate:
        return None
    return {
        "gate_report_id": gate.get("gate_report_id"),
        "gate_status": gate.get("gate_status"),
        "gate_reason": gate.get("gate_reason"),
        "exit_code": gate.get("exit_code"),
        "config_fingerprint": (gate.get("config") or {}).get("fingerprint"),
        "config_label": (gate.get("config") or {}).get("label"),
        "triggered_rules": [
            {
                "rule_id": r.get("rule_id"),
                "status": r.get("status"),
                "severity": r.get("severity"),
                "message": r.get("message"),
            }
            for r in gate.get("triggered_rules") or []
        ],
        "checks": [
            {
                "check_id": c.get("check_id"),
                "status": c.get("status"),
                "threshold": c.get("threshold"),
                "message": c.get("message"),
            }
            for c in gate.get("checks") or []
        ],
        "evidence_refs": gate.get("evidence_refs") or [],
        "limitations": gate.get("limitations") or [],
    }


def _build_final_report(record: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    stages = record.get("stages") or []
    status_counts = {status: 0 for status in STAGE_STATUSES}
    for stage in stages:
        status_counts[stage.get("status")] = status_counts.get(stage.get("status"), 0) + 1
    refs = _merge_refs(record, ctx)
    evaluation = _evaluation_summary(ctx)
    attribution = _attribution_summary(ctx)
    patterns = _patterns_summary(ctx)
    comparison = _comparison_summary(ctx)
    regression = _regression_summary(ctx)
    gate = _gate_final_summary(ctx)

    evidence_refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, ref_id: Any, detail: str | None = None) -> None:
        if not ref_id:
            return
        key = (kind, str(ref_id))
        if key in seen:
            return
        seen.add(key)
        item: dict[str, Any] = {"kind": kind, "ref_id": str(ref_id)}
        if detail:
            item["detail"] = detail
        evidence_refs.append(item)

    add("pipeline", record["pipeline_run_id"], "流水线运行")
    add("evaluation_run", refs.get("evaluation_run_id"), "确定性评测运行")
    for trace_id in (refs.get("trace_ids") or [])[:50]:
        add("trace", trace_id, "案例 Trace")
    for case in (ctx.get("bad_cases") or [])[:50]:
        add("bad_case", case.get("case_id"), f"trace={case.get('trace_id')}")
    for rid in refs.get("attribution_report_ids") or []:
        add("attribution_report", rid, "Bad Case 归因报告")
    add("failure_pattern_report", refs.get("failure_pattern_report_id"), "失败模式报告")
    add("comparison_report", refs.get("comparison_id"), "版本对比报告")
    add("regression_report", refs.get("regression_report_id"), "回归检测报告")
    add("quality_gate_report", refs.get("quality_gate_report_id"), "质量门禁报告")

    skipped = [s for s in stages if s.get("status") == "SKIPPED"]
    blocked = [s for s in stages if s.get("status") == "BLOCKED"]
    failed = [s for s in stages if s.get("status") == "FAILED"]
    limitations = list(record.get("limitations") or [])
    if record["config"]["execution_mode"] == "mock":
        limitations.append("本流水线为 Mock 模式（受控确定性模拟），结论不代表真实模型质量")
    if skipped:
        limitations.append(
            "有阶段被跳过（SKIPPED，未执行，不等于成功）："
            + ", ".join(f"{s['stage_id']}（{s.get('skip_reason')}）" for s in skipped)
        )
    if blocked:
        limitations.append(
            "有阶段被阻塞（BLOCKED）："
            + ", ".join(f"{s['stage_id']}（{s.get('skip_reason')}）" for s in blocked)
        )
    if failed:
        limitations.append(
            "有阶段失败（FAILED）："
            + ", ".join(f"{s['stage_id']}（{(s.get('error_details') or {}).get('message')}）" for s in failed)
        )
    if not gate:
        limitations.append("质量门禁未执行：缺少基线版本或版本对比报告，未产生门禁结论")

    return {
        "engine_version": ENGINE_VERSION,
        "pipeline_run_id": record["pipeline_run_id"],
        "status": _compute_pipeline_status(record),
        "created_at": record.get("created_at"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "duration_ms": record.get("duration_ms"),
        "config": record.get("config"),
        "reproducibility": _merge_reproducibility(record, ctx),
        "stage_summary": {
            "total": len(stages),
            "counts": status_counts,
            "succeeded": [s["stage_id"] for s in stages if s.get("status") == "SUCCEEDED"],
            "failed": [s["stage_id"] for s in stages if s.get("status") == "FAILED"],
            "skipped": [s["stage_id"] for s in stages if s.get("status") == "SKIPPED"],
            "blocked": [s["stage_id"] for s in stages if s.get("status") == "BLOCKED"],
            "cancelled": [s["stage_id"] for s in stages if s.get("status") == "CANCELLED"],
        },
        "stages": stages,
        "refs": refs,
        "evaluation": evaluation,
        "attribution": attribution,
        "failure_patterns": patterns,
        "version_comparison": comparison,
        "regression": regression,
        "quality_gate": gate,
        "evidence_refs": evidence_refs,
        "error": record.get("error"),
        "cancel_requested": bool(record.get("cancel_requested")),
        "limitations": limitations,
        "paid_api_called": False,
        "generated_at": _now_iso(),
    }


def _render_markdown(report: dict[str, Any]) -> str:
    status = report.get("status")
    repro = report.get("reproducibility") or {}
    lines: list[str] = [
        "# TracePilot Evaluation Pipeline Report",
        "",
        f"**Pipeline Status: `{status}`**",
        "",
        f"- Pipeline Run ID: `{report.get('pipeline_run_id')}`",
        f"- Created At: `{report.get('created_at')}`",
        f"- Duration: `{report.get('duration_ms')}` ms",
        f"- Dataset Version: `{repro.get('dataset_version')}`",
        f"- Evaluator Version: `{repro.get('evaluator_version')}`",
        f"- Agent Version: `{repro.get('agent_version')}`（受控执行器未提供，未伪造）",
        f"- Prompt Version: `{repro.get('prompt_version')}`",
        f"- Rubric Version: `{repro.get('rubric_version')}`",
        f"- Execution Mode: `{repro.get('execution_mode')}`",
        f"- Random Seed: `{repro.get('random_seed')}`",
        f"- Config Fingerprint: `{repro.get('config_fingerprint')}`",
        f"- Paid API Called: `{report.get('paid_api_called')}`",
        "",
        "## Stages",
        "",
        "| Stage | Status | Duration (ms) | Attempts | Skip/Error |",
        "|-------|--------|---------------|----------|------------|",
    ]
    for stage in report.get("stages") or []:
        detail = ""
        if stage.get("status") == "SKIPPED":
            detail = stage.get("skip_reason") or ""
        elif stage.get("status") in {"FAILED", "BLOCKED", "CANCELLED"}:
            detail = (stage.get("error_details") or {}).get("message") or stage.get("skip_reason") or ""
        detail = str(detail).replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {stage.get('stage_id')} | {stage.get('status')} | {stage.get('duration_ms')} | "
            f"{stage.get('attempts')} | {detail} |"
        )
    gate = report.get("quality_gate")
    lines += ["", "## Quality Gate", ""]
    if gate:
        lines += [
            f"- Gate Status: `{gate.get('gate_status')}`（exit code {gate.get('exit_code')}）",
            f"- Gate Report: `{gate.get('gate_report_id')}`",
            f"- Triggered Rules: "
            f"{', '.join(r.get('rule_id') for r in gate.get('triggered_rules') or []) or '无'}",
        ]
    else:
        lines.append("- 质量门禁未执行（缺少基线/版本对比）")
    lines += ["", "## Evidence References", ""]
    evidence = report.get("evidence_refs") or []
    if not evidence:
        lines.append("_无证据引用_")
    else:
        for ref in evidence:
            lines.append(f"- `{ref.get('kind')}:{ref.get('ref_id')}`")
    lines += ["", "## Limitations", ""]
    for item in report.get("limitations") or []:
        lines.append(f"- {item}")
    lines += [
        "",
        "> 本报告由确定性流水线生成；SKIPPED 不等于 SUCCEEDED。",
        "",
    ]
    return "\n".join(lines)
