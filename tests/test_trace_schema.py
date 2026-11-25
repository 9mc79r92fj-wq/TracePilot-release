"""Trace Schema 最小自动化测试集（pytest）。

覆盖第 9 步验收标准：
1. 正常 Trace 能够通过 Pydantic 验证；
2. Trace 能够序列化为 JSON；
3. JSON 能够重新解析为 Trace 对象；
4. 错误字段类型能够被拒绝；
5. 多父节点依赖关系能够正确保存；
6. TokenUsage 字段能够正确验证。
"""

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# 将项目根目录加入 sys.path，保证 `backend.app.schemas.trace` 可导入
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.schemas.trace import (  # noqa: E402
    StepStatus,
    StepType,
    TokenUsage,
    TraceRecord,
    TraceStatus,
)

SAMPLE_PATH = PROJECT_ROOT / "examples" / "sample_trace.json"


def load_sample() -> dict:
    """读取示例 Trace JSON。"""
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def make_valid_record_dict() -> dict:
    """构造一份最小的合法 TraceRecord 字典，供错误注入测试复用。"""
    return {
        "trace_id": "trace-test-001",
        "task_id": "task-test",
        "run_id": "run-test-1",
        "started_at": "2026-11-18T09:00:00Z",
        "ended_at": "2026-11-18T09:00:05Z",
        "status": "succeeded",
        "steps": [
            {
                "step_id": "step-1",
                "trace_id": "trace-test-001",
                "parent_step_ids": [],
                "step_type": "planning",
                "timestamp": "2026-11-18T09:00:01Z",
                "input": {"task": "测试任务"},
                "output": {"plan": ["step-2"]},
                "status": "succeeded",
            }
        ],
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# 1. 正常 Trace 通过 Pydantic 验证
# ---------------------------------------------------------------------------

def test_sample_trace_validates():
    """示例 sample_trace.json 能够通过 TraceRecord 验证，且核心字段正确。"""
    record = TraceRecord.model_validate(load_sample())

    assert record.trace_id == "trace-sales-001"
    assert record.task_id == "task-sales-analysis"
    assert record.run_id == "run-001"
    assert record.status == TraceStatus.SUCCEEDED
    assert len(record.steps) == 3
    assert [s.step_type for s in record.steps] == [
        StepType.PLANNING,
        StepType.TOOL_CALL,
        StepType.MODEL_CALL,
    ]


def test_sample_step_dependency_chain():
    """示例三步之间具有明确的串行依赖：step-1 ← step-2 ← step-3。"""
    record = TraceRecord.model_validate(load_sample())
    steps = {s.step_id: s for s in record.steps}

    assert steps["step-1"].parent_step_ids == []
    assert steps["step-2"].parent_step_ids == ["step-1"]
    assert steps["step-3"].parent_step_ids == ["step-2"]
    assert steps["step-2"].tool_name == "sql_query"
    assert steps["step-2"].status == StepStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# 2/3. JSON 序列化与反序列化往返
# ---------------------------------------------------------------------------

def test_serialize_to_json():
    """Trace 可序列化为 JSON 字符串，枚举呈现为字符串值。"""
    record = TraceRecord.model_validate(load_sample())
    raw = record.model_dump_json()

    assert isinstance(raw, str)
    data = json.loads(raw)
    assert data["status"] == "succeeded"
    assert data["steps"][1]["tool_name"] == "sql_query"
    assert data["steps"][0]["token_usage"]["total_tokens"] == 270


def test_json_roundtrip():
    """JSON → 对象 → JSON → 对象 往返后内容一致。"""
    original = TraceRecord.model_validate(load_sample())
    reparsed = TraceRecord.model_validate_json(original.model_dump_json())

    assert reparsed == original
    assert reparsed.steps == original.steps


# ---------------------------------------------------------------------------
# 4. 错误字段类型被拒绝
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("steps", "not-a-list"),
        ("steps", 123),
        ("status", "unknown_status"),
        ("status", 123),
        ("started_at", "not-a-timestamp"),
        ("started_at", ""),
        ("trace_id", 42),
        ("metadata", ["not", "a", "dict"]),
    ],
)
def test_invalid_field_type_rejected(field, bad_value):
    """错误字段类型 / 非法枚举值应抛出 ValidationError。"""
    data = make_valid_record_dict()
    data[field] = bad_value

    with pytest.raises(ValidationError):
        TraceRecord.model_validate(data)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("step_type", "not_a_step_type"),
        ("parent_step_ids", "step-1"),
        ("duration_ms", -5),
        ("duration_ms", "abc"),
        ("token_usage", {"input_tokens": "many"}),
    ],
)
def test_invalid_step_field_rejected(field, bad_value):
    """错误步骤字段类型 / 非法值应抛出 ValidationError。"""
    data = make_valid_record_dict()
    data["steps"][0][field] = bad_value

    with pytest.raises(ValidationError):
        TraceRecord.model_validate(data)


# ---------------------------------------------------------------------------
# 5. 多父节点依赖关系正确保存
# ---------------------------------------------------------------------------

def test_multi_parent_dependency_preserved():
    """一个步骤依赖多个前置步骤时，parent_step_ids 序列化往返后完整保留。"""
    data = make_valid_record_dict()
    data["steps"][0]["parent_step_ids"] = ["step-a", "step-b"]

    record = TraceRecord.model_validate(data)
    assert record.steps[0].parent_step_ids == ["step-a", "step-b"]

    reparsed = TraceRecord.model_validate_json(record.model_dump_json())
    assert reparsed.steps[0].parent_step_ids == ["step-a", "step-b"]


# ---------------------------------------------------------------------------
# 6. TokenUsage 字段正确验证
# ---------------------------------------------------------------------------

def test_token_usage_auto_total():
    """未显式提供 total_tokens 时，自动按 input + output 计算。"""
    usage = TokenUsage(input_tokens=100, output_tokens=50)
    assert usage.total_tokens == 150


def test_token_usage_explicit_total_respected():
    """显式提供 total_tokens 时，保留调用方给出的值。"""
    usage = TokenUsage(input_tokens=100, output_tokens=50, total_tokens=200)
    assert usage.total_tokens == 200


def test_token_usage_negative_rejected():
    """负数 token 应被拒绝。"""
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=-1)
    with pytest.raises(ValidationError):
        TokenUsage(output_tokens=-1, total_tokens=-1)
