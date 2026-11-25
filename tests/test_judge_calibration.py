"""Judge Calibration 测试（pytest，第 39 步）。全部 Mock，不触发付费 API。"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.judge_calibration import (  # noqa: E402
    CALIBRATION_DIR,
    MAX_REPEATS,
    CalibrationError,
    case_to_trace,
    list_calibration_datasets,
    load_calibration_dataset,
    run_calibration,
)
from backend.app.services.llm_judge import build_judge  # noqa: E402

REQUIRED_CATEGORIES = {
    "correct_answer",
    "partially_correct",
    "factual_error",
    "missing_information",
    "instruction_violation",
    "unreasonable_tool_use",
    "insufficient_evidence",
    "ambiguous_boundary",
}


# ---------- 1. 校准数据读取 ----------

def test_dataset_loadable():
    ds = load_calibration_dataset("calibration_set_v1")
    assert ds.calibration_id
    assert ds.version
    assert len(ds.cases) >= 20, "校准集至少 20 条"
    assert ds.annotation_source == "synthetic_reference"
    assert ds.annotation_note

    ids = [c.case_id for c in ds.cases]
    assert len(ids) == len(set(ids)), "case_id 必须唯一"

    for c in ds.cases:
        assert c.task
        assert c.annotation_rationale
        assert c.annotation_source in {"synthetic_reference", "human_double_blind"}
        assert c.rubric_id
        assert c.human_status in {"SCORED", "UNKNOWN"}
        if c.human_status == "SCORED":
            assert isinstance(c.human_score, int) and 1 <= c.human_score <= 5
        else:
            assert c.human_score is None


def test_dataset_covers_all_categories():
    ds = load_calibration_dataset()
    cats = {c.category for c in ds.cases}
    assert REQUIRED_CATEGORIES <= cats, f"缺少类别: {REQUIRED_CATEGORIES - cats}"


def test_dataset_listing():
    items = list_calibration_datasets()
    assert len(items) >= 1
    it = items[0]
    assert it["case_count"] >= 20
    assert it["annotation_source"] == "synthetic_reference"
    assert it["scored_cases"] + it["unknown_cases"] == it["case_count"]
    assert it["unknown_cases"] >= 1, "应包含证据不足（UNKNOWN）案例"


def test_dataset_path_traversal_rejected():
    with pytest.raises(CalibrationError):
        load_calibration_dataset("../../etc/passwd")
    with pytest.raises(CalibrationError):
        load_calibration_dataset("no_such_set")


def test_case_to_trace_no_fabrication():
    """案例→Trace 只映射真实材料，不编造工具调用。"""
    ds = load_calibration_dataset()
    no_tool = next(c for c in ds.cases if "tool_name" not in (c.reference_material or {}))
    tr = case_to_trace(no_tool)
    tool_steps = [s for s in tr.steps if s.step_type.value == "tool_call"]
    assert tool_steps == [], "案例未提供工具材料时不得生成工具步骤"

    with_tool = next(c for c in ds.cases if "tool_name" in (c.reference_material or {}))
    tr2 = case_to_trace(with_tool)
    tool_steps2 = [s for s in tr2.steps if s.step_type.value == "tool_call"]
    assert len(tool_steps2) == 1
    assert tool_steps2[0].tool_name == with_tool.reference_material["tool_name"]


# ---------- 2. 指标计算 ----------

def test_metrics_calculation():
    rep = run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=1)
    m = rep["metrics"]
    assert m["evaluable_cases"] > 0
    assert m["denominator_definition"]
    assert 0.0 <= m["exact_agreement"] <= 1.0
    assert m["mae"] >= 0.0
    assert 0.0 <= m["within_1_agreement"] <= 1.0
    assert m["within_1_agreement"] >= m["exact_agreement"], "Within-1 应不弱于 Exact"
    # 计数器与比例一致
    assert m["exact_agreement_count"] / m["evaluable_cases"] == pytest.approx(m["exact_agreement"], abs=1e-4)
    assert m["within_1_agreement_count"] / m["evaluable_cases"] == pytest.approx(m["within_1_agreement"], abs=1e-4)

    # 混淆矩阵：行列为 1..5，且非对角总和 = 评估案例数
    matrix = m["confusion_matrix"]
    assert set(matrix.keys()) == {"1", "2", "3", "4", "5"}
    total = sum(matrix[h][j] for h in matrix for j in matrix[h])
    assert total == m["evaluable_cases"]


def test_confusion_matrix_diagonal_matches_exact():
    rep = run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=1)
    m = rep["metrics"]
    diag = sum(m["confusion_matrix"][k][k] for k in "12345")
    assert diag == m["exact_agreement_count"]


def test_version_records():
    rep = run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=1)
    j = rep["judge"]
    assert j["provider_kind"] == "mock"
    assert j["is_mock"] is True
    assert j["model_name"]
    assert j["rubric_id"]
    assert j["rubric_version"]
    assert j["prompt_version"]
    assert j["judge_engine_version"]
    assert rep["calibration_engine_version"]
    assert rep["dataset"]["version"]
    assert rep["dataset"]["annotation_source"] == "synthetic_reference"


# ---------- 3. 评分差异 ----------

def test_bias_analysis():
    rep = run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=1)
    bias = rep["bias"]
    for key in ("overestimated", "underestimated", "aligned"):
        assert key in bias
    for item in bias["overestimated"]:
        assert item["delta"] >= 1
        assert item["judge_score"] > item["human_score"]
        assert item["rationale"]
    for item in bias["underestimated"]:
        assert item["delta"] <= -1
    for item in bias["aligned"]:
        assert abs(item["delta"]) < 1

    # 三分类之和 == 可评估案例数
    total = len(bias["overestimated"]) + len(bias["underestimated"]) + len(bias["aligned"])
    assert total == rep["metrics"]["evaluable_cases"]


def test_differences_list():
    rep = run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=1)
    diffs = rep["differences"]
    assert len(diffs) == rep["dataset"]["evaluated_cases"]
    for d in diffs:
        assert "case_id" in d
        assert "human_score" in d
        assert "judge_score" in d
        assert "rationale" in d


def test_case_evidence_auditable():
    rep = run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=1)
    for c in rep["cases"]:
        assert "dimension_scores" in c
        assert "evidence" in c
        assert isinstance(c["evidence"], dict)
    # 至少一个案例有引用证据
    assert any(c["evidence"] for c in rep["cases"])


# ---------- 4. UNKNOWN 处理 ----------

def test_unknown_handling():
    rep = run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=1)
    unk = rep["unknown"]
    # 校准集内应有参考标注为 UNKNOWN 的案例
    assert len(unk["reference_unknown_cases"]) >= 1
    unk_ids = {u["case_id"] for u in unk["reference_unknown_cases"]}
    # 参考 UNKNOWN 案例不得进入可评估分母
    evaluated_ids = {c["case_id"] for c in rep["cases"]}
    assert unk_ids <= evaluated_ids
    evaluable_ids = {
        c["case_id"] for c in rep["cases"] if c["human_score"] is not None and c["judge_score"] is not None
    }
    assert not (unk_ids & evaluable_ids), "参考 UNKNOWN 案例不得计入一致性分母"


# ---------- 5. 重复评分与稳定性 ----------

def test_stability_repeats():
    rep = run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=3, max_cases=5)
    s = rep["stability"]
    assert s["repeats"] == 3
    assert len(s["per_case_scores"]) == 5
    for scores in s["per_case_scores"]:
        assert len(scores) == 3


def test_mock_is_deterministic():
    """Mock Judge 为确定性评审：同案例重复评分完全相同（波动为 0）。"""
    rep = run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=3, max_cases=5)
    s = rep["stability"]
    assert s["mean_within_case_std"] == 0.0
    assert s["max_within_case_std"] == 0.0
    assert s["exact_stable"] is True
    assert s["consistent"] is True


def test_stability_with_unstable_provider():
    """注入一个不稳定的假 Provider，验证稳定性指标能真实反映波动。"""
    from backend.app.services.llm_judge import LLMJudge

    class FlakyProvider:
        kind = "deepseek"
        is_mock = False
        model_name = "flaky"
        _n = 0

        def score(self, rubric, judge_input, raw_block):
            FlakyProvider._n += 1
            val = 5 if FlakyProvider._n % 2 else 1
            return [
                {"dimension": d.key, "status": "SCORED", "score": val,
                 "evidence": ["synthetic"], "reason": "r"}
                for d in rubric.dimensions
            ], {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    rep = run_calibration(LLMJudge(FlakyProvider()), dataset_id="calibration_set_v1", repeats=2, max_cases=3)
    s = rep["stability"]
    assert s["mean_within_case_std"] > 0, "不稳定 Provider 的波动应 > 0"
    assert s["exact_stable"] is False
    assert s["consistent"] is False


def test_repeats_bounds():
    with pytest.raises(CalibrationError):
        run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=0)
    with pytest.raises(CalibrationError):
        run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=MAX_REPEATS + 1)


def test_max_cases_bounds():
    with pytest.raises(CalibrationError):
        run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", max_cases=0)
    with pytest.raises(CalibrationError):
        run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", max_cases=51)


def test_call_limit_raised_for_batch():
    """回归：批量校准不得因 Judge 默认 20 次调用上限而大面积失败。"""
    rep = run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=2)
    assert rep["unknown"]["execution_errors"] == [], "批量校准不应出现调用上限错误"
    assert rep["dataset"]["evaluated_cases"] == rep["dataset"]["total_cases"]


# ---------- 6. Mock / Real 隔离 ----------

def test_mock_real_isolation():
    rep = run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=1)
    assert rep["judge"]["is_mock"] is True
    assert rep["judge"]["simulated"] is True
    assert rep["judge"]["provider_kind"] == "mock"
    assert any("Mock Judge" in x for x in rep["limitations"])
    assert "synthetic_reference" in rep["disclaimer"] or any(
        "synthetic_reference" in x for x in rep["limitations"]
    )
    # 免责声明必须显式禁止冒充真实模型校准
    assert "不得冒充真实模型校准结果" in rep["disclaimer"]


def test_real_judge_not_constructible_without_authorization(monkeypatch):
    from backend.app.services.llm_judge import RealJudgeDisabledError

    monkeypatch.delenv("TRACEPILOT_JUDGE_ALLOW_REAL", raising=False)
    with pytest.raises(RealJudgeDisabledError):
        build_judge("deepseek")


# ---------- 7. 安全 ----------

def test_no_sensitive_leak():
    import json

    rep = run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=1)
    text = json.dumps(rep, ensure_ascii=False, default=str)
    assert "DEEPSEEK_API_KEY=" not in text
    assert "sk-proj-" not in text


def test_no_paid_api():
    rep = run_calibration(build_judge("mock"), dataset_id="calibration_set_v1", repeats=1)
    assert rep["judge"]["is_mock"] is True
    # Mock 不产生 token 消耗
    assert all(
        (c.get("repeat_scores") is not None) for c in rep["cases"]
    )


# ---------- 8. 数据集文件位置 ----------

def test_calibration_dir_location():
    assert CALIBRATION_DIR.exists()
    files = list(CALIBRATION_DIR.glob("*.json"))
    assert len(files) >= 1
    assert files[0].parent == CALIBRATION_DIR

def test_dataset_id_both_identifiers():
    """回归：文件名 stem 与 JSON 内部 calibration_id 都必须能解析（前端用后者）。"""
    by_stem = load_calibration_dataset("calibration_set_v1")
    by_calib_id = load_calibration_dataset("judge-calibration-v1")
    assert by_stem.calibration_id == by_calib_id.calibration_id
    assert by_stem.version == by_calib_id.version
    with pytest.raises(CalibrationError):
        load_calibration_dataset("not-a-dataset")
