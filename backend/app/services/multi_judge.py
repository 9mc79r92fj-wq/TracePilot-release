"""Multi-Judge Arbitration 多评测器仲裁（第 40 步）。

对同一批 Trace 用多个 Judge 独立评分，检测分歧、聚合仲裁，并在严重分歧时
拒绝给出确定性结论。

三个必须区分的 Judge 关系（`relationship`）：
- `distinct_model`  不同模型（真实模式下才可能成立）
- `prompt_variant`  同一模型的不同 Prompt 变体
- `resampling`      同一模型同一 Prompt 的重复采样

**独立性诚实声明**：多个 Mock variant **不是**独立真实模型。系统会计算
`independence.level`：
- `independent_models`：至少两个 Judge 的 (provider_kind, model_name) 各不相同
- `prompt_variants_only`：模型相同但 Prompt 不同
- `correlated`：模型与 Prompt 均相同（仅重复采样）→ 明确标注"不构成独立评测"

仲裁策略：
- `median`：可用 Judge 分数的中位数
- `mean`：算术平均
- `calibration_weighted`：**必须有真实校准结果**（judge_id → 权重 + 依据），
  否则抛 `MissingCalibrationError`，绝不编造可靠性权重
- `disagreement_flag` / `human_review_required`：分歧标记与人工复核建议
- 严重分歧时聚合结论为 `INCONCLUSIVE`，不强行给确定性结论

缺失评分（UNKNOWN）**不按 0 处理**：聚合时分母只计 SCORED 的 Judge；
若 SCORED 数量不足则结论为 INCONCLUSIVE / UNKNOWN。
"""

from __future__ import annotations

import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas.trace import TraceRecord
from backend.app.services.llm_judge import (
    DEFAULT_RUBRIC_ID,
    JudgeError,
    LLMJudge,
    build_judge,
    load_rubric,
)

__all__ = [
    "MultiJudgeError",
    "MissingCalibrationError",
    "JudgeSpec",
    "JudgeConfigSet",
    "DEFAULT_CONFIG_SETS",
    "build_judges",
    "assess_independence",
    "detect_disagreement",
    "arbitrate",
    "run_multi_judge",
    "MultiJudgeStore",
    "get_multi_judge_store",
]

ENGINE_VERSION = "1.0.0"
MAX_JUDGES = 5
MAX_CASES_PER_RUN = 30
DEFAULT_DISAGREEMENT_THRESHOLD = 2.0     # 最大分差阈值（1~5 分制）
DEFAULT_SEVERE_THRESHOLD = 3.0           # 严重分歧阈值
MIN_SCORED_FOR_CONCLUSION = 2            # 至少 2 个有效评分才能聚合

STRATEGIES = ("median", "mean", "calibration_weighted")


class MultiJudgeError(ValueError):
    """多 Judge 请求非法。"""


class MissingCalibrationError(MultiJudgeError):
    """缺少有效校准数据，无法使用加权聚合。"""


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------

class JudgeSpec(BaseModel):
    """单个 Judge 的配置。"""

    model_config = ConfigDict(extra="forbid")

    judge_id: str = Field(min_length=1, max_length=64)
    mode: str = Field(default="mock")            # mock | deepseek
    variant: str = Field(default="base")          # base | strict | lenient（mock 专用）
    model_name: str | None = None                 # 真实模型名（deepseek 用）
    sampling_index: int = Field(default=0, ge=0, le=9)


class JudgeConfigSet(BaseModel):
    """一个预设的多 Judge 配置组。"""

    model_config = ConfigDict(extra="forbid")

    config_id: str
    title: str
    description: str
    judges: list[JudgeSpec]
    expected_relationship: str
    limitation: str


# 预设配置组（全部基于 Mock，零成本）
DEFAULT_CONFIG_SETS: dict[str, JudgeConfigSet] = {
    "mock-mixed-variants": JudgeConfigSet(
        config_id="mock-mixed-variants",
        title="Mock 三变体对照（base / strict / lenient）",
        description="同一 Mock 模型的三个 Prompt 变体规则，用于验证分歧检测与仲裁链路。",
        expected_relationship="prompt_variant",
        limitation="三者共享同一规则基数，不是独立真实模型，分歧不代表真实模型间的分歧。",
        judges=[
            JudgeSpec(judge_id="judge-base", mode="mock", variant="base"),
            JudgeSpec(judge_id="judge-strict", mode="mock", variant="strict"),
            JudgeSpec(judge_id="judge-lenient", mode="mock", variant="lenient"),
        ],
    ),
    "mock-resampling": JudgeConfigSet(
        config_id="mock-resampling",
        title="Mock 重复采样（同模型同 Prompt ×3）",
        description="同一 Mock 模型同一 Prompt 重复 3 次，用于演示 correlated 场景。",
        expected_relationship="resampling",
        limitation="Mock 为确定性评审，重复采样结果完全相同，方差恒为 0；"
                   "该场景仅用于验证系统能识别「不构成独立评测」。",
        judges=[
            JudgeSpec(judge_id="judge-r0", mode="mock", variant="base", sampling_index=0),
            JudgeSpec(judge_id="judge-r1", mode="mock", variant="base", sampling_index=1),
            JudgeSpec(judge_id="judge-r2", mode="mock", variant="base", sampling_index=2),
        ],
    ),
    "mock-dual": JudgeConfigSet(
        config_id="mock-dual",
        title="Mock 双变体（base + strict）",
        description="两个 Prompt 变体，用于快速演示两评审器分歧。",
        expected_relationship="prompt_variant",
        limitation="不是独立真实模型。",
        judges=[
            JudgeSpec(judge_id="judge-base", mode="mock", variant="base"),
            JudgeSpec(judge_id="judge-strict", mode="mock", variant="strict"),
        ],
    ),
}


def build_judges(specs: list[JudgeSpec]) -> list[tuple[JudgeSpec, LLMJudge]]:
    """按配置构造 Judge 实例列表。

    Raises:
        MultiJudgeError: 配置非法（数量、重复 id、模式等）。
        RealJudgeDisabledError: 请求真实 Judge 但未授权。
    """
    if not (1 <= len(specs) <= MAX_JUDGES):
        raise MultiJudgeError(f"Judge 数量必须在 1..{MAX_JUDGES}")

    ids = [s.judge_id for s in specs]
    if len(ids) != len(set(ids)):
        raise MultiJudgeError("judge_id 必须唯一")

    out: list[tuple[JudgeSpec, LLMJudge]] = []
    for spec in specs:
        if spec.mode not in {"mock", "deepseek"}:
            raise MultiJudgeError(f"不支持的 judge mode: {spec.mode!r}")
        if spec.mode == "mock":
            judge = build_judge("mock", variant=spec.variant)
        else:
            judge = build_judge("deepseek", model=spec.model_name)
        out.append((spec, judge))
    return out


# ---------------------------------------------------------------------------
# 独立性评估
# ---------------------------------------------------------------------------

def assess_independence(
    results_by_judge: dict[str, dict[str, Any]],
    specs: list[JudgeSpec] | None = None,
) -> dict[str, Any]:
    """评估多个 Judge 之间的独立性（诚实标注，避免把同一 Mock 当独立模型）。

    关键规则：**Mock 变体的 model_name 后缀差异不构成独立模型**。
    所有 Mock 归一到同一模型身份 (mock, mock-rule-based-judge)，
    因此 mock 组合最高只能达到 `mock_prompt_variants`，永不声称 `independent_models`。
    """
    spec_map = {s.judge_id: s for s in (specs or [])}

    identities: dict[str, tuple[str, str, str]] = {}
    for jid, r in results_by_judge.items():
        pk = r.get("provider_kind")
        mn = r.get("model_name")
        spec = spec_map.get(jid)
        is_mock = r.get("is_mock") is True or (spec is not None and spec.mode == "mock")
        if is_mock:
            # Mock：忽略 variant 后缀，归一到同一模型身份
            model_identity = ("mock", "mock-rule-based-judge")
        else:
            model_identity = (pk, mn)
        identities[jid] = (model_identity[0], model_identity[1], r.get("prompt_version"))

    models = {(pk, mn) for pk, mn, _ in identities.values()}
    prompts = {(pk, mn, pv) for pk, mn, pv in identities.values()}
    mocks = [jid for jid, r in results_by_judge.items() if r.get("is_mock") is True]
    reals = [jid for jid, r in results_by_judge.items() if r.get("is_mock") is False]
    all_mock = len(mocks) == len(results_by_judge)

    if all_mock:
        if len(prompts) > 1:
            level = "mock_prompt_variants"
            note = (
                "全部为 Mock 规则评审，仅是同一规则基数的不同 Prompt 变体；"
                "**不构成独立真实模型评测**，分歧只反映变体规则差异。"
            )
        else:
            level = "correlated"
            note = (
                "全部 Mock 且模型与 Prompt 完全相同（仅重复采样）；"
                "**不构成独立评测**，方差无信息量（确定性评审下恒为 0）。"
            )
        not_independent = True
    elif len(models) > 1:
        level = "independent_models"
        note = "至少两个 Judge 使用不同模型标识（含真实模型），构成模型级独立评测。"
        not_independent = False
    elif len(prompts) > 1:
        level = "prompt_variants_only"
        note = "所有 Judge 共享同一模型，仅 Prompt 变体不同；分歧来自 Prompt 规则差异。"
        not_independent = True
    else:
        level = "correlated"
        note = "所有 Judge 的模型与 Prompt 完全相同（仅重复采样）；不构成独立评测。"
        not_independent = True

    return {
        "level": level,
        "note": note,
        "not_independent": not_independent,
        "distinct_model_count": len(models),
        "distinct_prompt_count": len(prompts),
        "mock_judges": mocks,
        "real_model_judges": reals,
        "all_mock": all_mock,
        "identity_map": {
            jid: {"model_identity": f"{pk}:{mn}", "prompt_version": pv}
            for jid, (pk, mn, pv) in identities.items()
        },
        "disclaimer": (
            "Mock Judge 为规则化模拟。多个 Mock 变体不构成独立真实模型评测，"
            "其分歧仅反映规则差异，不得作为真实模型一致性证据。"
            if all_mock else
            "含真实模型评审时，结果受模型版本与采样温度影响，可能不可复现。"
        ),
    }


# ---------------------------------------------------------------------------
# 分歧检测
# ---------------------------------------------------------------------------

def _norm(value: float | None) -> int | None:
    if value is None:
        return None
    return max(1, min(5, int(round(value))))


def detect_disagreement(
    per_case: list[dict[str, Any]],
    *,
    threshold: float = DEFAULT_DISAGREEMENT_THRESHOLD,
    severe_threshold: float = DEFAULT_SEVERE_THRESHOLD,
) -> dict[str, Any]:
    """对每个案例计算分歧指标并标记分歧案例。

    Args:
        per_case: 每项含 judge_id → {score, normalized, status, evidence, reason}

    缺失评分（UNKNOWN）不按 0 处理：只对 SCORED 的评分计算统计量。
    """
    case_reports: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []

    for c in per_case:
        scored_vals = [v["score"] for v in c["judges"].values() if v["status"] == "SCORED"]
        unknown_judges = [jid for jid, v in c["judges"].items() if v["status"] != "SCORED"]
        norm_vals = [_norm(v) for v in scored_vals]
        norm_vals = [v for v in norm_vals if v is not None]

        if len(scored_vals) >= 2:
            max_diff = round(max(scored_vals) - min(scored_vals), 4)
            variance = round(statistics.pvariance(scored_vals), 4)
            stdev = round(statistics.pstdev(scored_vals), 4)
            mean = round(statistics.mean(scored_vals), 4)
            median = round(statistics.median(scored_vals), 4)
            spread = round(max(scored_vals) - min(scored_vals), 4)
            agreement = round(1.0 - min(1.0, spread / 4.0), 4)  # 1~5 分制最大跨度 4
        else:
            max_diff = variance = stdev = mean = median = None
            agreement = None
            spread = None

        # 评分分布（归一化 1~5）
        distribution = {str(k): 0 for k in range(1, 6)}
        for v in norm_vals:
            distribution[str(v)] += 1

        is_flagged = bool(max_diff is not None and max_diff >= threshold)
        is_severe = bool(max_diff is not None and max_diff >= severe_threshold)
        enough = len(scored_vals) >= MIN_SCORED_FOR_CONCLUSION

        # 证据冲突：同一维度下不同 Judge 引用的证据集合是否完全不重叠
        evidence_conflicts = _evidence_conflicts(c["judges"])
        # 理由差异摘要
        reason_summary = {
            jid: (v.get("reason") or "")[:200] for jid, v in c["judges"].items()
        }

        row = {
            "case_id": c["case_id"],
            "trace_id": c.get("trace_id"),
            "judge_count": len(c["judges"]),
            "scored_count": len(scored_vals),
            "unknown_judges": unknown_judges,
            "max_diff": max_diff,
            "stdev": stdev,
            "variance": variance,
            "mean": mean,
            "median": median,
            "agreement": agreement,
            "distribution": distribution,
            "disagreement_flagged": is_flagged,
            "severe": is_severe,
            "sufficient_for_conclusion": enough,
            "evidence_conflicts": evidence_conflicts,
            "reason_summary": reason_summary,
            "human_review_required": bool(is_flagged or is_severe or not enough or unknown_judges),
        }
        case_reports.append(row)
        if row["disagreement_flagged"] or row["severe"]:
            flagged.append(row)

    all_diffs = [r["max_diff"] for r in case_reports if r["max_diff"] is not None]
    return {
        "threshold": threshold,
        "severe_threshold": severe_threshold,
        "min_scored_for_conclusion": MIN_SCORED_FOR_CONCLUSION,
        "cases": case_reports,
        "flagged_cases": flagged,
        "flagged_count": len(flagged),
        "case_count": len(case_reports),
        "aggregate": {
            "mean_max_diff": round(statistics.mean(all_diffs), 4) if all_diffs else None,
            "max_max_diff": round(max(all_diffs), 4) if all_diffs else None,
            "mean_agreement": (
                round(statistics.mean([r["agreement"] for r in case_reports if r["agreement"] is not None]), 4)
                if any(r["agreement"] is not None for r in case_reports) else None
            ),
            "cases_with_unknown_judges": sum(1 for r in case_reports if r["unknown_judges"]),
            "denominator_definition": "至少 2 个 Judge 给出 SCORED 的案例数",
            "conclusive_case_count": sum(1 for r in case_reports if r["sufficient_for_conclusion"]),
        },
        "note": "缺失评分（UNKNOWN）不计入统计、不按 0 处理；分歧统计仅基于 SCORED 评分。",
    }


def _evidence_conflicts(judges: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """检测同一维度下不同 Judge 引用的证据是否完全不重叠。"""
    by_dim: dict[str, dict[str, list[str]]] = {}
    for jid, v in judges.items():
        for dim, evs in (v.get("evidence") or {}).items():
            if isinstance(evs, list) and evs:
                by_dim.setdefault(dim, {})[jid] = [str(e)[:200] for e in evs]

    conflicts: list[dict[str, Any]] = []
    for dim, per_judge in by_dim.items():
        if len(per_judge) < 2:
            continue
        sets = {jid: set(evs) for jid, evs in per_judge.items()}
        jids = list(sets)
        any_overlap = False
        for i in range(len(jids)):
            for j in range(i + 1, len(jids)):
                if sets[jids[i]] & sets[jids[j]]:
                    any_overlap = True
                    break
            if any_overlap:
                break
        if not any_overlap:
            conflicts.append(
                {
                    "dimension": dim,
                    "judges": jids,
                    "detail": "各 Judge 在该维度引用的证据完全不重叠",
                    "evidence_by_judge": {jid: per_judge[jid][:2] for jid in jids},
                }
            )
    return conflicts


# ---------------------------------------------------------------------------
# 仲裁
# ---------------------------------------------------------------------------

def arbitrate(
    per_case: list[dict[str, Any]],
    *,
    strategy: str = "median",
    threshold: float = DEFAULT_DISAGREEMENT_THRESHOLD,
    severe_threshold: float = DEFAULT_SEVERE_THRESHOLD,
    calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按策略聚合多 Judge 评分。

    Args:
        strategy: median | mean | calibration_weighted
        calibration: 加权聚合所需的**真实校准依据**：
            {"source": str, "weights": {judge_id: float}, "evidence": str}
            缺失时 calibration_weighted 抛 MissingCalibrationError。

    Returns:
        仲裁结果（逐案例 + 汇总 + 人工复核建议）。
    """
    if strategy not in STRATEGIES:
        raise MultiJudgeError(f"不支持的聚合策略: {strategy!r}（仅 {', '.join(STRATEGIES)}）")

    weights: dict[str, float] | None = None
    calib_meta: dict[str, Any] | None = None
    if strategy == "calibration_weighted":
        if not calibration or not isinstance(calibration.get("weights"), dict):
            raise MissingCalibrationError(
                "加权聚合必须提供真实校准依据（judge_id→权重 + source + evidence）；"
                "没有有效校准数据时不得编造可靠性权重"
            )
        weights = {k: float(v) for k, v in calibration["weights"].items() if float(v) > 0}
        if not weights:
            raise MissingCalibrationError("校准权重为空或全为 0，无法加权聚合")
        if not calibration.get("source"):
            raise MissingCalibrationError("校准权重缺少 source 字段（无法追溯依据）")
        calib_meta = {
            "source": calibration.get("source"),
            "evidence": calibration.get("evidence"),
            "weights": weights,
        }

    results: list[dict[str, Any]] = []
    for c in per_case:
        judges = c["judges"]
        scored = {jid: v["score"] for jid, v in judges.items() if v["status"] == "SCORED"}
        unknown = [jid for jid, v in judges.items() if v["status"] != "SCORED"]
        vals = list(scored.values())
        max_diff = round(max(vals) - min(vals), 4) if len(vals) >= 2 else None
        severe = bool(max_diff is not None and max_diff >= severe_threshold)
        enough = len(vals) >= MIN_SCORED_FOR_CONCLUSION

        aggregated: float | None = None
        status = "SCORED"
        reason = ""

        if not enough:
            status = "INCONCLUSIVE"
            reason = f"有效评分不足（{len(vals)} < {MIN_SCORED_FOR_CONCLUSION}），无法聚合"
        elif severe:
            status = "INCONCLUSIVE"
            reason = f"严重分歧（最大分差 {max_diff} ≥ {severe_threshold}），不输出确定性结论"
        else:
            if strategy == "median":
                aggregated = round(statistics.median(vals), 4)
            elif strategy == "mean":
                aggregated = round(statistics.mean(vals), 4)
            else:  # calibration_weighted
                assert weights is not None
                used = {jid: w for jid, w in weights.items() if jid in scored}
                if not used:
                    status = "INCONCLUSIVE"
                    reason = "没有 Judge 具备校准权重，无法加权聚合"
                else:
                    total_w = sum(used.values())
                    aggregated = round(
                        sum(scored[jid] * w for jid, w in used.items()) / total_w, 4
                    )

        flagged = bool(max_diff is not None and max_diff >= threshold)
        results.append(
            {
                "case_id": c["case_id"],
                "status": status,
                "aggregated_score": aggregated,
                "aggregated_normalized": _norm(aggregated),
                "strategy": strategy,
                "judge_scores": scored,
                "unknown_judges": unknown,
                "max_diff": max_diff,
                "severe": severe,
                "disagreement_flagged": flagged,
                "human_review_required": bool(flagged or severe or not enough or unknown),
                "reason": reason,
            }
        )

    conclusive = [r for r in results if r["status"] == "SCORED"]
    return {
        "strategy": strategy,
        "cases": results,
        "summary": {
            "case_count": len(results),
            "conclusive_count": len(conclusive),
            "inconclusive_count": len(results) - len(conclusive),
            "human_review_required_count": sum(1 for r in results if r["human_review_required"]),
            "aggregated_mean": (
                round(statistics.mean([r["aggregated_score"] for r in conclusive]), 4)
                if conclusive else None
            ),
            "denominator_definition": "可获得确定性结论的案例数（排除 INCONCLUSIVE）",
        },
        "calibration": calib_meta,
        "note": (
            "INCONCLUSIVE 表示分歧过大或有效评分不足，系统拒绝给出确定性结论；"
            "这不是失败，而是诚实的不可判定。"
        ),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _judge_record(result: Any) -> dict[str, Any]:
    return {
        "status": "SCORED" if result.overall is not None else "UNKNOWN",
        "score": result.overall,
        "normalized": _norm(result.overall),
        "provider_kind": result.provider_kind,
        "model_name": result.model_name,
        "prompt_version": result.prompt_version,
        "rubric_id": result.rubric_id,
        "rubric_version": result.rubric_version,
        "judge_engine_version": result.judge_engine_version,
        "is_mock": result.is_mock,
        "simulated": result.simulated,
        "unknown_dimensions": list(result.unknown_dimensions),
        "dimension_scores": {s.dimension: s.score for s in result.scores},
        "evidence": {s.dimension: s.evidence for s in result.scores if s.evidence},
        "reason": " | ".join(
            f"{s.dimension}: {s.reason}" for s in result.scores if s.reason
        )[:1000],
        "limitations": list(result.limitations),
        "latency_ms": result.latency_ms,
        "token_usage": result.token_usage,
    }


def run_multi_judge(
    traces: list[tuple[str, TraceRecord]],
    specs: list[JudgeSpec],
    *,
    rubric_id: str = DEFAULT_RUBRIC_ID,
    strategy: str = "median",
    threshold: float = DEFAULT_DISAGREEMENT_THRESHOLD,
    severe_threshold: float = DEFAULT_SEVERE_THRESHOLD,
    calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对多条 Trace 执行多 Judge 独立评分 + 分歧检测 + 仲裁。

    Args:
        traces: [(case_id, TraceRecord)]
        specs: Judge 配置列表
        rubric_id: 所有 Judge 使用同一 Rubric
    """
    if not (1 <= len(traces) <= MAX_CASES_PER_RUN):
        raise MultiJudgeError(f"案例数量必须在 1..{MAX_CASES_PER_RUN}")

    rubric = load_rubric(rubric_id)
    pairs = build_judges(specs)

    # 为每个 Judge 显式分配足够预算（避免批量时触碰默认 20 次上限）
    needed = len(traces)
    for _spec, judge in pairs:
        if hasattr(judge, "set_call_limit"):
            judge.set_call_limit(max(needed, 1))

    started = time.monotonic()
    per_case: list[dict[str, Any]] = []
    judge_first: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []

    for case_id, trace in traces:
        judges_out: dict[str, dict[str, Any]] = {}
        for spec, judge in pairs:
            try:
                res = judge.judge(trace, rubric_id=rubric_id)
                rec = _judge_record(res)
            except Exception as exc:  # 单 Judge 失败互不影响
                rec = {
                    "status": "ERROR",
                    "score": None,
                    "normalized": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                errors.append({"case_id": case_id, "judge_id": spec.judge_id,
                               "error": rec["error"]})
            judges_out[spec.judge_id] = rec
            if spec.judge_id not in judge_first and rec.get("provider_kind"):
                judge_first[spec.judge_id] = rec
        per_case.append(
            {
                "case_id": case_id,
                "trace_id": trace.trace_id,
                "judges": judges_out,
            }
        )

    disagreement = detect_disagreement(
        per_case, threshold=threshold, severe_threshold=severe_threshold
    )
    arbitration = arbitrate(
        per_case,
        strategy=strategy,
        threshold=threshold,
        severe_threshold=severe_threshold,
        calibration=calibration,
    )
    independence = assess_independence(judge_first, [s for s, _ in pairs])

    return {
        "multi_judge_engine_version": ENGINE_VERSION,
        "rubric": {"rubric_id": rubric.rubric_id, "version": rubric.version,
                   "dimensions": rubric.dimension_keys},
        "judges": [
            {
                "judge_id": spec.judge_id,
                "mode": spec.mode,
                "variant": spec.variant if spec.mode == "mock" else None,
                "model_name": judge_first.get(spec.judge_id, {}).get("model_name"),
                "prompt_version": judge_first.get(spec.judge_id, {}).get("prompt_version"),
                "provider_kind": judge_first.get(spec.judge_id, {}).get("provider_kind"),
                "is_mock": judge_first.get(spec.judge_id, {}).get("is_mock"),
                "sampling_index": spec.sampling_index,
            }
            for spec, _j in pairs
        ],
        "independence": independence,
        "case_count": len(per_case),
        "cases": per_case,
        "disagreement": disagreement,
        "arbitration": arbitration,
        "errors": errors,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "limitations": [
            "Mock Judge 为规则化模拟；多 Mock 变体的分歧不代表真实模型间分歧",
            "仲裁结果为聚合启发式，不构成客观真值",
            "缺失评分（UNKNOWN）不参与统计，也不按 0 处理",
            "严重分歧时返回 INCONCLUSIVE，不强行输出确定性结论",
        ],
        "paid_api_called": any(
            j.get("is_mock") is False for j in judge_first.values()
        ),
    }


# ---------------------------------------------------------------------------
# 运行登记（内存，有界）
# ---------------------------------------------------------------------------

MAX_CONCURRENT_RUNS = 1
MAX_RUN_SECONDS = 120.0
MAX_STORED_RUNS = 20


class MultiJudgeStore:
    """内存运行登记表（并发 1、有超时、有上限）。"""

    def __init__(self, timeout_seconds: float = MAX_RUN_SECONDS,
                 max_stored: int = MAX_STORED_RUNS) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._running = 0
        self._timeout = timeout_seconds
        self._max_stored = max_stored
        self._executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_RUNS,
                                            thread_name_prefix="multi-judge")

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._running >= MAX_CONCURRENT_RUNS:
                raise MultiJudgeError("并发多 Judge 评测数已达上限，请稍后重试")
            run_id = f"mj-{uuid.uuid4().hex}"
            rec = {
                "run_id": run_id,
                "status": "running",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "result": None,
                "error": None,
            }
            self._runs[run_id] = rec
            self._order.append(run_id)
            self._running += 1
            while len(self._order) > self._max_stored:
                old = self._order.pop(0)
                self._runs.pop(old, None)
            self._executor.submit(self._execute, run_id, payload)
            return dict(rec)

    def _execute(self, run_id: str, payload: dict[str, Any]) -> None:
        started = time.monotonic()
        try:
            box: dict[str, Any] = {}

            def target() -> None:
                try:
                    box["value"] = run_multi_judge(**payload)
                except BaseException as exc:
                    box["error"] = exc

            t = threading.Thread(target=target, daemon=True)
            t.start()
            t.join(self._timeout)
            if t.is_alive():
                with self._lock:
                    self._runs[run_id].update(
                        status="failed", error=f"多 Judge 评测超时（>{self._timeout:g}s）")
                return
            if "error" in box:
                exc = box["error"]
                with self._lock:
                    self._runs[run_id].update(
                        status="failed", error=f"{type(exc).__name__}: {exc}")
                return
            with self._lock:
                self._runs[run_id].update(
                    status="succeeded", result=box["value"], error=None,
                    elapsed_ms=int((time.monotonic() - started) * 1000))
        finally:
            with self._lock:
                self._running = max(0, self._running - 1)

    def get(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            rec = self._runs.get(run_id)
            if rec is None:
                raise MultiJudgeError(f"多 Judge 评测运行不存在: {run_id}")
            return dict(rec)

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"run_id": r["run_id"], "status": r["status"], "created_at": r["created_at"]}
                for r in self._runs.values()
            ]


_store: MultiJudgeStore | None = None
_store_lock = threading.Lock()


def get_multi_judge_store() -> MultiJudgeStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = MultiJudgeStore()
    return _store
