"""Failure Pattern Mining 失败模式挖掘（第 48 步）。

基于已有的 Bad Case 归因结果，提取**结构化失败特征** → 生成可复现的
Failure Fingerprint → 分组统计 → 失败模式报告。

设计原则（诚实性优先）：
1. **指纹可复现**：字段取自确定性数据（归因 cause_type / Trace 工具失败 /
   指标 FAIL 列表 / recovery 元数据 / 场景类型），并用内容哈希生成 `fingerprint_id`；
2. **缺失字段保持缺失**：`None` 表示"该维度不可得"，绝不猜测填充，
   指纹字符串中用 `-` 表示缺失；
3. **相同指纹 ≠ 相同根因**：分组只说明"失败特征相似"，报告明写该限制；
4. **统计分母明确**：每个百分比都附带 denominator 名称与数值；
5. **未执行案例不入统计**：只统计已执行的 Bad Case；UNKNOWN 独立计数，
   既不并入任何模式占比的分母之外的通过，也不当作失败类型；
6. **语义聚类预留接口**：`register_cluster_strategy()` 允许后续替换聚类方式，
   当前默认 `fingerprint`。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.app.schemas.trace import StepStatus, StepType, TraceRecord
from backend.app.services.trace_store import load_trace

__all__ = [
    "FAILURE_TYPES",
    "RECOVERY_STATES",
    "FINGERPRINT_FIELDS",
    "FailurePatternError",
    "build_fingerprint",
    "analyze_run",
    "get_pattern_report",
    "list_pattern_reports",
    "compare_runs",
    "register_cluster_strategy",
    "CLUSTER_STRATEGIES",
    "PATTERN_DIR",
    "MAX_CASES",
]

ENGINE_VERSION = "1.0.0"
PATTERN_DIR = Path(__file__).resolve().parents[3] / "data" / "failure_patterns" / "reports"
MAX_CASES = 50

# 归因类型（与 bad_case_attribution.ATTRIBUTION_TYPES 保持一致）
FAILURE_TYPES = (
    "TOOL_TIMEOUT",
    "TOOL_EXCEPTION",
    "INVALID_ARGUMENTS",
    "INVALID_TOOL_RESULT",
    "EMPTY_RETRIEVAL",
    "PERMISSION_DENIED",
    "TASK_INCOMPLETE",
    "OUTPUT_MISMATCH",
    "PROCESS_VIOLATION",
    "UNKNOWN",
)

RECOVERY_STATES = ("recovered", "not_recovered", "no_tool_failure", "unknown")

FINGERPRINT_FIELDS = (
    "failure_type",
    "tool_name",
    "failed_metric",
    "recovery_status",
    "scenario_type",
)

# 证据等级优先级（用于选择主失败类型；越靠前越强）
_LEVEL_RANK = {
    "CONFIRMED_EVENT": 0,
    "INTERVENTION_SUPPORTED": 1,
    "OBSERVED_DIFFERENCE": 2,
    "POTENTIAL_CAUSE": 3,
    "UNKNOWN": 4,
}

# 失败类型优先级：具体故障/工具类型优先于泛化类型（避免把 TOOL_TIMEOUT
# 归成更泛化的 TASK_INCOMPLETE）。数值越小越具体。
_TYPE_RANK = {
    "TOOL_TIMEOUT": 0,
    "TOOL_EXCEPTION": 1,
    "INVALID_ARGUMENTS": 2,
    "INVALID_TOOL_RESULT": 3,
    "EMPTY_RETRIEVAL": 4,
    "PERMISSION_DENIED": 5,
    "PROCESS_VIOLATION": 6,
    "OUTPUT_MISMATCH": 7,
    "TASK_INCOMPLETE": 8,
    "UNKNOWN": 9,
}


class FailurePatternError(ValueError):
    """失败模式分析请求非法。"""


# ---------------------------------------------------------------------------
# 特征提取
# ---------------------------------------------------------------------------

def _primary_failure_type(attribution: dict[str, Any]) -> tuple[str, list[str]]:
    """从归因候选中选择主失败类型（确定性规则），并返回全部候选类型。"""
    candidates = attribution.get("candidate_causes") or []
    if not candidates:
        return "UNKNOWN", []
    # 过滤掉纯依赖推断产生的 UNKNOWN（只有当没有更强候选时才作为主类型）
    ranked = sorted(
        candidates,
        key=lambda c: (
            _LEVEL_RANK.get(c.get("evidence_level"), 9),
            _TYPE_RANK.get(c.get("cause_type") or "UNKNOWN", 99),
            c.get("cause_type") or "UNKNOWN",
        ),
    )
    types = sorted({(c.get("cause_type") or "UNKNOWN") for c in candidates})
    for c in ranked:
        t = c.get("cause_type") or "UNKNOWN"
        if t == "UNKNOWN" and len(types) > 1:
            continue
        if t in FAILURE_TYPES:
            return t, types
    return "UNKNOWN", types


def _tool_feature(trace: TraceRecord | None) -> tuple[str | None, bool]:
    """工具失败特征：返回 (工具名, 是否存在失败工具调用)。

    只在 Trace 明确记录 tool_call 失败时取工具名；否则为 None（不猜测）。
    """
    if trace is None:
        return None, False
    failed: set[str] = set()
    for step in trace.steps:
        if step.step_type != StepType.TOOL_CALL:
            continue
        if step.status == StepStatus.FAILED and step.tool_name:
            failed.add(step.tool_name)
    if not failed:
        return None, False
    return "+".join(sorted(failed)), True


def _recovery_feature(trace: TraceRecord | None, has_tool_failure: bool) -> str:
    """恢复状态：仅依据 Trace 中可核对的信息判定。

    判定顺序：
    1. `metadata.recovery_observed` 明确记录 → 直接采用；
    2. 否则比较工具调用的时间顺序：失败后仍有成功调用 → recovered；
       全部失败 → not_recovered（失败尝试次数 ≥2 才视为"重试过"，否则只是单次失败）；
    3. 无工具失败 → no_tool_failure；无 Trace → unknown。
    """
    if trace is None:
        return "unknown"
    meta = trace.metadata or {}
    if meta.get("recovery_observed") is True:
        return "recovered"
    if not has_tool_failure:
        return "no_tool_failure"

    tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL]
    failures = [i for i, s in enumerate(tool_steps) if s.status == StepStatus.FAILED]
    if meta.get("recovery_observed") is False:
        return "recovered" if any(
            s.status == StepStatus.SUCCEEDED for s in tool_steps[failures[0] + 1:]
        ) else "not_recovered"
    if not failures:
        return "no_tool_failure"
    # 失败之后是否出现成功调用（同一 Trace 内，顺序即证据）
    recovered = any(s.status == StepStatus.SUCCEEDED for s in tool_steps[failures[0] + 1:])
    if recovered:
        return "recovered"
    return "not_recovered" if len(tool_steps) >= 2 else "unknown"


def _failed_metric_feature(attribution: dict[str, Any]) -> str | None:
    names = sorted({m.get("metric") for m in (attribution.get("failed_metrics") or [])})
    names = [n for n in names if n]
    return "+".join(names) if names else None


def build_fingerprint(
    *,
    failure_type: str,
    tool_name: str | None,
    failed_metric: str | None,
    recovery_status: str,
    scenario_type: str | None,
) -> dict[str, Any]:
    """生成可复现的 Failure Fingerprint。

    - 缺失字段一律 `None`，**不填充**猜测值；
    - `fingerprint_id` 由 5 个字段的内容哈希派生（同输入 → 同 ID）；
    - `label` 用 `-` 表示缺失维度，便于人读。
    """
    fields = {
        "failure_type": failure_type,
        "tool_name": tool_name,
        "failed_metric": failed_metric,
        "recovery_status": recovery_status,
        "scenario_type": scenario_type,
    }
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint_id = f"fp-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]}"
    label = " | ".join(
        str(fields[k]) if fields[k] not in (None, "") else "-" for k in FINGERPRINT_FIELDS
    )
    missing = [k for k in FINGERPRINT_FIELDS if fields[k] in (None, "")]
    return {
        **fields,
        "fingerprint_id": fingerprint_id,
        "fingerprint_label": label,
        "missing_fields": missing,
        "canonical": canonical,
    }


# ---------------------------------------------------------------------------
# 聚类策略（预留语义聚类扩展接口）
# ---------------------------------------------------------------------------

def _fingerprint_key(feature: dict[str, Any]) -> str:
    return feature["fingerprint"]["fingerprint_id"]


CLUSTER_STRATEGIES: dict[str, Callable[[dict[str, Any]], str]] = {
    "fingerprint": _fingerprint_key,
}


def register_cluster_strategy(name: str, key_fn: Callable[[dict[str, Any]], str]) -> None:
    """注册聚类策略（为后续语义聚类预留：传入把 feature 映射为分组键的函数）。"""
    if not callable(key_fn):
        raise FailurePatternError("聚类策略必须是可调用对象")
    CLUSTER_STRATEGIES[name] = key_fn


# ---------------------------------------------------------------------------
# 分析
# ---------------------------------------------------------------------------

def _eval_run(run_id: str) -> dict[str, Any]:
    from backend.app.services.evaluation_store import (
        EvaluationNotFoundError,
        get_evaluation_store,
    )

    try:
        return get_evaluation_store().get(run_id)
    except EvaluationNotFoundError as exc:
        raise FailurePatternError(str(exc)) from exc


def _existing_attributions(run_id: str) -> dict[str, dict[str, Any]]:
    """复用已持久化的归因报告（按 case_id 索引），避免重复计算。"""
    from backend.app.services.bad_case_attribution import get_report, list_reports

    out: dict[str, dict[str, Any]] = {}
    try:
        items = list_reports(limit=200)
    except Exception:
        return out
    for item in items:
        if item.get("evaluation_run_id") != run_id:
            continue
        case_id = item.get("case_id")
        if not case_id:
            continue
        try:
            out[case_id] = get_report(item["attribution_id"])
        except Exception:
            continue
    return out


def _load_trace_safe(trace_id: str | None) -> TraceRecord | None:
    if not trace_id:
        return None
    try:
        return load_trace(trace_id)
    except Exception:
        return None


def analyze_run(
    evaluation_run_id: str,
    *,
    max_cases: int | None = None,
    strategy: str = "fingerprint",
    reuse_attribution: bool = True,
) -> dict[str, Any]:
    """对一次评测运行做失败模式挖掘。

    Args:
        max_cases: 最多分析的 Bad Case 数（≤MAX_CASES）。
        strategy: 聚类策略名（默认 fingerprint；语义聚类可后续注册替换）。
        reuse_attribution: 复用已持久化归因报告，避免重复计算。
    """
    if strategy not in CLUSTER_STRATEGIES:
        raise FailurePatternError(
            f"未知聚类策略: {strategy!r}（可用: {', '.join(sorted(CLUSTER_STRATEGIES))}）"
        )
    limit = min(max_cases or MAX_CASES, MAX_CASES)
    if limit < 1:
        raise FailurePatternError("max_cases 必须 ≥1")

    from backend.app.services.bad_case_attribution import attribute_case, persist_report

    run = _eval_run(evaluation_run_id)
    if run.get("status") != "succeeded":
        raise FailurePatternError(f"评测运行状态为 {run.get('status')!r}，无法分析")
    report = run.get("result") or run.get("report") or {}
    cases = report.get("case_results") or []

    executed = [c for c in cases if str(c.get("overall_status")) != "NOT_EXECUTED"]
    bad = [c for c in executed if c.get("is_bad_case")]
    selected = bad[:limit]

    cached = _existing_attributions(evaluation_run_id) if reuse_attribution else {}

    features: list[dict[str, Any]] = []
    unknown_cases: list[dict[str, Any]] = []
    for case in selected:
        case_id = case.get("case_id")
        attribution = cached.get(case_id)
        if attribution is None:
            attribution = attribute_case(
                evaluation_run_id=evaluation_run_id,
                case=case,
                dataset_id=report.get("dataset_id"),
            )
            # 落盘：否则「证据」按钮按 attribution_id 查询会 404
            try:
                persist_report(attribution)
            except Exception:
                pass

        trace = _load_trace_safe(case.get("trace_id"))
        tool_name, has_tool_failure = _tool_feature(trace)
        failure_type, cause_types = _primary_failure_type(attribution)
        fingerprint = build_fingerprint(
            failure_type=failure_type,
            tool_name=tool_name,
            failed_metric=_failed_metric_feature(attribution),
            recovery_status=_recovery_feature(trace, has_tool_failure),
            scenario_type=case.get("scenario_type"),
        )
        is_unknown = failure_type == "UNKNOWN" and attribution.get("root_cause_status") in {
            "UNKNOWN", None
        }
        feature = {
            "case_id": case_id,
            "trace_id": case.get("trace_id"),
            "scenario_type": case.get("scenario_type"),
            "overall_status": case.get("overall_status"),
            "attribution_id": attribution.get("attribution_id"),
            "attribution_root_cause_status": attribution.get("root_cause_status"),
            "cause_types": cause_types,
            "evidence_levels": attribution.get("evidence_levels") or {},
            "failed_metrics": [m.get("metric") for m in (attribution.get("failed_metrics") or [])],
            "is_unknown": is_unknown,
            "fingerprint": fingerprint,
            "evidence_refs": (attribution.get("evidence_refs") or [])[:6],
            "limitations": attribution.get("limitations") or [],
        }
        features.append(feature)
        if is_unknown:
            unknown_cases.append({
                "case_id": case_id,
                "trace_id": case.get("trace_id"),
                "reason": "归因未找到可确认的失败类型（证据不足）",
                "attribution_id": attribution.get("attribution_id"),
            })

    # ---- 分组 ----
    key_fn = CLUSTER_STRATEGIES[strategy]
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in features:
        groups.setdefault(key_fn(f), []).append(f)

    patterns: list[dict[str, Any]] = []
    for key, members in groups.items():
        first = members[0]
        fp = first["fingerprint"]
        level_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for m in members:
            for cause in m["cause_types"]:
                type_counts[cause] = type_counts.get(cause, 0) + 1
            for lv in (m["evidence_levels"] or {}):
                level_counts[lv] = level_counts.get(lv, 0) + 1

        representative = sorted(
            members,
            key=lambda m: (
                # 优先有确认级证据的案例
                0 if "CONFIRMED_EVENT" in (m["evidence_levels"] or {}) else 1,
                m["case_id"],
            ),
        )[0]

        patterns.append({
            "pattern_id": key,
            "fingerprint_id": fp["fingerprint_id"],
            "fingerprint_fields": {k: fp[k] for k in FINGERPRINT_FIELDS},
            "fingerprint_label": fp["fingerprint_label"],
            "missing_fields": fp["missing_fields"],
            "case_count": len(members),
            "share_of_bad_cases": round(len(members) / len(features), 4) if features else None,
            "has_unknown_cases": any(m["is_unknown"] for m in members),
            "cause_type_counts": type_counts,
            "evidence_level_counts": level_counts,
            "representative_case": {
                "case_id": representative["case_id"],
                "trace_id": representative["trace_id"],
                "attribution_id": representative["attribution_id"],
                "overall_status": representative["overall_status"],
            },
            "case_ids": [m["case_id"] for m in members],
            "trace_ids": [m["trace_id"] for m in members if m["trace_id"]],
        })

    patterns.sort(key=lambda p: (-p["case_count"], p["fingerprint_id"]))

    denominators = {
        "total_cases_in_run": len(cases),
        "executed_cases": len(executed),
        "bad_cases": len(bad),
        "analyzed_cases": len(features),
        "unknown_cases": len(unknown_cases),
        "notes": {
            "total_cases_in_run": "本次评测运行的全部案例数",
            "executed_cases": "实际执行完成的案例数（未执行不计入）",
            "bad_cases": "评测判定为 Bad Case 的数量",
            "analyzed_cases": "本次分析的 Bad Case 数（受 max_cases 限制）",
            "unknown_cases": "归因未找到可确认失败类型的案例数",
            "share_of_bad_cases": "该模式案例数 / analyzed_cases",
        },
    }

    report_id = f"fp-report-{uuid.uuid4().hex}"
    result = {
        "engine_version": ENGINE_VERSION,
        "pattern_report_id": report_id,
        "evaluation_run_id": evaluation_run_id,
        "dataset_id": report.get("dataset_id"),
        "strategy": strategy,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "denominators": denominators,
        "pattern_count": len(patterns),
        "patterns": patterns,
        "cases": [
            {
                "case_id": f["case_id"],
                "trace_id": f["trace_id"],
                "scenario_type": f["scenario_type"],
                "fingerprint_id": f["fingerprint"]["fingerprint_id"],
                "fingerprint_label": f["fingerprint"]["fingerprint_label"],
                "failure_type": f["fingerprint"]["failure_type"],
                "tool_name": f["fingerprint"]["tool_name"],
                "failed_metric": f["fingerprint"]["failed_metric"],
                "recovery_status": f["fingerprint"]["recovery_status"],
                "is_unknown": f["is_unknown"],
                "attribution_id": f["attribution_id"],
                "evidence_levels": f["evidence_levels"],
                "cause_types": f["cause_types"],
                "failed_metrics": f["failed_metrics"],
            }
            for f in features
        ],
        "unknown_cases": unknown_cases,
        "top_patterns": [p["fingerprint_id"] for p in patterns[:5]],
        "data_source": "analysis_over_attribution_reports",
        "limitations": [
            "相同指纹只表示失败特征相似，**不代表已证明的相同根因**",
            "缺失字段以 null 表示不可得，不做猜测填充",
            "统计分母见 denominators；未执行案例不计入任何模式",
            "UNKNOWN 案例单独计数，不并入任何失败类型",
            "指纹基于确定性规则，尚未做语义聚类（已预留策略接口）",
        ],
        "paid_api_called": False,
    }

    _persist(result)
    return result


# ---------------------------------------------------------------------------
# 持久化与查询
# ---------------------------------------------------------------------------

def _persist(result: dict[str, Any]) -> Path:
    PATTERN_DIR.mkdir(parents=True, exist_ok=True)
    path = PATTERN_DIR / f"{result['pattern_report_id']}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def get_pattern_report(report_id: str) -> dict[str, Any]:
    if not report_id.startswith("fp-report-") or not report_id.replace("fp-report-", "").isalnum():
        raise FailurePatternError(f"非法模式报告 ID: {report_id!r}")
    path = PATTERN_DIR / f"{report_id}.json"
    if not path.exists():
        raise FailurePatternError(f"模式报告不存在: {report_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_pattern_reports(limit: int = 30) -> list[dict[str, Any]]:
    if not PATTERN_DIR.exists():
        return []
    files = sorted(PATTERN_DIR.glob("fp-report-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "pattern_report_id": r.get("pattern_report_id"),
            "evaluation_run_id": r.get("evaluation_run_id"),
            "pattern_count": r.get("pattern_count"),
            "analyzed_cases": (r.get("denominators") or {}).get("analyzed_cases"),
            "unknown_cases": (r.get("denominators") or {}).get("unknown_cases"),
            "generated_at": r.get("generated_at"),
        })
    return out


# ---------------------------------------------------------------------------
# 运行对比
# ---------------------------------------------------------------------------

def compare_runs(
    run_a: str,
    run_b: str,
    *,
    max_cases: int | None = None,
    strategy: str = "fingerprint",
) -> dict[str, Any]:
    """对比两次评测运行的失败模式变化（按 fingerprint_id 对齐）。"""
    a = analyze_run(run_a, max_cases=max_cases, strategy=strategy)
    b = analyze_run(run_b, max_cases=max_cases, strategy=strategy)

    a_map = {p["fingerprint_id"]: p for p in a["patterns"]}
    b_map = {p["fingerprint_id"]: p for p in b["patterns"]}
    keys = sorted(set(a_map) | set(b_map))

    rows: list[dict[str, Any]] = []
    for key in keys:
        pa, pb = a_map.get(key), b_map.get(key)
        ca = pa["case_count"] if pa else 0
        cb = pb["case_count"] if pb else 0
        rows.append({
            "fingerprint_id": key,
            "fingerprint_label": (pa or pb)["fingerprint_label"],
            "failure_type": (pa or pb)["fingerprint_fields"]["failure_type"],
            "tool_name": (pa or pb)["fingerprint_fields"]["tool_name"],
            "cases_a": ca,
            "cases_b": cb,
            "delta": cb - ca,
            "status": (
                "NEW" if pa is None else
                "RESOLVED" if pb is None else
                "INCREASED" if cb > ca else
                "DECREASED" if cb < ca else "UNCHANGED"
            ),
        })
    rows.sort(key=lambda r: (-abs(r["delta"]), r["fingerprint_id"]))

    return {
        "engine_version": ENGINE_VERSION,
        "run_a": {
            "evaluation_run_id": run_a,
            "pattern_report_id": a["pattern_report_id"],
            "pattern_count": a["pattern_count"],
            "denominators": a["denominators"],
        },
        "run_b": {
            "evaluation_run_id": run_b,
            "pattern_report_id": b["pattern_report_id"],
            "pattern_count": b["pattern_count"],
            "denominators": b["denominators"],
        },
        "rows": rows,
        "summary": {
            "new_patterns": sum(1 for r in rows if r["status"] == "NEW"),
            "resolved_patterns": sum(1 for r in rows if r["status"] == "RESOLVED"),
            "increased": sum(1 for r in rows if r["status"] == "INCREASED"),
            "decreased": sum(1 for r in rows if r["status"] == "DECREASED"),
            "unchanged": sum(1 for r in rows if r["status"] == "UNCHANGED"),
            "bad_cases_a": a["denominators"]["analyzed_cases"],
            "bad_cases_b": b["denominators"]["analyzed_cases"],
        },
        "notes": [
            "对比以 fingerprint_id 对齐；仅在两侧都存在的模式才算增加/减少",
            "分母差异已列在 run_a/run_b.denominators 中，占比变化需结合分母解读",
            "指纹相同不代表根因相同",
        ],
        "paid_api_called": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
