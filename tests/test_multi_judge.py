"""Multi-Judge Arbitration 测试（pytest，第 40 步）。全部 Mock，不触发付费 API。"""

import statistics
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.evaluation_engine import execute_case, load_dataset  # noqa: E402
from backend.app.services.multi_judge import (  # noqa: E402
    DEFAULT_CONFIG_SETS,
    MAX_JUDGES,
    MissingCalibrationError,
    MultiJudgeError,
    JudgeSpec,
    arbitrate,
    assess_independence,
    build_judges,
    detect_disagreement,
    run_multi_judge,
)
from backend.app.services.llm_judge import build_judge  # noqa: E402


def _traces(n: int = 3):
    ds = load_dataset()
    return [(c["case_id"], execute_case(c)) for c in ds["cases"][:n]]


# ---------- 1. 多 Judge 配置加载 ----------

def test_config_sets_available():
    assert len(DEFAULT_CONFIG_SETS) >= 3
    for cfg in DEFAULT_CONFIG_SETS.values():
        assert cfg.config_id
        assert 1 <= len(cfg.judges) <= MAX_JUDGES
        assert cfg.limitation, "每个配置组必须声明限制"
        ids = [j.judge_id for j in cfg.judges]
        assert len(ids) == len(set(ids))


def test_build_judges():
    cfg = DEFAULT_CONFIG_SETS["mock-mixed-variants"]
    pairs = build_judges(cfg.judges)
    assert len(pairs) == 3
    for spec, judge in pairs:
        assert judge.is_mock is True
        assert spec.judge_id


def test_build_judges_validation():
    with pytest.raises(MultiJudgeError):
        build_judges([])
    with pytest.raises(MultiJudgeError):
        build_judges([JudgeSpec(judge_id=f"j{i}") for i in range(MAX_JUDGES + 1)])
    with pytest.raises(MultiJudgeError):
        build_judges([JudgeSpec(judge_id="dup"), JudgeSpec(judge_id="dup")])
    with pytest.raises(MultiJudgeError):
        build_judges([JudgeSpec(judge_id="bad", mode="gpt")])
    with pytest.raises(Exception):  # mock variant 非法
        build_judges([JudgeSpec(judge_id="bad", variant="weird")])


def test_mock_variants_are_actually_different():
    """三个 Mock 变体必须产生不同分数，否则就是"同一规则复制三份"。"""
    trace = _traces(1)[0][1]
    scores = {}
    for variant in ("base", "strict", "lenient"):
        j = build_judge("mock", variant=variant)
        scores[variant] = j.judge(trace).overall
    assert len(set(scores.values())) == 3, f"变体分数应有差异，实际 {scores}"
    assert scores["strict"] < scores["base"] < scores["lenient"]


def test_variant_prompt_version_recorded():
    for variant in ("base", "strict", "lenient"):
        j = build_judge("mock", variant=variant)
        r = j.judge(_traces(1)[0][1])
        assert r.prompt_version
        if variant != "base":
            assert variant in r.prompt_version


# ---------- 2. 独立评分结果保存 ----------

def test_independent_scores_recorded():
    rep = run_multi_judge(_traces(2), DEFAULT_CONFIG_SETS["mock-mixed-variants"].judges)
    assert rep["case_count"] == 2
    for c in rep["cases"]:
        assert len(c["judges"]) == 3
        for jid, v in c["judges"].items():
            assert "status" in v
            assert "score" in v
            assert "evidence" in v
            assert "reason" in v
            assert "dimension_scores" in v
            assert "model_name" in v
            assert "prompt_version" in v
            assert "rubric_version" in v


def test_judge_metadata_recorded():
    rep = run_multi_judge(_traces(1), DEFAULT_CONFIG_SETS["mock-mixed-variants"].judges)
    assert len(rep["judges"]) == 3
    for j in rep["judges"]:
        assert j["judge_id"]
        assert j["model_name"]
        assert j["prompt_version"]
        assert j["provider_kind"] == "mock"
        assert j["is_mock"] is True
    assert rep["rubric"]["rubric_id"]
    assert rep["rubric"]["version"]
    assert rep["multi_judge_engine_version"]


# ---------- 3. 独立性判定（核心诚实性） ----------

def test_independence_mock_variants_not_independent():
    """同一 Mock 规则的不同变体不得被判定为独立模型。"""
    rep = run_multi_judge(_traces(1), DEFAULT_CONFIG_SETS["mock-mixed-variants"].judges)
    ind = rep["independence"]
    assert ind["level"] == "mock_prompt_variants"
    assert ind["level"] != "independent_models", "Mock 变体不得冒充独立模型"
    assert ind["not_independent"] is True
    assert ind["all_mock"] is True
    assert ind["distinct_model_count"] == 1
    assert ind["distinct_prompt_count"] == 3
    assert "不构成独立真实模型评测" in ind["note"]
    assert "不得作为真实模型一致性证据" in ind["disclaimer"]


def test_independence_resampling_correlated():
    rep = run_multi_judge(_traces(1), DEFAULT_CONFIG_SETS["mock-resampling"].judges)
    ind = rep["independence"]
    assert ind["level"] == "correlated"
    assert ind["not_independent"] is True
    assert ind["distinct_model_count"] == 1
    assert ind["distinct_prompt_count"] == 1


def test_independence_levels_via_stub():
    """用桩数据验证独立模型级别可被正确识别（真实模式）。"""
    stub = {
        "a": {"provider_kind": "deepseek", "model_name": "m1", "prompt_version": "p1", "is_mock": False},
        "b": {"provider_kind": "deepseek", "model_name": "m2", "prompt_version": "p1", "is_mock": False},
    }
    ind = assess_independence(stub)
    assert ind["level"] == "independent_models"
    assert ind["not_independent"] is False
    assert len(ind["real_model_judges"]) == 2


# ---------- 4/5/6. 分歧检测与聚合 ----------

def _stub_case(case_id: str, scores: dict[str, float | None]) -> dict:
    judges = {}
    for jid, s in scores.items():
        judges[jid] = {
            "status": "SCORED" if s is not None else "UNKNOWN",
            "score": s,
            "normalized": None if s is None else max(1, min(5, round(s))),
            "evidence": {"task_relevance": [f"{jid}-evidence"]},
            "reason": f"{jid} reason",
        }
    return {"case_id": case_id, "trace_id": f"t-{case_id}", "judges": judges}


def test_max_diff_and_variance():
    d = detect_disagreement([_stub_case("c1", {"a": 5, "b": 3, "c": 4})], threshold=2, severe_threshold=3)
    row = d["cases"][0]
    assert row["max_diff"] == 2
    assert row["stdev"] == pytest.approx(round(statistics.pstdev([5, 3, 4]), 4))
    assert row["variance"] == pytest.approx(round(statistics.pvariance([5, 3, 4]), 4))
    assert row["mean"] == pytest.approx(4.0)
    assert row["median"] == 4
    assert row["disagreement_flagged"] is True   # max_diff >= 2
    assert row["severe"] is False                # < 3


def test_severe_disagreement():
    d = detect_disagreement([_stub_case("c1", {"a": 5, "b": 1})], threshold=2, severe_threshold=3)
    row = d["cases"][0]
    assert row["max_diff"] == 4
    assert row["severe"] is True
    assert row["human_review_required"] is True


def test_distribution_and_agreement():
    d = detect_disagreement(
        [_stub_case("c1", {"a": 4, "b": 4, "c": 5})], threshold=2, severe_threshold=3
    )
    row = d["cases"][0]
    assert row["distribution"]["4"] == 2
    assert row["distribution"]["5"] == 1
    assert row["agreement"] == pytest.approx(0.75)  # 1 - 1/4


def test_unknown_not_treated_as_zero():
    """缺失评分不能默认为 0。"""
    d = detect_disagreement(
        [_stub_case("c1", {"a": 4, "b": None, "c": 4})], threshold=2, severe_threshold=3
    )
    row = d["cases"][0]
    assert row["scored_count"] == 2
    assert row["unknown_judges"] == ["b"]
    assert row["max_diff"] == 0.0, "UNKNOWN 不得参与计算"
    assert row["mean"] == pytest.approx(4.0)
    # 分布里不得出现 0 分档的计数
    assert row["distribution"].get("0") is None


def test_evidence_conflicts_detected():
    d = detect_disagreement([_stub_case("c1", {"a": 4, "b": 2})], threshold=2, severe_threshold=3)
    row = d["cases"][0]
    assert row["evidence_conflicts"], "证据完全不重叠时应记录冲突"
    assert row["evidence_conflicts"][0]["dimension"] == "task_relevance"
    assert row["reason_summary"]["a"]


def test_median_aggregation():
    per_case = [_stub_case("c1", {"a": 5, "b": 3, "c": 4})]
    a = arbitrate(per_case, strategy="median")
    assert a["cases"][0]["aggregated_score"] == 4
    assert a["cases"][0]["status"] == "SCORED"


def test_mean_aggregation():
    per_case = [_stub_case("c1", {"a": 5, "b": 3, "c": 4})]
    a = arbitrate(per_case, strategy="mean")
    assert a["cases"][0]["aggregated_score"] == pytest.approx(4.0)


def test_severe_returns_inconclusive():
    per_case = [_stub_case("c1", {"a": 5, "b": 1, "c": 3})]
    a = arbitrate(per_case, strategy="median", severe_threshold=3)
    row = a["cases"][0]
    assert row["status"] == "INCONCLUSIVE"
    assert row["aggregated_score"] is None, "严重分歧不得输出确定性聚合值"
    assert "严重分歧" in row["reason"]
    assert row["human_review_required"] is True
    assert a["summary"]["inconclusive_count"] == 1
    assert a["summary"]["conclusive_count"] == 0


def test_insufficient_scores_inconclusive():
    per_case = [_stub_case("c1", {"a": 4, "b": None, "c": None})]
    a = arbitrate(per_case, strategy="mean")
    row = a["cases"][0]
    assert row["status"] == "INCONCLUSIVE"
    assert row["aggregated_score"] is None
    assert "有效评分不足" in row["reason"]


def test_calibration_weighted_requires_calibration():
    per_case = [_stub_case("c1", {"a": 5, "b": 3})]
    with pytest.raises(MissingCalibrationError):
        arbitrate(per_case, strategy="calibration_weighted")
    with pytest.raises(MissingCalibrationError):
        arbitrate(per_case, strategy="calibration_weighted", calibration={"weights": {}})
    with pytest.raises(MissingCalibrationError):
        arbitrate(
            per_case, strategy="calibration_weighted",
            calibration={"weights": {"a": 1.0}},  # 缺 source
        )


def test_calibration_weighted_computation():
    per_case = [_stub_case("c1", {"a": 5, "b": 3})]
    a = arbitrate(
        per_case, strategy="calibration_weighted",
        calibration={"source": "calibration_set_v1", "evidence": "mock calib", "weights": {"a": 3.0, "b": 1.0}},
    )
    expected = (5 * 3.0 + 3 * 1.0) / 4.0
    assert a["cases"][0]["aggregated_score"] == pytest.approx(round(expected, 4))
    assert a["calibration"]["source"] == "calibration_set_v1"
    assert a["calibration"]["weights"] == {"a": 3.0, "b": 1.0}


def test_invalid_strategy():
    with pytest.raises(MultiJudgeError):
        arbitrate([_stub_case("c1", {"a": 4, "b": 4})], strategy="max")


# ---------- 7. 端到端运行 ----------

def test_run_multi_judge_end_to_end():
    rep = run_multi_judge(_traces(3), DEFAULT_CONFIG_SETS["mock-mixed-variants"].judges)
    assert rep["case_count"] == 3
    assert len(rep["cases"]) == 3
    assert rep["disagreement"]["case_count"] == 3
    assert rep["arbitration"]["cases"]
    assert rep["errors"] == []
    assert rep["limitations"]
    assert rep["paid_api_called"] is False
    # 汇总口径明确
    s = rep["arbitration"]["summary"]
    assert s["conclusive_count"] + s["inconclusive_count"] == 3
    assert s["denominator_definition"]


def test_run_multi_judge_resampling_zero_variance():
    rep = run_multi_judge(_traces(2), DEFAULT_CONFIG_SETS["mock-resampling"].judges)
    for row in rep["disagreement"]["cases"]:
        assert row["max_diff"] == 0.0
        assert row["stdev"] == 0.0
    assert rep["independence"]["level"] == "correlated"


def test_run_multi_judge_bounds():
    with pytest.raises(MultiJudgeError):
        run_multi_judge([], DEFAULT_CONFIG_SETS["mock-dual"].judges)


def test_judge_error_isolation(monkeypatch):
    """单 Judge 失败不影响其他 Judge 的结果记录，且不产生虚假评分。"""
    import backend.app.services.multi_judge as mj
    from backend.app.services.llm_judge import LLMJudge, MockJudgeProvider

    class BrokeProvider:
        kind = "deepseek"
        is_mock = False
        model_name = "broken"
        prompt_version = "p-broken"

        def score(self, rubric, judge_input, raw_block):
            raise RuntimeError("judge down")

    specs = [JudgeSpec(judge_id="ok", mode="mock"), JudgeSpec(judge_id="bad", mode="mock")]
    real_build = mj.build_judges

    def fake_build(s):
        pairs = real_build(s)
        # 把第二个 judge 的 provider 替换为必然失败的 provider
        pairs[1] = (pairs[1][0], LLMJudge(BrokeProvider()))
        return pairs

    monkeypatch.setattr(mj, "build_judges", fake_build)

    rep = mj.run_multi_judge(_traces(2), specs, strategy="median")
    # 失败被记录在 errors 中
    assert len(rep["errors"]) == 2
    for e in rep["errors"]:
        assert e["judge_id"] == "bad"
        assert "judge down" in e["error"]
    # 失败的 judge 标记为 ERROR 且无分数
    for c in rep["cases"]:
        assert c["judges"]["ok"]["status"] == "SCORED"
        assert c["judges"]["bad"]["status"] == "ERROR"
        assert c["judges"]["bad"]["score"] is None, "失败不得伪造分数"
    # 仲裁只使用有效评分，且因有效分不足返回 INCONCLUSIVE
    for row in rep["arbitration"]["cases"]:
        assert row["aggregated_score"] is None
        assert row["status"] == "INCONCLUSIVE"
        assert "bad" in row["unknown_judges"]


# ---------- 8. Mock / Real 区分 ----------

def test_real_judge_requires_authorization(monkeypatch):
    from backend.app.services.llm_judge import RealJudgeDisabledError

    monkeypatch.delenv("TRACEPILOT_JUDGE_ALLOW_REAL", raising=False)
    with pytest.raises(RealJudgeDisabledError):
        build_judges([JudgeSpec(judge_id="real", mode="deepseek")])


def test_all_mock_flagged():
    rep = run_multi_judge(_traces(1), DEFAULT_CONFIG_SETS["mock-dual"].judges)
    assert rep["independence"]["all_mock"] is True
    assert rep["paid_api_called"] is False


# ---------- 9. 安全 ----------

def test_no_sensitive_leak():
    import json

    rep = run_multi_judge(_traces(1), DEFAULT_CONFIG_SETS["mock-dual"].judges)
    text = json.dumps(rep, ensure_ascii=False, default=str)
    assert "DEEPSEEK_API_KEY=" not in text
    assert "sk-proj-" not in text
