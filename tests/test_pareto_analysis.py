"""第 54 步：Cost–Latency–Quality Pareto Analysis 测试。

覆盖需求第九部分 20 项：
1 明确支配 / 2 非支配 / 3 多候选 / 4 完全相同指标（并列）/ 5 单一候选 /
6-8 缺失成本/延迟/质量 / 9 数据集不同 / 10 币种不同 / 11 价格版本不同 /
12 模拟与真实数据混用 / 13 无可行候选 / 14 约束筛选 / 15 安全门禁筛选 /
16 Pareto Frontier 正确 / 17 API 正常 / 18 前端构建（独立执行）/ 19 后端测试 /
20 不调用付费 API。

诚实性约束：缺失指标为 null（不按 0）；混合真实/模拟数据 → NOT_COMPARABLE；
不得把示例数据冒充真实 Agent 评测结果（候选全部来自真实评测运行或测试构造）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.services import pareto_analysis as pa  # noqa: E402
from backend.app.services.evaluation_store import get_evaluation_store  # noqa: E402

client = TestClient(app)


# ---------------------------------------------------------------------------
# 构造工具（纯逻辑测试，不依赖评测执行）
# ---------------------------------------------------------------------------

def _cand(
    candidate_id: str,
    *,
    quality: float | None = 0.8,
    latency: float | None = 100.0,
    cost: float | None = 0.01,
    **overrides,
) -> dict:
    candidate = {
        "candidate_id": candidate_id,
        "agent_version": None,
        "agent_version_note": "测试候选：无独立 agent_version",
        "evaluation_run_id": f"run-{candidate_id}",
        "evaluation_created_at": "2026-11-19T00:00:00+00:00",
        "evaluation_elapsed_ms": 10,
        "dataset_id": "tracepilot-eval-v1",
        "dataset_version": "1.0.0",
        "evaluator_version": "1.0.0",
        "judge_mode": "none",
        "rubric_id": None,
        "rubric_version": None,
        "task_success_rate": quality,
        "average_latency_ms": latency,
        "average_cost": cost,
        "total_cost": None,
        "currency": "CNY" if cost is not None else None,
        "cost_status": "SIMULATED" if cost is not None else "PRICE_UNKNOWN",
        "pricing_versions": ["v-test"] if cost is not None else [],
        "pricing_sources": ["TEST"] if cost is not None else [],
        "sample_size": 8,
        "data_source": "simulated",
        "execution_mode": "controlled_simulation",
        "paid_api_called": False,
        "safety_pass_rate": 1.0,
        "safety_gate_status": "PASS",
        "gate_report_id": None,
        "missing_fields": [
            name
            for name, value in (
                ("task_success_rate", quality),
                ("average_latency_ms", latency),
                ("average_cost", cost),
            )
            if value is None
        ],
        "metrics_complete": quality is not None and latency is not None and cost is not None,
        "trace_ids": [f"trace-{candidate_id}"],
        "trace_count": 1,
        "comparability_status": "COMPARABLE",
        "comparability_issues": [],
        "in_comparison_group": True,
        "exclusion_reasons": [],
        "eligible": False,
        "dominated_by": [],
        "dominates": [],
        "dominance": "EXCLUDED",
        "tie_group": None,
    }
    candidate.update(overrides)
    if "dataset_group_key" not in overrides:
        candidate["dataset_group_key"] = (
            f"{candidate.get('dataset_id')}@{candidate.get('dataset_version')}"
        )
    if "missing_fields" not in overrides:
        candidate["missing_fields"] = [
            name
            for name, value in (
                ("task_success_rate", candidate["task_success_rate"]),
                ("average_latency_ms", candidate["average_latency_ms"]),
                ("average_cost", candidate["average_cost"]),
            )
            if value is None
        ]
    if "metrics_complete" not in overrides:
        candidate["metrics_complete"] = not candidate["missing_fields"]
    return candidate


def _run_pareto(candidates: list[dict], constraints: dict | None = None) -> dict:
    normalized = pa._normalize_constraints(constraints)
    pa._apply_constraints(candidates, normalized)
    return pa._compute_pareto(candidates, constraints=normalized)


def _frontier_ids(pareto: dict) -> list[str]:
    return [c["candidate_id"] for c in pareto["frontier"]]


# ---------------------------------------------------------------------------
# 1-5：支配 / 非支配 / 多候选 / 并列 / 单一候选
# ---------------------------------------------------------------------------

def test_clear_dominance_relation():
    a = _cand("a", quality=0.9, latency=100.0, cost=0.01)
    b = _cand("b", quality=0.8, latency=200.0, cost=0.02)
    result = _run_pareto([a, b])
    assert _frontier_ids(result) == ["a"]
    assert result["dominated_count"] == 1
    assert b["dominance"] == "DOMINATED"
    assert b["dominated_by"] == ["a"]
    assert a["dominance"] == "NON_DOMINATED"
    relation = result["dominance_relations"][0]
    assert relation["dominator_id"] == "a"
    assert relation["dominated_id"] == "b"
    assert set(relation["strict_axes"]) == {"quality", "latency", "cost"}


def test_non_dominated_tradeoff():
    # 质量更高但延迟/成本更差 → 互不支配
    a = _cand("a", quality=0.9, latency=300.0, cost=0.03)
    b = _cand("b", quality=0.7, latency=100.0, cost=0.01)
    result = _run_pareto([a, b])
    assert set(_frontier_ids(result)) == {"a", "b"}
    assert result["dominated_count"] == 0
    assert result["dominance_relations"] == []


def test_multiple_candidates_with_mixed_frontier():
    candidates = [
        _cand("high-q", quality=0.95, latency=400.0, cost=0.04),
        _cand("fast", quality=0.90, latency=100.0, cost=0.02),
        _cand("cheap", quality=0.85, latency=200.0, cost=0.005),
        _cand("dominated", quality=0.80, latency=500.0, cost=0.05),
        _cand("also-dominated", quality=0.70, latency=300.0, cost=0.02),
    ]
    result = _run_pareto(candidates)
    frontier = set(_frontier_ids(result))
    assert frontier == {"high-q", "fast", "cheap"}
    assert result["dominated_count"] == 2
    assert "dominated" in [c["candidate_id"] for c in result["dominated"]]


def test_identical_metrics_are_tied_not_dominating():
    a = _cand("same-a")
    b = _cand("same-b")
    result = _run_pareto([a, b])
    assert set(_frontier_ids(result)) == {"same-a", "same-b"}
    assert result["dominance_relations"] == []
    assert len(result["tie_groups"]) == 1
    tie = result["tie_groups"][0]
    assert set(tie["candidate_ids"]) == {"same-a", "same-b"}
    assert a["tie_group"] == b["tie_group"] == tie["tie_group"]


def test_single_candidate_frontier():
    solo = _cand("only")
    result = _run_pareto([solo])
    assert _frontier_ids(result) == ["only"]
    assert result["dominated_count"] == 0
    assert solo["dominance"] == "NON_DOMINATED"


# ---------------------------------------------------------------------------
# 6-8：缺失成本 / 延迟 / 质量
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field,override",
    [
        ("average_cost", {"average_cost": None, "currency": None}),
        ("average_latency_ms", {"average_latency_ms": None}),
        ("task_success_rate", {"task_success_rate": None}),
    ],
)
def test_missing_metric_excluded_not_zero(field, override):
    good = _cand("good")
    incomplete = _cand("incomplete", **override)
    assert field in incomplete["missing_fields"]

    normalized = pa._normalize_constraints(None)
    pa._apply_constraints([good, incomplete], normalized)
    assert incomplete["eligible"] is False
    assert any("MISSING_METRICS" in reason for reason in incomplete["exclusion_reasons"])

    result = pa._compute_pareto([good, incomplete], constraints=normalized)
    assert _frontier_ids(result) == ["good"]
    assert incomplete["dominance"] == "EXCLUDED"
    # 缺失值不能被当成 0：incomplete 不出现于前沿，也不被标记为最大优势
    assert incomplete["average_cost"] is None or incomplete["average_latency_ms"] is None \
        or incomplete["task_success_rate"] is None


# ---------------------------------------------------------------------------
# 9-12：可比性（数据集 / 币种 / 价格版本 / 真实与模拟混用）
# ---------------------------------------------------------------------------

def test_dataset_mismatch_marks_not_comparable():
    a = _cand("a")
    b = _cand("b", dataset_id="tracepilot-fault-v1", dataset_version="2.0.0")
    comparability = pa._assess_comparability(
        [a, b], dataset_version=None, min_sample_size=3
    )
    # 跨数据集候选被排除出比较组：组内仍可比，但整体给出明确警告
    assert comparability["verdict"] == "COMPARABLE_WITH_WARNINGS"
    assert any(
        issue["code"] in {"DATASET_ID_MISMATCH", "DATASET_VERSION_MISMATCH"}
        for issue in comparability["issues"]
    )
    assert b["comparability_status"] == "NOT_COMPARABLE"
    assert a["in_comparison_group"] is True
    assert b["in_comparison_group"] is False
    # 前沿仅使用同数据集组，跨数据集候选不进入
    normalized = pa._normalize_constraints(None)
    pa._apply_constraints([a, b], normalized)
    result = pa._compute_pareto([a, b], constraints=normalized)
    assert "b" not in _frontier_ids(result)


def test_currency_mismatch_blocks_comparison():
    a = _cand("a", cost=0.01, currency="CNY")
    b = _cand("b", cost=0.02, currency="USD")
    comparability = pa._assess_comparability(
        [a, b], dataset_version=None, min_sample_size=3
    )
    assert comparability["verdict"] == "NOT_COMPARABLE"
    assert any(issue["code"] == "CURRENCY_MISMATCH" for issue in comparability["issues"])


def test_pricing_version_mismatch_is_warning():
    a = _cand("a", pricing_versions=["v1"])
    b = _cand("b", quality=0.7, latency=50.0, cost=0.005, pricing_versions=["v2"])
    comparability = pa._assess_comparability(
        [a, b], dataset_version=None, min_sample_size=3
    )
    assert comparability["verdict"] == "COMPARABLE_WITH_WARNINGS"
    assert any(
        issue["code"] == "PRICING_VERSION_MISMATCH" for issue in comparability["issues"]
    )


def test_mixed_real_and_simulated_is_not_comparable():
    simulated = _cand("sim")
    real = _cand("real", data_source="real_api", execution_mode="real_api")

    comparability = pa._assess_comparability(
        [simulated, real], dataset_version=None, min_sample_size=3
    )
    assert comparability["verdict"] == "NOT_COMPARABLE"
    assert comparability["mixed_real_and_simulated"] is True
    assert any(
        issue["code"] == "MIXED_REAL_AND_SIMULATED" for issue in comparability["critical_issues"]
    )
    for candidate in (simulated, real):
        assert candidate["comparability_status"] == "NOT_COMPARABLE"

    normalized = pa._normalize_constraints(None)
    pa._apply_constraints([simulated, real], normalized)
    result = pa._compute_pareto([simulated, real], constraints=normalized)
    assert result["pareto_frontier_count"] == 0
    status, message = pa._analysis_status(
        [simulated, real],
        comparable_eligible=0,
        feasible=0,
        constraints=normalized,
    )
    assert status == "NO_COMPARABLE_CANDIDATE"
    assert "不得解释为真实模型性能比较" in " ".join(
        [message, *[w for w in comparability["notes"]]]
    )


# ---------------------------------------------------------------------------
# 13-15：无可行候选 / 约束筛选 / 安全门禁
# ---------------------------------------------------------------------------

def test_no_feasible_candidate_when_constraints_too_strict():
    candidates = [_cand("a"), _cand("b", quality=0.6)]
    normalized = pa._normalize_constraints({"minimum_quality": 0.99})
    pa._apply_constraints(candidates, normalized)
    result = pa._compute_pareto(candidates, constraints=normalized)
    assert result["pareto_frontier_count"] == 0
    status, message = pa._analysis_status(
        candidates, comparable_eligible=2, feasible=0, constraints=normalized
    )
    assert status == "NO_FEASIBLE_CANDIDATE"
    assert "没有满足当前约束" in message


def test_constraints_filter_candidates():
    a = _cand("a", quality=0.95, latency=100.0, cost=0.01)
    b = _cand("b", quality=0.80, latency=500.0, cost=0.50)
    c = _cand("c", quality=0.70, latency=200.0, cost=0.02)
    candidates = [a, b, c]

    pa._apply_constraints(candidates, pa._normalize_constraints({"minimum_quality": 0.75}))
    assert a["eligible"] and b["eligible"] and not c["eligible"]

    pa._apply_constraints(candidates, pa._normalize_constraints({"maximum_latency_ms": 150}))
    assert a["eligible"] and not b["eligible"] and not c["eligible"]

    pa._apply_constraints(candidates, pa._normalize_constraints({"maximum_cost": 0.05}))
    assert a["eligible"] and c["eligible"] and not b["eligible"]


def test_safety_gate_filter():
    passing = _cand("passing", safety_gate_status="PASS")
    warn = _cand("warn", quality=0.85, latency=90.0, cost=0.009, safety_gate_status="WARN")
    unknown = _cand("unknown", quality=0.84, latency=80.0, cost=0.008, safety_gate_status=None)
    candidates = [passing, warn, unknown]

    pa._apply_constraints(
        candidates, pa._normalize_constraints({"required_safety_gate": "PASS"})
    )
    assert passing["eligible"] is True
    assert warn["eligible"] is False
    assert unknown["eligible"] is False
    assert any(
        "SAFETY_GATE_NOT_SATISFIED" in reason for reason in warn["exclusion_reasons"]
    )

    pa._apply_constraints(
        candidates, pa._normalize_constraints({"required_safety_gate": "WARN"})
    )
    assert passing["eligible"] and warn["eligible"] and not unknown["eligible"]


def test_invalid_constraints_rejected():
    with pytest.raises(pa.ParetoValidationError):
        pa._normalize_constraints({"minimum_quality": 1.5})
    with pytest.raises(pa.ParetoValidationError):
        pa._normalize_constraints({"maximum_latency_ms": -1})
    with pytest.raises(pa.ParetoValidationError):
        pa._normalize_constraints({"required_safety_gate": "MAYBE"})
    with pytest.raises(pa.ParetoValidationError):
        pa._normalize_constraints({"unknown_field": 1})


def test_filter_view_switches_dataset_group_recomputes_comparability():
    """切换到另一数据集组时，不能沿用原分析里「组外不可比」状态。"""
    eval_candidate = _cand("eval-cand")
    fault_candidate = _cand(
        "fault-cand",
        quality=0.0,
        latency=5.0,
        cost=0.0,
        dataset_id="tracepilot-fault-v1",
        dataset_version="1.0.0",
    )
    # 模拟原始分析（选择 eval 组）时 fault 候选被标为组外不可比
    fault_candidate["in_comparison_group"] = False
    fault_candidate["comparability_status"] = "NOT_COMPARABLE"
    report = {
        "analysis_id": "pareto-test-switch",
        "generated_at": "2026-11-19T00:00:00+00:00",
        "status": "OK",
        "candidates": [eval_candidate, fault_candidate],
        "comparability": {
            "minimum_sample_size": 3,
            "verdict": "COMPARABLE_WITH_WARNINGS",
        },
        "metric_definitions": pa.METRIC_DEFINITIONS,
        "objective": {"quality": "max", "latency": "min", "cost": "min"},
        "data_source_summary": {"simulated": 2},
        "warnings": [],
        "limitations": [],
    }
    view = pa._filtered_view(
        report,
        pa._normalize_constraints(
            {"dataset_version": "tracepilot-fault-v1", "minimum_quality": 1}
        ),
    )
    assert view["status"] == "NO_FEASIBLE_CANDIDATE"
    switched = next(c for c in view["candidates"] if c["candidate_id"] == "fault-cand")
    assert switched["in_comparison_group"] is True
    assert switched["comparability_status"] in {"COMPARABLE", "COMPARABLE_WITH_WARNINGS"}
    outside = next(c for c in view["candidates"] if c["candidate_id"] == "eval-cand")
    assert outside["in_comparison_group"] is False


# ---------------------------------------------------------------------------
# 16：Pareto Frontier 正确性（已知构造集）
# ---------------------------------------------------------------------------

def test_pareto_frontier_correctness():
    candidates = [
        _cand("A", quality=0.90, latency=100.0, cost=0.02),
        _cand("B", quality=0.90, latency=120.0, cost=0.015),
        _cand("C", quality=0.95, latency=200.0, cost=0.03),
        _cand("D", quality=0.85, latency=150.0, cost=0.025),  # 被 A 支配
        _cand("E", quality=0.90, latency=120.0, cost=0.02),  # 被 A、B 支配
        _cand("F", quality=0.95, latency=200.0, cost=0.03),  # 与 C 并列
    ]
    result = _run_pareto(candidates)
    assert set(_frontier_ids(result)) == {"A", "B", "C", "F"}
    assert {c["candidate_id"] for c in result["dominated"]} == {"D", "E"}
    # C 与 F 完全相同 → 同一 tie_group；A、B 不同 → 不并列
    tie = result["tie_groups"]
    assert len(tie) == 1
    assert set(tie[0]["candidate_ids"]) == {"C", "F"}
    # 排名按质量降序、延迟升序、成本升序
    assert [c["candidate_id"] for c in result["frontier"]] == ["C", "F", "A", "B"]
    assert [c["frontier_rank"] for c in result["frontier"]] == [1, 2, 3, 4]
    # 被支配关系覆盖
    assert "D" in candidates[0]["dominates"]
    assert set(candidates[4]["dominated_by"]) == {"A", "B"}


# ---------------------------------------------------------------------------
# 17 + 20：API（真实评测运行 → 分析 → 查询 → 关联资源）
# ---------------------------------------------------------------------------

def _wait_run(run_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/evaluation/runs/{run_id}")
        assert response.status_code == 200, response.text
        record = response.json()
        if record.get("status") in {"succeeded", "failed"}:
            return record
        time.sleep(0.1)
    raise AssertionError(f"评测运行未在 {timeout}s 内完成: {run_id}")


def test_api_pareto_end_to_end():
    store = get_evaluation_store()
    run = store.submit("tracepilot-eval-v1", 3, judge_mode="none", dedup=False)
    record = _wait_run(run["run_id"])
    assert record["status"] == "succeeded", record.get("error")

    candidates_response = client.get("/api/pareto/candidates")
    assert candidates_response.status_code == 200, candidates_response.text
    candidates_body = candidates_response.json()
    assert candidates_body["count"] >= 1
    assert candidates_body["paid_api_called"] is False
    assert candidates_body["metric_definitions"]["quality"]["field"] == "task_success_rate"
    candidate_ids = [c["candidate_id"] for c in candidates_body["candidates"]]
    assert f"cand-{run['run_id']}" in candidate_ids

    created = client.post(
        "/api/pareto/analyses",
        json={
            "constraints": {"minimum_quality": 0.5},
            "min_sample_size": 1,
        },
    )
    assert created.status_code == 200, created.text
    analysis = created.json()
    assert analysis["analysis_id"].startswith("pareto-")
    assert analysis["status"] in {"OK", "NO_FEASIBLE_CANDIDATE", "NO_COMPARABLE_CANDIDATE"}
    assert analysis["paid_api_called"] is False
    assert analysis["metric_definitions"]["latency"]["direction"] == "min"
    assert analysis["objective"]["quality"] == "max"
    analysis_id = analysis["analysis_id"]
    assert Path(PROJECT_ROOT / analysis["report_path"]).exists()

    fetched = client.get(f"/api/pareto/analyses/{analysis_id}")
    assert fetched.status_code == 200
    assert fetched.json()["analysis_id"] == analysis_id

    frontier = client.get(f"/api/pareto/analyses/{analysis_id}/frontier")
    assert frontier.status_code == 200
    frontier_body = frontier.json()
    assert frontier_body["count"] == len(frontier_body["frontier"])
    assert frontier_body["paid_api_called"] is False

    dominated = client.get(f"/api/pareto/analyses/{analysis_id}/dominated")
    assert dominated.status_code == 200
    assert dominated.json()["count"] == len(dominated.json()["dominated"])

    dominance = client.get(f"/api/pareto/analyses/{analysis_id}/dominance")
    assert dominance.status_code == 200
    assert "definition" in dominance.json()
    assert dominance.json()["paid_api_called"] is False

    # 约束重新筛选：质量 1.0 + 延迟 <=0.001ms + 必须 PASS 安全门禁
    # （模拟评测运行没有已落盘门禁报告 → 不可能满足）
    filtered = client.post(
        f"/api/pareto/analyses/{analysis_id}/filter",
        json={
            "minimum_quality": 1.0,
            "maximum_latency_ms": 0.001,
            "required_safety_gate": "PASS",
        },
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["status"] == "NO_FEASIBLE_CANDIDATE"
    assert filtered.json()["feasible_count"] == 0
    assert filtered.json()["view_of"] == analysis_id

    # 关联 Evaluation Run 与 Trace
    candidate_id = analysis["candidates"][0]["candidate_id"]
    evaluation = client.get(
        f"/api/pareto/analyses/{analysis_id}/candidates/{candidate_id}/evaluation"
    )
    assert evaluation.status_code == 200, evaluation.text
    assert evaluation.json()["evaluation_run_id"]
    assert evaluation.json()["paid_api_called"] is False

    traces = client.get(
        f"/api/pareto/analyses/{analysis_id}/candidates/{candidate_id}/traces"
    )
    assert traces.status_code == 200
    traces_body = traces.json()
    assert traces_body["trace_count"] == len(traces_body["trace_ids"])
    assert traces_body["paid_api_called"] is False

    listing = client.get("/api/pareto/analyses")
    assert listing.status_code == 200
    assert any(a["analysis_id"] == analysis_id for a in listing.json()["analyses"])

    # 错误路径
    assert client.get("/api/pareto/analyses/pareto-doesnotexist").status_code == 404
    assert client.get("/api/pareto/analyses/not-an-id").status_code == 404
    assert client.post(
        "/api/pareto/analyses", json={"constraints": {"bad": 1}}
    ).status_code == 400
    assert client.post(
        f"/api/pareto/analyses/{analysis_id}/filter",
        json={"minimum_quality": 2},
    ).status_code == 400
    assert client.get(
        f"/api/pareto/analyses/{analysis_id}/candidates/cand-missing/evaluation"
    ).status_code == 404


def test_api_no_paid_api_called_and_no_secret_leak():
    responses = [
        client.get("/api/pareto/candidates"),
        client.get("/api/pareto/analyses"),
    ]
    created = client.post("/api/pareto/analyses", json={})
    assert created.status_code == 200
    responses.append(created)
    responses.append(client.get(f"/api/pareto/analyses/{created.json()['analysis_id']}"))
    for response in responses:
        body = response.text
        assert "sk-" not in body.lower() or "skipped" in body.lower()
        payload = response.json()
        if "paid_api_called" in payload:
            assert payload["paid_api_called"] is False
