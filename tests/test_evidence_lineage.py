"""Evidence Lineage 与数据一致性测试（pytest，第 44 步）。

全部使用 Mock / 本地数据，不调用付费 API。
"""

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.schemas.trace import (  # noqa: E402
    TokenUsage,
    TraceRecord,
    TraceStatus,
    TraceStep,
)
from backend.app.services.derived_trace import (  # noqa: E402
    DERIVATION_KINDS,
    build_derived_metadata,
)
from backend.app.services.evidence_lineage import (  # noqa: E402
    ORIGINS,
    STORAGE_STATES,
    NotFoundEvidence,
    build_evaluation_case_lineage,
    build_experiment_lineage,
    build_trace_lineage,
    classify_origin,
    classify_storage,
    consistency_check,
)

client = TestClient(app)
TS = "2026-01-01T00:00:00+00:00"


def _trace(trace_id: str, *, metadata=None, steps=None) -> TraceRecord:
    return TraceRecord(
        trace_id=trace_id,
        task_id="task-test",
        run_id="run-test",
        started_at=TS,
        ended_at=TS,
        status=TraceStatus.SUCCEEDED,
        steps=steps or [
            TraceStep.model_validate({
                "step_id": f"{trace_id}-s001", "trace_id": trace_id, "parent_step_ids": [],
                "step_type": "planning", "timestamp": TS, "status": "succeeded",
            }),
            TraceStep.model_validate({
                "step_id": f"{trace_id}-s002", "trace_id": trace_id,
                "parent_step_ids": [f"{trace_id}-s001"], "step_type": "tool_call",
                "timestamp": TS, "tool_name": "calculator",
                "tool_arguments": {"expression": "125*48"},
                "tool_result": {"expression": "125*48", "result": 6000},
                "status": "succeeded", "token_usage": TokenUsage(input_tokens=10, output_tokens=2),
            }),
        ],
        metadata=metadata or {},
    )


# ---------- 1. 来源分类（REAL_API / MOCK / SIMULATED / UNKNOWN） ----------

def test_origin_vocabulary():
    assert set(ORIGINS) == {"REAL_API", "MOCK", "SIMULATED", "UNKNOWN"}
    assert set(STORAGE_STATES) == {"RECORDED", "MEMORY", "NOT_AVAILABLE"}


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"data_source": "real_api", "simulated": False}, "REAL_API"),
        ({"data_source": "mock", "simulated": True}, "MOCK"),
        ({"data_source": "simulated", "simulated": True}, "SIMULATED"),
        ({"simulated": True}, "SIMULATED"),            # 旧数据缺 data_source
        ({}, "UNKNOWN"),
        ({"model": "deepseek-chat"}, "UNKNOWN"),        # 仅模型名不足以判定
        ({"data_source": "real_api", "simulated": True}, "UNKNOWN"),  # 自相矛盾
    ],
)
def test_classify_origin(metadata, expected):
    origin, evidence = classify_origin(metadata)
    assert origin == expected
    assert evidence, "必须给出判定依据"


def test_classify_origin_never_guesses_real_api():
    """缺 data_source 时绝不能推断为 REAL_API。"""
    for meta in ({}, {"simulated": None}, {"model": "deepseek-chat"}):
        origin, _ = classify_origin(meta)
        assert origin != "REAL_API"


# ---------- 2. 存储状态与来源正交 ----------

def test_classify_storage():
    state, note = classify_storage("trace-doesnotexist")
    assert state == "NOT_AVAILABLE"
    assert note
    state2, _ = classify_storage(None)
    assert state2 == "NOT_AVAILABLE"


def test_recorded_does_not_imply_real_api():
    """RECORDED 只代表已落盘，不代表来自真实 API。"""
    rec = _trace("trace-lineage-recorded", metadata={"data_source": "mock", "simulated": True})
    origin, _ = classify_origin(rec.metadata)
    assert origin == "MOCK"


# ---------- 3. 派生 Trace 元数据（不伪造真实性） ----------

def test_derived_metadata_preserves_source():
    src = _trace("trace-src-1", metadata={"data_source": "real_api", "simulated": False})
    meta = build_derived_metadata(src, derivation="counterfactual", engine_version="0.1.0")
    assert meta["data_source"] == "simulated"
    assert meta["simulated"] is True
    assert meta["derived_from"] == "trace-src-1"
    assert meta["derivation"] == "counterfactual"
    assert meta["source_data_source"] == "REAL_API", "必须保留原始来源（规范词表）"
    assert meta["source_data_source_raw"] == "real_api", "原始字段值也必须保留"
    assert meta["data_source"] != "real_api", "派生结果不得标记为 real_api"


def test_derived_metadata_kinds():
    src = _trace("trace-src-2")
    with pytest.raises(ValueError):
        build_derived_metadata(src, derivation="evil", engine_version="1")
    for kind in DERIVATION_KINDS:
        meta = build_derived_metadata(src, derivation=kind, engine_version="1")
        assert meta["derivation"] == kind


# ---------- 4. Trace 血缘 ----------

def test_trace_lineage_from_real_trace():
    traces = client.get("/api/traces").json()["traces"]
    tid = traces[0]["trace_id"]
    r = client.get(f"/api/lineage/trace/{tid}")
    assert r.status_code == 200
    data = r.json()
    assert data["root_kind"] == "trace"
    assert data["root_id"] == tid
    kinds = {n["kind"] for n in data["nodes"]}
    assert "trace" in kinds
    assert "step" in kinds
    # 每个节点必须带来源与存储状态
    for n in data["nodes"]:
        assert n["origin"] in ORIGINS
        assert n["storage"] in STORAGE_STATES
        assert n["status"] in {"AVAILABLE", "NOT_AVAILABLE"}
    assert data["summary"]["node_count"] == len(data["nodes"])


def test_trace_lineage_unknown_trace_404():
    r = client.get("/api/lineage/trace/trace-doesnotexist")
    assert r.status_code == 404


def test_trace_lineage_tool_nodes_from_agent_run():
    """Agent mock 运行产生的 Trace 必须能追溯 Run → Tool Call → Tool Result。"""
    run = client.post("/api/agent/runs", json={"task": "计算125乘以48", "mode": "mock"}).json()["run"]
    r = client.get(f"/api/lineage/trace/{run['trace_id']}")
    assert r.status_code == 200
    data = r.json()
    node_ids = {n["node_id"] for n in data["nodes"]}
    assert run["run_id"] in node_ids, "run_id 必须可作为血缘节点追溯"
    kinds = {n["kind"] for n in data["nodes"]}
    assert {"tool_call", "tool_result"} <= kinds

    tool_result = next(n for n in data["nodes"] if n["kind"] == "tool_result")
    assert tool_result["detail"]["tool_result"]["result"] == 6000
    assert tool_result["origin"] == "MOCK"
    # 边关系必须存在：trace→step、tool_call→tool_result
    relations = {e["relation"] for e in data["edges"]}
    assert "contains" in relations
    assert "produced" in relations


def test_lineage_edges_reference_existing_nodes():
    run = client.post("/api/agent/runs", json={"task": "计算2+3", "mode": "mock"}).json()["run"]
    data = client.get(f"/api/lineage/trace/{run['trace_id']}").json()
    node_ids = {n["node_id"] for n in data["nodes"]}
    for e in data["edges"]:
        assert e["from"] in node_ids, f"边起点不存在: {e}"
        assert e["to"] in node_ids, f"边终点不存在: {e}"


# ---------- 5. 评测案例血缘 ----------

def test_evaluation_case_lineage():
    created = client.post(
        "/api/evaluation/runs",
        json={"dataset_id": "tracepilot-eval-v1", "max_cases": 2, "judge_mode": "mock"},
    ).json()
    run_id = created["run_id"]

    import time

    for _ in range(300):
        d = client.get(f"/api/evaluation/runs/{run_id}").json()
        if d.get("status") != "running":
            break
        time.sleep(0.1)
    assert d["status"] == "succeeded"
    case = d["report"]["case_results"][0]

    r = client.get(f"/api/lineage/evaluation/{run_id}/cases/{case['case_id']}")
    assert r.status_code == 200
    data = r.json()
    kinds = {n["kind"] for n in data["nodes"]}
    assert "evaluation_run" in kinds
    assert "evaluation_case" in kinds
    assert "trace" in kinds

    # 案例 → Trace 边
    rels = {(e["from"], e["to"], e["relation"]) for e in data["edges"]}
    assert (run_id, case["case_id"], "contains_case") in rels
    assert (case["case_id"], case["trace_id"], "evaluated_trace") in rels

    trace_node = next(n for n in data["nodes"] if n["kind"] == "trace")
    assert trace_node["storage"] == "RECORDED"
    assert trace_node["origin"] in ORIGINS


def test_evaluation_case_missing_case_not_available():
    created = client.post(
        "/api/evaluation/runs",
        json={"dataset_id": "tracepilot-eval-v1", "max_cases": 1, "judge_mode": "none"},
    ).json()
    import time

    for _ in range(300):
        d = client.get(f"/api/evaluation/runs/{created['run_id']}").json()
        if d.get("status") != "running":
            break
        time.sleep(0.1)

    data = build_evaluation_case_lineage(created["run_id"], "case-doesnotexist")
    missing = [n for n in data["nodes"] if n["status"] == "NOT_AVAILABLE"]
    assert missing, "不存在的案例必须标记 NOT_AVAILABLE"
    assert data["summary"]["has_missing_evidence"] is True


def test_evaluation_case_unknown_run_404():
    r = client.get("/api/lineage/evaluation/eval-nope/cases/case-01")
    assert r.status_code == 404


# ---------- 6. 实验血缘 ----------

def test_experiment_lineage_and_id_consistency():
    traces = client.get("/api/traces").json()["traces"]
    fault = next((t for t in traces if t["fault_types"]), None)
    assert fault is not None
    created = client.post(
        "/api/counterfactual/experiments",
        json={"trace_id": fault["trace_id"], "intervention": {"type": "remove_fault"}},
    ).json()
    exp_id = created["experiment_id"]

    import time

    for _ in range(300):
        d = client.get(f"/api/counterfactual/experiments/{exp_id}").json()
        if d.get("status") != "running":
            break
        time.sleep(0.1)
    assert d["status"] == "succeeded"

    data = client.get(f"/api/lineage/experiment/{exp_id}").json()
    kinds = {n["kind"] for n in data["nodes"]}
    assert "experiment" in kinds
    assert "trace" in kinds
    # 不得出现 ID 一致性告警
    assert "consistency_warning" not in kinds, "实验登记表 ID 与引擎结果 ID 必须一致"


def test_experiment_variant_trace_recorded_and_marked():
    """派生 Trace 必须可查询，且来源标记为 simulated + derived_from 保留。"""
    traces = client.get("/api/traces").json()["traces"]
    fault = next((t for t in traces if t["fault_types"]), None)
    created = client.post(
        "/api/counterfactual/experiments",
        json={"trace_id": fault["trace_id"], "intervention": {"type": "remove_fault"}},
    ).json()
    exp_id = created["experiment_id"]
    import time

    for _ in range(300):
        d = client.get(f"/api/counterfactual/experiments/{exp_id}").json()
        if d.get("status") != "running":
            break
        time.sleep(0.1)
    res = client.get(f"/api/counterfactual/experiments/{exp_id}/results").json()["result"]
    variant_tid = res["intervention_outcome"]["trace_id"]

    tr = client.get(f"/api/traces/{variant_tid}")
    assert tr.status_code == 200, "派生 Trace 必须可查询（否则证据链断裂）"
    meta = tr.json()["trace"]["metadata"]
    assert meta["data_source"] == "simulated"
    assert meta["simulated"] is True
    assert meta["derived_from"] == fault["trace_id"]
    assert meta["source_data_source"] in ORIGINS or meta["source_data_source"] == "unknown"


def test_original_trace_not_modified():
    """派生执行不得改写原始 Trace。"""
    traces = client.get("/api/traces").json()["traces"]
    fault = next((t for t in traces if t["fault_types"]), None)
    before = client.get(f"/api/traces/{fault['trace_id']}").json()["trace"]
    client.post(
        "/api/counterfactual/experiments",
        json={"trace_id": fault["trace_id"], "intervention": {"type": "remove_fault"}},
    )
    after = client.get(f"/api/traces/{fault['trace_id']}").json()["trace"]
    assert before["metadata"] == after["metadata"]
    assert [s["step_id"] for s in before["steps"]] == [s["step_id"] for s in after["steps"]]


# ---------- 7. 一致性检查 ----------

def test_consistency_check_api():
    r = client.get("/api/lineage/consistency?limit=30")
    assert r.status_code == 200
    data = r.json()
    assert data["checked_traces"] >= 1
    assert isinstance(data["issues"], list)
    assert data["ok"] is (data["issue_count"] == 0)
    assert "derived_marked_real_api" in data["issue_kinds"]


def test_consistency_detects_broken_derived_from():
    """人为构造指向不存在 Trace 的派生记录，检查必须报出。"""
    from backend.app.services.trace_store import DEFAULT_TRACES_DIR

    bad_id = "trace-lineage-bad-derived"
    rec = _trace(bad_id, metadata={
        "data_source": "simulated", "simulated": True,
        "derived_from": "trace-nonexistent-source",
    })
    path = Path(DEFAULT_TRACES_DIR) / f"{bad_id}.json"
    path.write_text(rec.model_dump_json(indent=2), encoding="utf-8")
    try:
        result = consistency_check(trace_ids=[bad_id])
        kinds = {i["kind"] for i in result["issues"]}
        assert "broken_derived_from" in kinds
    finally:
        path.unlink(missing_ok=True)


def test_consistency_detects_derived_marked_real_api():
    from backend.app.services.trace_store import DEFAULT_TRACES_DIR

    bad_id = "trace-lineage-bad-real"
    rec = _trace(bad_id, metadata={
        "data_source": "real_api", "simulated": False,
        "derived_from": "trace-whatever",
    })
    path = Path(DEFAULT_TRACES_DIR) / f"{bad_id}.json"
    path.write_text(rec.model_dump_json(indent=2), encoding="utf-8")
    try:
        result = consistency_check(trace_ids=[bad_id])
        kinds = {i["kind"] for i in result["issues"]}
        assert "derived_marked_real_api" in kinds
    finally:
        path.unlink(missing_ok=True)


# ---------- 8. 安全 ----------

def test_no_sensitive_leak_in_lineage():
    traces = client.get("/api/traces").json()["traces"]
    text = client.get(f"/api/lineage/trace/{traces[0]['trace_id']}").text
    assert "DEEPSEEK_API_KEY=" not in text
    # 注意：task-* 等普通 ID 含 "sk-"，必须用严格密钥模式，避免误报
    assert not re.search(r"\bsk-[A-Za-z0-9]{20,}\b", text)
    assert "authorization" not in text.lower()


def test_no_paid_api_used():
    """血缘查询纯只读本地数据，不应触发任何 Agent/真实 API 调用。"""
    traces = client.get("/api/traces").json()["traces"]
    before = client.get("/api/agent/runs").json()["count"]
    client.get(f"/api/lineage/trace/{traces[0]['trace_id']}")
    client.get("/api/lineage/consistency?limit=5")
    after = client.get("/api/agent/runs").json()["count"]
    assert after == before
