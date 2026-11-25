"""Judge Calibration 评测器校准模块（第 39 步）。

用人工参考标注（本数据集为 synthetic_reference）检验 LLM Judge 的评分一致性。

实现的指标（每个 Provider 独立统计，Mock 与 Real 严格隔离）：
- Exact Agreement：四舍五入后的 Judge 总分 == 参考分
- MAE（Mean Absolute Error）：|Judge 总分 − 参考分| 的均值
- Within-1 Agreement：|Judge 总分 − 参考分| ≤ 1 的比例
- Confusion Matrix：参考分（1~5）× 归一化 Judge 分（1~5）
- 高估 / 低估案例：偏差 ≥ 1 分
- 逐案例评分差异列表（含证据与理由）
- UNKNOWN 案例：参考标注为 UNKNOWN 或 Judge 给出 UNKNOWN

稳定性分析：同一案例重复评分 R 次，记录每次分值、波动（标准差）、一致性。

纪律：
- 绝不把 Mock Judge 结果冒充真实模型校准结果：结果按 provider_kind 分区，
  并在报告中显式标注 is_mock / simulated；
- 绝不伪造参考标注：annotation_source 必须显式记录，synthetic_reference 全量标注；
- 不输出未经验证的置信概率（仅输出可计算的统计量）。
"""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas.trace import TraceRecord, TraceStatus, TraceStep, TokenUsage
from backend.app.services.llm_judge import LLMJudge, load_rubric

__all__ = [
    "CalibrationError",
    "CalibrationCase",
    "CalibrationDataset",
    "load_calibration_dataset",
    "list_calibration_datasets",
    "case_to_trace",
    "run_calibration",
    "CALIBRATION_DIR",
    "MAX_REPEATS",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CALIBRATION_DIR = PROJECT_ROOT / "data" / "evaluation" / "calibration"

ENGINE_VERSION = "1.0.0"
MAX_REPEATS = 5          # 单案例重复评分上限
MAX_CASES_PER_RUN = 50   # 单次校准案例上限
SCORE_LEVELS = (1, 2, 3, 4, 5)
TS = "2026-01-01T00:00:00+00:00"


class CalibrationError(ValueError):
    """校准请求非法。"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class CalibrationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    category: str
    task: str
    candidate_answer: str
    reference_material: dict[str, Any] = Field(default_factory=dict)
    rubric_id: str
    human_score: int | None = Field(default=None, ge=1, le=5)
    human_status: str = Field(default="SCORED", pattern="^(SCORED|UNKNOWN)$")
    annotation_rationale: str
    annotation_source: str

    def is_reference_unknown(self) -> bool:
        return self.human_status == "UNKNOWN" or self.human_score is None


class CalibrationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calibration_id: str
    version: str
    rubric_id: str
    created_at: str
    annotation_source: str
    annotation_note: str
    categories: list[str]
    cases: list[CalibrationCase]


def load_calibration_dataset(path_or_id: str | None = None) -> CalibrationDataset:
    """加载并校验校准数据集。

    仅允许目录内固定文件或安全 ID，防路径穿越。同时支持两种标识：
    - 文件名 stem（如 `calibration_set_v1`）
    - JSON 内部的 `calibration_id`（如 `judge-calibration-v1`）

    第二种是 API/前端实际使用的标识，二者必须都能解析，避免契约不一致导致 404。
    """
    if path_or_id is None:
        path_or_id = "calibration_set_v1"
    raw = str(path_or_id).strip()

    if not raw.endswith(".json") and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw):
        raise CalibrationError(f"非法校准集标识: {path_or_id!r}")

    candidate: Path | None = None
    if raw.endswith(".json"):
        c = (CALIBRATION_DIR / Path(raw).name).resolve()
        if c.parent == CALIBRATION_DIR.resolve():
            candidate = c
    else:
        c = (CALIBRATION_DIR / f"{raw}.json").resolve()
        if c.parent == CALIBRATION_DIR.resolve() and c.exists():
            candidate = c

    # 回退：按 JSON 内部 calibration_id 匹配（前端使用该标识）
    if candidate is None or not candidate.exists():
        if CALIBRATION_DIR.exists():
            for p in sorted(CALIBRATION_DIR.glob("*.json")):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if str(data.get("calibration_id", "")).strip() == raw:
                    candidate = p.resolve()
                    break

    if candidate is None or not candidate.exists():
        raise CalibrationError(f"校准集不存在: {raw}")

    try:
        return CalibrationDataset.model_validate(
            json.loads(candidate.read_text(encoding="utf-8"))
        )
    except Exception as exc:
        raise CalibrationError(f"校准集结构非法: {exc}") from exc


def list_calibration_datasets() -> list[dict[str, Any]]:
    """列出可用校准集元信息。"""
    out: list[dict[str, Any]] = []
    if not CALIBRATION_DIR.exists():
        return out
    for p in sorted(CALIBRATION_DIR.glob("*.json")):
        try:
            ds = load_calibration_dataset(p.stem)
        except CalibrationError:
            continue
        scored = [c for c in ds.cases if not c.is_reference_unknown()]
        out.append(
            {
                "calibration_id": ds.calibration_id,
                "version": ds.version,
                "rubric_id": ds.rubric_id,
                "case_count": len(ds.cases),
                "scored_cases": len(scored),
                "unknown_cases": len(ds.cases) - len(scored),
                "annotation_source": ds.annotation_source,
                "annotation_note": ds.annotation_note,
                "categories": ds.categories,
            }
        )
    return out


# ---------------------------------------------------------------------------
# 案例 → Trace（供 Judge 消费）
# ---------------------------------------------------------------------------

def case_to_trace(case: CalibrationCase) -> TraceRecord:
    """把校准案例转换为最小 TraceRecord，供 Judge 的可观察材料提取逻辑使用。

    构造原则：只映射案例中真实提供的材料，**不编造**工具调用或返回值。
    """
    trace_id = f"trace-cal-{case.case_id}"
    steps: list[dict[str, Any]] = []

    # 1) 任务步骤（承载 task）
    steps.append(
        {
            "step_id": f"{trace_id}-s001",
            "trace_id": trace_id,
            "parent_step_ids": [],
            "step_type": "planning",
            "timestamp": TS,
            "input": {"task": case.task},
            "output": {"plan": ["执行任务"]},
            "status": "succeeded",
            "metadata": {"calibration": True},
        }
    )
    last = steps[-1]["step_id"]

    # 2) 工具步骤（仅当案例确实提供了工具材料）
    material = case.reference_material or {}
    tool_name = material.get("tool_name")
    if isinstance(tool_name, str) and tool_name:
        steps.append(
            {
                "step_id": f"{trace_id}-s002",
                "trace_id": trace_id,
                "parent_step_ids": [last],
                "step_type": "tool_call",
                "timestamp": TS,
                "tool_name": tool_name,
                "tool_arguments": material.get("tool_arguments"),
                "tool_result": material.get("tool_result"),
                "status": "succeeded",
                "metadata": {"calibration": True},
            }
        )
        last = steps[-1]["step_id"]

    # 3) 最终回答步骤
    steps.append(
        {
            "step_id": f"{trace_id}-s003",
            "trace_id": trace_id,
            "parent_step_ids": [last],
            "step_type": "model_call",
            "timestamp": TS,
            "input": {"messages": [{"role": "user", "content": case.task}]},
            "output": case.candidate_answer,
            "status": "succeeded",
            "token_usage": TokenUsage(input_tokens=0, output_tokens=0),
            "metadata": {"calibration": True},
        }
    )

    # notes 作为可观察上下文附加到 planning 输出（不伪装成工具返回）
    notes = material.get("notes")
    if isinstance(notes, str) and notes:
        steps[0]["output"] = {"plan": ["执行任务"], "notes": notes}

    return TraceRecord(
        trace_id=trace_id,
        task_id=f"task-cal-{case.case_id}",
        run_id=f"run-cal-{case.case_id}",
        started_at=TS,
        ended_at=TS,
        status=TraceStatus.SUCCEEDED,
        steps=[TraceStep.model_validate(s) for s in steps],
        metadata={"calibration": True, "case_id": case.case_id, "simulated": True},
    )


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------

def _norm_label(value: float | None) -> int | None:
    """把连续总分归一化为 1~5 整级（四舍五入并夹紧）。"""
    if value is None:
        return None
    return max(1, min(5, int(round(value))))


def _empty_matrix() -> dict[str, dict[str, int]]:
    return {str(h): {str(j): 0 for j in SCORE_LEVELS} for h in SCORE_LEVELS}


def _compute_agreement(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """基于逐案例行计算一致性指标（仅统计双方都 SCORED 的案例）。"""
    scored = [r for r in rows if r["human_score"] is not None and r["judge_score"] is not None]
    n = len(scored)
    if n == 0:
        return {
            "evaluable_cases": 0,
            "exact_agreement": None,
            "mae": None,
            "within_1_agreement": None,
            "confusion_matrix": _empty_matrix(),
            "denominator_definition": "参考标注与 Judge 均为 SCORED 的案例数",
        }

    abs_errors = [abs(r["judge_score"] - r["human_score"]) for r in scored]
    exact = sum(1 for r in scored if _norm_label(r["judge_score"]) == r["human_score"])
    within1 = sum(1 for e in abs_errors if e <= 1)

    matrix = _empty_matrix()
    for r in scored:
        h = str(r["human_score"])
        j = str(_norm_label(r["judge_score"]))
        matrix[h][j] += 1

    return {
        "evaluable_cases": n,
        "exact_agreement": round(exact / n, 4),
        "exact_agreement_count": exact,
        "mae": round(sum(abs_errors) / n, 4),
        "within_1_agreement": round(within1 / n, 4),
        "within_1_agreement_count": within1,
        "confusion_matrix": matrix,
        "denominator_definition": "参考标注与 Judge 均为 SCORED 的案例数",
    }


def _bias_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """高估 / 低估 / 一致 案例分类（偏差阈值 1 分）。"""
    over: list[dict[str, Any]] = []
    under: list[dict[str, Any]] = []
    aligned: list[dict[str, Any]] = []
    for r in rows:
        if r["human_score"] is None or r["judge_score"] is None:
            continue
        delta = r["judge_score"] - r["human_score"]
        item = {
            "case_id": r["case_id"],
            "category": r["category"],
            "human_score": r["human_score"],
            "judge_score": r["judge_score"],
            "delta": round(delta, 3),
            "rationale": r["annotation_rationale"],
        }
        if delta >= 1:
            over.append(item)
        elif delta <= -1:
            under.append(item)
        else:
            aligned.append(item)
    return {"overestimated": over, "underestimated": under, "aligned": aligned}


def _stability(repeat_scores: list[list[float | None]]) -> dict[str, Any]:
    """稳定性：同一案例多次评分的波动与一致性。

    注意：波动必须在**同一案例的重复评分之间**计算，而不是跨案例。
    这里给出每个案例的标准差，再汇总为平均逐案例标准差。
    """
    valid = [s for s in repeat_scores if len(s) > 1 and all(v is not None for v in s)]
    if not valid:
        return {
            "repeats": len(repeat_scores[0]) if repeat_scores and repeat_scores[0] else 0,
            "per_case_scores": repeat_scores,
            "per_case_std": [],
            "mean_within_case_std": None,
            "max_within_case_std": None,
            "exact_stable": None,
            "normalized_stable": None,
            "consistent": None,
            "note": "无有效重复评分（repeats<2、含 UNKNOWN 或未执行）",
        }

    per_case_std = [round(statistics.pstdev(s), 4) for s in valid]
    exact_stable = all(len(set(s)) == 1 for s in valid)
    norm_sets = [{_norm_label(v) for v in s} for s in valid]
    norm_stable = all(len(s) == 1 for s in norm_sets)

    return {
        "repeats": len(valid[0]),
        "per_case_scores": [[round(v, 3) if v is not None else None for v in s] for s in valid],
        "per_case_std": per_case_std,
        "mean_within_case_std": round(statistics.mean(per_case_std), 4),
        "max_within_case_std": round(max(per_case_std), 4),
        "exact_stable": exact_stable,
        "normalized_stable": norm_stable,
        "consistent": norm_stable,
        "note": (
            "per_case_std 为同一案例多次评分的总体标准差；"
            "mean_within_case_std 为其均值（越接近 0 越稳定）；"
            "normalized_stable 表示归一化到 1~5 整级后完全一致"
        ),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_calibration(
    judge: LLMJudge,
    dataset: CalibrationDataset | None = None,
    *,
    dataset_id: str | None = None,
    max_cases: int | None = None,
    repeats: int = 1,
) -> dict[str, Any]:
    """运行一次 Judge 校准。

    Args:
        judge: LLM Judge 实例（Mock 或 Real，由调用方保证授权）。
        dataset: 校准数据集；None 时按 dataset_id 加载。
        max_cases: 案例上限（1..50）。
        repeats: 每案例重复评分次数（1..5），用于稳定性分析。

    Returns:
        校准报告（含指标、混淆矩阵、差异列表、稳定性、UNKNOWN、版本记录、限制）。
    """
    ds = dataset or load_calibration_dataset(dataset_id)
    cases = ds.cases
    if max_cases is not None:
        if not (1 <= max_cases <= MAX_CASES_PER_RUN):
            raise CalibrationError(f"max_cases 必须在 1..{MAX_CASES_PER_RUN}")
        cases = cases[:max_cases]
    if not (1 <= repeats <= MAX_REPEATS):
        raise CalibrationError(f"repeats 必须在 1..{MAX_REPEATS}")

    # 校准是批量场景：显式把 Judge 调用预算提升到本次所需上限，
    # 否则会因单个 LLMJudge 实例的默认 20 次上限而大面积失败（已修复）。
    needed_calls = len(cases) * repeats
    if hasattr(judge, "set_call_limit"):
        judge.set_call_limit(max(needed_calls, 1))

    rows: list[dict[str, Any]] = []
    judge_unknown: list[dict[str, Any]] = []
    reference_unknown: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    all_results: list[Any] = []

    for case in cases:
        try:
            trace = case_to_trace(case)
            results = [judge.judge(trace, rubric_id=case.rubric_id) for _ in range(repeats)]
        except Exception as exc:
            errors.append({"case_id": case.case_id, "error": f"{type(exc).__name__}: {exc}"})
            continue

        first = results[0]
        all_results.append(first)
        scores = [r.overall for r in results]
        score = first.overall

        row = {
            "case_id": case.case_id,
            "category": case.category,
            "rubric_id": case.rubric_id,
            "human_score": case.human_score,
            "human_status": case.human_status,
            "judge_score": round(score, 3) if score is not None else None,
            "judge_score_normalized": _norm_label(score),
            "delta": (
                round(score - case.human_score, 3)
                if score is not None and case.human_score is not None else None
            ),
            "annotation_rationale": case.annotation_rationale,
            "annotation_source": case.annotation_source,
            "repeat_scores": [round(s, 3) if s is not None else None for s in scores],
            "unknown_dimensions": list(first.unknown_dimensions),
            "evidence": {
                s.dimension: s.evidence for s in first.scores if s.evidence
            },
            "dimension_scores": {
                s.dimension: s.score for s in first.scores
            },
            "limitations": list(first.limitations),
        }
        rows.append(row)

        if case.is_reference_unknown():
            reference_unknown.append({"case_id": case.case_id, "judge_score": row["judge_score"]})
        elif score is None:
            judge_unknown.append(
                {
                    "case_id": case.case_id,
                    "human_score": case.human_score,
                    "unknown_dimensions": row["unknown_dimensions"],
                }
            )

    agreement = _compute_agreement(rows)
    bias = _bias_analysis(rows)
    stability = _stability([r["repeat_scores"] for r in rows])

    # Provider 版本信息（取自首个成功结果，无结果时回退）
    first_res = all_results[0] if all_results else None
    provider_kind = getattr(judge, "provider_kind", "unknown")
    is_mock = getattr(judge, "is_mock", None)

    rubric_id = first_res.rubric_id if first_res else ds.rubric_id
    rubric_version = first_res.rubric_version if first_res else None
    try:
        rubric_version = rubric_version or load_rubric(rubric_id).version
    except Exception:
        pass

    return {
        "calibration_engine_version": ENGINE_VERSION,
        "dataset": {
            "calibration_id": ds.calibration_id,
            "version": ds.version,
            "rubric_id": ds.rubric_id,
            "annotation_source": ds.annotation_source,
            "annotation_note": ds.annotation_note,
            "total_cases": len(ds.cases),
            "evaluated_cases": len(rows),
        },
        "judge": {
            "provider_kind": provider_kind,
            "model_name": getattr(judge, "model_name", "unknown"),
            "is_mock": is_mock,
            "simulated": is_mock,
            "rubric_id": rubric_id,
            "rubric_version": rubric_version,
            "prompt_version": first_res.prompt_version if first_res else None,
            "judge_engine_version": first_res.judge_engine_version if first_res else None,
            "repeats": repeats,
        },
        "metrics": agreement,
        "bias": bias,
        "stability": stability,
        "differences": [
            {
                "case_id": r["case_id"],
                "category": r["category"],
                "human_score": r["human_score"],
                "judge_score": r["judge_score"],
                "judge_score_normalized": r["judge_score_normalized"],
                "delta": r["delta"],
                "unknown_dimensions": r["unknown_dimensions"],
                "rationale": r["annotation_rationale"],
            }
            for r in rows
        ],
        "cases": rows,
        "unknown": {
            "reference_unknown_cases": reference_unknown,
            "judge_unknown_cases": judge_unknown,
            "execution_errors": errors,
            "note": "参考标注为 UNKNOWN 的案例不进入一致性指标分母；Judge 给出 UNKNOWN 的案例按缺失处理，不臆造分值。",
        },
        "disclaimer": (
            "本校准结果仅在 provider_kind 标注的评审器上成立。"
            "Mock Judge 结果为规则化模拟，不得冒充真实模型校准结果；"
            "annotation_source=synthetic_reference 表示参考标注非独立人工双盲标注，"
            "因此一致率不得对外宣称等同于真实人工标注一致率。"
        ),
        "limitations": [
            f"参考标注来源：{ds.annotation_source}（非独立第三方人工标注）",
            f"校准案例数 {len(rows)}，样本量有限，指标存在抽样波动",
            "Judge 总分为 Rubric 加权平均，混淆矩阵使用四舍五入到 1~5 整级归一化",
            "未实现 Judge 与确定性验证器的分歧率交叉统计",
        ]
        + (
            ["Mock Judge：评分来自规则化模拟，不代表真实模型判断"]
            if is_mock else
            ["真实模型评审：受模型版本与采样温度影响，结果可能不可复现"]
        ),
    }
