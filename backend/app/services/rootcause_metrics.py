"""Root-Cause Accuracy Metrics（第 55 步审计修复：原需求创新点 6 / 评测指标）。

用**已标注真实根因的受控故障评测集**（`data/evaluation/dataset_fault_v1.json`，
由 EvaluationEngine 的确定性故障注入生成）作为金标准，与 Bad Case Attribution 的输出对比：

- Root Cause Top-1 准确率：首个候选根因类型是否命中金标准故障类型；
- Top-3 覆盖率：金标准类型是否出现在前 3 个候选；
- 步骤级 Top-1 / Top-3：候选 affected_steps 是否包含真实故障注入步骤；
- 误归因率：首候选非 UNKNOWN 但与金标准不符；
- UNKNOWN 率、多候选率；
- 平均诊断耗时；平均 Token / 成本（来自 CostAnalytics；无价格时明确 PRICE_UNKNOWN）；
- 可选：推荐修复成功率（MinimalRepairEngine 受控验证）；
- 基线对比：多数类基线（always 最常见故障类型）、随机基线期望值（1/类型数）。

诚实性：
- 只使用真实运行记录与数据集标注；不构造金标准、不伪造准确率；
- 金标准缺失的案例计入 `unlabeled_cases`，不进入准确率分母；
- 模拟执行器数据不代表真实模型性能；成本为价格表估算，不是实际账单；
- 修复成功率只有实际执行受控验证后才给出，否则为 null 并注明原因。
"""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "RootCauseMetricsError",
    "RootCauseMetricsNotFoundError",
    "compute_metrics",
    "get_metrics",
    "list_metrics",
    "METRIC_DEFINITIONS",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
METRICS_DIR = PROJECT_ROOT / "data" / "rootcause_metrics" / "reports"
MAX_STORED = 100

# 数据集 fault.type → Attribution cause_type（与 bad_case_attribution 内部映射一致）
_FAULT_TO_CAUSE = {
    "tool_timeout": "TOOL_TIMEOUT",
    "tool_exception": "TOOL_EXCEPTION",
    "invalid_arguments": "INVALID_ARGUMENTS",
    "invalid_tool_result": "INVALID_TOOL_RESULT",
    "empty_retrieval": "EMPTY_RETRIEVAL",
    "permission_denied": "PERMISSION_DENIED",
}

METRIC_DEFINITIONS = {
    "type_top1_accuracy": {
        "label": "Root Cause Top-1 准确率（类型）",
        "definition": "首个候选 cause_type 与数据集标注故障类型一致的案例数 / 有金标准且已归因案例数",
    },
    "type_top3_coverage": {
        "label": "Root Cause Top-3 覆盖率（类型）",
        "definition": "金标准类型出现在前 3 个候选中的案例数 / 有金标准且已归因案例数",
    },
    "step_top1_accuracy": {
        "label": "根因步骤 Top-1 准确率",
        "definition": "首个候选 affected_steps 包含真实故障注入步骤的案例数 / 有步骤金标准的案例数",
    },
    "step_top3_coverage": {
        "label": "根因步骤 Top-3 覆盖率",
        "definition": "前 3 个候选 affect_steps 任一包含真实故障步骤的案例数 / 有步骤金标准的案例数",
    },
    "misattribution_rate": {
        "label": "误归因率",
        "definition": "首候选不是 UNKNOWN 且与金标准类型不一致的案例数 / 有金标准案例数",
    },
    "repair_success_rate": {
        "label": "推荐修复成功率",
        "definition": "首个修复候选经受控验证 status=verified 的案例数 / 实际验证尝试数（未开启验证时为 null）",
    },
    "average_diagnosis_ms": {
        "label": "平均诊断耗时（ms）",
        "definition": "归因（+可选修复验证）实际耗时均值；仅测量本机执行时间，不代表生产 SLA",
    },
    "average_cost": {
        "label": "平均诊断成本",
        "definition": "CostAnalytics 中该评测运行的平均每任务成本；价格表估算，不是 Provider 账单",
    },
}


class RootCauseMetricsError(ValueError):
    """指标计算失败（运行不可查、数据集无金标准等）。"""


class RootCauseMetricsNotFoundError(RootCauseMetricsError):
    """指标报告不存在。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_div(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _load_fault_dataset(dataset_id: str) -> dict[str, Any]:
    from backend.app.services.evaluation_engine import DATASET_DIR, load_dataset
    from backend.app.services.evaluation_store import ALLOWED_DATASETS

    filename = ALLOWED_DATASETS.get(dataset_id)
    if not filename:
        raise RootCauseMetricsError(
            f"数据集 {dataset_id!r} 不在白名单中，无法核对金标准"
        )
    path = DATASET_DIR / filename
    if not path.exists():
        raise RootCauseMetricsError(f"数据集文件不存在: {path}")
    return load_dataset(path)


def _gold_type(case: dict[str, Any]) -> str | None:
    fault = ((case.get("execution") or {}).get("fault") or {})
    fault_type = fault.get("type") or case.get("scenario_type")
    if not fault_type:
        return None
    return _FAULT_TO_CAUSE.get(str(fault_type), str(fault_type).upper())


def _gold_step_ids(trace: Any) -> list[str]:
    ids: list[str] = []
    for step in getattr(trace, "steps", []) or []:
        meta = step.metadata or {}
        fault = meta.get("fault_injection")
        if isinstance(fault, dict) and (fault.get("injected") is True or fault.get("type")):
            ids.append(step.step_id)
    return ids


def _candidate_types(report: dict[str, Any], depth: int) -> list[str]:
    candidates = report.get("candidate_causes") or []
    return [str(c.get("cause_type")) for c in candidates[:depth]]


def _candidate_steps(report: dict[str, Any], depth: int) -> list[str]:
    steps: list[str] = []
    for candidate in (report.get("candidate_causes") or [])[:depth]:
        for step_id in candidate.get("affected_steps") or []:
            if step_id not in steps:
                steps.append(step_id)
    return steps


def _repair_success(
    trace: Any,
) -> tuple[bool | None, str | None, int | None]:
    """受控验证首个修复候选是否有效；返回 (成功, 说明, 耗时ms)。"""
    from backend.app.services.minimal_repair import (
        RepairError,
        generate_candidates,
        verify_candidate,
    )

    started = time.monotonic()
    try:
        candidates = generate_candidates(trace, max_candidates=1)
    except RepairError as exc:
        return None, f"修复候选生成失败: {exc}", None
    if not candidates:
        return None, "未生成修复候选", None
    try:
        verified = verify_candidate(trace, candidates[0])
    except Exception as exc:  # noqa: BLE001 - 验证异常不伪造成功
        return None, f"修复验证不可用: {type(exc).__name__}: {exc}", None
    elapsed = int((time.monotonic() - started) * 1000)
    if verified.verification_status == "verified":
        return True, None, elapsed
    return False, verified.verification_error or "受控验证未观察到改善", elapsed


def _evaluation_cost(evaluation_run_id: str) -> dict[str, Any]:
    from backend.app.services.token_analytics import analyze_evaluation_run

    try:
        data = analyze_evaluation_run(evaluation_run_id)
    except Exception as exc:  # noqa: BLE001
        return {
            "average_cost": None,
            "currency": None,
            "status": "UNKNOWN",
            "note": f"成本不可计算: {type(exc).__name__}: {exc}",
            "pricing_versions": [],
        }
    aggregate = data.get("aggregate", {}).get("cost", {})
    executed = (data.get("sample_size") or 0) if "sample_size" in data else None
    if executed is None:
        traces = data.get("traces") or []
        executed = len(traces)
    amount = aggregate.get("amount")
    average = round(amount / executed, 8) if amount is not None and executed else None
    return {
        "average_cost": average,
        "total_cost": amount,
        "currency": aggregate.get("currency"),
        "status": aggregate.get("status"),
        "note": aggregate.get("note"),
        "pricing_versions": aggregate.get("pricing_versions") or [],
        "sample_size": executed,
    }


def compute_metrics(
    evaluation_run_id: str,
    *,
    max_cases: int = 50,
    include_repair: bool = False,
) -> dict[str, Any]:
    """计算一次评测运行的根因准确率指标（真实归因输出 vs 数据集金标准）。"""
    from backend.app.services.bad_case_attribution import (
        AttributionError,
        attribute_case,
    )
    from backend.app.services.evaluation_store import (
        EvaluationNotFoundError,
        get_evaluation_store,
    )
    from backend.app.services.trace_store import TraceNotFoundError, load_trace

    if not (1 <= max_cases <= 50):
        raise RootCauseMetricsError("max_cases 必须在 1..50")
    started = time.monotonic()
    try:
        rec = get_evaluation_store().get(evaluation_run_id)
    except EvaluationNotFoundError as exc:
        raise RootCauseMetricsError(str(exc)) from exc
    if rec.get("status") != "succeeded":
        raise RootCauseMetricsError(
            f"评测运行状态为 {rec.get('status')!r}，无法计算指标"
        )
    report = rec.get("report") or rec.get("result") or {}
    dataset_id = report.get("dataset_id")
    if not dataset_id:
        raise RootCauseMetricsError("评测报告缺少 dataset_id")
    dataset = _load_fault_dataset(dataset_id)
    gold_cases = {c["case_id"]: c for c in dataset.get("cases") or []}

    cases = [c for c in (report.get("case_results") or []) if c.get("is_bad_case")]
    cases = cases[:max_cases]

    rows: list[dict[str, Any]] = []
    labeled = attributed = 0
    type_top1 = type_top3 = step_top1 = step_top3 = 0
    step_labeled = 0
    misattribution = 0
    unknown_first = 0
    multi_candidate = 0
    repair_attempted = repair_succeeded = 0
    repair_errors: list[str] = []
    type_errors: list[str] = []
    diagnosis_ms: list[int] = []

    for case in cases:
        case_id = case.get("case_id")
        gold_case = gold_cases.get(case_id)
        gold_type = _gold_type(gold_case) if gold_case else None
        started_case = time.monotonic()
        try:
            attribution = attribute_case(
                evaluation_run_id=evaluation_run_id,
                case=case,
                dataset_id=dataset_id,
            )
        except AttributionError as exc:
            type_errors.append(f"{case_id}: {exc}")
            continue
        elapsed = int((time.monotonic() - started_case) * 1000)
        attribution_ms = elapsed
        attributed += 1
        top1_types = _candidate_types(attribution, 1)
        top3_types = _candidate_types(attribution, 3)
        first_type = top1_types[0] if top1_types else "UNKNOWN"

        gold_steps: list[str] = []
        if attribution.get("trace_id"):
            try:
                trace = load_trace(attribution["trace_id"])
                gold_steps = _gold_step_ids(trace)
            except TraceNotFoundError:
                gold_steps = []
            except Exception:  # noqa: BLE001
                gold_steps = []
        else:
            trace = None

        row: dict[str, Any] = {
            "case_id": case_id,
            "trace_id": attribution.get("trace_id"),
            "gold_cause_type": gold_type,
            "first_candidate_type": first_type,
            "top3_candidate_types": top3_types,
            "gold_step_ids": gold_steps,
            "attribution_id": attribution.get("attribution_id"),
            "attribution_ms": attribution_ms,
        }
        if gold_type:
            labeled += 1
            hit1 = first_type == gold_type
            hit3 = gold_type in top3_types
            type_top1 += int(hit1)
            type_top3 += int(hit3)
            if first_type == "UNKNOWN":
                unknown_first += 1
            elif not hit1:
                misattribution += 1
            if len(top3_types) > 1:
                multi_candidate += 1
            row.update({"type_top1_hit": hit1, "type_top3_hit": hit3})
        if gold_steps:
            step_labeled += 1
            steps1 = _candidate_steps(attribution, 1)
            steps3 = _candidate_steps(attribution, 3)
            hit_s1 = any(step_id in steps1 for step_id in gold_steps)
            hit_s3 = any(step_id in steps3 for step_id in gold_steps)
            step_top1 += int(hit_s1)
            step_top3 += int(hit_s3)
            row.update({"step_top1_hit": hit_s1, "step_top3_hit": hit_s3})

        if include_repair and trace is not None:
            success, error, repair_ms = _repair_success(trace)
            if success is not None:
                repair_attempted += 1
                repair_succeeded += int(success)
            elif error:
                repair_errors.append(f"{case_id}: {error}")
            if repair_ms is not None:
                attribution_ms += repair_ms
            row["repair_verified"] = success
            row["repair_error"] = error
        diagnosis_ms.append(attribution_ms)
        rows.append(row)

    gold_type_counts = Counter(
        _gold_type(gold_cases.get(case.get("case_id"))) for case in cases
    )
    gold_type_counts.pop(None, None)
    majority_type = None
    majority_baseline = None
    random_baseline = None
    if gold_type_counts:
        majority_type, majority_count = gold_type_counts.most_common(1)[0]
        majority_baseline = _safe_div(majority_count, sum(gold_type_counts.values()))
        random_baseline = round(1 / len(gold_type_counts), 4)

    cost = _evaluation_cost(evaluation_run_id)
    total_ms = int((time.monotonic() - started) * 1000)

    metrics = {
        "labeled_cases": labeled,
        "attributed_cases": attributed,
        "unlabeled_cases": max(0, len(cases) - labeled),
        "type_top1_accuracy": _safe_div(type_top1, labeled),
        "type_top3_coverage": _safe_div(type_top3, labeled),
        "step_labeled_cases": step_labeled,
        "step_top1_accuracy": _safe_div(step_top1, step_labeled),
        "step_top3_coverage": _safe_div(step_top3, step_labeled),
        "misattribution_rate": _safe_div(misattribution, labeled),
        "unknown_first_rate": _safe_div(unknown_first, labeled),
        "multi_candidate_rate": _safe_div(multi_candidate, labeled),
        "repair_attempted_cases": repair_attempted,
        "repair_success_rate": _safe_div(repair_succeeded, repair_attempted)
        if repair_attempted
        else None,
        "repair_note": (
            None
            if repair_attempted
            else "未开启或无法执行受控修复验证（repair_success_rate=null，不伪造）"
        ),
        "average_diagnosis_ms": round(sum(diagnosis_ms) / len(diagnosis_ms), 2)
        if diagnosis_ms
        else None,
        "max_diagnosis_ms": max(diagnosis_ms) if diagnosis_ms else None,
        "average_cost": cost.get("average_cost"),
        "cost_currency": cost.get("currency"),
        "cost_status": cost.get("status"),
        "cost_note": cost.get("note"),
        "pricing_versions": cost.get("pricing_versions"),
        "baselines": {
            "majority_cause_type": majority_type,
            "majority_type_baseline_top1": majority_baseline,
            "random_uniform_expected_top1": random_baseline,
            "gold_type_distribution": dict(gold_type_counts),
            "note": "基线仅说明数据集类别分布，不代表任何真实模型性能",
        },
    }

    metrics_id = f"rcm-{uuid.uuid4().hex}"
    result = {
        "metrics_id": metrics_id,
        "engine_version": "1.0.0",
        "generated_at": _now_iso(),
        "evaluation_run_id": evaluation_run_id,
        "dataset_id": dataset_id,
        "dataset_version": dataset.get("dataset_version"),
        "data_source": report.get("dataset_data_source"),
        "bad_case_count": len([c for c in (report.get("case_results") or []) if c.get("is_bad_case")]),
        "evaluated_cases": len(cases),
        "attributed_cases": attributed,
        "include_repair": bool(include_repair),
        "metrics": metrics,
        "metric_definitions": METRIC_DEFINITIONS,
        "cases": rows,
        "errors": {
            "attribution_errors": type_errors,
            "repair_errors": repair_errors[:20],
        },
        "elapsed_ms": total_ms,
        "limitations": [
            "金标准来自受控故障注入数据集的合成案例，不代表真实生产故障分布",
            "归因候选为规则/证据驱动，Top-1/Top-3 命中不代表因果证明",
            "模拟执行器数据不代表真实模型性能；成本为本地价格表估算，不是 Provider 账单",
            "有金标准但未成功归因的案例计入分母但不计命中（不剔除失败样本）",
            "修复成功率为 null 时表示未验证，绝不按 0 或 100% 处理",
        ],
        "paid_api_called": False,
    }
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    path = METRICS_DIR / f"{metrics_id}.json"
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result["report_path"] = str(path.relative_to(PROJECT_ROOT))
    return result


def get_metrics(metrics_id: str) -> dict[str, Any]:
    if not metrics_id.startswith("rcm-") or not metrics_id[4:].isalnum():
        raise RootCauseMetricsNotFoundError(f"非法指标报告 ID: {metrics_id!r}")
    path = METRICS_DIR / f"{metrics_id}.json"
    if not path.exists():
        raise RootCauseMetricsNotFoundError(f"指标报告不存在: {metrics_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_metrics(limit: int = 20) -> dict[str, Any]:
    if not METRICS_DIR.exists():
        return {"count": 0, "reports": [], "paid_api_called": False}
    files = sorted(
        METRICS_DIR.glob("rcm-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[: max(1, limit)]
    rows: list[dict[str, Any]] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        metrics = data.get("metrics") or {}
        rows.append(
            {
                "metrics_id": data.get("metrics_id"),
                "evaluation_run_id": data.get("evaluation_run_id"),
                "dataset_id": data.get("dataset_id"),
                "generated_at": data.get("generated_at"),
                "evaluated_cases": data.get("evaluated_cases"),
                "labeled_cases": metrics.get("labeled_cases"),
                "type_top1_accuracy": metrics.get("type_top1_accuracy"),
                "type_top3_coverage": metrics.get("type_top3_coverage"),
                "step_top1_accuracy": metrics.get("step_top1_accuracy"),
                "repair_success_rate": metrics.get("repair_success_rate"),
                "average_cost": metrics.get("average_cost"),
                "cost_status": metrics.get("cost_status"),
            }
        )
    return {"count": len(rows), "reports": rows, "paid_api_called": False}
