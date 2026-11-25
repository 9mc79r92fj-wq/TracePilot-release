"""Evaluation Version Comparison 评测版本对比（第 49 步）。

把两次 Evaluation Run 视为两个"评测版本"，先做**可比性检查**，再做指标差异与
Case-level Diff，最后给出结构化版本对比报告。

诚实性约束：
1. **版本元数据只来自真实执行记录**（`evaluation_store` 记录 + 报告 + 数据集文件
   内容哈希），不根据前端选择项拼装；
2. 指标差异**保留原始分子/分母**，同时输出百分点变化（pp）与相对变化（%），
   两者分开命名，不混用；
3. **UNKNOWN 不当作 0**：任一版本指标为 `null` 时差异为 `null` 并标记 `UNKNOWN`；
4. 不把不可比较的结果解释为"性能提升"：可比性问题写入
   `comparability_warnings`，风险/门禁由第 50 步按规则判定；
5. 仅对**共同案例**做配对比较，缺失案例单独列出。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "VersionComparisonError",
    "build_version_descriptor",
    "check_comparability",
    "compare_runs",
    "get_comparison",
    "list_comparisons",
    "COMPARISON_DIR",
    "RATE_METRICS",
    "LOWER_IS_BETTER_METRICS",
    "MAX_CASES",
]

ENGINE_VERSION = "1.0.0"
COMPARISON_DIR = Path(__file__).resolve().parents[3] / "data" / "version_comparison" / "reports"
MAX_CASES = 50

# 比率型指标（百分点变化有意义）
RATE_METRICS = (
    "task_success_rate",
    "tool_correctness_rate",
    "recovery_rate",
    "safety_pass_rate",
)
# 越低越好的指标
LOWER_IS_BETTER_METRICS = ("average_duration_ms", "total_tokens")
# 逐案例可判定的维度（用于 case-level 评分差异）
CASE_DIMENSIONS = (
    "task_success",
    "tool_correctness",
    "process_quality",
    "recovery",
    "efficiency",
    "safety",
)

_STATUS_RANK = {"PASS": 0, "UNKNOWN": 1, "NOT_APPLICABLE": 2, "FAIL": 3}


class VersionComparisonError(ValueError):
    """版本对比请求非法。"""


# ---------------------------------------------------------------------------
# 版本元数据（只来自真实记录）
# ---------------------------------------------------------------------------

def _dataset_fingerprint(dataset_id: str) -> tuple[str | None, str | None, int | None]:
    """数据集文件内容哈希（真实文件），返回 (fingerprint, version, case_count)。"""
    try:
        from backend.app.services.evaluation_engine import DATASET_DIR, load_dataset
        from backend.app.services.evaluation_store import ALLOWED_DATASETS
    except Exception:  # pragma: no cover
        return None, None, None

    filename = ALLOWED_DATASETS.get(dataset_id)
    if not filename:
        return None, None, None
    path = DATASET_DIR / filename
    if not path.exists():
        return None, None, None
    raw = path.read_bytes()
    fingerprint = hashlib.sha256(raw).hexdigest()
    try:
        data = load_dataset(path)
    except Exception:
        return fingerprint, None, None
    return fingerprint, data.get("dataset_version"), len(data.get("cases") or [])


def _rubric_version(rubric_id: str | None) -> str | None:
    if not rubric_id:
        return None
    try:
        from backend.app.services.llm_judge import load_rubric

        return load_rubric(rubric_id).version
    except Exception:
        return None


def build_version_descriptor(run_id: str) -> dict[str, Any]:
    """构造单个评测运行的版本描述（全部字段可追溯到真实数据）。"""
    from backend.app.services.evaluation_store import (
        EvaluationNotFoundError,
        get_evaluation_store,
    )

    try:
        rec = get_evaluation_store().get(run_id)
    except EvaluationNotFoundError as exc:
        raise VersionComparisonError(str(exc)) from exc

    if rec.get("status") != "succeeded":
        raise VersionComparisonError(
            f"评测运行 {run_id} 状态为 {rec.get('status')!r}，无报告可比较"
        )
    report = rec.get("report") or rec.get("result") or {}
    env = report.get("environment") or {}
    model = env.get("model") or {}
    tools = sorted(env.get("tools") or [])

    dataset_id = report.get("dataset_id") or rec.get("dataset_id")
    fingerprint, ds_version, ds_cases = _dataset_fingerprint(dataset_id or "")
    dataset_version = report.get("dataset_version") or ds_version

    judge_mode = rec.get("judge_mode") or "none"
    rubric_id = rec.get("judge_rubric_id") if judge_mode != "none" else None
    rubric_version = _rubric_version(rubric_id)

    judge_breakdown = (report.get("metrics") or {}).get("judge_breakdown") or {}
    prompt_version = judge_breakdown.get("prompt_version") if judge_breakdown else None

    tools_fingerprint = (
        hashlib.sha256(json.dumps(tools, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        if tools else None
    )

    execution_config = {
        "data_source": report.get("dataset_data_source"),
        "judge_mode": judge_mode,
        "judge_rubric_id": rubric_id,
        "max_cases": rec.get("max_cases"),
        "execution_engine": "controlled_simulation" if model.get("simulated") else "unknown",
        "paid_api_called": bool(model.get("paid_api_called")),
    }

    # 版本 ID：由真实版本字段内容哈希派生（同输入同 ID，不含时间）
    identity = {
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "dataset_fingerprint": fingerprint,
        "engine_version": report.get("engine_version"),
        "judge_mode": judge_mode,
        "rubric_id": rubric_id,
        "rubric_version": rubric_version,
        "model": {k: model.get(k) for k in ("provider", "simulated")},
        "tools": tools,
    }
    version_id = "ev-ver-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]

    return {
        "evaluation_version_id": version_id,
        "evaluation_run_id": run_id,
        # 以下字段严格来自记录；本项目受控执行器无独立 agent_version 字段 → null
        "agent_version": None,
        "agent_version_note": "受控执行器（确定性模拟）无独立 agent_version 字段，未伪造",
        "prompt_version": prompt_version,
        "prompt_version_note": (
            "仅当启用 Judge 时存在 prompt_version；确定性验证器无 prompt"
            if prompt_version is None else None
        ),
        "model_name": model.get("provider"),
        "model_simulated": model.get("simulated"),
        "tool_config_version": tools_fingerprint,
        "tools": tools,
        "evaluator_version": report.get("engine_version"),
        "rubric_version": rubric_version,
        "rubric_id": rubric_id,
        "judge_mode": judge_mode,
        "dataset_version": dataset_version,
        "dataset_id": dataset_id,
        "dataset_fingerprint": fingerprint,
        "dataset_case_count": ds_cases,
        "execution_config": execution_config,
        "created_at": rec.get("created_at"),
        "identity_hash": hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }


# ---------------------------------------------------------------------------
# 可比性检查
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}


def _judge_mode_of(desc: dict[str, Any]) -> str:
    """读取版本描述中的 Judge 模式；缺失时按是否存在 rubric 推断。"""
    mode = (desc.get("execution_config") or {}).get("judge_mode") or desc.get("judge_mode")
    if mode:
        return str(mode)
    return "inferred_from_rubric" if desc.get("rubric_id") else "none"


def check_comparability(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """检查两个版本是否可比较，返回问题列表与总体判定。"""
    issues: list[dict[str, Any]] = []

    def warn(code: str, severity: str, field: str, va: Any, vb: Any, message: str) -> None:
        issues.append({
            "code": code,
            "severity": severity,
            "field": field,
            "baseline": va,
            "candidate": vb,
            "message": message,
        })

    def cmp_field(code: str, field: str, severity: str, message: str) -> None:
        va, vb = a.get(field), b.get(field)
        # 双方都缺失 → 不可比（无法核对）
        if va is None and vb is None:
            warn(f"{code}", "WARNING", field, va, vb, message + "（双方均缺失，无法核对）")
        elif va != vb:
            warn(code, severity, field, va, vb, message)

    cmp_field("DATASET_ID_MISMATCH", "dataset_id", "CRITICAL", "数据集 ID 不一致，指标不可直接比较")
    cmp_field("DATASET_VERSION_MISMATCH", "dataset_version", "CRITICAL", "数据集版本不一致")
    cmp_field("DATASET_FINGERPRINT_MISMATCH", "dataset_fingerprint", "CRITICAL",
              "数据集内容指纹不一致（案例集可能已变化）")
    cmp_field("EVALUATOR_VERSION_MISMATCH", "evaluator_version", "WARNING",
              "评测器版本不一致，判定口径可能变化")
    cmp_field("JUDGE_MODE_MISMATCH", "judge_mode", "WARNING", "Judge 模式不一致")

    # Rubric 仅在启用 Judge 时才参与评分：双方都未启用时属于"不适用"，
    # 不应报成"元数据缺失"，否则会把确定性评测误判为不可比。
    skipped_checks: list[dict[str, Any]] = []
    judge_none_a = _judge_mode_of(a) == "none"
    judge_none_b = _judge_mode_of(b) == "none"
    if judge_none_a and judge_none_b:
        skipped_checks.append({
            "code": "RUBRIC_CHECK_SKIPPED",
            "field": "rubric",
            "reason": "双方均未启用 Judge，Rubric 不参与评分（不适用，非缺失）",
        })
    else:
        cmp_field("RUBRIC_ID_MISMATCH", "rubric_id", "CRITICAL",
                  "Rubric 不一致，评分规则不同")
        cmp_field("RUBRIC_VERSION_MISMATCH", "rubric_version", "CRITICAL",
                  "Rubric 版本不一致")
    cmp_field("TOOL_CONFIG_MISMATCH", "tool_config_version", "WARNING", "工具配置不一致")

    # 执行模式
    mode_a = (a.get("execution_config") or {}).get("execution_engine")
    mode_b = (b.get("execution_config") or {}).get("execution_engine")
    if mode_a != mode_b:
        warn("EXECUTION_MODE_MISMATCH", "CRITICAL", "execution_engine", mode_a, mode_b,
             "执行模式不一致（模拟 / 真实等），不可直接比较")
    if (a.get("execution_config") or {}).get("paid_api_called") or \
            (b.get("execution_config") or {}).get("paid_api_called"):
        warn("PAID_API_INVOLVED", "WARNING", "paid_api_called",
             (a.get("execution_config") or {}).get("paid_api_called"),
             (b.get("execution_config") or {}).get("paid_api_called"),
             "对比涉及付费 API 调用，成本口径不同")

    # 案例集一致性
    ca, cb = a.get("dataset_case_count"), b.get("dataset_case_count")
    if ca is not None and cb is not None and ca != cb:
        warn("CASE_COUNT_MISMATCH", "CRITICAL", "dataset_case_count", ca, cb,
             "数据集案例数不一致，共同案例子集对比仅供诊断")

    max_sev = max((_SEVERITY_ORDER.get(i["severity"], 0) for i in issues), default=-1)
    if not issues:
        verdict = "COMPARABLE"
    elif max_sev >= _SEVERITY_ORDER["CRITICAL"]:
        verdict = "NOT_COMPARABLE"
    else:
        verdict = "COMPARABLE_WITH_WARNINGS"

    return {
        "verdict": verdict,
        "is_comparable": verdict != "NOT_COMPARABLE",
        "issue_count": len(issues),
        "issues": issues,
        "critical_issues": [i for i in issues if i["severity"] == "CRITICAL"],
        "warning_issues": [i for i in issues if i["severity"] == "WARNING"],
        "skipped_checks": skipped_checks,
        "note": "不可比结果不得解释为性能提升；差异只能作为诊断线索",
    }


def _metadata_comparability(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    """补充检查：案例集合、未执行案例、缺失数据（基于运行报告）。"""
    extra: list[dict[str, Any]] = []
    ra, rb = a.get("_report") or {}, b.get("_report") or {}
    ids_a = {c.get("case_id") for c in (ra.get("case_results") or [])}
    ids_b = {c.get("case_id") for c in (rb.get("case_results") or [])}
    if ids_a != ids_b:
        extra.append({
            "code": "CASE_SET_MISMATCH",
            "severity": "WARNING",
            "field": "case_ids",
            "baseline": len(ids_a),
            "candidate": len(ids_b),
            "message": f"案例集合不同（仅 A {len(ids_a - ids_b)} 例 / 仅 B {len(ids_b - ids_a)} 例），"
                       "仅对共同案例做配对比较",
        })
    for label, rep in (("baseline", ra), ("candidate", rb)):
        not_exec = rep.get("not_executed_cases") or 0
        errs = rep.get("execution_error_cases") or 0
        if not_exec:
            extra.append({
                "code": "MISSING_DATA",
                "severity": "WARNING",
                "field": f"{label}.not_executed_cases",
                "baseline": None,
                "candidate": not_exec,
                "message": f"{label} 有 {not_exec} 个未执行案例（不计入任何指标分母）",
            })
        if errs:
            extra.append({
                "code": "EXECUTION_ERRORS",
                "severity": "WARNING",
                "field": f"{label}.execution_error_cases",
                "baseline": None,
                "candidate": errs,
                "message": f"{label} 有 {errs} 个执行异常案例",
            })
    return extra


# ---------------------------------------------------------------------------
# 指标差异
# ---------------------------------------------------------------------------

def _norm_metric(m: Any) -> dict[str, Any]:
    """指标可能是 {value, numerator, denominator} 结构，也可能是裸数值。

    裸数值（如 average_duration_ms / total_tokens）没有分子分母，
    此处如实标记为 None，不编造分子分母。
    """
    if isinstance(m, dict):
        return m
    if isinstance(m, (int, float)):
        return {"value": float(m), "numerator": None, "denominator": None,
                "shape": "scalar"}
    return {}


def _metric_delta(name: str, ma: Any, mb: Any) -> dict[str, Any]:
    """计算单项指标差异：保留分子分母，pp 与相对变化分开。"""
    a = _norm_metric(ma)
    b = _norm_metric(mb)
    va = a.get("value")
    vb = b.get("value")

    delta_pp = None
    rel_change = None
    status = "UNKNOWN"
    if va is not None and vb is not None:
        delta_pp = round((vb - va) * 100.0, 2) if name in RATE_METRICS else round(vb - va, 4)
        if va != 0:
            rel_change = round((vb - va) / abs(va), 4)
        lower_better = name in LOWER_IS_BETTER_METRICS
        if vb == va:
            status = "UNCHANGED"
        elif (vb > va) != lower_better:
            status = "IMPROVED"
        else:
            status = "REGRESSED"

    return {
        "metric": name,
        "baseline": {"value": va, "numerator": a.get("numerator"), "denominator": a.get("denominator")},
        "candidate": {"value": vb, "numerator": b.get("numerator"), "denominator": b.get("denominator")},
        "delta": delta_pp,
        "delta_unit": "percentage_point" if name in RATE_METRICS else "absolute",
        "relative_change": rel_change,
        "relative_change_unit": "ratio_of_baseline",
        "status": status,
        "denominator_definition": a.get("denominator_definition") or b.get("denominator_definition"),
        "shape": a.get("shape") or b.get("shape") or "ratio",
        "note": "任一版本为 null 时差异为 null（UNKNOWN 不当作 0）" if status == "UNKNOWN" else None,
    }


def _case_status(case: dict[str, Any]) -> str | None:
    st = case.get("overall_status")
    return st if st in _STATUS_RANK else None


def _case_level_scores(case: dict[str, Any]) -> dict[str, Any]:
    """逐案例可比较的评分型数据（只取真实存在的数值）。"""
    out: dict[str, Any] = {}
    metrics = case.get("metrics") or {}
    for dim in CASE_DIMENSIONS:
        m = metrics.get(dim) or {}
        out[f"{dim}_status"] = m.get("status")
    judge = case.get("judge") or {}
    overall = judge.get("overall")
    out["judge_overall"] = overall if isinstance(overall, (int, float)) else None
    return out


def _case_diff(ra: dict[str, Any], rb: dict[str, Any]) -> dict[str, Any]:
    """共同案例配对比较 + 缺失案例识别。"""
    map_a = {c.get("case_id"): c for c in (ra.get("case_results") or [])}
    map_b = {c.get("case_id"): c for c in (rb.get("case_results") or [])}
    common = sorted(set(map_a) & set(map_b))
    only_a = sorted(set(map_a) - set(map_b))
    only_b = sorted(set(map_b) - set(map_a))

    rows: list[dict[str, Any]] = []
    for cid in common:
        ca, cb = map_a[cid], map_b[cid]
        sa, sb = _case_status(ca), _case_status(cb)
        if sa == "FAIL" and sb == "PASS":
            transition = "FAIL_TO_PASS"
        elif sa == "PASS" and sb == "FAIL":
            transition = "PASS_TO_FAIL"
        elif sa == "PASS" and sb == "PASS":
            transition = "PASS_TO_PASS"
        elif sa == "FAIL" and sb == "FAIL":
            transition = "FAIL_TO_FAIL"
        else:
            transition = "UNKNOWN_INVOLVED"

        score_a = _case_level_scores(ca)
        score_b = _case_level_scores(cb)
        judge_delta = None
        if score_a.get("judge_overall") is not None and score_b.get("judge_overall") is not None:
            judge_delta = round(score_b["judge_overall"] - score_a["judge_overall"], 4)

        rows.append({
            "case_id": cid,
            "scenario_type": ca.get("scenario_type") or cb.get("scenario_type"),
            "status_baseline": sa,
            "status_candidate": sb,
            "transition": transition,
            "is_regression": transition == "PASS_TO_FAIL",
            "is_improvement": transition == "FAIL_TO_PASS",
            "judge_overall_baseline": score_a.get("judge_overall"),
            "judge_overall_candidate": score_b.get("judge_overall"),
            "judge_overall_delta": judge_delta,
            "dimension_status": {
                dim: {"baseline": score_a.get(f"{dim}_status"),
                      "candidate": score_b.get(f"{dim}_status")}
                for dim in CASE_DIMENSIONS
            },
            "trace_id_baseline": ca.get("trace_id"),
            "trace_id_candidate": cb.get("trace_id"),
            "links": {
                "trace_baseline": f"/api/traces/{ca['trace_id']}" if ca.get("trace_id") else None,
                "trace_candidate": f"/api/traces/{cb['trace_id']}" if cb.get("trace_id") else None,
                "dag_baseline": f"/api/traces/{ca['trace_id']}/dag" if ca.get("trace_id") else None,
                "dag_candidate": f"/api/traces/{cb['trace_id']}/dag" if cb.get("trace_id") else None,
            },
        })

    counts = {
        "common": len(common),
        "improved": sum(1 for r in rows if r["transition"] == "FAIL_TO_PASS"),
        "regressed": sum(1 for r in rows if r["transition"] == "PASS_TO_FAIL"),
        "stable_pass": sum(1 for r in rows if r["transition"] == "PASS_TO_PASS"),
        "stable_fail": sum(1 for r in rows if r["transition"] == "FAIL_TO_FAIL"),
        "unknown_involved": sum(1 for r in rows if r["transition"] == "UNKNOWN_INVOLVED"),
        "only_in_baseline": len(only_a),
        "only_in_candidate": len(only_b),
    }
    return {
        "rows": rows,
        "counts": counts,
        "regressed_cases": [
            {"case_id": r["case_id"], "scenario_type": r["scenario_type"],
             "trace_id_baseline": r["trace_id_baseline"],
             "trace_id_candidate": r["trace_id_candidate"]}
            for r in rows if r["is_regression"]
        ],
        "improved_cases": [
            {"case_id": r["case_id"], "scenario_type": r["scenario_type"],
             "trace_id_baseline": r["trace_id_baseline"],
             "trace_id_candidate": r["trace_id_candidate"]}
            for r in rows if r["is_improvement"]
        ],
        "missing_cases": {
            "only_in_baseline": only_a,
            "only_in_candidate": only_b,
        },
        "note": "只对共同案例做配对比较；缺失案例单独列出，不参与状态变化统计",
    }


def _failure_pattern_diff(run_a: str, run_b: str) -> dict[str, Any]:
    """失败模式分布变化（复用 Failure Pattern Mining）。"""
    try:
        from backend.app.services.failure_pattern_mining import analyze_run
    except Exception:  # pragma: no cover
        return {"status": "NOT_AVAILABLE", "note": "失败模式模块不可用"}

    out: dict[str, Any] = {"status": "AVAILABLE"}
    try:
        pa = analyze_run(run_a, max_cases=MAX_CASES)
        pb = analyze_run(run_b, max_cases=MAX_CASES)
    except Exception as exc:
        return {"status": "NOT_AVAILABLE", "note": f"失败模式分析失败: {type(exc).__name__}: {exc}"}

    ma = {p["fingerprint_id"]: p for p in pa["patterns"]}
    mb = {p["fingerprint_id"]: p for p in pb["patterns"]}
    rows = []
    for fid in sorted(set(ma) | set(mb)):
        a, b = ma.get(fid), mb.get(fid)
        rows.append({
            "fingerprint_id": fid,
            "fingerprint_label": (a or b)["fingerprint_label"],
            "failure_type": (a or b)["fingerprint_fields"]["failure_type"],
            "cases_baseline": a["case_count"] if a else 0,
            "cases_candidate": b["case_count"] if b else 0,
            "delta": (b["case_count"] if b else 0) - (a["case_count"] if a else 0),
            "status": "NEW" if a is None else "RESOLVED" if b is None else
                      "INCREASED" if (b["case_count"] > a["case_count"]) else
                      "DECREASED" if (b["case_count"] < a["case_count"]) else "UNCHANGED",
        })
    rows.sort(key=lambda r: (-abs(r["delta"]), r["fingerprint_id"]))
    out.update({
        "baseline": {"pattern_report_id": pa["pattern_report_id"],
                     "pattern_count": pa["pattern_count"],
                     "denominators": pa["denominators"]},
        "candidate": {"pattern_report_id": pb["pattern_report_id"],
                      "pattern_count": pb["pattern_count"],
                      "denominators": pb["denominators"]},
        "rows": rows,
        "new_patterns": [r for r in rows if r["status"] == "NEW"],
        "resolved_patterns": [r for r in rows if r["status"] == "RESOLVED"],
        "note": "指纹相同不代表根因相同；分布变化只作为诊断线索",
    })
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def compare_runs(
    run_a: str,
    run_b: str,
    *,
    max_cases: int | None = None,
) -> dict[str, Any]:
    """对比两个评测运行（版本 A = baseline，版本 B = candidate）。"""
    if run_a == run_b:
        raise VersionComparisonError("两个运行不能相同")
    if max_cases is not None and not (1 <= max_cases <= MAX_CASES):
        raise VersionComparisonError(f"max_cases 必须在 1..{MAX_CASES}")

    from backend.app.services.evaluation_store import (
        EvaluationNotFoundError,
        get_evaluation_store,
    )

    va = build_version_descriptor(run_a)
    vb = build_version_descriptor(run_b)

    store = get_evaluation_store()
    try:
        ra = (store.get(run_a).get("report") or {})
        rb = (store.get(run_b).get("report") or {})
    except EvaluationNotFoundError as exc:  # pragma: no cover
        raise VersionComparisonError(str(exc)) from exc

    comparability = check_comparability(va, vb)
    extra = _metadata_comparability({**va, "_report": ra}, {**vb, "_report": rb})
    if extra:
        comparability["issues"].extend(extra)
        comparability["issue_count"] = len(comparability["issues"])
        comparability["warning_issues"] = [
            i for i in comparability["issues"] if i["severity"] == "WARNING"
        ]
        comparability["critical_issues"] = [
            i for i in comparability["issues"] if i["severity"] == "CRITICAL"
        ]
        if comparability["critical_issues"]:
            comparability["verdict"] = "NOT_COMPARABLE"
            comparability["is_comparable"] = False
        elif comparability["warning_issues"]:
            comparability["verdict"] = "COMPARABLE_WITH_WARNINGS"

    ma = ra.get("metrics") or {}
    mb = rb.get("metrics") or {}
    metric_names = list(RATE_METRICS) + list(LOWER_IS_BETTER_METRICS)
    metric_deltas = [
        _metric_delta(n, ma.get(n), mb.get(n))
        for n in metric_names
        if (n in ma) or (n in mb)
    ]

    # 维度通过率差异（来自 dimension_breakdown，保留通过/可判定数）
    dims_a = ma.get("dimension_breakdown") or {}
    dims_b = mb.get("dimension_breakdown") or {}
    dimension_deltas = []
    for dim in sorted(set(dims_a) | set(dims_b)):
        da = dims_a.get(dim) or {}
        db = dims_b.get(dim) or {}
        pa, pb = da.get("pass_rate"), db.get("pass_rate")
        dimension_deltas.append({
            "dimension": dim,
            "baseline": {"pass": da.get("pass"), "fail": da.get("fail"),
                         "unknown": da.get("unknown"),
                         "applicable_denominator": da.get("applicable_denominator"),
                         "pass_rate": pa},
            "candidate": {"pass": db.get("pass"), "fail": db.get("fail"),
                          "unknown": db.get("unknown"),
                          "applicable_denominator": db.get("applicable_denominator"),
                          "pass_rate": pb},
            "delta": round((pb - pa) * 100.0, 2) if pa is not None and pb is not None else None,
            "delta_unit": "percentage_point",
            "status": (
                "UNKNOWN" if pa is None or pb is None else
                "IMPROVED" if pb > pa else "REGRESSED" if pb < pa else "UNCHANGED"
            ),
        })

    bad = {
        "baseline": {"failed_cases": ra.get("failed_cases"),
                     "unknown_cases": ra.get("unknown_cases"),
                     "bad_case_count": len(ra.get("bad_cases") or [])},
        "candidate": {"failed_cases": rb.get("failed_cases"),
                      "unknown_cases": rb.get("unknown_cases"),
                      "bad_case_count": len(rb.get("bad_cases") or [])},
        "delta_failed_cases": (rb.get("failed_cases") or 0) - (ra.get("failed_cases") or 0),
        "note": "UNKNOWN 案例既不算通过也不算失败，单独计数",
    }

    cases = _case_diff(ra, rb)
    if max_cases is not None:
        cases["rows"] = cases["rows"][:max_cases]

    comparison_id = f"cmp-{uuid.uuid4().hex}"
    report = {
        "engine_version": ENGINE_VERSION,
        "comparison_id": comparison_id,
        "baseline_version": {k: v for k, v in va.items() if k != "_report"},
        "candidate_version": {k: v for k, v in vb.items() if k != "_report"},
        "comparability": comparability,
        "metric_deltas": metric_deltas,
        "dimension_deltas": dimension_deltas,
        "bad_case_diff": bad,
        "cases": {
            "counts": cases["counts"],
            "rows": cases["rows"],
            "regressed_cases": cases["regressed_cases"],
            "improved_cases": cases["improved_cases"],
            "missing_cases": cases["missing_cases"],
            "note": cases["note"],
        },
        "failure_pattern_diff": _failure_pattern_diff(run_a, run_b),
        "limits": {
            "max_cases": max_cases,
            "compared_cases": len(cases["rows"]),
        },
        "limitations": [
            "版本元数据仅来自真实运行记录与数据集文件哈希，未根据前端选择拼装",
            "指标差异保留原始分子/分母；pp（百分点）与相对变化（%）分开呈现",
            "UNKNOWN 不当作 0：任一版本缺值时差异为 null",
            "不可比结果不得解释为性能提升（见 comparability.issues）",
            "仅对共同案例做配对比较；缺失案例单独列出",
        ],
        "paid_api_called": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _persist(report)
    return report


def _persist(report: dict[str, Any]) -> Path:
    COMPARISON_DIR.mkdir(parents=True, exist_ok=True)
    path = COMPARISON_DIR / f"{report['comparison_id']}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def get_comparison(comparison_id: str) -> dict[str, Any]:
    if not comparison_id.startswith("cmp-") or not comparison_id.replace("cmp-", "").isalnum():
        raise VersionComparisonError(f"非法对比 ID: {comparison_id!r}")
    path = COMPARISON_DIR / f"{comparison_id}.json"
    if not path.exists():
        raise VersionComparisonError(f"对比报告不存在: {comparison_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_comparisons(limit: int = 20) -> list[dict[str, Any]]:
    if not COMPARISON_DIR.exists():
        return []
    files = sorted(COMPARISON_DIR.glob("cmp-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "comparison_id": r.get("comparison_id"),
            "baseline_run_id": (r.get("baseline_version") or {}).get("evaluation_run_id"),
            "candidate_run_id": (r.get("candidate_version") or {}).get("evaluation_run_id"),
            "comparability": (r.get("comparability") or {}).get("verdict"),
            "regressed_cases": (r.get("cases") or {}).get("counts", {}).get("regressed"),
            "generated_at": r.get("generated_at"),
        })
    return out
