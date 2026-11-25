"""第 55-A 步：数据治理与可信度测试。

覆盖：
1. 来源分类规则（REAL_API / MOCK / SIMULATED / REPLAY / UNKNOWN，含保守规则）；
2. 全库扫描口径一致性（总数 = 两个目录之和 = 各来源之和）与 sidecar 写入/复验；
3. Trace 列表接口的来源筛选 / 状态筛选 / 故障筛选与口径字段；
4. /api/governance/* 汇总、失败 Trace 口径、单条分类与受控实验资格；
5. 成本统计的来源范围过滤（不改变金额口径、UNKNOWN 不按 0 计）；
6. 非法来源参数返回 400，未知 trace_id 返回 404。

全部为本地只读计算，不调用付费 API、不修改已有 Trace。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.schemas.trace import TraceStatus
from backend.app.services import data_governance as gov
from backend.app.services import token_analytics as ta
from backend.app.services.trace_collector import TraceCollector

client = TestClient(app)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 工具函数：合成 Trace 文件 / 内存 Trace
# ---------------------------------------------------------------------------

def _write_trace(
    directory: Path,
    trace_id: str,
    *,
    metadata: dict | None = None,
    status: str = "succeeded",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{trace_id}.json"
    payload = {
        "trace_id": trace_id,
        "task_id": f"task-{trace_id}",
        "run_id": "run-1",
        "started_at": "2026-11-19T00:00:00+00:00",
        "ended_at": "2026-11-19T00:00:01+00:00",
        "status": status,
        "steps": [],
        "metadata": metadata or {},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _raw_steps(trace_id: str, *, fault_type: str | None = None, tool: str = "calculator") -> list[dict]:
    ts = "2026-11-19T00:00:00+00:00"
    steps = [
        {
            "step_id": f"{trace_id}-s001",
            "trace_id": trace_id,
            "parent_step_ids": [],
            "step_type": "planning",
            "timestamp": ts,
            "input": {"task": "计算 1+1"},
            "output": {"plan": ["调用计算器"]},
            "status": "succeeded",
        },
        {
            "step_id": f"{trace_id}-s002",
            "trace_id": trace_id,
            "parent_step_ids": [f"{trace_id}-s001"],
            "step_type": "tool_call",
            "timestamp": ts,
            "tool_name": tool,
            "tool_arguments": {"expression": "1+1"},
            "tool_result": {"result": 2},
            "status": "succeeded",
            "duration_ms": 1,
        },
    ]
    if fault_type:
        steps[1] = {
            **steps[1],
            "status": "failed",
            "tool_result": {"error": "注入故障"},
            "error": {"error_type": fault_type, "error_message": "注入故障", "recoverable": False},
            "metadata": {
                "fault_injection": {"type": fault_type, "injected": True, "message": "注入故障"}
            },
        }
    return steps


def _build_record(trace_id: str, *, steps: list[dict], status: str = "failed", metadata: dict | None = None):
    collector = TraceCollector(task_id=f"task-{trace_id}", trace_id=trace_id, run_id="run-1")
    return collector.build(steps, status=status, metadata=metadata or {})


# ---------------------------------------------------------------------------
# 1. 来源分类规则
# ---------------------------------------------------------------------------

def test_classify_real_api_verified():
    result = gov.classify_metadata({"data_source": "real_api", "real_api_verified": True})
    assert result["source_class"] == "REAL_API"
    assert "real_api_verified=true" in result["evidence"]


def test_classify_real_api_without_verification_is_unknown():
    """缺少独立验证标记时不得自动升级为 REAL_API。"""
    result = gov.classify_metadata({"data_source": "real_api"})
    assert result["source_class"] == "UNKNOWN"
    assert any("real_api_verified" in reason for reason in result["reasons"])


def test_classify_mock():
    result = gov.classify_metadata({"data_source": "mock", "simulated": True})
    assert result["source_class"] == "MOCK"


def test_classify_simulated():
    assert gov.classify_metadata({"simulated": True})["source_class"] == "SIMULATED"
    assert gov.classify_metadata({"evaluation": True})["source_class"] == "SIMULATED"
    assert gov.classify_metadata({"case_id": "case-1"})["source_class"] == "SIMULATED"


def test_classify_derivation_takes_replay_precedence():
    result = gov.classify_metadata(
        {"data_source": "mock", "simulated": True, "derived_from": "trace-base"}
    )
    assert result["source_class"] == "REPLAY"
    result2 = gov.classify_metadata({"data_source": "mock", "derivation": "counterfactual"})
    assert result2["source_class"] == "REPLAY"


def test_classify_contradictory_verified_marks_unknown():
    result = gov.classify_metadata(
        {"data_source": "real_api", "real_api_verified": True, "simulated": True}
    )
    assert result["source_class"] == "UNKNOWN"


def test_classify_missing_metadata_unknown():
    result = gov.classify_metadata({})
    assert result["source_class"] == "UNKNOWN"
    assert result["reasons"]


# ---------------------------------------------------------------------------
# 2. 全库扫描口径一致性 + sidecar
# ---------------------------------------------------------------------------

def test_scan_counts_are_consistent(monkeypatch, tmp_path):
    agent_dir = tmp_path / "agent"
    eval_dir = tmp_path / "evaluation"
    _write_trace(agent_dir, "trace-mock", metadata={"data_source": "mock", "simulated": True})
    _write_trace(
        agent_dir,
        "trace-real-claim",
        metadata={"data_source": "real_api"},
    )
    _write_trace(
        agent_dir,
        "trace-derived",
        metadata={"data_source": "mock", "derived_from": "trace-base"},
        status="failed",
    )
    _write_trace(eval_dir, "trace-eval", metadata={"simulated": True, "evaluation": True})
    (agent_dir / "broken.json").write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(gov, "TRACE_DIRS", (agent_dir, eval_dir))

    scanned = gov._scan_uncached()  # noqa: SLF001 - 直接验证扫描逻辑
    summary = scanned["summary"]
    assert summary["total_traces"] == 5
    assert summary["agent_traces"] == 3
    assert summary["evaluation_traces"] == 1
    assert summary["malformed_files"] == 1
    assert summary["by_source"] == {
        "REAL_API": 0,
        "MOCK": 1,
        "SIMULATED": 1,
        "REPLAY": 1,
        "UNKNOWN": 1,
    }
    # 坏文件计入 total_traces 但不进入来源分类；两者必须能被显式区分
    assert sum(summary["by_source"].values()) + summary["malformed_files"] == summary["total_traces"]
    assert summary["fault_traces_by_source"] == {
        "REAL_API": 0,
        "MOCK": 0,
        "SIMULATED": 0,
        "REPLAY": 0,
        "UNKNOWN": 0,
    }
    assert summary["duplicate_trace_ids"] == {}
    assert scanned["entries"]["trace-mock"]["source_class"] == "MOCK"


def test_scan_detects_real_api_claim_and_duplicates(monkeypatch, tmp_path):
    agent_dir = tmp_path / "agent"
    eval_dir = tmp_path / "evaluation"
    _write_trace(agent_dir, "trace-a", metadata={"data_source": "real_api"})
    _write_trace(eval_dir, "trace-a", metadata={"simulated": True})
    monkeypatch.setattr(gov, "TRACE_DIRS", (agent_dir, eval_dir))
    summary = gov._scan_uncached()["summary"]  # noqa: SLF001
    assert summary["total_traces"] == 2
    assert "trace-a" in summary["duplicate_trace_ids"]
    assert summary["legacy_real_api_claims"][0]["classified_as"] == "UNKNOWN"


def test_sidecar_write_and_verify_roundtrip(monkeypatch, tmp_path):
    agent_dir = tmp_path / "agent"
    eval_dir = tmp_path / "evaluation"
    _write_trace(agent_dir, "trace-mock", metadata={"data_source": "mock"})
    _write_trace(eval_dir, "trace-eval", metadata={"simulated": True})
    monkeypatch.setattr(gov, "TRACE_DIRS", (agent_dir, eval_dir))
    monkeypatch.setattr(gov, "SOURCE_INDEX_PATH", tmp_path / "source_index.json")
    monkeypatch.setattr(gov, "_CACHE", {"signature": None, "ts": 0.0, "summary": None})

    written = gov.write_source_index()
    assert written["entry_count"] == 2
    sidecar = json.loads((tmp_path / "source_index.json").read_text(encoding="utf-8"))
    assert sidecar["entry_count"] == 2
    assert sidecar["by_source"]["MOCK"] == 1

    # 侧车签名一致时直接复用（不重新扫描）
    cached = gov.scan_sources(force=False)
    assert cached["total_traces"] == 2
    assert cached["by_source"]["SIMULATED"] == 1

    # 目录变化后签名不匹配 → 重新扫描
    _write_trace(agent_dir, "trace-mock-2", metadata={"data_source": "mock"})
    refreshed = gov.scan_sources(force=False)
    assert refreshed["total_traces"] == 3


def test_sources_stale_while_revalidate(monkeypatch, tmp_path):
    """目录变化时页面读取返回旧快照（stale=True），后台单飞重扫并回写 sidecar。"""
    agent_dir = tmp_path / "agent"
    eval_dir = tmp_path / "evaluation"
    _write_trace(agent_dir, "trace-mock", metadata={"data_source": "mock"})
    monkeypatch.setattr(gov, "TRACE_DIRS", (agent_dir, eval_dir))
    monkeypatch.setattr(gov, "SOURCE_INDEX_PATH", tmp_path / "source_index.json")
    monkeypatch.setattr(gov, "_CACHE", {"signature": None, "ts": 0.0, "summary": None})
    monkeypatch.setattr(gov, "_REFRESH_STATE", {"thread": None})

    gov.write_source_index()
    assert json.loads((tmp_path / "source_index.json").read_text(encoding="utf-8"))["total_traces"] == 1

    _write_trace(agent_dir, "trace-new-1", metadata={"data_source": "mock"})
    _write_trace(agent_dir, "trace-new-2", metadata={"data_source": "mock"})

    stale = gov.scan_sources(allow_stale=True)
    assert stale["stale"] is True
    assert stale["total_traces"] == 1  # 不等待 35 秒扫描，先返回旧快照

    thread = gov._REFRESH_STATE["thread"]  # noqa: SLF001 - 测试等待后台刷新完成
    assert thread is not None
    thread.join(timeout=30)
    fresh = gov.scan_sources()
    assert fresh["stale"] is False
    assert fresh["total_traces"] == 3
    sidecar = json.loads((tmp_path / "source_index.json").read_text(encoding="utf-8"))
    assert sidecar["total_traces"] == 3


def test_get_trace_can_skip_full_reload(monkeypatch):
    """流水线等历史引用缺失 Trace 时不得逐条强制全量重载（否则归因退化到分钟级）。"""
    calls: list[bool] = []

    def fake_list_all_traces(*, force: bool = False):
        calls.append(force)
        return []

    monkeypatch.setattr(ta, "list_all_traces", fake_list_all_traces)
    with pytest.raises(ta.CostAnalyticsError):
        ta._get_trace("trace-not-exist-55a", allow_full_reload=False)  # noqa: SLF001
    assert calls == []


def test_trace_eligibility_rules():
    no_steps = _build_record("trace-empty", steps=[])
    caps = gov.trace_eligibility(no_steps)["capabilities"]
    assert caps["recorded_replay"]["eligible"] is False
    assert caps["recorded_replay"]["reasons"]

    fault_record = _build_record(
        "trace-fault", steps=_raw_steps("trace-fault", fault_type="tool_timeout")
    )
    eligibility = gov.trace_eligibility(fault_record)
    assert all(cap["eligible"] for cap in eligibility["capabilities"].values())
    assert eligibility["fault_events"][0]["fault_type"] == "tool_timeout"

    unsafe_record = _build_record(
        "trace-unsafe",
        steps=_raw_steps("trace-unsafe", fault_type="tool_timeout", tool="unknown_tool"),
    )
    unsafe_caps = gov.trace_eligibility(unsafe_record)["capabilities"]
    assert unsafe_caps["controlled_rerun"]["eligible"] is False
    assert any("白名单" in reason for reason in unsafe_caps["controlled_rerun"]["reasons"])

    no_fault = _build_record(
        "trace-ok", steps=_raw_steps("trace-ok"), status="succeeded"
    )
    no_fault_caps = gov.trace_eligibility(no_fault)["capabilities"]
    assert no_fault_caps["controlled_rerun"]["eligible"] is False
    assert no_fault_caps["repair_candidates"]["eligible"] is False


# ---------------------------------------------------------------------------
# 3. Trace 列表接口（来源 / 状态 / 故障筛选）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def traces_response() -> dict:
    response = client.get("/api/traces")
    assert response.status_code == 200
    return response.json()


def test_traces_list_scope_and_source_fields(traces_response):
    assert traces_response["scope"] == "agent_traces"
    assert traces_response["total_count"] == len(traces_response["traces"])
    assert traces_response["count"] == traces_response["total_count"]
    assert set(traces_response["by_source"]) == set(gov.SOURCE_CLASSES)
    assert sum(traces_response["by_source"].values()) == traces_response["total_count"]
    for row in traces_response["traces"][:50]:
        assert row["source_class"] in gov.SOURCE_CLASSES
        assert isinstance(row["source_reasons"], list)


def test_traces_filter_by_source_and_status(traces_response):
    mock_rows = client.get("/api/traces", params={"source_class": "mock"}).json()
    assert mock_rows["filters"]["source_class"] == "MOCK"
    assert all(row["source_class"] == "MOCK" for row in mock_rows["traces"])
    assert mock_rows["count"] == traces_response["by_source"]["MOCK"]
    assert mock_rows["total_count"] == traces_response["total_count"]

    failed = client.get("/api/traces", params={"status": "failed"}).json()
    assert failed["count"] == traces_response["by_status"].get("failed", 0)
    assert all(row["status"] == "failed" for row in failed["traces"])

    fault_only = client.get("/api/traces", params={"fault_only": "true"}).json()
    assert all(row["fault_types"] for row in fault_only["traces"])
    assert fault_only["count"] <= failed["count"]


def test_traces_invalid_source_class():
    response = client.get("/api/traces", params={"source_class": "bogus"})
    assert response.status_code == 400
    assert "source_class" in (response.json().get("detail") or "")


# ---------------------------------------------------------------------------
# 4. Governance API（汇总 / 失败口径 / 分类 / 资格）
# ---------------------------------------------------------------------------

def _fake_summary() -> dict:
    return {
        "generated_at": "2026-11-19T00:00:00+00:00",
        "governance_version": "1.0.0",
        "scope": {"agent_dir": "data/traces", "evaluation_dir": "data/evaluation/traces"},
        "total_traces": 4,
        "agent_traces": 3,
        "evaluation_traces": 1,
        "by_source": {"REAL_API": 0, "MOCK": 1, "SIMULATED": 2, "REPLAY": 1, "UNKNOWN": 0},
        "by_scope_source": {
            "agent": {"REAL_API": 0, "MOCK": 1, "SIMULATED": 1, "REPLAY": 1, "UNKNOWN": 0},
            "evaluation": {"REAL_API": 0, "MOCK": 0, "SIMULATED": 1, "REPLAY": 0, "UNKNOWN": 0},
        },
        "status_by_source": {"MOCK": {"failed": 1}},
        "fault_traces_by_source": {"REAL_API": 0, "MOCK": 1, "SIMULATED": 0, "REPLAY": 0, "UNKNOWN": 0},
        "unknown_count": 0,
    }


def test_governance_sources_contract(monkeypatch):
    monkeypatch.setattr(gov, "scan_sources", lambda **kwargs: _fake_summary())
    response = client.get("/api/governance/sources")
    assert response.status_code == 200
    data = response.json()
    assert data["source_classes"] == list(gov.SOURCE_CLASSES)
    assert data["definitions"]["REAL_API"] == gov.SOURCE_DEFINITIONS["REAL_API"]
    assert data["paid_api_called"] is False
    assert data["read_only"] is True
    assert data["total_traces"] == 4


def test_governance_failed_summary_consistency(traces_response):
    response = client.get("/api/governance/failed-summary", params={"limit": 20})
    assert response.status_code == 200
    data = response.json()
    assert data["scope"] == "agent_traces"
    assert data["failed_count"] == traces_response["by_status"].get("failed", 0)
    assert sum(data["failed_by_source"].values()) == data["failed_count"]
    assert data["failed_with_fault_injection"] <= data["failed_count"]
    assert data["paid_api_called"] is False
    if data["samples"]:
        sample = data["samples"][0]
        assert sample["eligibility"].startswith("/api/governance/traces/")
        assert sample["detail"].startswith("/api/traces/")


def test_governance_classification_endpoint(traces_response):
    row = traces_response["traces"][0]
    response = client.get(f"/api/governance/traces/{row['trace_id']}")
    assert response.status_code == 200
    classification = response.json()["classification"]
    assert classification["source_class"] == row["source_class"]
    assert classification["trace_id"] == row["trace_id"]
    assert classification["source_definitions"]

    missing = client.get("/api/governance/traces/trace-does-not-exist-55a")
    assert missing.status_code == 404


def test_governance_eligibility_endpoint(traces_response):
    row = traces_response["traces"][0]
    response = client.get(f"/api/governance/traces/{row['trace_id']}/eligibility")
    assert response.status_code == 200
    eligibility = response.json()["eligibility"]
    assert set(eligibility["capabilities"]) == {
        "recorded_replay",
        "controlled_rerun",
        "counterfactual_experiment",
        "repair_candidates",
    }
    for cap in eligibility["capabilities"].values():
        assert isinstance(cap["eligible"], bool)
        assert isinstance(cap["reasons"], list)
        assert isinstance(cap["requirements"], list)
    assert eligibility["read_only"] is True


def test_governance_trace_classification_helper(traces_response):
    from backend.app.services.trace_store import load_trace

    row = traces_response["traces"][0]
    record = load_trace(row["trace_id"])
    result = gov.get_trace_classification(record)
    assert result["trace_id"] == row["trace_id"]
    assert result["source_class"] in gov.SOURCE_CLASSES


# ---------------------------------------------------------------------------
# 5. 成本统计来源范围
# ---------------------------------------------------------------------------

def _price_data() -> dict:
    return {
        "pricing_version": "test-v1",
        "effective_date": "2026-01-01",
        "pricing_source": "test",
        "currency": "CNY",
        "path": "test.yaml",
        "entries": [],
    }


def _cost_block(amount: float | None, status: str) -> dict:
    return {
        "amount": amount,
        "currency": "CNY" if amount is not None else None,
        "status": status,
        "complete": status == "SIMULATED",
        "partial": status != "SIMULATED",
        "pricing_version": "test-v1",
        "pricing_source": "test",
        "effective_date": "2026-01-01",
        "note": None,
        "disclaimer": "d",
    }


def _fake_analysis(trace_id: str, source_class: str) -> dict:
    cost = _cost_block(0.0, "SIMULATED")
    call_cost = {**cost}
    return {
        "trace_id": trace_id,
        "run_id": f"run-{trace_id}",
        "task_id": f"task-{trace_id}",
        "status": "succeeded",
        "data_source": "mock",
        "source_class": source_class,
        "simulated": True,
        "agent_version": None,
        "agent_version_note": None,
        "provider": "local-simulation",
        "model_name": "mock-model",
        "started_at": "2026-11-19T00:00:00+00:00",
        "ended_at": "2026-11-19T00:00:01+00:00",
        "tokens": {
            "prompt_tokens": 4,
            "completion_tokens": 6,
            "total_tokens": 10,
            "known_model_calls": 1,
            "unknown_model_calls": 0,
            "complete": True,
            "usage_sources": {
                "PROVIDER_REPORTED": 0,
                "ESTIMATED": 0,
                "SIMULATED": 1,
                "UNKNOWN": 0,
            },
        },
        "cost": cost,
        "latency": {
            "model_latency_ms": 10,
            "tool_latency_ms": None,
            "total_run_latency_ms": 20,
            "model_latency_avg_ms": 10.0,
            "tool_latency_avg_ms": None,
            "total_run_latency_avg_ms": 20.0,
            "unknown_fields": ["tool_latency_ms"],
        },
        "model_calls": [
            {
                "model_call_id": f"{trace_id}-s002",
                "trace_id": trace_id,
                "run_id": f"run-{trace_id}",
                "step_type": "model_call",
                "step_parent_ids": [],
                "provider": "local-simulation",
                "model_name": "mock-model",
                "prompt_tokens": 4,
                "completion_tokens": 6,
                "total_tokens": 10,
                "usage_source": "SIMULATED",
                "usage_confirmed": True,
                "usage_meta_source": "SIMULATED",
                "raw_usage": None,
                "recorded_at": "2026-11-19T00:00:00+00:00",
                "duration_ms": 10,
                "cost": call_cost,
            }
        ],
        "billing_disclaimer": "d",
        "paid_api_called": False,
        "data_sources": {},
        "limitations": [],
    }


@pytest.fixture
def fake_analyses():
    return [
        _fake_analysis("trace-mock", "MOCK"),
        _fake_analysis("trace-sim", "SIMULATED"),
        _fake_analysis("trace-replay", "REPLAY"),
    ]


def test_cost_overview_source_scoping(monkeypatch, fake_analyses):
    monkeypatch.setattr(ta, "load_pricing", lambda: _price_data())
    monkeypatch.setattr(ta, "_cached_analyses", lambda pricing, force=False: fake_analyses)
    overview = ta.analyze_overview(source_class="mock")
    assert overview["trace_count"] == 1
    assert overview["source_scope"]["selected"] == "MOCK"
    assert overview["source_scope"]["total_trace_count"] == 3
    assert overview["source_scope"]["available_by_source"] == {
        "REAL_API": 0,
        "MOCK": 1,
        "SIMULATED": 1,
        "REPLAY": 1,
        "UNKNOWN": 0,
    }
    assert overview["paid_api_called"] is False

    real = ta.analyze_overview(source_class="REAL_API")
    assert real["trace_count"] == 0
    assert real["tokens"]["total_tokens"] is None
    assert real["cost"]["amount"] is None
    assert real["cost"]["status"] == "UNKNOWN"

    with pytest.raises(ta.CostAnalyticsError):
        ta.analyze_overview(source_class="bogus")


def test_cost_attribution_source_scoping(monkeypatch, fake_analyses):
    monkeypatch.setattr(ta, "load_pricing", lambda: _price_data())
    monkeypatch.setattr(ta, "_cached_analyses", lambda pricing, force=False: fake_analyses)
    mock_rows = ta.cost_attribution(level="trace", source_class="MOCK")
    assert mock_rows["count"] == 1
    assert mock_rows["rows"][0]["id"] == "trace-mock"
    assert mock_rows["source_class_filter"] == "MOCK"

    real_rows = ta.cost_attribution(level="trace", source_class="REAL_API")
    assert real_rows["count"] == 0
    assert real_rows["paid_api_called"] is False

    with pytest.raises(ta.CostAnalyticsError):
        ta.cost_attribution(source_class="bogus")


def test_cost_endpoints_reject_invalid_source_class():
    overview = client.get("/api/cost/overview", params={"source_class": "bogus"})
    assert overview.status_code == 400
    attribution = client.get("/api/cost/attribution", params={"source_class": "bogus"})
    assert attribution.status_code == 400
