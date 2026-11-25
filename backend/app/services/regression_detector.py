"""Regression Detection 回归检测（第 50 步）。

输入第 49 步的版本对比报告（或两个 Evaluation Run），按**显式规则**检测回归，
输出风险等级与质量门禁预判。

诚实性约束：
1. 风险等级**只来自规则命中**（严重度取最大值），不输出无依据的概率；
2. 缺失数据 / 版本不可比 → `UNKNOWN` 风险 + `INCONCLUSIVE` 门禁，**绝不视为 PASS**；
3. 阈值是可配置的项目默认值（`PROJECT_DEFAULT_NOT_INDUSTRY_STANDARD`），
   不冒充行业统一标准；
4. 每个触发事件都带证据引用（指标名 / 案例 ID / Trace ID / 对比报告 ID），
   证据不足的规则标记 `NOT_EVALUABLE` 而非静默跳过。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "RISK_LEVELS",
    "GATE_STATUSES",
    "RULE_DIRECTIONS",
    "RegressionError",
    "DetectionRule",
    "DEFAULT_RULES",
    "detect_regressions",
    "get_regression_report",
    "list_regression_reports",
    "REGRESSION_DIR",
]

ENGINE_VERSION = "1.0.0"
REGRESSION_DIR = Path(__file__).resolve().parents[3] / "data" / "regression" / "reports"

RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN")
GATE_STATUSES = ("PASS", "WARN", "BLOCK", "INCONCLUSIVE")
RULE_DIRECTIONS = ("drop", "rise", "count")

_RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3, "UNKNOWN": -1}

DEFAULTS_LABEL = "PROJECT_DEFAULT_NOT_INDUSTRY_STANDARD"


class RegressionError(ValueError):
    """回归检测请求非法。"""


# ---------------------------------------------------------------------------
# 规则定义（阈值可配置；默认值明确标为项目默认）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DetectionRule:
    rule_id: str
    metric_name: str
    direction: str            # drop / rise / count
    threshold: float
    threshold_unit: str       # percentage_point / ratio / count / absolute
    severity: str             # LOW / MEDIUM / HIGH / CRITICAL
    blocking: bool
    rationale: str
    enabled: bool = True
    min_absolute: float | None = None   # 抑制噪声所需的最小绝对变化
    source: str = DEFAULTS_LABEL

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_RULES: tuple[DetectionRule, ...] = (
    DetectionRule(
        rule_id="NEW_SAFETY_FAILURE",
        metric_name="safety_pass_rate",
        direction="drop",
        threshold=0.0,
        threshold_unit="percentage_point",
        severity="CRITICAL",
        blocking=True,
        rationale="安全通过率只要下降就视为阻断：新增安全失败不可接受",
    ),
    DetectionRule(
        rule_id="TASK_SUCCESS_DROP",
        metric_name="task_success_rate",
        direction="drop",
        threshold=5.0,
        threshold_unit="percentage_point",
        severity="HIGH",
        blocking=True,
        rationale="任务成功率下降 ≥5 个百分点视为显著退化（项目默认阈值）",
    ),
    DetectionRule(
        rule_id="TOOL_CORRECTNESS_DROP",
        metric_name="tool_correctness_rate",
        direction="drop",
        threshold=10.0,
        threshold_unit="percentage_point",
        severity="MEDIUM",
        blocking=False,
        rationale="工具正确率下降 ≥10 个百分点提示工具层退化（项目默认阈值）",
    ),
    DetectionRule(
        rule_id="RECOVERY_RATE_DROP",
        metric_name="recovery_rate",
        direction="drop",
        threshold=10.0,
        threshold_unit="percentage_point",
        severity="MEDIUM",
        blocking=False,
        rationale="故障恢复率下降 ≥10 个百分点提示恢复能力退化（项目默认阈值）",
    ),
    DetectionRule(
        rule_id="LATENCY_INCREASE",
        metric_name="average_duration_ms",
        direction="rise",
        threshold=0.25,
        threshold_unit="ratio",
        severity="LOW",
        blocking=False,
        rationale="平均耗时相对上升 ≥25% 提示性能退化（项目默认阈值）",
        min_absolute=5.0,
    ),
    DetectionRule(
        rule_id="COST_INCREASE",
        metric_name="total_tokens",
        direction="rise",
        threshold=0.25,
        threshold_unit="ratio",
        severity="MEDIUM",
        blocking=False,
        rationale="Token 成本相对上升 ≥25% 提示成本退化（项目默认阈值）",
        min_absolute=1.0,
    ),
    DetectionRule(
        rule_id="CASE_REGRESSION",
        metric_name="case_level",
        direction="count",
        threshold=1.0,
        threshold_unit="count",
        severity="HIGH",
        blocking=True,
        rationale="存在 PASS→FAIL 的案例回归即视为阻断（项目默认阈值：1 例）",
    ),
    DetectionRule(
        rule_id="NEW_FAILURE_PATTERN",
        metric_name="failure_pattern",
        direction="count",
        threshold=1.0,
        threshold_unit="count",
        severity="MEDIUM",
        blocking=False,
        rationale="出现新的失败模式指纹提示未被覆盖的新失败类型（项目默认阈值：1 个）",
    ),
)

_DEFAULT_BY_ID = {r.rule_id: r for r in DEFAULT_RULES}


def _rules(custom: list[dict[str, Any]] | None) -> list[DetectionRule]:
    """合并默认规则与调用方覆盖（只允许覆盖阈值/启用/严重度/阻断）。"""
    if not custom:
        return list(DEFAULT_RULES)
    out: dict[str, DetectionRule] = {r.rule_id: r for r in DEFAULT_RULES}
    for item in custom:
        rid = item.get("rule_id")
        if rid not in _DEFAULT_BY_ID:
            raise RegressionError(f"未知规则: {rid!r}")
        base = out[rid]
        updates: dict[str, Any] = {}
        if "threshold" in item:
            t = item["threshold"]
            if not isinstance(t, (int, float)) or t < 0:
                raise RegressionError(f"规则 {rid} 的 threshold 必须是非负数")
            updates["threshold"] = float(t)
        if "enabled" in item:
            updates["enabled"] = bool(item["enabled"])
        if "severity" in item:
            if item["severity"] not in RISK_LEVELS or item["severity"] == "UNKNOWN":
                raise RegressionError(f"规则 {rid} 的 severity 非法")
            updates["severity"] = item["severity"]
        if "blocking" in item:
            updates["blocking"] = bool(item["blocking"])
        if "min_absolute" in item:
            updates["min_absolute"] = float(item["min_absolute"])
        out[rid] = DetectionRule(**{**base.to_dict(), **updates})
    return [out[r.rule_id] for r in DEFAULT_RULES]


# ---------------------------------------------------------------------------
# 规则求值
# ---------------------------------------------------------------------------

def _metric_map(comparison: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["metric"]: m for m in (comparison.get("metric_deltas") or [])}


def _evaluate_rule(
    rule: DetectionRule,
    comparison: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """对单条规则求值，返回事件（TRIGGERED / NOT_TRIGGERED / NOT_EVALUABLE）。"""
    event: dict[str, Any] = {
        "rule_id": rule.rule_id,
        "metric_name": rule.metric_name,
        "direction": rule.direction,
        "threshold": rule.threshold,
        "threshold_unit": rule.threshold_unit,
        "severity": rule.severity,
        "blocking": rule.blocking,
        "rationale": rule.rationale,
        "enabled": rule.enabled,
        "threshold_source": rule.source,
        "status": "NOT_EVALUABLE",
        "observed": None,
        "evidence_refs": [],
        "reason": "",
    }

    if not rule.enabled:
        # 显式配置禁用 ≠ 证据缺失：单独标记 DISABLED，不参与"无法求值"统计，
        # 但报告必须清楚列出被禁用的规则（含阻断规则），避免门禁被静默放宽。
        event["status"] = "DISABLED"
        event["reason"] = "规则在本次检测中被显式配置为禁用"
        return event

    # 案例级回归
    if rule.rule_id == "CASE_REGRESSION":
        counts = (comparison.get("cases") or {}).get("counts") or {}
        regressed = counts.get("regressed")
        if regressed is None:
            event["reason"] = "对比报告缺少案例级统计"
            return event
        rows = (comparison.get("cases") or {}).get("rows") or []
        reg_cases = [r for r in rows if r.get("transition") == "PASS_TO_FAIL"]
        event["observed"] = {"regressed_cases": regressed, "threshold": rule.threshold}
        event["evidence_refs"] = [
            {
                "kind": "case",
                "ref_id": c["case_id"],
                "detail": "PASS→FAIL",
                "trace_ids": [c.get("trace_id_baseline"), c.get("trace_id_candidate")],
            }
            for c in (comparison.get("cases") or {}).get("regressed_cases") or []
        ]
        event["status"] = "TRIGGERED" if regressed >= rule.threshold else "NOT_TRIGGERED"
        event["reason"] = (
            f"PASS→FAIL 案例 {regressed} 例（阈值 {rule.threshold:g}）"
        )
        return event

    # 新失败模式
    if rule.rule_id == "NEW_FAILURE_PATTERN":
        diff = comparison.get("failure_pattern_diff") or {}
        if diff.get("status") != "AVAILABLE":
            event["reason"] = diff.get("note") or "失败模式分布不可用"
            return event
        new_patterns = diff.get("new_patterns") or []
        event["observed"] = {"new_patterns": len(new_patterns), "threshold": rule.threshold}
        event["evidence_refs"] = [
            {
                "kind": "failure_pattern",
                "ref_id": p["fingerprint_id"],
                "detail": p["fingerprint_label"],
            }
            for p in new_patterns
        ]
        event["status"] = (
            "TRIGGERED" if len(new_patterns) >= rule.threshold else "NOT_TRIGGERED"
        )
        event["reason"] = f"新增失败模式 {len(new_patterns)} 个（阈值 {rule.threshold:g}）"
        return event

    # 指标型规则
    m = metrics.get(rule.metric_name)
    if m is None:
        event["reason"] = f"对比报告不含指标 {rule.metric_name}"
        return event
    va = (m.get("baseline") or {}).get("value")
    vb = (m.get("candidate") or {}).get("value")
    if va is None or vb is None:
        da = (m.get("baseline") or {}).get("denominator")
        db = (m.get("candidate") or {}).get("denominator")
        # 分母为 0 表示该维度在本批次不适用（如无恢复场景），不是数据缺失
        not_applicable = (da == 0 and db == 0)
        event["status"] = "NOT_APPLICABLE" if not_applicable else "NOT_EVALUABLE"
        event["reason"] = (
            f"指标 {rule.metric_name} 的判定分母为 0（该维度不适用），规则跳过"
            if not_applicable else
            f"指标 {rule.metric_name} 在{'基线' if va is None else '候选'}侧为 null（UNKNOWN），"
            "无法判定（不当作 0）"
        )
        event["observed"] = {"baseline": va, "candidate": vb,
                             "baseline_denominator": da, "candidate_denominator": db}
        return event

    delta = vb - va
    event["observed"] = {
        "baseline": va,
        "candidate": vb,
        "delta": round(delta, 6),
    }
    event["evidence_refs"] = [{
        "kind": "metric",
        "ref_id": rule.metric_name,
        "detail": f"baseline={va} candidate={vb}",
        "numerator_baseline": (m.get("baseline") or {}).get("numerator"),
        "denominator_baseline": (m.get("baseline") or {}).get("denominator"),
        "numerator_candidate": (m.get("candidate") or {}).get("numerator"),
        "denominator_candidate": (m.get("candidate") or {}).get("denominator"),
    }]

    if rule.threshold_unit == "percentage_point":
        magnitude = delta * 100.0
        # 方向必须真实发生（严格 >0 / <0），否则 threshold=0 会在"相等"时误触发
        if rule.direction == "drop":
            triggered = magnitude < 0 and magnitude <= -rule.threshold
        else:
            triggered = magnitude > 0 and magnitude >= rule.threshold
        event["observed"]["change_pp"] = round(magnitude, 2)
        event["reason"] = (
            f"{rule.metric_name} 变化 {magnitude:+.2f}pp（阈值 "
            f"{'+' if rule.direction == 'rise' else '-'}{rule.threshold:g}pp）"
        )
    else:  # ratio（相对变化）
        base = abs(va)
        ratio = (delta / base) if base else None
        event["observed"]["relative_change"] = round(ratio, 4) if ratio is not None else None

        if delta == 0:
            event["status"] = "NOT_TRIGGERED"
            event["reason"] = f"{rule.metric_name} 无变化（{va} → {vb}）"
            return event

        # 噪声抑制：变化幅度低于最小绝对值时不判定
        if rule.min_absolute is not None and abs(delta) < rule.min_absolute:
            event["status"] = "NOT_TRIGGERED"
            event["reason"] = (
                f"{rule.metric_name} 变化 {delta:+.2f} 低于最小绝对阈值 "
                f"{rule.min_absolute:g}（噪声抑制）"
            )
            return event

        if ratio is None:
            # 基线为 0 且确实发生变化：相对变化无定义，按绝对变化判定并如实说明
            triggered = delta > 0 if rule.direction == "rise" else delta < 0
            event["status"] = "TRIGGERED" if triggered else "NOT_TRIGGERED"
            event["reason"] = (
                f"{rule.metric_name} 基线为 0，相对变化无定义；按绝对变化 {delta:+.2f} 判定"
            )
            return event

        # 同样要求方向真实发生，避免 threshold=0 时相等误触发
        if rule.direction == "drop":
            triggered = ratio < 0 and ratio <= -rule.threshold
        else:
            triggered = ratio > 0 and ratio >= rule.threshold
        event["reason"] = (
            f"{rule.metric_name} 相对变化 {ratio:+.2%}（阈值 {rule.threshold:.0%}）"
        )

    event["status"] = "TRIGGERED" if triggered else "NOT_TRIGGERED"
    return event


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _comparison_or_build(
    comparison: dict[str, Any] | None,
    baseline_run_id: str | None,
    candidate_run_id: str | None,
    max_cases: int | None,
) -> tuple[dict[str, Any], str]:
    from backend.app.services.evaluation_version_comparison import (
        VersionComparisonError,
        compare_runs,
    )

    if comparison is not None:
        return comparison, comparison.get("comparison_id")
    if not baseline_run_id or not candidate_run_id:
        raise RegressionError("必须提供 comparison 或 (baseline_run_id, candidate_run_id)")
    try:
        cmp = compare_runs(baseline_run_id, candidate_run_id, max_cases=max_cases)
    except VersionComparisonError as exc:
        raise RegressionError(str(exc)) from exc
    return cmp, cmp["comparison_id"]


def detect_regressions(
    *,
    comparison: dict[str, Any] | None = None,
    baseline_run_id: str | None = None,
    candidate_run_id: str | None = None,
    max_cases: int | None = None,
    rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """执行回归检测，返回 RegressionReport（并持久化）。"""
    cmp, comparison_id = _comparison_or_build(
        comparison, baseline_run_id, candidate_run_id, max_cases
    )
    active_rules = _rules(rules)

    comparability = cmp.get("comparability") or {}
    metrics = _metric_map(cmp)

    events = [_evaluate_rule(r, cmp, metrics) for r in active_rules]
    triggered = [e for e in events if e["status"] == "TRIGGERED"]
    not_evaluable = [e for e in events if e["status"] == "NOT_EVALUABLE"]
    not_applicable = [e for e in events if e["status"] == "NOT_APPLICABLE"]
    disabled = [e for e in events if e["status"] == "DISABLED"]

    # ---- 风险等级：取触发规则的最大严重度；不可比 → UNKNOWN ----
    comparable = bool(comparability.get("is_comparable", False))
    if not comparable:
        risk_level = "UNKNOWN"
        risk_reason = "版本不可比：指标差异不能解释为退化或提升"
    elif triggered:
        worst = max(triggered, key=lambda e: _RISK_ORDER.get(e["severity"], -1))
        risk_level = worst["severity"]
        risk_reason = f"最高严重度规则命中：{worst['rule_id']}（{worst['severity']}）"
    elif disabled and len(disabled) == len(events):
        risk_level = "UNKNOWN"
        risk_reason = "全部规则被配置为禁用，无法判定风险"
    elif not_evaluable and len(not_evaluable) == len(events):
        risk_level = "UNKNOWN"
        risk_reason = "所有规则均无法求值（关键证据缺失）"
    elif not_evaluable:
        risk_level = "LOW"
        risk_reason = (
            f"无规则命中，但 {len(not_evaluable)} 条规则因证据缺失无法求值（见 limitations）"
        )
    else:
        risk_level = "LOW"
        risk_reason = "所有规则均可求值且无命中"

    # ---- 质量门禁 ----
    blocking_triggered = [e for e in triggered if e["blocking"]]
    if not comparable:
        gate_status = "INCONCLUSIVE"
        gate_reason = "版本不可比（存在 CRITICAL 级可比性问题），门禁无法判定，不视为通过"
    elif risk_level == "UNKNOWN":
        gate_status = "INCONCLUSIVE"
        gate_reason = risk_reason + "；INCONCLUSIVE 不等同于 PASS"
    elif blocking_triggered:
        gate_status = "BLOCK"
        gate_reason = "触发阻断规则：" + ", ".join(e["rule_id"] for e in blocking_triggered)
    elif triggered:
        gate_status = "WARN"
        gate_reason = "存在非阻断退化：" + ", ".join(e["rule_id"] for e in triggered)
    elif not_evaluable:
        # 无退化但必要证据缺失 → 不能给 PASS（只允许 NOT_APPLICABLE 跳过）
        gate_status = "WARN"
        gate_reason = (
            "无规则命中，但以下规则因证据缺失无法求值（不视为通过）："
            + ", ".join(e["rule_id"] for e in not_evaluable)
        )
    else:
        gate_status = "PASS"
        skipped_parts: list[str] = []
        if not_applicable:
            skipped_parts.append(f"{len(not_applicable)} 条规则因维度不适用跳过")
        if disabled:
            skipped_parts.append(
                f"{len(disabled)} 条规则被配置禁用："
                + ", ".join(e["rule_id"] for e in disabled)
            )
        gate_reason = "无规则命中且证据完整" + (
            f"（{'；'.join(skipped_parts)}）" if skipped_parts else ""
        )

    # ---- 证据汇总 ----
    evidence_refs: list[dict[str, Any]] = [{
        "kind": "comparison_report",
        "ref_id": comparison_id,
        "detail": f"comparability={comparability.get('verdict')}",
    }]
    seen: set[tuple[str, str]] = {("comparison_report", comparison_id)}
    for e in triggered + not_evaluable:
        for ref in e.get("evidence_refs") or []:
            key = (ref["kind"], ref["ref_id"])
            if key in seen:
                continue
            seen.add(key)
            evidence_refs.append(ref)

    regressed_cases = (cmp.get("cases") or {}).get("regressed_cases") or []
    metric_deltas = {
        m["metric"]: {
            "baseline": (m.get("baseline") or {}).get("value"),
            "candidate": (m.get("candidate") or {}).get("value"),
            "delta": m.get("delta"),
            "delta_unit": m.get("delta_unit"),
            "relative_change": m.get("relative_change"),
            "status": m.get("status"),
            "numerators_denominators": {
                "baseline": [(m.get("baseline") or {}).get("numerator"),
                             (m.get("baseline") or {}).get("denominator")],
                "candidate": [(m.get("candidate") or {}).get("numerator"),
                              (m.get("candidate") or {}).get("denominator")],
            },
        }
        for m in (cmp.get("metric_deltas") or [])
    }

    limitations = list(cmp.get("limitations") or []) + [
        "风险等级由规则严重度取最大值决定，不含模型生成的概率",
        "阈值是项目默认值（PROJECT_DEFAULT_NOT_INDUSTRY_STANDARD），可按需配置",
        "INCONCLUSIVE 表示无法判定，绝不视为 PASS",
    ]
    if not_evaluable:
        limitations.append(
            "以下规则无法求值（证据缺失）：" + ", ".join(e["rule_id"] for e in not_evaluable)
        )
    if disabled:
        limitations.append(
            "以下规则被配置禁用，其覆盖的风险未被检查："
            + ", ".join(e["rule_id"] for e in disabled)
        )
    if not comparable:
        limitations.append(
            "可比性 CRITICAL 问题：" + ", ".join(
                i["code"] for i in comparability.get("critical_issues") or []
            )
        )

    report = {
        "engine_version": ENGINE_VERSION,
        "regression_report_id": f"reg-{uuid.uuid4().hex}",
        "comparison_id": comparison_id,
        "baseline_version": cmp.get("baseline_version"),
        "candidate_version": cmp.get("candidate_version"),
        "comparability": comparability,
        "rules": [r.to_dict() for r in active_rules],
        "rule_events": events,
        "triggered_rules": [
            {k: e[k] for k in ("rule_id", "metric_name", "severity", "blocking",
                               "threshold", "threshold_unit", "reason", "observed")}
            for e in triggered
        ],
        "not_evaluable_rules": [
            {"rule_id": e["rule_id"], "reason": e["reason"]} for e in not_evaluable
        ],
        "not_applicable_rules": [
            {"rule_id": e["rule_id"], "reason": e["reason"]} for e in not_applicable
        ],
        "disabled_rules": [
            {"rule_id": e["rule_id"], "reason": e["reason"]} for e in disabled
        ],
        "regressed_cases": regressed_cases,
        "metric_deltas": metric_deltas,
        "failure_pattern_changes": {
            "status": (cmp.get("failure_pattern_diff") or {}).get("status"),
            "new_patterns": (cmp.get("failure_pattern_diff") or {}).get("new_patterns") or [],
            "resolved_patterns": (cmp.get("failure_pattern_diff") or {}).get("resolved_patterns") or [],
            "rows": (cmp.get("failure_pattern_diff") or {}).get("rows") or [],
        },
        "risk_level": risk_level,
        "risk_reason": risk_reason,
        "gate_status": gate_status,
        "gate_reason": gate_reason,
        "evidence_refs": evidence_refs,
        "limitations": limitations,
        "threshold_source": DEFAULTS_LABEL,
        "data_source": "analysis_over_evaluation_runs",
        "paid_api_called": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _persist(report)
    return report


def _persist(report: dict[str, Any]) -> Path:
    REGRESSION_DIR.mkdir(parents=True, exist_ok=True)
    path = REGRESSION_DIR / f"{report['regression_report_id']}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def get_regression_report(report_id: str) -> dict[str, Any]:
    if not report_id.startswith("reg-") or not report_id.replace("reg-", "").isalnum():
        raise RegressionError(f"非法回归报告 ID: {report_id!r}")
    path = REGRESSION_DIR / f"{report_id}.json"
    if not path.exists():
        raise RegressionError(f"回归报告不存在: {report_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_regression_reports(limit: int = 20) -> list[dict[str, Any]]:
    if not REGRESSION_DIR.exists():
        return []
    files = sorted(REGRESSION_DIR.glob("reg-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "regression_report_id": r.get("regression_report_id"),
            "comparison_id": r.get("comparison_id"),
            "risk_level": r.get("risk_level"),
            "gate_status": r.get("gate_status"),
            "triggered_rules": [e["rule_id"] for e in r.get("triggered_rules") or []],
            "generated_at": r.get("generated_at"),
        })
    return out
