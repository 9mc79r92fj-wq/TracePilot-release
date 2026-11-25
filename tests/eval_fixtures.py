"""第 49/50 步测试共用的运行构造工具。

两种运行来源：

1. `real_run()`：触发**真实的**受控评测执行（确定性模拟器，零成本、无付费 API、
   无真实网络调用）。用于验证版本元数据、可比性与持久化链路。
2. `build_report()` + `inject_run()`：构造差异化的"已完成"运行记录，注入内存
   `EvaluationStore`（仅测试使用；生产代码没有该入口），用于稳定复现指标下降、
   案例状态迁移等真实数据集难以出现的场景。

诚实性约定：
- 注入的运行不改写版本元数据：`dataset_id` / `dataset_version` /
  `engine_version` 等字段仍取自真实数据集与真实引擎版本；
- 只有指标值与案例状态是受控构造的，且测试断言的对象正是这些差异；
- 任何测试都不会调用付费 API。
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.app.services.evaluation_store import get_evaluation_store

DEFAULT_MODEL = {"provider": "local-simulation", "simulated": True, "paid_api_called": False}
DEFAULT_TOOLS = ["calculator", "sqlite_query", "text_analyzer"]
CASE_DIMENSIONS = (
    "task_success",
    "tool_correctness",
    "process_quality",
    "recovery",
    "efficiency",
    "safety",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 真实运行
# ---------------------------------------------------------------------------

def wait_for_run(run_id: str, *, timeout: float = 180.0) -> dict[str, Any]:
    """等待一次评测运行结束（succeeded / failed）。"""
    store = get_evaluation_store()
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = store.get(run_id)
        if rec.get("status") in {"succeeded", "failed"}:
            return rec
        time.sleep(0.05)
    raise TimeoutError(f"评测运行 {run_id} 超时未完成")


def real_run(
    dataset_id: str = "tracepilot-fault-v1",
    *,
    judge_mode: str = "none",
    max_cases: int | None = None,
) -> str:
    """创建一次真实受控评测（关闭去重，保证是独立运行）并等待完成。"""
    store = get_evaluation_store()
    rec = store.submit(dataset_id, max_cases, judge_mode=judge_mode, dedup=False)
    rec = wait_for_run(rec["run_id"])
    assert rec["status"] == "succeeded", rec.get("error")
    return rec["run_id"]


# ---------------------------------------------------------------------------
# 受控运行（注入内存 Store）
# ---------------------------------------------------------------------------

def new_run_id() -> str:
    return f"eval-test-{uuid.uuid4().hex}"


def rate(numerator: int, denominator: int, definition: str = "可判定案例数") -> dict[str, Any]:
    """构造与真实引擎一致的比率指标（value/numerator/denominator 三件套）。"""
    return {
        "value": round(numerator / denominator, 6) if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "denominator_definition": definition,
    }


def make_metrics(
    *,
    task_success: tuple[int, int] | None = None,
    tool_correctness: tuple[int, int] | None = None,
    recovery: tuple[int, int] | None = None,
    safety: tuple[int, int] | None = None,
    average_duration_ms: float | None = None,
    total_tokens: float | None = None,
    dimension_breakdown: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造指标字典；未传入的指标不出现（等价于"该指标不可得"）。"""
    pairs = {
        "task_success_rate": task_success,
        "tool_correctness_rate": tool_correctness,
        "recovery_rate": recovery,
        "safety_pass_rate": safety,
    }
    out: dict[str, Any] = {}
    for name, pair in pairs.items():
        if pair is not None:
            out[name] = rate(*pair)
    if average_duration_ms is not None:
        out["average_duration_ms"] = float(average_duration_ms)
    if total_tokens is not None:
        out["total_tokens"] = float(total_tokens)
    if dimension_breakdown is not None:
        out["dimension_breakdown"] = dimension_breakdown
    return out


def make_case(
    case_id: str,
    status: str,
    *,
    trace_id: str | None = None,
    scenario_type: str = "tool_failure",
    dimension_status: dict[str, str] | None = None,
    is_bad_case: bool = False,
) -> dict[str, Any]:
    """构造与真实评测报告一致的案例结果（metrics 每维含 status）。"""
    dims = dimension_status or {dim: status for dim in CASE_DIMENSIONS}
    metrics = {
        dim: {
            "metric_name": dim,
            "metric_value": None if st in {"UNKNOWN", "NOT_APPLICABLE"} else True,
            "evaluation_method": "test fixture",
            "evidence": [],
            "confidence_or_limitations": "测试构造",
            "status": st,
        }
        for dim, st in dims.items()
    }
    return {
        "case_id": case_id,
        "scenario_type": scenario_type,
        "data_source": "synthetic",
        "trace_id": trace_id,
        "overall_status": status,
        "metrics": metrics,
        "is_bad_case": is_bad_case,
    }


def build_report(
    run_id: str,
    case_results: list[dict[str, Any]],
    *,
    dataset_id: str = "tracepilot-fault-v1",
    dataset_version: str = "1.0.0",
    engine_version: str = "0.1.0",
    metrics: dict[str, Any] | None = None,
    model: dict[str, Any] | None = None,
    tools: list[str] | None = None,
    not_executed: int = 0,
    execution_errors: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一份评测报告（字段结构与真实 evaluation_engine 输出一致）。"""
    statuses = [c["overall_status"] for c in case_results]
    report: dict[str, Any] = {
        "evaluation_run_id": run_id,
        "engine_version": engine_version,
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "dataset_data_source": "synthetic",
        "total_cases": len(case_results),
        "executed_cases": len(case_results) - not_executed,
        "passed_cases": statuses.count("PASS"),
        "failed_cases": statuses.count("FAIL"),
        "unknown_cases": statuses.count("UNKNOWN"),
        "not_applicable_cases": statuses.count("NOT_APPLICABLE"),
        "not_executed_cases": not_executed,
        "execution_error_cases": execution_errors,
        "bad_cases": [c["case_id"] for c in case_results if c["overall_status"] == "FAIL"],
        "case_results": list(case_results),
        "metrics": metrics or {},
        "environment": {
            "model": dict(model or DEFAULT_MODEL),
            "tools": list(tools if tools is not None else DEFAULT_TOOLS),
        },
    }
    if extra:
        report.update(extra)
    return report


def inject_run(
    report: dict[str, Any],
    *,
    judge_mode: str = "none",
    rubric_id: str | None = None,
    max_cases: int | None = None,
) -> str:
    """把构造好的报告作为"已成功完成"的运行登记到内存 Store（仅测试）。"""
    store = get_evaluation_store()
    run_id = report["evaluation_run_id"]
    record = {
        "run_id": run_id,
        "status": "succeeded",
        "created_at": _now_iso(),
        "created_ts": time.time(),
        "dataset_id": report["dataset_id"],
        "max_cases": max_cases,
        "judge_mode": judge_mode,
        "judge_rubric_id": rubric_id,
        "fingerprint": f"test-{run_id}",
        "report": report,
        "error": None,
    }
    # 生产 Store 不提供"外部报告"入口；测试直接写入内部映射以模拟差异运行。
    with store._lock:  # noqa: SLF001 - 仅测试注入
        store._runs[run_id] = record
        store._order.append(run_id)
        store._evict_if_needed()
    return run_id


def simple_pair(
    *,
    baseline_metrics: dict[str, Any],
    candidate_metrics: dict[str, Any],
    baseline_statuses: list[str],
    candidate_statuses: list[str],
    case_ids: list[str] | None = None,
    **report_kwargs: Any,
) -> tuple[str, str]:
    """构造一对案例集相同的受控运行，返回 (baseline_run_id, candidate_run_id)。"""
    if case_ids is None:
        count = max(len(baseline_statuses), len(candidate_statuses))
        case_ids = [f"case-{i + 1:02d}" for i in range(count)]
    ids = case_ids
    base_cases = [
        make_case(ids[i], st) for i, st in enumerate(baseline_statuses[: len(ids)])
    ]
    cand_cases = [
        make_case(ids[i], st) for i, st in enumerate(candidate_statuses[: len(ids)])
    ]
    base_id = new_run_id()
    cand_id = new_run_id()
    inject_run(build_report(base_id, base_cases, metrics=baseline_metrics, **report_kwargs))
    inject_run(build_report(cand_id, cand_cases, metrics=candidate_metrics, **report_kwargs))
    return base_id, cand_id
