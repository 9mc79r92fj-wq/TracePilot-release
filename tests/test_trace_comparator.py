"""Trace 对比分析测试（pytest，第 22 步）。

使用第 21 步真实本地执行生成的两份 Trace（正常 / tool_timeout 故障）验证对比逻辑，
并补充匹配策略、unknown 处理、JSON 序列化等单元测试。不调用 DeepSeek API。
"""

import glob
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.schemas.trace import TraceRecord  # noqa: E402
from backend.app.services.trace_comparator import (  # noqa: E402
    compare_traces,
    compare_trace_files,
)

DATA_TRACES = Path(__file__).resolve().parent / "fixtures" / "traces"


# 第 21 步固定 fixture Trace（显式指定，避免被 data/traces 中新增运行记录干扰）
STEP21_NORMAL_ID = "trace-bf1f2e2690f743379fadc80d22e03adf"
STEP21_FAULT_ID = "trace-3071fea861694ee1b70731a400f28000"


def _load_by_id(trace_id: str):
    p = DATA_TRACES / f"{trace_id}.json"
    if not p.exists():
        return None
    return TraceRecord.model_validate_json(p.read_text(encoding="utf-8"))


def _load_step21_pair():
    """加载第 21 步生成的正常与故障 Trace（按固定 trace_id，避免误取其他 Trace）。"""
    return _load_by_id(STEP21_NORMAL_ID), _load_by_id(STEP21_FAULT_ID)


def _record(trace_id, steps, status="succeeded", metadata=None):
    return TraceRecord.model_validate({
        "trace_id": trace_id, "task_id": "task-x", "run_id": "run-x",
        "started_at": "2026-11-18T12:00:00+00:00",
        "ended_at": "2026-11-18T12:00:05+00:00",
        "status": status, "steps": steps, "metadata": metadata or {},
    })


def _step(step_id, step_type, tool_name=None, status="succeeded", tool_result=None, token=None):
    s = {
        "step_id": step_id, "trace_id": "x", "parent_step_ids": [],
        "step_type": step_type, "timestamp": "2026-11-18T12:00:00+00:00", "status": status,
    }
    if tool_name:
        s["tool_name"] = tool_name
    if tool_result is not None:
        s["tool_result"] = tool_result
    if token is not None:
        s["token_usage"] = token
    return s


# ---------------------------------------------------------------------------
# 主集成：第 21 步两份 Trace 对比
# ---------------------------------------------------------------------------

def test_compare_step21_traces():
    normal, fault = _load_step21_pair()
    assert normal is not None and fault is not None

    report = compare_traces(normal, fault)

    # 步骤全部可靠匹配（同类型同工具，数量一致）
    assert report["unmatched"]["reference_only"] == []
    assert report["unmatched"]["target_only"] == []

    # 差异节点：工具调用步骤
    assert len(report["diff_nodes"]) >= 1
    tool_node = [n for n in report["diff_nodes"] if "tool_call" in n["match_key"]][0]
    assert tool_node["step_id"].endswith("-s004")
    assert "tool_result" in tool_node["differences"]
    assert "error_type" in tool_node["differences"]
    # 上下游依赖：上游为触发它的模型调用步骤，下游为最终回答步骤
    assert len(tool_node["upstream"]) >= 1
    assert len(tool_node["downstream"]) >= 1

    # 最早差异 = 该工具步骤
    assert report["earliest_difference"]["step_id"].endswith("-s004")
    assert "不构成根因" in report["earliest_difference"]["note"]

    # 故障与恢复
    assert report["differences"]["fault_types"] == {"ref": [], "tgt": ["tool_timeout"]}
    assert report["differences"]["recovery_observed"]["tgt"] is True

    # 三类结论分区存在
    assert report["observations"]
    assert report["inferred_impacts"]
    assert report["unverified"]

    # 可 JSON 序列化
    json.dumps(report, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 匹配策略：不依赖 step_id，按 step_type + tool_name + 次序
# ---------------------------------------------------------------------------

def test_matching_by_semantics_not_step_id():
    ref = _record("t-ref", [
        _step("a-1", "tool_call", tool_name="calculator", tool_result={"r": 2}),
        _step("a-2", "model_call"),
    ])
    tgt = _record("t-tgt", [
        _step("b-1", "tool_call", tool_name="calculator", tool_result={"r": 3}),
        _step("b-2", "model_call"),
    ])
    report = compare_traces(ref, tgt)
    assert report["unmatched"]["reference_only"] == []
    assert report["unmatched"]["target_only"] == []
    # 工具结果差异被捕捉
    assert report["diff_nodes"][0]["differences"]["tool_result"] == {"ref": {"r": 2}, "tgt": {"r": 3}}


def test_unmatched_when_tool_name_differs():
    ref = _record("t-ref", [_step("a-1", "tool_call", tool_name="calculator", tool_result={"r": 2})])
    tgt = _record("t-tgt", [_step("b-1", "tool_call", tool_name="sqlite_query", tool_result={"r": 2})])
    report = compare_traces(ref, tgt)
    assert report["unmatched"]["reference_only"] == ["a-1"]
    assert report["unmatched"]["target_only"] == ["b-1"]


# ---------------------------------------------------------------------------
# unknown 处理：Token 未采集时用 null，不填 0
# ---------------------------------------------------------------------------

def test_unknown_token_not_padded_to_zero():
    ref = _record("t-ref", [_step("a-1", "model_call", token={"input_tokens": 1, "output_tokens": 1})])
    tgt = _record("t-tgt", [_step("b-1", "model_call", token=None)])
    report = compare_traces(ref, tgt)
    assert report["traces"]["target"]["token_usage"]["sum"] is None
    assert report["traces"]["target"]["token_usage"]["has_unknown"] is True


def test_earliest_difference_not_claimed_as_root_cause():
    ref = _record("t-ref", [_step("a-1", "model_call")])
    tgt = _record("t-tgt", [_step("b-1", "model_call", status="failed")])
    report = compare_traces(ref, tgt)
    assert report["earliest_difference"]["step_id"] == "b-1"
    assert "不构成根因" in report["earliest_difference"]["note"]
    assert any("反事实" in u for u in report["unverified"])
