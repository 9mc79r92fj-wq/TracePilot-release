"""Failure Replay 测试（pytest，第 30 步）。"""

import glob
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.schemas.trace import TraceRecord  # noqa: E402
from backend.app.services.failure_replay import (  # noqa: E402
    SCENARIO_WHITELIST,
    UnsupportedScenarioError,
    controlled_rerun,
    recorded_replay,
)

DATA_TRACES = PROJECT_ROOT / "data" / "traces"


def _fault_trace() -> TraceRecord:
    for p in sorted(glob.glob(str(DATA_TRACES / "*.json"))):
        r = TraceRecord.model_validate_json(Path(p).read_text(encoding="utf-8"))
        if any(
            s.metadata.get("fault_injection", {}).get("injected") is True for s in r.steps
        ):
            return r
    pytest.skip("缺少故障 Trace 数据")


def test_scenario_whitelist_is_safe():
    assert set(SCENARIO_WHITELIST) == {"calculator_add", "calculator_div_zero", "text_analyzer"}
    for meta in SCENARIO_WHITELIST.values():
        # 只允许 calculator / text_analyzer，禁止 subprocess / 文件 / 网络
        assert meta["tool"] in {"calculator", "text_analyzer"}


def test_recorded_replay_reads_data():
    trace = _fault_trace()
    r = recorded_replay(trace)
    assert r["mode"] == "recorded"
    assert r["trace_id"] == trace.trace_id
    assert r["step_count"] == len(trace.steps)
    assert r["executed_anything"] is False
    assert len(r["fault_event_indices"]) >= 1
    fault_ev = r["events"][r["first_fault_index"]]
    assert fault_ev["is_fault"] is True
    assert fault_ev["fault_type"] == "tool_timeout"
    assert fault_ev["error_type"] == "tool_timeout"


def test_controlled_rerun_generates_new_trace():
    trace = _fault_trace()
    r = controlled_rerun(trace)
    assert r["mode"] == "controlled"
    assert r["new_trace_id"] != trace.trace_id
    assert r["new_trace_id"].startswith("trace-replay-")
    assert r["old_trace_id"] == trace.trace_id
    assert len(r["trace"]["steps"]) >= 3
    assert r["trace"]["run_id"] != trace.run_id


def test_fault_type_reproduced():
    trace = _fault_trace()
    r = controlled_rerun(trace)
    assert "tool_timeout" in r["repeatable_faults"]
    new_steps = r["trace"]["steps"]
    fault_steps = [s for s in new_steps if s["metadata"].get("fault_injection")]
    assert fault_steps, "重放 Trace 中应有故障标记步骤"
    assert fault_steps[0]["metadata"]["fault_injection"]["type"] == "tool_timeout"


def test_original_trace_not_modified():
    trace = _fault_trace()
    before = trace.model_dump_json()
    controlled_rerun(trace)
    after = trace.model_dump_json()
    assert before == after, "原始 Trace 不得被修改"


def test_no_dangerous_tools_executed():
    trace = _fault_trace()
    r = controlled_rerun(trace)
    assert r["called_paid_api"] is False
    for tool in r["executed_real_tools"]:
        assert tool in {"calculator", "text_analyzer"}
    for step in r["trace"]["steps"]:
        assert step["step_type"] in {"planning", "model_call", "tool_call"}
        assert step.get("tool_name") in (None, "calculator", "text_analyzer")


def test_result_comparable():
    trace = _fault_trace()
    r = controlled_rerun(trace)
    cmp = r["comparison"]
    assert "differences" in cmp
    assert "diff_nodes" in cmp
    assert cmp["traces"]["reference"]["trace_id"] == trace.trace_id
    assert cmp["traces"]["target"]["trace_id"] == r["new_trace_id"]


def test_unsupported_scenario_rejected():
    trace = _fault_trace()
    with pytest.raises(UnsupportedScenarioError):
        controlled_rerun(trace, scenario="rm_rf_system")


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------

def test_api_replay_endpoints():
    from fastapi.testclient import TestClient

    from backend.app.main import app

    client = TestClient(app)
    trace = _fault_trace()

    s = client.get("/api/replay/scenarios")
    assert s.status_code == 200
    assert len(s.json()["scenarios"]) == 3

    r1 = client.post("/api/replay/recorded", json={"trace_id": trace.trace_id})
    assert r1.status_code == 200
    assert r1.json()["replay"]["mode"] == "recorded"

    r2 = client.post("/api/replay/controlled", json={"trace_id": trace.trace_id})
    assert r2.status_code == 200
    assert r2.json()["replay"]["new_trace_id"].startswith("trace-replay-")

    bad = client.post("/api/replay/controlled", json={"trace_id": trace.trace_id, "scenario": "evil"})
    assert bad.status_code == 400
