"""Cost–Latency–Quality Pareto Analysis（第 54 步）。

在既有 EvaluationEngine / EvaluationVersionComparison / CostAnalytics / RegressionDetector /
QualityGate 之上做三维帕累托分析：

- 质量（task_success_rate）：越高越好；
- 延迟（average_latency_ms）：越低越好；
- 成本（average_cost，按任务平均）：越低越好。

诚实性约束（硬性）：
1. 候选全部来自**本进程内真实执行过的评测运行**（evaluation_store），不伪造 Agent 版本；
   `agent_version` 缺失时为 null 并注明原因。
2. 缺失指标一律为 null，**绝不把 UNKNOWN 当 0**；缺失任一维度的候选不进入帕累托前沿，
   而是列入 excluded 并写明原因。
3. 真实 API 与模拟执行数据**不混合比较**：混用时整体判定 NOT_COMPARABLE，前沿为空，
   并明确「不得解释为真实模型性能比较」。
4. 成本来自 CostAnalytics：金额是本地价格表估算，不是 Provider 实际账单；
   PRICE_UNKNOWN 时 average_cost 为 null（不按 0 计费）。
5. 分析只读取本地数据，不调用任何付费 API。
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "ParetoError",
    "ParetoNotFoundError",
    "ParetoValidationError",
    "METRIC_DEFINITIONS",
    "SAFETY_GATE_ORDER",
    "build_candidates",
    "list_candidates",
    "create_analysis",
    "get_analysis",
    "list_analyses",
    "filter_analysis",
    "candidate_evaluation_run",
    "candidate_traces",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PARETO_DIR = PROJECT_ROOT / "data" / "pareto" / "reports"
MAX_STORED_REPORTS = 100
DEFAULT_MIN_SAMPLE_SIZE = 3

METRIC_DEFINITIONS = {
    "quality": {
        "field": "task_success_rate",
        "direction": "max",
        "label": "质量 / 任务成功率",
        "definition": "评测报告中 task_success_rate = 整体 PASS 案例数 / 已实际执行案例数",
    },
    "latency": {
        "field": "average_latency_ms",
        "direction": "min",
        "label": "平均延迟（ms）",
        "definition": "评测报告 metrics.average_duration_ms（每案例 Trace 执行时长均值）",
    },
    "cost": {
        "field": "average_cost",
        "direction": "min",
        "label": "平均成本（按任务）",
        "definition": (
            "CostAnalytics 的评测总成本 / 已执行案例数；金额为本地价格表估算，"
            "不是 Provider 实际账单；PRICE_UNKNOWN 时为 null"
        ),
    },
}

SAFETY_GATE_ORDER = {
    "PASS": 4,
    "WARN": 3,
    "INCONCLUSIVE": 2,
    "BLOCK": 1,
    "NOT_AVAILABLE": 0,
}

_COMPARABILITY_ORDER = {
    "COMPARABLE": 0,
    "COMPARABLE_WITH_WARNINGS": 1,
    "NOT_COMPARABLE": 2,
    "INSUFFICIENT_DATA": 3,
}

CONSTRAINT_FIELDS = (
    "minimum_quality",
    "maximum_latency_ms",
    "maximum_cost",
    "required_safety_gate",
    "dataset_version",
)


class ParetoError(ValueError):
    """帕累托分析基础异常。"""


class ParetoValidationError(ParetoError):
    """约束 / 输入参数非法。"""


class ParetoNotFoundError(ParetoError):
    """分析报告或候选不存在。"""


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

_STORE_LOCK = threading.Lock()
_ANALYSES: dict[str, dict[str, Any]] = {}
_ANALYSIS_ORDER: list[str] = []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist_analysis(report: dict[str, Any]) -> Path:
    PARETO_DIR.mkdir(parents=True, exist_ok=True)
    path = PARETO_DIR / f"{report['analysis_id']}.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def _remember(report: dict[str, Any]) -> None:
    with _STORE_LOCK:
        _ANALYSES[report["analysis_id"]] = report
        _ANALYSIS_ORDER.append(report["analysis_id"])
        while len(_ANALYSIS_ORDER) > MAX_STORED_REPORTS:
            old = _ANALYSIS_ORDER.pop(0)
            _ANALYSES.pop(old, None)


def _load_analysis(analysis_id: str) -> dict[str, Any]:
    if not isinstance(analysis_id, str) or not analysis_id.startswith("pareto-"):
        raise ParetoNotFoundError(f"非法帕累托分析 ID: {analysis_id!r}")
    with _STORE_LOCK:
        report = _ANALYSES.get(analysis_id)
        if report is not None:
            return report
    path = PARETO_DIR / f"{analysis_id}.json"
    if not path.exists():
        raise ParetoNotFoundError(f"帕累托分析报告不存在: {analysis_id}")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParetoNotFoundError(f"帕累托分析报告不可读: {analysis_id}（{exc}）") from exc
    _remember(report)
    return report


# ---------------------------------------------------------------------------
# 候选构建
# ---------------------------------------------------------------------------

def _metric_value(metric: Any) -> float | None:
    if isinstance(metric, dict):
        value = metric.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None
    if isinstance(metric, (int, float)) and not isinstance(metric, bool):
        return float(metric)
    return None


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _short_hash(value: str, length: int = 10) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _gate_status_by_evaluation_run(limit: int = 20) -> dict[str, dict[str, Any]]:
    """从已落盘的质量门禁报告中读取 candidate 评测运行对应的安全门禁状态。

    仅使用真实存在的门禁报告；没有门禁记录时不伪造状态。
    """
    from backend.app.services.quality_gate import QUALITY_GATE_DIR

    mapping: dict[str, dict[str, Any]] = {}
    if not QUALITY_GATE_DIR.exists():
        return mapping
    files = sorted(
        QUALITY_GATE_DIR.glob("qg-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in files[:limit]:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 单个坏文件不影响候选构建
            continue
        candidate_run = (report.get("candidate_version") or {}).get("evaluation_run_id")
        if not candidate_run:
            continue
        mapping.setdefault(
            candidate_run,
            {
                "safety_gate_status": report.get("gate_status"),
                "gate_report_id": report.get("gate_report_id"),
                "gate_exit_code": report.get("exit_code"),
                "gate_generated_at": report.get("generated_at"),
            },
        )
    return mapping


def _candidate_from_run(
    rec: dict[str, Any],
    *,
    gate_map: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    from backend.app.services.evaluation_version_comparison import (
        build_version_descriptor,
    )
    from backend.app.services.token_analytics import analyze_evaluation_run

    report = rec.get("report") or {}
    metrics = report.get("metrics") or {}
    run_id = rec["run_id"]

    try:
        desc = build_version_descriptor(run_id)
    except Exception as exc:  # noqa: BLE001 - 无法构造版本描述时不伪造
        desc = {
            "evaluation_run_id": run_id,
            "agent_version": None,
            "agent_version_note": f"版本描述不可用: {type(exc).__name__}",
            "dataset_version": report.get("dataset_version"),
            "evaluator_version": report.get("engine_version"),
            "judge_mode": rec.get("judge_mode", "none"),
            "rubric_version": None,
            "execution_config": {},
        }

    quality = _metric_value(metrics.get("task_success_rate"))
    latency = _finite_float(metrics.get("average_duration_ms"))
    safety_rate = _metric_value(metrics.get("safety_pass_rate"))
    sample_size = report.get("executed_cases")
    if not isinstance(sample_size, int) or isinstance(sample_size, bool):
        sample_size = None

    env_model = (report.get("environment") or {}).get("model") or {}
    simulated = bool(env_model.get("simulated"))
    paid_api_called = bool(env_model.get("paid_api_called"))
    if simulated:
        data_source = "simulated"
    elif paid_api_called:
        data_source = "real_api"
    else:
        data_source = "unknown"

    total_cost: float | None = None
    average_cost: float | None = None
    cost_currency: str | None = None
    cost_status: str | None = None
    pricing_versions: list[str] = []
    pricing_sources: list[str] = []
    cost_note: str | None = None
    cost_error: str | None = None
    if report.get("trace_ids"):
        try:
            cost = analyze_evaluation_run(run_id)
            aggregate_cost = cost.get("aggregate", {}).get("cost", {})
            total_cost = _finite_float(aggregate_cost.get("amount"))
            cost_currency = aggregate_cost.get("currency")
            cost_status = aggregate_cost.get("status")
            pricing_versions = list(aggregate_cost.get("pricing_versions") or [])
            pricing_sources = list(aggregate_cost.get("pricing_sources") or [])
            cost_note = aggregate_cost.get("note")
            if total_cost is not None and sample_size:
                average_cost = round(total_cost / sample_size, 8)
        except Exception as exc:  # noqa: BLE001 - 成本不可计算 → null，不按 0 计
            cost_error = f"{type(exc).__name__}: {exc}"
    else:
        cost_error = "评测报告没有关联 Trace，无法计算成本（null，不当作 0）"

    missing_fields: list[str] = []
    if quality is None:
        missing_fields.append("task_success_rate")
    if latency is None:
        missing_fields.append("average_latency_ms")
    if average_cost is None:
        missing_fields.append("average_cost")

    gate = gate_map.get(run_id) or {}
    safety_gate_status = gate.get("safety_gate_status")
    if safety_gate_status is None and safety_rate is None:
        safety_gate_status = "NOT_AVAILABLE"

    dataset_id = report.get("dataset_id") or desc.get("dataset_id") or rec.get("dataset_id")
    dataset_version = report.get("dataset_version") or desc.get("dataset_version")
    candidate = {
        "candidate_id": f"cand-{run_id}",
        "agent_version": desc.get("agent_version"),
        "agent_version_note": desc.get("agent_version_note"),
        "evaluation_run_id": run_id,
        "evaluation_created_at": rec.get("created_at"),
        "evaluation_elapsed_ms": rec.get("elapsed_ms"),
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "dataset_group_key": f"{dataset_id or 'UNKNOWN'}@{dataset_version or 'UNKNOWN'}",
        "evaluator_version": report.get("engine_version") or desc.get("evaluator_version"),
        "judge_mode": desc.get("judge_mode") or rec.get("judge_mode", "none"),
        "rubric_id": desc.get("rubric_id"),
        "rubric_version": desc.get("rubric_version"),
        "task_success_rate": quality,
        "average_latency_ms": latency,
        "average_cost": average_cost,
        "total_cost": total_cost,
        "currency": cost_currency,
        "cost_status": cost_status,
        "cost_note": cost_note,
        "cost_error": cost_error,
        "pricing_versions": pricing_versions,
        "pricing_sources": pricing_sources,
        "sample_size": sample_size,
        "data_source": data_source,
        "execution_mode": (
            "controlled_simulation" if simulated else "real_api" if paid_api_called else "unknown"
        ),
        "paid_api_called": paid_api_called,
        "safety_pass_rate": safety_rate,
        "safety_gate_status": safety_gate_status,
        "gate_report_id": gate.get("gate_report_id"),
        "missing_fields": missing_fields,
        "metrics_complete": not missing_fields,
        "trace_ids": list(report.get("trace_ids") or [])[:50],
        "trace_count": len(report.get("trace_ids") or []),
        "comparability_status": "UNKNOWN",
        "comparability_issues": [],
        "in_comparison_group": True,
        "exclusion_reasons": [],
        "eligible": False,
        "dominated_by": [],
        "dominates": [],
        "dominance": "EXCLUDED",
        "tie_group": None,
    }
    return candidate


def build_candidates(*, limit: int = 50) -> dict[str, Any]:
    """读取本进程内真实执行过的评测运行，构建候选版本列表（不伪造数据）。"""
    from backend.app.services.evaluation_store import get_evaluation_store

    store = get_evaluation_store()
    items = store.list_all()
    items = sorted(
        items,
        key=lambda r: r.get("created_at") or "",
        reverse=True,
    )
    if limit > 0:
        items = items[:limit]
    gate_map = _gate_status_by_evaluation_run()

    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in items:
        if item.get("status") != "succeeded":
            skipped.append(
                {
                    "evaluation_run_id": item["run_id"],
                    "reason": f"评测运行状态为 {item.get('status')!r}，无报告",
                }
            )
            continue
        try:
            rec = store.get(item["run_id"])
        except Exception as exc:  # noqa: BLE001
            skipped.append(
                {"evaluation_run_id": item["run_id"], "reason": f"运行记录不可读: {exc}"}
            )
            continue
        candidate = _candidate_from_run(rec, gate_map=gate_map)
        if candidate is not None:
            candidates.append(candidate)

    return {
        "generated_at": _now_iso(),
        "count": len(candidates),
        "candidates": candidates,
        "skipped_runs": skipped,
        "datasets": _dataset_groups(candidates),
        "data_sources": _count_by(candidates, "data_source"),
        "paid_api_called": False,
        "notes": [
            "候选来自本进程内真实评测运行（evaluation_store），不伪造 Agent 版本",
            "模拟执行器数据不代表真实模型性能；真实 API 与模拟数据不混合比较",
            "缺失指标为 null，绝不按 0 处理",
        ],
    }


def _count_by(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get(field) or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _dataset_groups(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 dataset_id + dataset_version 组合分组（同版本不同数据集不算同组）。"""
    groups: dict[str, dict[str, Any]] = {}
    for c in candidates:
        dataset_id = str(c.get("dataset_id") or "UNKNOWN")
        dataset_version = str(c.get("dataset_version") or "UNKNOWN")
        key = f"{dataset_id}@{dataset_version}"
        group = groups.setdefault(
            key,
            {
                "group_key": key,
                "dataset_id": dataset_id,
                "dataset_version": dataset_version,
                "dataset_ids": set(),
                "candidate_count": 0,
                "latest_evaluated_at": None,
            },
        )
        group["candidate_count"] += 1
        group["dataset_ids"].add(dataset_id)
        latest = c.get("evaluation_created_at")
        if latest and (not group["latest_evaluated_at"] or latest > group["latest_evaluated_at"]):
            group["latest_evaluated_at"] = latest
    out = []
    for group in groups.values():
        group["dataset_ids"] = sorted(group["dataset_ids"])
        out.append(group)
    return sorted(out, key=lambda g: (-g["candidate_count"], g["group_key"]))


def list_candidates(*, limit: int = 50) -> dict[str, Any]:
    """查询候选版本（不进行分析，供前端选择数据集/版本）。"""
    data = build_candidates(limit=limit)
    comparability = _assess_comparability(
        data["candidates"],
        dataset_version=None,
        min_sample_size=DEFAULT_MIN_SAMPLE_SIZE,
    )
    return {
        "generated_at": data["generated_at"],
        "count": data["count"],
        "candidates": data["candidates"],
        "skipped_runs": data["skipped_runs"],
        "datasets": data["datasets"],
        "data_sources": data["data_sources"],
        "metric_definitions": METRIC_DEFINITIONS,
        "safety_gate_options": list(SAFETY_GATE_ORDER.keys()),
        "comparability": comparability,
        "paid_api_called": False,
        "notes": data["notes"],
    }


# ---------------------------------------------------------------------------
# 可比性检查
# ---------------------------------------------------------------------------

def _normalize_constraints(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ParetoValidationError("约束必须是 JSON 对象")
    unknown = set(raw) - set(CONSTRAINT_FIELDS)
    if unknown:
        raise ParetoValidationError(
            f"未知约束字段: {sorted(unknown)}（允许: {', '.join(CONSTRAINT_FIELDS)}）"
        )

    out: dict[str, Any] = {field: None for field in CONSTRAINT_FIELDS}
    for field in ("minimum_quality", "maximum_latency_ms", "maximum_cost"):
        value = raw.get(field)
        if value is None:
            continue
        value = _finite_float(value)
        if value is None or value < 0:
            raise ParetoValidationError(f"{field} 必须是非负数值或 null")
        out[field] = value
    if out["minimum_quality"] is not None and out["minimum_quality"] > 1:
        raise ParetoValidationError("minimum_quality 必须在 0..1 之间")

    gate = raw.get("required_safety_gate")
    if gate is not None:
        gate = str(gate).upper()
        if gate not in SAFETY_GATE_ORDER:
            raise ParetoValidationError(
                f"required_safety_gate 必须是 {', '.join(SAFETY_GATE_ORDER)} 之一"
            )
        out["required_safety_gate"] = gate

    dataset_version = raw.get("dataset_version")
    if dataset_version is not None:
        dataset_version = str(dataset_version).strip()
        if not dataset_version:
            raise ParetoValidationError("dataset_version 不能为空字符串")
        out["dataset_version"] = dataset_version
    return out


def _worst_status(statuses: list[str]) -> str:
    if not statuses:
        return "COMPARABLE"
    return max(statuses, key=lambda s: _COMPARABILITY_ORDER.get(s, 0))


def _issue(
    code: str,
    severity: str,
    field: str,
    message: str,
    *,
    candidate_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "field": field,
        "message": message,
        "candidate_ids": candidate_ids or [],
    }


def _assess_comparability(
    candidates: list[dict[str, Any]],
    *,
    dataset_version: str | None,
    min_sample_size: int,
) -> dict[str, Any]:
    """对候选集合做 9 项可比性检查，并逐候选给出状态。

    检查：数据集一致、评分规则一致、指标定义一致、执行模式一致、币种一致、
    价格版本一致、样本量合理、数据来源兼容、缺失指标。
    """
    issues: list[dict[str, Any]] = []
    if not candidates:
        return {
            "verdict": "INSUFFICIENT_DATA",
            "comparable": False,
            "group_key": None,
            "group_dataset_id": None,
            "group_dataset_version": None,
            "group_candidate_count": 0,
            "outside_group_candidate_count": 0,
            "dataset_groups": [],
            "issues": [],
            "critical_issues": [],
            "warning_issues": [],
            "real_candidate_count": 0,
            "simulated_candidate_count": 0,
            "mixed_real_and_simulated": False,
            "minimum_sample_size": min_sample_size,
            "notes": ["没有可用候选；请先运行评测"],
        }

    groups = _dataset_groups(candidates)
    group = None
    if dataset_version is not None:
        for g in groups:
            if dataset_version in {
                g["group_key"],
                g["dataset_version"],
                g["dataset_id"],
            }:
                group = g
                break
        if group is None:
            issues.append(
                _issue(
                    "DATASET_VERSION_FILTER_MISSING",
                    "CRITICAL",
                    "dataset_version",
                    f"约束的 dataset_version={dataset_version!r} 不在候选数据集中",
                )
            )
            group = groups[0]
    else:
        group = groups[0]

    chosen_key = group["group_key"]
    in_group: list[dict[str, Any]] = []
    outside: list[dict[str, Any]] = []
    for c in candidates:
        candidate_key = c.get("dataset_group_key") or (
            f"{c.get('dataset_id') or 'UNKNOWN'}@{c.get('dataset_version') or 'UNKNOWN'}"
        )
        if candidate_key == chosen_key:
            c["in_comparison_group"] = True
            in_group.append(c)
        else:
            c["in_comparison_group"] = False
            outside.append(c)

    if len(groups) > 1:
        dataset_ids = {g["dataset_id"] for g in groups}
        versions = {g["dataset_version"] for g in groups}
        code = "DATASET_ID_MISMATCH" if len(dataset_ids) > 1 else "DATASET_VERSION_MISMATCH"
        # 跨数据集候选被排除出比较组；这不代表组内候选不可比，故记为 WARNING +
        # 对组外候选逐条 NOT_COMPARABLE，避免“选一个数据集却无法分析”。
        issues.append(
            _issue(
                code,
                "WARNING",
                "dataset_id" if code == "DATASET_ID_MISMATCH" else "dataset_version",
                "候选来自不同数据集，跨数据集指标不可直接比较；"
                f"分析仅使用 {chosen_key!r} 组，其余候选已排除",
                candidate_ids=[c["candidate_id"] for c in outside],
            )
        )

    # 评分规则 / 评测器版本
    evaluator_versions = {c.get("evaluator_version") for c in in_group if c.get("evaluator_version")}
    if len(evaluator_versions) > 1:
        issues.append(
            _issue(
                "EVALUATOR_VERSION_MISMATCH",
                "WARNING",
                "evaluator_version",
                f"评测器版本不一致: {sorted(evaluator_versions)}，判定口径可能变化",
            )
        )
    rubric_versions = {c.get("rubric_version") for c in in_group if c.get("rubric_version")}
    if len(rubric_versions) > 1:
        issues.append(
            _issue(
                "SCORING_RULE_MISMATCH",
                "CRITICAL",
                "rubric_version",
                f"评分规则（Rubric）版本不一致: {sorted(rubric_versions)}",
            )
        )
    judge_modes = {c.get("judge_mode") for c in in_group if c.get("judge_mode")}
    if len(judge_modes) > 1:
        issues.append(
            _issue(
                "SCORING_MODE_MISMATCH",
                "WARNING",
                "judge_mode",
                f"Judge 模式不一致: {sorted(judge_modes)}，评分口径可能不同",
            )
        )

    # 指标定义一致性（本引擎固定三维定义；这里显式确认）
    issues.append(
        _issue(
            "METRIC_DEFINITION_OK",
            "INFO",
            "metric_definition",
            "三维指标均使用固定定义（task_success_rate / average_duration_ms / 平均成本）",
        )
    )

    # 执行模式 / 数据来源
    sources = {c.get("data_source") for c in in_group}
    real_count = sum(1 for c in in_group if c.get("data_source") == "real_api")
    sim_count = sum(1 for c in in_group if c.get("data_source") == "simulated")
    mixed_sources = real_count > 0 and sim_count > 0
    if mixed_sources:
        issues.append(
            _issue(
                "MIXED_REAL_AND_SIMULATED",
                "CRITICAL",
                "data_source",
                "真实 API 数据与模拟数据混合，禁止解释为真实模型性能比较",
                candidate_ids=[c["candidate_id"] for c in in_group],
            )
        )
    elif "unknown" in sources:
        issues.append(
            _issue(
                "DATA_SOURCE_UNKNOWN",
                "WARNING",
                "data_source",
                "存在数据来源未知的候选（既非模拟也非真实 API）",
            )
        )

    # 币种
    currencies = {
        str(c.get("currency")) for c in in_group if c.get("currency")
    }
    if len(currencies) > 1:
        issues.append(
            _issue(
                "CURRENCY_MISMATCH",
                "CRITICAL",
                "currency",
                f"成本币种不一致: {sorted(currencies)}，金额不可直接比较",
            )
        )
    elif not currencies:
        issues.append(
            _issue(
                "CURRENCY_UNKNOWN",
                "WARNING",
                "currency",
                "候选成本币种未知或成本不可用（不得按 0 计）",
            )
        )

    # 价格版本
    pricing_versions: set[str] = set()
    for c in in_group:
        pricing_versions.update(c.get("pricing_versions") or [])
    if len(pricing_versions) > 1:
        issues.append(
            _issue(
                "PRICING_VERSION_MISMATCH",
                "WARNING",
                "pricing_version",
                f"价格版本不一致: {sorted(pricing_versions)}，成本估算口径可能不同",
            )
        )

    # 样本量
    small = [
        c
        for c in in_group
        if c.get("sample_size") is None or int(c["sample_size"]) < min_sample_size
    ]
    if small:
        issues.append(
            _issue(
                "SMALL_SAMPLE_SIZE",
                "WARNING",
                "sample_size",
                f"有 {len(small)} 个候选的已执行样本数 < {min_sample_size}，统计稳定性不足",
                candidate_ids=[c["candidate_id"] for c in small],
            )
        )

    # 缺失指标（逐候选）
    incomplete = [c for c in in_group if c.get("missing_fields")]
    if incomplete:
        issues.append(
            _issue(
                "MISSING_METRICS",
                "WARNING",
                "metrics",
                f"有 {len(incomplete)} 个候选缺失三维指标，已排除出帕累托前沿（不按 0 计）",
                candidate_ids=[c["candidate_id"] for c in incomplete],
            )
        )

    critical = [i for i in issues if i["severity"] == "CRITICAL"]
    warnings = [i for i in issues if i["severity"] == "WARNING"]
    verdict = (
        "NOT_COMPARABLE"
        if critical
        else "COMPARABLE_WITH_WARNINGS"
        if warnings
        else "COMPARABLE"
    )

    # 逐候选状态：不在组内 / 指标缺失 / 组级判定
    for c in candidates:
        statuses: list[str] = []
        c_issues: list[dict[str, Any]] = []
        if not c.get("in_comparison_group"):
            statuses.append("NOT_COMPARABLE")
            c_issues.append(
                {
                    "code": "DATASET_VERSION_MISMATCH",
                    "severity": "CRITICAL",
                    "message": f"候选属于其他数据集版本（{c.get('dataset_version')!r}）",
                }
            )
        elif c.get("missing_fields"):
            statuses.append("INSUFFICIENT_DATA")
            c_issues.append(
                {
                    "code": "MISSING_METRICS",
                    "severity": "CRITICAL",
                    "message": f"缺失指标: {', '.join(c['missing_fields'])}",
                }
            )
        elif verdict == "NOT_COMPARABLE":
            statuses.append("NOT_COMPARABLE")
        elif verdict == "COMPARABLE_WITH_WARNINGS":
            statuses.append("COMPARABLE_WITH_WARNINGS")
        else:
            statuses.append("COMPARABLE")
        c["comparability_status"] = _worst_status(statuses)
        c["comparability_issues"] = c_issues

    return {
        "verdict": verdict,
        "comparable": verdict != "NOT_COMPARABLE",
        "group_key": chosen_key,
        "group_dataset_id": group["dataset_id"],
        "group_dataset_version": group["dataset_version"],
        "group_candidate_count": len(in_group),
        "outside_group_candidate_count": len(outside),
        "dataset_groups": groups,
        "issues": issues,
        "critical_issues": critical,
        "warning_issues": warnings,
        "real_candidate_count": real_count,
        "simulated_candidate_count": sim_count,
        "mixed_real_and_simulated": mixed_sources,
        "minimum_sample_size": min_sample_size,
        "notes": [
            "不可比结果不得解释为真实模型性能比较或版本提升",
            "跨数据集比较只作诊断线索；前端默认按数据集分组",
            "缺失指标一律为 null，不按 0 处理",
        ],
    }


# ---------------------------------------------------------------------------
# Pareto 计算
# ---------------------------------------------------------------------------

def _dominates(a: dict[str, Any], b: dict[str, Any]) -> tuple[bool, list[str]]:
    """判断 a 是否支配 b（质量更高、延迟更低、成本更低，且至少一项严格更优）。

    三项指标全部已知（未知候选不参与支配计算）；完全相等的候选互不支配。
    """
    qa, qb = a["task_success_rate"], b["task_success_rate"]
    la, lb = a["average_latency_ms"], b["average_latency_ms"]
    ca, cb = a["average_cost"], b["average_cost"]
    strict_axes: list[str] = []
    if qa < qb or la > lb or ca > cb:
        return False, []
    if qa > qb:
        strict_axes.append("quality")
    if la < lb:
        strict_axes.append("latency")
    if ca < cb:
        strict_axes.append("cost")
    return bool(strict_axes), strict_axes


def _metric_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (
        a["task_success_rate"] == b["task_success_rate"]
        and a["average_latency_ms"] == b["average_latency_ms"]
        and a["average_cost"] == b["average_cost"]
    )


def _apply_constraints(
    candidates: list[dict[str, Any]],
    constraints: dict[str, Any],
) -> None:
    """计算每个候选是否满足约束，并记录排除原因（不修改指标）。"""
    required_gate = constraints.get("required_safety_gate")
    required_rank = SAFETY_GATE_ORDER.get(required_gate or "", -1)
    for c in candidates:
        reasons: list[str] = []
        if not c.get("in_comparison_group"):
            reasons.append("DATASET_VERSION_MISMATCH")
        if c.get("missing_fields"):
            reasons.append("MISSING_METRICS:" + ",".join(c["missing_fields"]))
        if c.get("comparability_status") in ("NOT_COMPARABLE", "INSUFFICIENT_DATA"):
            reasons.append(str(c["comparability_status"]))
        q = c.get("task_success_rate")
        if constraints["minimum_quality"] is not None:
            if q is None:
                reasons.append("MISSING_QUALITY")
            elif q < constraints["minimum_quality"]:
                reasons.append("BELOW_MINIMUM_QUALITY")
        lat = c.get("average_latency_ms")
        if constraints["maximum_latency_ms"] is not None:
            if lat is None:
                reasons.append("MISSING_LATENCY")
            elif lat > constraints["maximum_latency_ms"]:
                reasons.append("ABOVE_MAXIMUM_LATENCY")
        cost = c.get("average_cost")
        if constraints["maximum_cost"] is not None:
            if cost is None:
                reasons.append("MISSING_COST")
            elif cost > constraints["maximum_cost"]:
                reasons.append("ABOVE_MAXIMUM_COST")
        if required_gate is not None:
            gate = c.get("safety_gate_status")
            if not gate or gate == "NOT_AVAILABLE" or SAFETY_GATE_ORDER.get(gate, -1) < required_rank:
                reasons.append("SAFETY_GATE_NOT_SATISFIED")
        wanted_dataset = constraints.get("dataset_version")
        if wanted_dataset is not None:
            if wanted_dataset not in {
                str(c.get("dataset_version")),
                str(c.get("dataset_id")),
                str(c.get("dataset_group_key")),
            }:
                reasons.append("DATASET_VERSION_FILTER_MISMATCH")
        # 去重但保持顺序
        seen: set[str] = set()
        unique: list[str] = []
        for reason in reasons:
            if reason not in seen:
                seen.add(reason)
                unique.append(reason)
        c["exclusion_reasons"] = unique
        c["eligible"] = not unique
        c["feasible"] = not unique


def _compute_pareto(
    candidates: list[dict[str, Any]],
    *,
    constraints: dict[str, Any],
) -> dict[str, Any]:
    eligible = [c for c in candidates if c.get("eligible")]
    # 先清空上一次计算痕迹（filter 会重用同一份候选列表）
    for c in candidates:
        c["dominates"] = []
        c["dominated_by"] = []
        c["dominance"] = "EXCLUDED" if not c.get("eligible") else "NON_DOMINATED"
        c["tie_group"] = None

    relations: list[dict[str, Any]] = []
    for a in eligible:
        for b in eligible:
            if a is b:
                continue
            dominated, axes = _dominates(a, b)
            if dominated:
                a["dominates"].append(b["candidate_id"])
                b["dominated_by"].append(a["candidate_id"])
                relations.append(
                    {
                        "dominator_id": a["candidate_id"],
                        "dominated_id": b["candidate_id"],
                        "strict_axes": axes,
                        "margins": {
                            "quality": round(
                                a["task_success_rate"] - b["task_success_rate"], 6
                            ),
                            "latency_ms": round(
                                a["average_latency_ms"] - b["average_latency_ms"], 3
                            ),
                            "cost": round(a["average_cost"] - b["average_cost"], 8),
                        },
                        "definition": "更高/相等质量 且 更低/相等延迟 且 更低/相等成本，至少一项严格更优",
                    }
                )

    for c in eligible:
        c["dominance"] = "DOMINATED" if c["dominated_by"] else "NON_DOMINATED"

    # 并列处理：三项指标完全相同的非支配候选归入同一 tie_group
    tie_groups: list[dict[str, Any]] = []
    non_dominated = [c for c in eligible if c["dominance"] == "NON_DOMINATED"]
    assigned: set[str] = set()
    for c in non_dominated:
        if c["candidate_id"] in assigned:
            continue
        peers = [
            other
            for other in non_dominated
            if other["candidate_id"] not in assigned and _metric_equal(c, other)
            and other is not c
        ]
        group_id = f"tie-{_short_hash(c['candidate_id'] + str(len(tie_groups)), 6)}"
        members = [c, *peers]
        if peers:
            for member in members:
                member["tie_group"] = group_id
                assigned.add(member["candidate_id"])
            tie_groups.append(
                {
                    "tie_group": group_id,
                    "candidate_ids": [m["candidate_id"] for m in members],
                    "metrics": {
                        "quality": c["task_success_rate"],
                        "latency_ms": c["average_latency_ms"],
                        "cost": c["average_cost"],
                    },
                    "note": "三项指标完全相同，互不支配，并列进入帕累托前沿",
                }
            )

    def sort_key(c: dict[str, Any]) -> tuple[Any, ...]:
        return (
            -float(c["task_success_rate"]),
            float(c["average_latency_ms"]),
            float(c["average_cost"]),
            c["candidate_id"],
        )

    non_dominated_sorted = sorted(non_dominated, key=sort_key)
    dominated = sorted(
        [c for c in eligible if c["dominance"] == "DOMINATED"],
        key=lambda c: (-len(c["dominated_by"]), -float(c["task_success_rate"]), c["candidate_id"]),
    )
    for rank, c in enumerate(non_dominated_sorted, start=1):
        c["frontier_rank"] = rank

    return {
        "eligible_count": len(eligible),
        "frontier": non_dominated_sorted,
        "dominated": dominated,
        "dominance_relations": sorted(
            relations,
            key=lambda r: (r["dominated_id"], r["dominator_id"]),
        ),
        "tie_groups": tie_groups,
        "pareto_frontier_count": len(non_dominated_sorted),
        "dominated_count": len(dominated),
    }


def _analysis_status(
    candidates: list[dict[str, Any]],
    *,
    comparable_eligible: int,
    feasible: int,
    constraints: dict[str, Any],
) -> tuple[str, str]:
    if not candidates:
        return "NO_CANDIDATE", "没有可用的评测候选（请先运行评测）"
    if comparable_eligible == 0:
        statuses = {c.get("comparability_status") for c in candidates}
        if statuses and statuses.issubset({"INSUFFICIENT_DATA"}):
            return "INSUFFICIENT_DATA", "所有候选均缺失三维指标，无法计算帕累托前沿"
        return "NO_COMPARABLE_CANDIDATE", "没有可比候选（数据集/执行模式/币种或评分规则不一致）"
    if feasible == 0:
        constrained = any(
            constraints.get(field) is not None
            for field in ("minimum_quality", "maximum_latency_ms", "maximum_cost",
                          "required_safety_gate", "dataset_version")
        )
        if constrained:
            return "NO_FEASIBLE_CANDIDATE", "没有满足当前约束的候选版本"
        return "NO_COMPARABLE_CANDIDATE", "候选均因数据缺失或不可比被排除"
    return "OK", "满足条件的候选版本已计算帕累托前沿"


def _filtered_view(
    report: dict[str, Any],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    """基于已保存的报告重新应用约束并计算帕累托前沿（不伪造新指标）。"""
    copied: list[dict[str, Any]] = []
    for c in report.get("candidates") or []:
        clone = json.loads(json.dumps(c, ensure_ascii=False))
        clone.setdefault("in_comparison_group", True)
        copied.append(clone)
    # 切换 dataset_version 时必须基于新数据集组重新做可比性检查；
    # 否则会沿用原分析的「组外不可比」状态，把新组也误判为不可比。
    comparability = report.get("comparability")
    if constraints.get("dataset_version") is not None:
        comparability = _assess_comparability(
            copied,
            dataset_version=str(constraints["dataset_version"]),
            min_sample_size=int(
                (report.get("comparability") or {}).get("minimum_sample_size")
                or DEFAULT_MIN_SAMPLE_SIZE
            ),
        )
    _apply_constraints(copied, constraints)
    pareto = _compute_pareto(copied, constraints=constraints)
    comparable_eligible = sum(
        1
        for c in copied
        if c.get("in_comparison_group")
        and c.get("comparability_status") not in ("NOT_COMPARABLE", "INSUFFICIENT_DATA")
        and c.get("metrics_complete")
    )
    feasible = pareto["eligible_count"]
    status, message = _analysis_status(
        copied,
        comparable_eligible=comparable_eligible,
        feasible=feasible,
        constraints=constraints,
    )
    view = {
        "analysis_id": report.get("analysis_id"),
        "generated_at": _now_iso(),
        "source_generated_at": report.get("generated_at"),
        "constraints": constraints,
        "status": status,
        "status_message": message,
        "feasible_count": feasible,
        "candidate_count": len(copied),
        "candidates": copied,
        "frontier": pareto["frontier"],
        "dominated": pareto["dominated"],
        "dominance_relations": pareto["dominance_relations"],
        "tie_groups": pareto["tie_groups"],
        "pareto_frontier_count": pareto["pareto_frontier_count"],
        "dominated_count": pareto["dominated_count"],
        "comparability": comparability,
        "metric_definitions": report.get("metric_definitions"),
        "objective": report.get("objective"),
        "data_source_summary": report.get("data_source_summary"),
        "warnings": report.get("warnings"),
        "limitations": report.get("limitations"),
        "paid_api_called": False,
        "view_of": report.get("analysis_id"),
    }
    return view


def create_analysis(
    constraints: dict[str, Any] | None = None,
    *,
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
    limit: int = 50,
) -> dict[str, Any]:
    """构建候选 → 可比性检查 → 约束筛选 → 帕累托前沿 → 结构化报告。"""
    normalized = _normalize_constraints(constraints)
    if not isinstance(min_sample_size, int) or min_sample_size < 1:
        raise ParetoValidationError("min_sample_size 必须是 >= 1 的整数")
    built = build_candidates(limit=limit)
    candidates = built["candidates"]
    comparability = _assess_comparability(
        candidates,
        dataset_version=normalized["dataset_version"],
        min_sample_size=min_sample_size,
    )

    # dataset_version 约束已在可比性分组与逐候选约束中处理
    _apply_constraints(candidates, normalized)
    pareto = _compute_pareto(candidates, constraints=normalized)

    comparable_eligible = sum(
        1
        for c in candidates
        if c.get("in_comparison_group")
        and c.get("comparability_status") not in ("NOT_COMPARABLE", "INSUFFICIENT_DATA")
        and c.get("metrics_complete")
    )
    status, message = _analysis_status(
        candidates,
        comparable_eligible=comparable_eligible,
        feasible=pareto["eligible_count"],
        constraints=normalized,
    )

    warnings: list[str] = []
    if comparability.get("mixed_real_and_simulated"):
        warnings.append(
            "候选混合了真实 API 与模拟数据：结果不得解释为真实模型性能比较，前沿为空"
        )
    for issue in comparability.get("critical_issues") or []:
        if issue["code"] not in (
            "MIXED_REAL_AND_SIMULATED",
            "DATASET_VERSION_MISMATCH",
            "DATASET_ID_MISMATCH",
            "DATASET_VERSION_FILTER_MISSING",
        ):
            warnings.append(issue["message"])
    if comparability.get("verdict") == "COMPARABLE_WITH_WARNINGS":
        warnings.append("存在可比性警告，请结合 warnings 解读结果")
    if any(c.get("data_source") == "simulated" for c in candidates):
        warnings.append(
            "候选包含模拟执行数据（Task Success 来自确定性模拟模型），不代表真实模型性能"
        )

    analysis_id = f"pareto-{uuid.uuid4().hex}"
    report = {
        "analysis_id": analysis_id,
        "generated_at": _now_iso(),
        "engine_version": "1.0.0",
        "status": status,
        "status_message": message,
        "objective": {
            "quality": "max",
            "latency": "min",
            "cost": "min",
            "definition": "A 支配 B：质量更高或相等，延迟与成本更低或相等，且至少一项严格更优",
        },
        "metric_definitions": METRIC_DEFINITIONS,
        "constraints": normalized,
        "constraints_applied": {
            k: v for k, v in normalized.items() if v is not None
        },
        "candidate_count": len(candidates),
        "feasible_count": pareto["eligible_count"],
        "comparable_candidate_count": comparable_eligible,
        "candidates": candidates,
        "frontier": pareto["frontier"],
        "dominated": pareto["dominated"],
        "dominance_relations": pareto["dominance_relations"],
        "tie_groups": pareto["tie_groups"],
        "pareto_frontier_count": pareto["pareto_frontier_count"],
        "dominated_count": pareto["dominated_count"],
        "comparability": comparability,
        "skipped_runs": built["skipped_runs"],
        "datasets": built["datasets"],
        "data_source_summary": built["data_sources"],
        "warnings": warnings,
        "limitations": [
            "候选全部来自本进程内真实执行过的评测运行；重启后内存记录不可查",
            "模拟执行器数据不代表真实模型性能；真实 API 与模拟数据不混合比较",
            "成本为本地价格表估算，不是 Provider 实际账单；PRICE_UNKNOWN 时成本为 null",
            "缺失指标为 null，不按 0 计；缺失候选不进入前沿",
            "agent_version 缺失时为 null（受控执行器无独立版本字段，未伪造）",
            "帕累托前沿只表示候选集合内的非支配关系，不表示绝对最优或统计显著性",
        ],
        "paid_api_called": False,
    }
    report["report_path"] = str(_persist_analysis(report).relative_to(PROJECT_ROOT))
    _remember(report)
    return report


def get_analysis(analysis_id: str) -> dict[str, Any]:
    return _load_analysis(analysis_id)


def list_analyses(limit: int = 20) -> dict[str, Any]:
    with _STORE_LOCK:
        ids = list(reversed(_ANALYSIS_ORDER))[:limit]
        reports = [_ANALYSES[i] for i in ids if i in _ANALYSES]
    if not reports and PARETO_DIR.exists():
        files = sorted(
            PARETO_DIR.glob("pareto-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            _remember(data)
            reports.append(data)
    return {
        "count": len(reports),
        "analyses": [
            {
                "analysis_id": r.get("analysis_id"),
                "generated_at": r.get("generated_at"),
                "status": r.get("status"),
                "status_message": r.get("status_message"),
                "candidate_count": r.get("candidate_count"),
                "feasible_count": r.get("feasible_count"),
                "pareto_frontier_count": r.get("pareto_frontier_count"),
                "dominated_count": r.get("dominated_count"),
                "constraints_applied": r.get("constraints_applied"),
                "comparability_verdict": (r.get("comparability") or {}).get("verdict"),
                "paid_api_called": False,
            }
            for r in reports
        ],
        "paid_api_called": False,
    }


def filter_analysis(analysis_id: str, constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    report = _load_analysis(analysis_id)
    normalized = _normalize_constraints(constraints)
    return _filtered_view(report, normalized)


def candidate_evaluation_run(analysis_id: str, candidate_id: str) -> dict[str, Any]:
    """候选关联的 Evaluation Run 摘要（真实记录，不含环境变量/密钥）。"""
    report = _load_analysis(analysis_id)
    candidate = _find_candidate(report, candidate_id)
    from backend.app.services.evaluation_store import get_evaluation_store

    try:
        rec = get_evaluation_store().get(candidate["evaluation_run_id"])
    except Exception as exc:  # noqa: BLE001
        raise ParetoNotFoundError(
            f"候选 {candidate_id} 的评测运行不可查（可能已因进程重启丢失）: {exc}"
        ) from exc
    run_report = rec.get("report") or {}
    return {
        "analysis_id": analysis_id,
        "candidate_id": candidate_id,
        "evaluation_run_id": candidate["evaluation_run_id"],
        "status": rec.get("status"),
        "dataset_id": run_report.get("dataset_id"),
        "dataset_version": run_report.get("dataset_version"),
        "created_at": rec.get("created_at"),
        "elapsed_ms": rec.get("elapsed_ms"),
        "total_cases": run_report.get("total_cases"),
        "executed_cases": run_report.get("executed_cases"),
        "metrics": {
            "task_success_rate": (run_report.get("metrics") or {}).get("task_success_rate"),
            "safety_pass_rate": (run_report.get("metrics") or {}).get("safety_pass_rate"),
            "average_duration_ms": (run_report.get("metrics") or {}).get("average_duration_ms"),
        },
        "trace_ids": run_report.get("trace_ids") or [],
        "report_available": bool(run_report),
        "paid_api_called": False,
    }


def candidate_traces(analysis_id: str, candidate_id: str) -> dict[str, Any]:
    """候选关联的 Trace ID 列表（供前端跳转 Trace Explorer / DAG）。"""
    report = _load_analysis(analysis_id)
    candidate = _find_candidate(report, candidate_id)
    trace_ids = list(candidate.get("trace_ids") or [])
    return {
        "analysis_id": analysis_id,
        "candidate_id": candidate_id,
        "evaluation_run_id": candidate["evaluation_run_id"],
        "trace_count": len(trace_ids),
        "trace_ids": trace_ids,
        "links": [
            {"trace_id": trace_id, "api": f"/api/traces/{trace_id}",
             "ui": f"/traces/{trace_id}"}
            for trace_id in trace_ids
        ],
        "paid_api_called": False,
    }


def _find_candidate(report: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    for candidate in report.get("candidates") or []:
        if candidate.get("candidate_id") == candidate_id:
            return candidate
    raise ParetoNotFoundError(f"候选不存在: {candidate_id}")
