"""故障传播路径分析测试（pytest，第 23 步）。"""

import glob
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.schemas.trace import TraceRecord  # noqa: E402
from backend.app.services.failure_propagation import (  # noqa: E402
    analyze_failure_propagation,
)

DATA_TRACES = PROJECT_ROOT / "data" / "traces"
TS = "2026-11-18T12:00:00+00:00"
FAULT = {"type": "tool_timeout", "injected": True, "message": "注入故障"}


def _step(step_id, step_type, parents=(), status="succeeded", fault=None, tool_result=None):
    s = {
        "step_id": step_id, "trace_id": "x", "parent_step_ids": list(parents),
        "step_type": step_type, "timestamp": TS, "status": status, "metadata": {},
    }
    if fault:
        s["metadata"]["fault_injection"] = fault
    if tool_result is not None:
        s["tool_result"] = tool_result
    return s


def _record(trace_id, steps):
    for s in steps:
        s["trace_id"] = trace_id
    return TraceRecord.model_validate({
        "trace_id": trace_id, "task_id": "t", "run_id": "r",
        "started_at": TS, "ended_at": TS, "status": "succeeded",
        "steps": steps, "metadata": {},
    })


def _classify(report):
    return {n["id"]: n["classification"] for n in report["nodes"]}


# ---------------------------------------------------------------------------
# 1. 串行传播
# ---------------------------------------------------------------------------

def test_serial_propagation():
    normal = _record("n", [
        _step("a", "planning"),
        _step("b", "tool_call", ["a"], tool_result={"r": 2}),
        _step("c", "model_call", ["b"]),
    ])
    fault = _record("f", [
        _step("a", "planning"),
        _step("b", "tool_call", ["a"], status="failed", fault=FAULT, tool_result={"error": "x"}),
        _step("c", "model_call", ["b"]),
    ])

    report = analyze_failure_propagation(fault, normal)
    cls = _classify(report)

    assert cls["b"] == "confirmed_fault"
    assert cls["c"] == "potentially_affected"  # b 的后代，且与正常无差异
    assert cls["a"] == "unaffected"
    assert report["paths"] == [{"fault_node": "b", "terminal": "c", "path": ["b", "c"]}]


# ---------------------------------------------------------------------------
# 2. 并行分支
# ---------------------------------------------------------------------------

def test_parallel_branches():
    normal = _record("n", [
        _step("a", "planning"),
        _step("b", "tool_call", ["a"], tool_result={"r": 1}),
        _step("c", "tool_call", ["a"], tool_result={"r": 2}),
    ])
    fault = _record("f", [
        _step("a", "planning", fault=FAULT, status="failed"),
        _step("b", "tool_call", ["a"], tool_result={"r": 1}),
        _step("c", "tool_call", ["a"], tool_result={"r": 2}),
    ])

    report = analyze_failure_propagation(fault, normal)
    cls = _classify(report)

    assert cls["a"] == "confirmed_fault"
    assert cls["b"] == "potentially_affected"
    assert cls["c"] == "potentially_affected"
    terminals = {p["terminal"] for p in report["paths"]}
    assert terminals == {"b", "c"}


# ---------------------------------------------------------------------------
# 3. 多故障节点（菱形图）
# ---------------------------------------------------------------------------

def test_multiple_fault_nodes():
    normal = _record("n", [
        _step("a", "planning"),
        _step("b", "tool_call", ["a"], tool_result={"r": 1}),
        _step("c", "tool_call", ["a"], tool_result={"r": 2}),
        _step("d", "model_call", ["b", "c"]),
    ])
    fault = _record("f", [
        _step("a", "planning"),
        _step("b", "tool_call", ["a"], status="failed", fault=FAULT, tool_result={"e": 1}),
        _step("c", "tool_call", ["a"], status="failed", fault=FAULT, tool_result={"e": 2}),
        _step("d", "model_call", ["b", "c"]),
    ])

    report = analyze_failure_propagation(fault, normal)
    cls = _classify(report)
    s = report["summary"]

    assert set(s["confirmed_fault"]) == {"b", "c"}
    assert cls["d"] == "potentially_affected"  # 多父汇合节点，两个故障共同的后代
    assert cls["a"] == "unaffected"


# ---------------------------------------------------------------------------
# 4. 无后代节点（故障在叶节点）
# ---------------------------------------------------------------------------

def test_no_descendants():
    normal = _record("n", [
        _step("a", "planning"),
        _step("b", "tool_call", ["a"], tool_result={"r": 1}),
    ])
    fault = _record("f", [
        _step("a", "planning"),
        _step("b", "tool_call", ["a"], status="failed", fault=FAULT, tool_result={"e": 1}),
    ])

    report = analyze_failure_propagation(fault, normal)
    cls = _classify(report)

    assert cls["b"] == "confirmed_fault"
    assert cls["a"] == "unaffected"
    assert report["summary"]["potentially_affected"] == []
    assert report["paths"] == [{"fault_node": "b", "terminal": "b", "path": ["b"]}]


# ---------------------------------------------------------------------------
# 5. 无法匹配的步骤 -> unknown
# ---------------------------------------------------------------------------

def test_unmatched_steps_unknown():
    normal = _record("n", [
        _step("a", "planning"),
        _step("b", "tool_call", ["a"], tool_result={"r": 1}),
    ])
    fault = _record("f", [
        _step("a", "planning"),
        _step("c", "tool_call", ["a"], fault=FAULT, status="failed", tool_result={"e": 1}),
    ])

    report = analyze_failure_propagation(fault, normal)
    cls = _classify(report)

    assert cls["c"] == "confirmed_fault"  # 故障节点优先
    assert cls["a"] == "unaffected"
    assert "c" not in report["summary"]["unknown"]


# ---------------------------------------------------------------------------
# 集成：第 21 步真实本地 Trace
# ---------------------------------------------------------------------------

def test_step21_propagation():
    # 使用固定 trace_id 的 fixture，避免被 data/traces 中新增的运行记录干扰
    normal_path = DATA_TRACES / "trace-bf1f2e2690f743379fadc80d22e03adf.json"
    fault_path = DATA_TRACES / "trace-3071fea861694ee1b70731a400f28000.json"
    if not (normal_path.exists() and fault_path.exists()):
        pytest.skip("缺少第 21 步 fixture Trace")
    normal = TraceRecord.model_validate_json(normal_path.read_text(encoding="utf-8"))
    fault = TraceRecord.model_validate_json(fault_path.read_text(encoding="utf-8"))
    report = analyze_failure_propagation(fault, normal)
    cls = _classify(report)

    fault_nodes = [n for n in report["nodes"] if n["is_fault"]]
    assert len(fault_nodes) == 1
    fn = fault_nodes[0]
    assert fn["fault_type"] == "tool_timeout"
    assert fn["classification"] == "confirmed_fault"

    # 故障节点的后代（最终回答步骤）为 potentially_affected
    assert fn["id"].endswith("-s004")
    assert cls[fn["id"].replace("-s004", "-s005")] == "potentially_affected"
