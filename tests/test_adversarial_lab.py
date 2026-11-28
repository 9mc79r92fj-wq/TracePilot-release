"""Adversarial Evaluation Lab 测试（pytest，第 46 步）。

覆盖：类型接口、生成接口、运行接口、结果查询、覆盖率计算口径、
Bad Case 关联、Trace 关联、DAG 跳转、安全限制、数据来源标记。
全部受控沙盒执行，不调用付费 API。
"""

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.services import red_team_generator as rt  # noqa: E402
from backend.app.services.trace_dag import build_dag  # noqa: E402
from backend.app.services.trace_store import load_trace  # noqa: E402

client = TestClient(app)


def _generate(**kwargs):
    payload = {"variations": 1, "seed": 42}
    payload.update(kwargs)
    r = client.post("/api/red-team/generate", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _run(**kwargs):
    payload = {"regenerate": True, "variations": 1, "seed": 42, "persist_traces": True}
    payload.update(kwargs)
    r = client.post("/api/red-team/runs", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 1. 类型接口 ----------

def test_types_endpoint():
    r = client.get("/api/red-team/types")
    assert r.status_code == 200
    d = r.json()
    assert len(d["attack_types"]) == 6
    assert {t["key"] for t in d["attack_types"]} == set(rt.ATTACK_TYPES)
    assert d["difficulties"] == ["easy", "medium", "hard"]
    assert len(d["safety_constraints"]) >= 10
    # 安全限制必须声明
    limits = d["limits"]
    assert limits["max_execute_cases"] <= 60
    assert limits["max_generated_cases"] <= 120
    assert limits["max_variants"] == 4
    assert limits["max_concurrent_runs"] == 1
    assert limits["paid_api_called"] is False
    assert limits["execution_environment"] == "controlled_execution"
    assert "工具白名单" in limits["execution_environment_note"]
    assert "安全通过率" in d["metrics_note"]


# ---------- 2. 生成接口 ----------

def test_generate_endpoint():
    d = _generate()
    assert d["dataset"]["total_cases"] >= 6
    assert d["dataset"]["data_source"] == "synthetic"
    assert d["cases"]
    for c in d["cases"]:
        assert c["data_source"] == "synthetic"
        assert c["attack_type"] in rt.ATTACK_TYPES
        assert c["safety_constraints"]
        assert "expected_safe_behavior" in c


def test_generate_with_difficulty_filter():
    d = _generate(difficulties=["easy"])
    assert d["dataset"]["by_difficulty"] == {"easy": d["dataset"]["total_cases"]}
    assert all(c["difficulty"] == "easy" for c in d["cases"])


def test_generate_with_attack_type_filter():
    d = _generate(attack_types=["prompt_injection"], include_canary=False)
    assert {c["attack_type"] for c in d["cases"]} == {"prompt_injection"}


def test_generate_reproducible_same_seed():
    a = _generate(seed=7)
    b = _generate(seed=7)
    assert [c["case_id"] for c in a["cases"]] == [c["case_id"] for c in b["cases"]]


def test_generate_invalid_params():
    for payload in (
        {"variations": 0},
        {"variations": 9},
        {"seed": -1},
        {"difficulties": ["impossible"]},
        {"attack_types": ["evil"]},
    ):
        r = client.post("/api/red-team/generate", json=payload)
        assert r.status_code in (400, 422), payload


# ---------- 3. 运行接口 ----------

def test_run_endpoint():
    d = _run()
    assert d["run_id"].startswith("rt-run-")
    assert d["status"] == "succeeded"
    assert d["case_count"] >= 6
    assert d["paid_api_called"] is False
    assert d["data_source"] == "synthetic"
    m = d["metrics"]
    # 安全与任务指标分离
    assert m["safety_pass_rate"] is not None
    assert m["task_success_rate"] is not None
    assert "两个独立指标" in m["note"]
    assert d["coverage"]["coverage_status"] in {"FULL", "PARTIAL", "NOT_COVERED"}


def test_run_selected_cases():
    gen = _generate()
    ids = [c["case_id"] for c in gen["cases"][:3]]
    d = _run(case_ids=ids)
    assert d["case_count"] == 3
    assert d["report"]["dataset"]["selected_cases"] == 3
    assert d["coverage"]["coverage_status"] in {"PARTIAL", "FULL"}


def test_run_unknown_case_ids_rejected():
    r = client.post("/api/red-team/runs", json={"case_ids": ["rt-doesnotexist"]})
    assert r.status_code == 400
    assert "未知案例" in r.json()["detail"]


def test_run_limit_enforced():
    r = client.post("/api/red-team/runs", json={"case_ids": [f"rt-x-{i}" for i in range(61)]})
    assert r.status_code in (400, 422)


def test_run_max_cases_capped():
    r = client.post("/api/red-team/runs", json={"regenerate": True, "max_cases": 999})
    assert r.status_code in (400, 422)


def test_run_detail_and_404():
    d = _run(max_cases=5)
    r = client.get(f"/api/red-team/runs/{d['run_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == d["run_id"]
    assert body["coverage"]["entries"]
    assert body["limits"]["paid_api_called"] is False
    assert client.get("/api/red-team/runs/rt-run-nope").status_code == 404


# ---------- 4. 结果查询接口（筛选） ----------

def test_cases_endpoint_filters():
    d = _run()
    run_id = d["run_id"]
    all_cases = client.get(f"/api/red-team/runs/{run_id}/cases")
    assert all_cases.status_code == 200
    total = all_cases.json()["count"]
    assert total == d["case_count"]

    safe = client.get(f"/api/red-team/runs/{run_id}/cases?safety=SAFE").json()
    unsafe = client.get(f"/api/red-team/runs/{run_id}/cases?safety=UNSAFE").json()
    assert safe["count"] + unsafe["count"] <= total
    for c in safe["cases"]:
        assert c["safety"]["verdict"] == "SAFE"
    for c in unsafe["cases"]:
        assert c["safety"]["verdict"] == "UNSAFE"

    by_type = client.get(f"/api/red-team/runs/{run_id}/cases?attack_type=prompt_injection").json()
    assert all(c["attack_type"] == "prompt_injection" for c in by_type["cases"])

    executed = client.get(f"/api/red-team/runs/{run_id}/cases?executed_only=true").json()
    assert all(c["status"] == "EXECUTED" for c in executed["cases"])


def test_case_detail_endpoint():
    d = _run(max_cases=3)
    case_id = d["report"]["cases"][0]["case_id"]
    r = client.get(f"/api/red-team/runs/{d['run_id']}/cases/{case_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["case"]["case_id"] == case_id
    assert body["case"]["safety"]["checked"], "必须返回安全约束逐条结果"
    assert body["data_source"] == "synthetic"
    assert client.get(f"/api/red-team/runs/{d['run_id']}/cases/nope").status_code == 404


# ---------- 5. 覆盖率计算口径 ----------

def test_coverage_uses_real_data_only():
    d = _run()
    cov = d["coverage"]
    totals = cov["totals"]
    # 总数一致
    assert totals["generated_cases"] == d["report"]["dataset"]["total_cases"]
    assert totals["executed_cases"] == d["case_count"]
    # 通过 + 失败 + 未知 == 已执行（UNKNOWN 不并入任何一侧、也不算丢失）
    assert totals["safety_passed"] + totals["safety_failed"] + totals["unknown_cases"] == totals["executed_cases"]
    # 未执行 = 生成 - 已执行
    assert totals["not_executed_cases"] == totals["generated_cases"] - totals["executed_cases"]
    # 每类条目自洽
    for e in cov["entries"]:
        assert e["safety_passed"] + e["safety_failed"] + e["unknown_cases"] == e["executed_cases"]
        assert e["coverage_status"] in {"FULL", "PARTIAL", "NOT_COVERED"}


def test_coverage_partial_when_not_all_executed():
    gen = _generate()
    ids = [c["case_id"] for c in gen["cases"][:2]]
    d = _run(case_ids=ids)
    cov = d["coverage"]
    assert cov["totals"]["not_executed_cases"] > 0
    assert cov["coverage_status"] == "PARTIAL"
    # 未执行案例不得计入通过
    assert cov["totals"]["safety_passed"] <= cov["totals"]["executed_cases"]


def test_coverage_endpoint():
    d = _run(max_cases=4)
    r = client.get(f"/api/red-team/runs/{d['run_id']}/coverage")
    assert r.status_code == 200
    body = r.json()
    assert body["coverage"]["definitions"]["safety_passed"]
    assert "not_executed_cases" in body["coverage"]["definitions"]
    assert body["metrics"]["canary_count"] is not None


# ---------- 6. Bad Case 关联 ----------

def test_bad_cases_endpoint():
    d = _run()
    r = client.get(f"/api/red-team/runs/{d['run_id']}/bad-cases")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == len(body["bad_cases"])
    for b in body["bad_cases"]:
        assert b["violations"], "Bad Case 必须给出违反的约束"
        assert b["trace_id"], "Bad Case 必须关联 Trace"
        assert b["links"]["trace"]
        assert b["links"]["dag"]


# ---------- 7. Trace / 8. DAG 关联 ----------

def test_trace_and_dag_links_resolve():
    d = _run(max_cases=3)
    case = d["report"]["cases"][0]
    trace_id = case["trace_id"]
    assert trace_id

    tr = client.get(f"/api/traces/{trace_id}")
    assert tr.status_code == 200, "案例关联的 Trace 必须可查询"
    trace = tr.json()["trace"]
    assert trace["metadata"]["red_team"] is True
    assert trace["metadata"]["data_source"] == "simulated"

    dag = client.get(f"/api/traces/{trace_id}/dag")
    assert dag.status_code == 200
    dag_body = dag.json()["dag"]
    assert len(dag_body["nodes"]) == len(trace["steps"])
    assert dag_body["topological_order"]


def test_dag_buildable_from_loader():
    d = _run(max_cases=1)
    trace = load_trace(d["report"]["cases"][0]["trace_id"])
    dag = build_dag(trace)
    assert dag.node_count == len(trace.steps)


# ---------- 11. 安全限制 ----------

def test_generate_case_upper_bound():
    # variations=4 时总数仍在生成上限内
    d = _generate(variations=4)
    assert d["dataset"]["total_cases"] <= 120


def test_no_dangerous_tool_executed():
    d = _run(max_cases=8)
    for row in d["report"]["cases"]:
        if row["status"] != "EXECUTED":
            continue
        trace = load_trace(row["trace_id"])
        for s in trace.steps:
            if s.step_type.value != "tool_call":
                continue
            if s.metadata.get("executed") is False:
                continue
            assert s.tool_name in {"calculator", "text_analyzer", "sqlite_query"}


def test_no_paid_api_and_no_tokens():
    d = _run(max_cases=4)
    assert d["paid_api_called"] is False
    for row in d["report"]["cases"]:
        if not row["trace_id"]:
            continue
        trace = load_trace(row["trace_id"])
        assert trace.metadata["data_source"] == "simulated"
        total = sum((s.token_usage.total_tokens if s.token_usage else 0) for s in trace.steps)
        assert total == 0


# ---------- 12. 数据来源标记 ----------

def test_data_source_marking():
    d = _run(max_cases=2)
    assert d["data_source"] == "synthetic"
    gen = _generate()
    assert gen["dataset"]["data_source"] == "synthetic"
    assert all(c["data_source"] == "synthetic" for c in gen["cases"])
    types = client.get("/api/red-team/types").json()
    assert types["data_sources"] == ["synthetic"]


def test_no_sensitive_leak():
    d = _run(max_cases=2)
    text = client.get(f"/api/red-team/runs/{d['run_id']}").text
    assert "DEEPSEEK_API_KEY=" not in text
    assert not re.search(r"\bsk-[A-Za-z0-9]{20,}\b", text)
