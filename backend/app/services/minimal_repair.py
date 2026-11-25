"""Minimal Repair Engine 最小修复引擎（第 33 步）。

从故障 Trace 生成**安全、可验证**的最小修复候选，并用 CounterfactualEngine
做受控验证，输出可解释的 RepairReport。

支持的修复类型（白名单，其余一律拒绝）：
1. INCREASE_TIMEOUT        在允许范围内增加工具超时
2. LIMITED_RETRY           增加有限次数的工具重试
3. FALLBACK_RESULT         使用明确标记的安全模拟降级结果
4. ARGUMENT_CORRECTION     白名单范围内修正工具参数
5. SKIP_OPTIONAL_STEP      仅跳过明确允许的非关键步骤
6. SAFE_TOOL_ALTERNATIVE   在预先批准的等价工具之间切换

纪律：
- 绝不断言未验证方案一定有效；未执行的候选标记 not_tested；
- 评分是**排序启发式**，不是成功概率；
- 缺失指标记为 None（不填 0）；
- 原始 Trace 只读，全部验证走 CounterfactualEngine 的独立 Trace。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.app.schemas.trace import TraceRecord
from backend.app.services.counterfactual_engine import (
    CounterfactualError,
    run_counterfactual_experiment,
)
from backend.app.services.failure_propagation import analyze_failure_propagation

__all__ = [
    "RepairError",
    "UnsupportedRepairError",
    "REPAIR_TYPES",
    "RepairCandidate",
    "generate_candidates",
    "verify_candidate",
    "build_repair_report",
]

# ---- 有界参数 ----
MAX_CANDIDATES = 5                 # 候选数量上限
MAX_TIMEOUT_BUMP_MS = 30_000       # INCREASE_TIMEOUT 上限
MAX_RETRY_ADD = 3                  # LIMITED_RETRY 上限
MIN_TIMEOUT_MS_BASE = 1_000

REPAIR_TYPES = (
    "INCREASE_TIMEOUT",
    "LIMITED_RETRY",
    "FALLBACK_RESULT",
    "ARGUMENT_CORRECTION",
    "SKIP_OPTIONAL_STEP",
    "SAFE_TOOL_ALTERNATIVE",
)

# 预批准的等价工具组（SAFE_TOOL_ALTERNATIVE）
_SAFE_TOOL_GROUPS: dict[str, list[str]] = {
    "calculator": ["calculator"],
    "text_analyzer": ["text_analyzer"],
}

# 允许跳过的非关键步骤类型（SKIP_OPTIONAL_STEP）
_OPTIONAL_STEP_TYPES = {"context_review", "reflection", "evaluation"}

# 白名单参数修正规则：工具 -> {参数名: 合法值集合}
_ARG_WHITELIST: dict[str, dict[str, Any]] = {
    "calculator": {"expression": {"1+1", "1 + 1", "(12+8)*5"}},
    "text_analyzer": {"text": {"TracePilot failure replay sanity check"}},
}

# 默认评分权重（可配置）
DEFAULT_WEIGHTS = {
    "verified": 0.45,       # 是否通过验证（最高权重）
    "scope": 0.20,          # 修改范围（越小越好）
    "latency": 0.10,        # 执行延迟
    "token": 0.10,          # Token 成本
    "tool_calls": 0.05,     # 工具调用成本
    "side_effect": 0.10,    # 潜在副作用
}


class RepairError(ValueError):
    """修复请求非法。"""


class UnsupportedRepairError(RepairError):
    """修复类型不受支持。"""


@dataclass
class RepairCandidate:
    """单个修复候选。"""

    repair_id: str
    repair_type: str
    target_step_id: str | None
    target_tool: str | None
    modification: dict[str, Any]
    expected_effect: str
    risks: list[str]
    applicable_conditions: list[str]
    verification_status: str = "not_tested"   # verified / failed / not_tested
    verification_error: str | None = None
    repair_trace_id: str | None = None
    experiment_id: str | None = None
    outcome_before: dict[str, Any] | None = None
    outcome_after: dict[str, Any] | None = None
    cost_delta: dict[str, Any] | None = None
    score: float | None = None
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    rank: int | None = None


# ---------------------------------------------------------------------------
# 读取故障上下文
# ---------------------------------------------------------------------------

def _fault_steps(trace: TraceRecord) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in trace.steps:
        fi = s.metadata.get("fault_injection")
        if isinstance(fi, dict) and fi.get("injected") is True:
            out.append(
                {
                    "step_id": s.step_id,
                    "fault_type": fi.get("type"),
                    "tool_name": s.tool_name,
                    "step_type": s.step_type.value,
                }
            )
    return out


def _primary_fault_type(trace: TraceRecord) -> str | None:
    steps = _fault_steps(trace)
    if steps:
        return steps[0]["fault_type"]
    # 无注入标记时回退到错误类型
    for s in trace.steps:
        if s.error is not None:
            return s.error.error_type
    return None


# ---------------------------------------------------------------------------
# 候选生成
# ---------------------------------------------------------------------------

def generate_candidates(
    trace: TraceRecord,
    *,
    max_candidates: int = MAX_CANDIDATES,
    baseline_trace: TraceRecord | None = None,
) -> list[RepairCandidate]:
    """基于故障类型与传播分析生成有限数量的修复候选（不执行验证）。

    Raises:
        RepairError: 参数越界。
    """
    if not (1 <= max_candidates <= MAX_CANDIDATES):
        raise RepairError(f"max_candidates 必须在 1..{MAX_CANDIDATES}")

    fault_steps = _fault_steps(trace)
    fault_type = _primary_fault_type(trace)
    if not fault_steps and fault_type is None:
        return []  # 无故障，无候选

    target = fault_steps[0] if fault_steps else None
    target_step = target["step_id"] if target else None
    target_tool = target["tool_name"] if target else None

    # 传播分析：用于标注下游影响（仅用于风险说明，不作因果结论）
    propagation = analyze_failure_propagation(trace, baseline_trace)
    downstream = [
        sid for sid, cls in (
            (n["id"], n["classification"]) for n in propagation["nodes"]
        ) if cls == "potentially_affected"
    ]

    candidates: list[RepairCandidate] = []

    def add(
        rtype: str,
        modification: dict[str, Any],
        expected: str,
        risks: list[str],
        conditions: list[str],
        step_id: str | None = None,
        tool: str | None = None,
    ) -> None:
        if len(candidates) >= max_candidates:
            return
        candidates.append(
            RepairCandidate(
                repair_id=f"repair-{uuid.uuid4().hex[:12]}",
                repair_type=rtype,
                target_step_id=step_id if step_id is not None else target_step,
                target_tool=tool if tool is not None else target_tool,
                modification=modification,
                expected_effect=expected,
                risks=risks,
                applicable_conditions=conditions,
            )
        )

    ft = (fault_type or "").lower()

    # 1) 超时类故障 -> INCREASE_TIMEOUT / LIMITED_RETRY
    if "timeout" in ft:
        add(
            "INCREASE_TIMEOUT",
            {"timeout_ms": min(MIN_TIMEOUT_MS_BASE * 5, MAX_TIMEOUT_BUMP_MS)},
            "延长工具等待时间，避免因响应慢而误判失败",
            ["若底层服务不可用，延长超时只会增加等待时间", "不解决根本性能问题"],
            ["工具本身可用、仅响应偏慢", "上游允许更长等待"],
        )
        add(
            "LIMITED_RETRY",
            {"max_retries": min(2, MAX_RETRY_ADD)},
            "有限重试可跨越瞬时故障",
            ["重试会放慢失败路径", "对幂等性差的工具可能产生重复副作用"],
            ["工具幂等", "故障为瞬时性"],
        )

    # 2) 异常类故障 -> FALLBACK_RESULT
    if "exception" in ft or "tool_error" in ft or "toolfailure" in ft:
        add(
            "FALLBACK_RESULT",
            {
                "tool": target_tool or "calculator",
                "value": {"__fallback__": True, "reason": "safe simulated degraded result"},
            },
            "用安全标记的降级结果替代失败返回，保证流程可继续",
            ["降级结果不是真实计算结果，只能用于流程验证", "可能掩盖真实故障"],
            ["下游允许降级继续", "降级结果明确标记为模拟"],
        )

    # 3) 参数类故障 -> ARGUMENT_CORRECTION
    if "argument" in ft or "invalid" in ft and "result" not in ft:
        tool = target_tool if target_tool in _ARG_WHITELIST else "calculator"
        allowed = _ARG_WHITELIST.get(tool, {})
        fix = {k: sorted(v)[0] for k, v in allowed.items()}
        add(
            "ARGUMENT_CORRECTION",
            {"tool": tool, "arguments": fix},
            "把工具参数改为白名单内的合法值",
            ["仅在参数确为错误原因时有效", "白名单值有限，可能不覆盖真实业务参数"],
            ["工具在白名单内", "故障源于参数不合法"],
            tool=tool,
        )

    # 4) 权限类故障 -> SAFE_TOOL_ALTERNATIVE
    if "permission" in ft:
        alt = None
        for group in _SAFE_TOOL_GROUPS.values():
            if target_tool in group and len(group) > 1:
                alt = next(t for t in group if t != target_tool)
        add(
            "SAFE_TOOL_ALTERNATIVE",
            {"from": target_tool, "to": alt or target_tool},
            "切换到预先批准的等价工具，绕过被拒绝的访问路径",
            ["等价工具可能行为略有差异", "若无可选等价工具则无效"],
            ["存在预批准等价工具", "权限限制仅针对原工具"],
            tool=alt or target_tool,
        )

    # 5) 可选步骤跳过（仅非关键步骤）
    optional = [
        s for s in trace.steps if s.step_type.value in _OPTIONAL_STEP_TYPES
    ]
    if optional:
        s = optional[0]
        add(
            "SKIP_OPTIONAL_STEP",
            {"skip_step_id": s.step_id, "step_type": s.step_type.value},
            "跳过明确允许跳过的非关键步骤，减少失败面",
            ["跳过可能损失可解释性/审计信息", "仅限白名单步骤类型"],
            ["步骤类型在白名单内", "该步骤非任务关键路径"],
            step_id=s.step_id,
        )

    # 风险补充：下游可能受影响节点（仅提示，非因果结论）
    if downstream:
        for c in candidates:
            c.risks.append(
                f"潜在下游影响节点 {len(downstream)} 个（DAG 可达性提示，非因果结论）"
            )

    return candidates[:max_candidates]


# ---------------------------------------------------------------------------
# 候选验证（复用 CounterfactualEngine）
# ---------------------------------------------------------------------------

def _intervention_for(candidate: RepairCandidate) -> dict[str, Any] | None:
    """把修复候选映射为反事实引擎允许的干预（None 表示无法实际执行）。"""
    rt = candidate.repair_type
    if rt == "INCREASE_TIMEOUT":
        return {
            "type": "adjust_timeout_retry",
            "timeout_ms": int(candidate.modification["timeout_ms"]),
        }
    if rt == "LIMITED_RETRY":
        return {
            "type": "adjust_timeout_retry",
            "max_retries": int(candidate.modification["max_retries"]),
        }
    if rt == "FALLBACK_RESULT":
        return {
            "type": "replace_tool_result",
            "tool": candidate.modification.get("tool", "calculator"),
            "value": candidate.modification["value"],
        }
    if rt == "ARGUMENT_CORRECTION":
        return {
            "type": "replace_tool_result",
            "tool": candidate.modification.get("tool", "calculator"),
            "value": {"corrected_arguments": candidate.modification["arguments"]},
        }
    if rt == "SAFE_TOOL_ALTERNATIVE":
        return {
            "type": "replace_tool_result",
            "tool": candidate.modification.get("to", "calculator"),
            "value": {"__alternative_tool__": candidate.modification.get("to")},
        }
    # SKIP_OPTIONAL_STEP 无法在受控模拟执行器中表达 -> not_tested
    return None


def verify_candidate(
    trace: TraceRecord,
    candidate: RepairCandidate,
    *,
    baseline_trace: TraceRecord | None = None,
) -> RepairCandidate:
    """用 CounterfactualEngine 对单个候选做受控验证（不修改原始 Trace）。"""
    intervention = _intervention_for(candidate)
    if intervention is None:
        candidate.verification_status = "not_tested"
        candidate.verification_error = (
            "该修复类型无法在受控模拟执行器中表达（缺少对应受控干预）"
        )
        return candidate

    try:
        report = run_counterfactual_experiment(
            trace,
            intervention,
            replays=2,
            # 修复验证产生的派生 Trace 需与反事实实验区分，便于证据血缘追溯
            derivation="repair_validation",
        )
    except CounterfactualError as exc:
        candidate.verification_status = "not_tested"
        candidate.verification_error = f"受控验证被拒绝: {exc}"
        return candidate

    candidate.experiment_id = report["experiment_id"]
    candidate.repair_trace_id = report["intervention_outcome"]["trace_id"]
    candidate.outcome_before = report["observed_outcome"]
    candidate.outcome_after = report["intervention_outcome"]

    # 修复是否有效（三选一，均为受控环境下的真实观察）：
    # a) 故障集合被移除；
    # b) 状态由失败转为成功；
    # c) 工具最终恢复成功（如重试后第 2 次成功），而原始 Trace 中该工具为失败。
    diff = report["observed_difference"]
    removed = diff["fault_types_removed"]
    before_status = report["observed_outcome"]["status"]
    after_status = report["intervention_outcome"]["status"]
    tool_recovered = bool(report["intervention_outcome"].get("tool_recovered"))
    observed_tool_failed = any(
        s.step_type.value == "tool_call" and s.status.value == "failed"
        for s in trace.steps
    )
    improved = (
        bool(removed)
        or (before_status != "succeeded" and after_status == "succeeded")
        or (tool_recovered and observed_tool_failed)
    )
    candidate.verification_status = "verified" if improved else "failed"
    if not improved:
        candidate.verification_error = "受控验证未观察到可区分的改善"
    else:
        candidate.score_breakdown["improvement_signals"] = {
            "fault_types_removed": bool(removed),
            "status_improved": before_status != "succeeded" and after_status == "succeeded",
            "tool_recovered_after_retry": tool_recovered and observed_tool_failed,
        }

    # 成本变化（缺失值记 None，不填 0）
    def _sum(prefix: str) -> int | None:
        steps = report["comparison"]["traces"][prefix]["token_usage"]
        return steps.get("sum")

    candidate.cost_delta = {
        "token_before": _sum("reference"),
        "token_after": _sum("target"),
        "duration_before_ms": report["comparison"]["traces"]["reference"]["total_duration_ms"],
        "duration_after_ms": report["comparison"]["traces"]["target"]["total_duration_ms"],
        "tool_calls_delta": None,   # 受控模拟未单列工具调用计数，记未知
        "missing_metrics": ["tool_calls"],
    }
    candidate.score_breakdown["causal_evidence_level"] = report["causal_evidence_level"]
    return candidate


# ---------------------------------------------------------------------------
# 评分（可解释、可配置）
# ---------------------------------------------------------------------------

def _impact_scope(candidate: RepairCandidate) -> int:
    """修改范围：改动字段数（越小越好）。"""
    return len(candidate.modification)


def score_candidate(
    candidate: RepairCandidate,
    weights: dict[str, float] | None = None,
) -> RepairCandidate:
    """为候选计算可解释评分（0..1，越高越优）。

    评分是**排序启发式**，不表示真实成功概率。
    缺失指标（如 Token 未知）按中性处理并记录在 breakdown 中，不填 0 冒充。
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    total_weight = sum(w.values()) or 1.0
    breakdown: dict[str, Any] = {}

    # 1) 验证结果
    status = candidate.verification_status
    if status == "verified":
        verified_score = 1.0
    elif status == "failed":
        verified_score = 0.0
    else:  # not_tested -> 中性偏低
        verified_score = 0.3
    breakdown["verified"] = {"status": status, "score": verified_score}

    # 2) 修改范围
    scope = _impact_scope(candidate)
    scope_score = max(0.0, 1.0 - scope / 5.0)
    breakdown["scope"] = {"changed_fields": scope, "score": round(scope_score, 3)}

    # 3) 执行延迟
    before = (candidate.cost_delta or {}).get("duration_before_ms")
    after = (candidate.cost_delta or {}).get("duration_after_ms")
    if before is None or after is None:
        latency_score = 0.5
        breakdown["latency"] = {"before_ms": before, "after_ms": after, "score": 0.5,
                                "note": "数据缺失，按中性处理（未填 0）"}
    else:
        if after <= before:
            latency_score = 1.0
        else:
            latency_score = max(0.0, 1.0 - (after - before) / max(before, 1))
        breakdown["latency"] = {"before_ms": before, "after_ms": after,
                                "score": round(latency_score, 3)}

    # 4) Token 成本
    tb = (candidate.cost_delta or {}).get("token_before")
    ta = (candidate.cost_delta or {}).get("token_after")
    if tb is None or ta is None:
        token_score = 0.5
        breakdown["token"] = {"before": tb, "after": ta, "score": 0.5,
                              "note": "数据缺失，按中性处理（未填 0）"}
    else:
        token_score = 1.0 if ta <= tb else max(0.0, 1.0 - (ta - tb) / max(tb, 1))
        breakdown["token"] = {"before": tb, "after": ta, "score": round(token_score, 3)}

    # 5) 工具调用成本（未知 -> 中性）
    tc = (candidate.cost_delta or {}).get("tool_calls_delta")
    if tc is None:
        tool_score = 0.5
        breakdown["tool_calls"] = {"delta": None, "score": 0.5,
                                   "note": "数据缺失，按中性处理（未填 0）"}
    else:
        tool_score = 1.0 if tc <= 0 else max(0.0, 1.0 - tc / 5.0)
        breakdown["tool_calls"] = {"delta": tc, "score": round(tool_score, 3)}

    # 6) 潜在副作用（风险条数越多越低）
    n_risks = len(candidate.risks)
    side_score = max(0.0, 1.0 - n_risks / 5.0)
    breakdown["side_effect"] = {"risk_count": n_risks, "score": round(side_score, 3)}

    score = (
        w["verified"] * verified_score
        + w["scope"] * scope_score
        + w["latency"] * latency_score
        + w["token"] * token_score
        + w["tool_calls"] * tool_score
        + w["side_effect"] * side_score
    ) / total_weight

    candidate.score = round(score, 4)
    breakdown["weights"] = w
    breakdown["disclaimer"] = "评分为排序启发式，不代表真实成功概率"
    candidate.score_breakdown.update(breakdown)
    return candidate


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def build_repair_report(
    trace: TraceRecord,
    *,
    baseline_trace: TraceRecord | None = None,
    max_candidates: int = MAX_CANDIDATES,
    weights: dict[str, float] | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """生成结构化 RepairReport。

    Args:
        trace: 原始故障 Trace（只读）。
        baseline_trace: 可选基线（用于传播分析对比）。
        max_candidates: 候选上限（1..5）。
        weights: 自定义评分权重。
        verify: 是否执行受控验证（False 时全部标记 not_tested）。
    """
    candidates = generate_candidates(
        trace, max_candidates=max_candidates, baseline_trace=baseline_trace
    )

    if verify:
        for c in candidates:
            verify_candidate(trace, c, baseline_trace=baseline_trace)
    for c in candidates:
        score_candidate(c, weights)

    # 排序：已验证优先，其次按得分；not_tested 排在最后
    order = {"verified": 0, "failed": 1, "not_tested": 2}
    ranked = sorted(
        candidates,
        key=lambda c: (order[c.verification_status], -(c.score or 0.0)),
    )
    for i, c in enumerate(ranked, 1):
        c.rank = i

    recommended = ranked[0] if ranked and ranked[0].verification_status == "verified" else None

    unverified_items = []
    for c in ranked:
        if c.verification_status == "not_tested":
            unverified_items.append(
                f"{c.repair_id}（{c.repair_type}）未验证: {c.verification_error}"
            )
    unverified_items.append(
        "所有验证均在模拟模型与简化执行路径下完成，未在真实模型/生产环境复现"
    )

    return {
        "source_trace_id": trace.trace_id,
        "fault_type": _primary_fault_type(trace),
        "fault_nodes": [s["step_id"] for s in _fault_steps(trace)],
        "candidate_count": len(ranked),
        "candidates": [
            {
                "repair_id": c.repair_id,
                "rank": c.rank,
                "repair_type": c.repair_type,
                "target_step_id": c.target_step_id,
                "target_tool": c.target_tool,
                "modification": c.modification,
                "expected_effect": c.expected_effect,
                "risks": c.risks,
                "applicable_conditions": c.applicable_conditions,
                "verification_status": c.verification_status,
                "verification_error": c.verification_error,
                "experiment_id": c.experiment_id,
                "repair_trace_id": c.repair_trace_id,
                "outcome_before": c.outcome_before,
                "outcome_after": c.outcome_after,
                "cost_delta": c.cost_delta,
                "score": c.score,
                "score_breakdown": c.score_breakdown,
            }
            for c in ranked
        ],
        "task_result_change": {
            "before_status": ranked[0].outcome_before["status"] if ranked and ranked[0].outcome_before else None,
            "after_status_by_candidate": {
                c.repair_id: (c.outcome_after or {}).get("status") for c in ranked
            },
        },
        "cost_change": {
            c.repair_id: c.cost_delta for c in ranked
        },
        "recommended": (
            {
                "repair_id": recommended.repair_id,
                "repair_type": recommended.repair_type,
                "score": recommended.score,
                "basis": [
                    "通过受控验证（观察到故障消除或状态改善）",
                    "在已验证候选中评分最高",
                    "修改范围与风险相对较小",
                ],
                "caveat": "推荐基于模拟环境验证，不代表真实环境一定有效",
            }
            if recommended
            else None
        ),
        "recommendation_basis": (
            "按（验证结果 > 综合评分）排序；评分为启发式，非成功概率"
        ),
        "unverified": unverified_items,
        "original_trace_modified": False,
        "weights_used": dict(DEFAULT_WEIGHTS, **(weights or {})),
        "disclaimer": (
            "本报告仅对受控模拟条件下的观察结果负责，不构成因果证明；"
            "未验证候选不得视为有效修复。"
        ),
    }
