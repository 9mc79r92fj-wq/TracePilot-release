"""QueryForge Trace 互操作（协议 v1）测试。

覆盖：
1. 合法示例通过校验并映射为 TraceRecord（event_type -> StepType，metadata 保留原类型）；
2. 非法父节点 / 重复 span_id / 自引用 / 成环 / 伪 token / 未知事件 被拒绝；
3. token 用量缺失 / 全 0 / 部分缺失不得被补 0（保持 unknown 并留档）；
4. 映射结果可构建 DAG、步骤时间顺序正确；
5. 导入写入 tmp_path（隔离，不污染 data/traces），可通过 trace_store.load_trace 读取，
   metadata 标注来源 queryforge；同名 trace_id 冲突拒绝且不覆盖；非法 trace_id 安全重命名；
6. JSON Schema 文件与 Pydantic 模型一致。
"""

from __future__ import annotations

import copy
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402

from backend.app.schemas.queryforge_trace import (  # noqa: E402
    QUERYFORGE_SCHEMA_VERSION,
    QueryForgeEventType,
    QueryForgeTraceV1,
)
from backend.app.schemas.trace import (  # noqa: E402
    SCHEMA_VERSION,
    StepStatus,
    StepType,
    TraceRecord,
    TraceStatus,
)
from backend.app.services.data_governance import classify_metadata  # noqa: E402
from backend.app.services.trace_dag import build_dag  # noqa: E402
from backend.app.services.trace_interop import (  # noqa: E402
    EVENT_TYPE_TO_STEP_TYPE,
    TOKEN_STATUS_COMPLETE,
    TOKEN_STATUS_NOT_APPLICABLE,
    TOKEN_STATUS_PARTIAL,
    TOKEN_STATUS_UNKNOWN,
    ImportedTrace,
    TraceIdConflictError,
    TraceInteropError,
    TracePayloadError,
    TraceValidationError,
    import_queryforge_trace,
    to_trace_record,
    validate_queryforge_trace,
)
from backend.app.services.trace_store import load_trace  # noqa: E402

EXAMPLE_PATH = PROJECT_ROOT / "examples" / "interop" / "queryforge_trace_example.json"
INVALID_EXAMPLE_PATH = (
    PROJECT_ROOT / "examples" / "interop" / "queryforge_trace_invalid_example.json"
)
SCHEMA_PATH = PROJECT_ROOT / "docs" / "schemas" / "queryforge_trace_v1.schema.json"

_SAFE_TRACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def example_payload() -> dict:
    """读取合法示例 JSON（shape-only，非真实运行数据）。"""
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def _payload(example_payload: dict) -> dict:
    """深拷贝示例，供错误注入测试安全修改。"""
    return copy.deepcopy(example_payload)


def _base_payload() -> dict:
    """构造最小合法协议 v1 载荷（2 个 span：planning -> model_call）。"""
    return {
        "trace_id": "trace-qf-unit-001",
        "schema_version": "1.0",
        "task_id": "task-qf-unit",
        "run_id": "run-qf-unit-1",
        "execution_mode": "simulated",
        "model": "deepseek-flash",
        "started_at": "2026-11-25T00:00:00Z",
        "ended_at": "2026-11-25T00:00:05Z",
        "status": "succeeded",
        "attributes": {"example": True, "note": "unit-test fixture"},
        "spans": [
            {
                "span_id": "s1",
                "parent_span_id": None,
                "event_type": "planning",
                "name": "route_query",
                "started_at": "2026-11-25T00:00:00.100Z",
                "latency_ms": 1.0,
                "input": {"query": "q"},
                "output": {"query_type": "balanced"},
                "attributes": {"router": "query_router"},
                "token_usage": None,
                "error": None,
                "status": "succeeded",
            },
            {
                "span_id": "s2",
                "parent_span_id": "s1",
                "event_type": "model_call",
                "name": "deepseek-flash/generate",
                "started_at": "2026-11-25T00:00:00.200Z",
                "latency_ms": 800.4,
                "input": {"messages": [{"role": "user", "content": "q"}]},
                "output": {"answer": "a"},
                "attributes": {"model_provider": "deepseek"},
                "token_usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
                "error": None,
                "status": "succeeded",
            },
        ],
    }


def _step_by_id(record: TraceRecord, step_id: str):
    matches = [step for step in record.steps if step.step_id == step_id]
    assert len(matches) == 1, f"step {step_id!r} 不存在或重复"
    return matches[0]


# ---------------------------------------------------------------------------
# 1. 合法示例：校验、来源元数据与映射
# ---------------------------------------------------------------------------


def test_valid_example_passes_and_returns_imported_trace(example_payload):
    imported = validate_queryforge_trace(example_payload)

    assert isinstance(imported, ImportedTrace)
    assert isinstance(imported.record, TraceRecord)
    assert imported.source == "queryforge"
    assert imported.schema_version == QUERYFORGE_SCHEMA_VERSION == "1.0"
    assert imported.original_trace_id == example_payload["trace_id"]
    assert imported.record.trace_id == example_payload["trace_id"]
    assert imported.warnings == []
    assert imported.saved_path is None  # 纯校验不写文件


def test_example_record_metadata_marks_source(example_payload):
    record = validate_queryforge_trace(example_payload).record

    assert record.metadata["source"] == "queryforge"
    assert record.metadata["imported"] is True
    assert record.metadata["schema_version"] == SCHEMA_VERSION  # TracePilot 内部 Schema 版本
    assert record.metadata["data_source"] == "simulated"  # 示例明确为模拟数据

    qf = record.metadata["queryforge"]
    assert qf["schema_version"] == "1.0"
    assert qf["execution_mode"] == "simulated"
    assert qf["model"] == "deepseek-flash"
    assert qf["original_trace_id"] == example_payload["trace_id"]
    assert qf["span_count"] == len(example_payload["spans"])
    assert qf["attributes"] == example_payload["attributes"]
    assert datetime.fromisoformat(qf["imported_at"]).tzinfo is not None
    assert qf["warnings"] == []

    # 来源分类（只读派生）应归 SIMULATED，而不是 REAL_API
    assert classify_metadata(record.metadata)["source_class"] == "SIMULATED"


def test_example_spans_map_to_trace_steps(example_payload):
    record = validate_queryforge_trace(example_payload).record

    assert len(record.steps) == len(example_payload["spans"])
    spans_by_id = {span["span_id"]: span for span in example_payload["spans"]}
    assert {step.step_id for step in record.steps} == set(spans_by_id)
    for step in record.steps:
        assert step.trace_id == record.trace_id
        span = spans_by_id[step.step_id]
        assert step.parent_step_ids == (
            [span["parent_span_id"]] if span["parent_span_id"] else []
        )
        assert step.timestamp == datetime.fromisoformat(
            span["started_at"].replace("Z", "+00:00")
        )


def test_event_type_mapping_table_is_explicit():
    expected = {
        "planning": "planning",
        "model_call": "model_call",
        "tool_call": "tool_call",
        "retrieval": "retrieval",
        "rerank": "tool_call",  # 重排映射为 tool_call，原始类型保留在 metadata
        "citation_validation": "evaluation",
        "reflection": "reflection",
    }
    assert set(EVENT_TYPE_TO_STEP_TYPE) == set(QueryForgeEventType)
    assert {
        event.value: step_type.value for event, step_type in EVENT_TYPE_TO_STEP_TYPE.items()
    } == expected


def test_example_step_types_match_mapping(example_payload):
    record = validate_queryforge_trace(example_payload).record
    spans_by_id = {span["span_id"]: span for span in example_payload["spans"]}

    for step in record.steps:
        event_type = QueryForgeEventType(spans_by_id[step.step_id]["event_type"])
        assert step.step_type is EVENT_TYPE_TO_STEP_TYPE[event_type]
        assert step.metadata["queryforge"]["event_type"] == event_type.value


def test_reflection_event_maps_to_reflection_step():
    payload = _base_payload()
    payload["spans"][1]["event_type"] = "reflection"
    payload["spans"][1]["name"] = "self_reflection"

    record = to_trace_record(payload)
    step = _step_by_id(record, "s2")
    assert step.step_type is StepType.REFLECTION
    assert step.metadata["queryforge"]["event_type"] == "reflection"


def test_example_metadata_preserves_attributes_and_original_latency(example_payload):
    record = validate_queryforge_trace(example_payload).record
    spans_by_id = {span["span_id"]: span for span in example_payload["spans"]}

    retrieval = _step_by_id(record, "span-qf-ret-001")
    assert retrieval.step_type is StepType.RETRIEVAL
    assert retrieval.tool_name == "hybrid_retrieval"
    assert retrieval.metadata["queryforge"]["attributes"] == spans_by_id[
        "span-qf-ret-001"
    ]["attributes"]
    assert retrieval.metadata["queryforge"]["latency_ms"] == pytest.approx(96.3)
    assert retrieval.duration_ms == 96  # 毫秒取整，原始小数保留在 metadata

    rerank = _step_by_id(record, "span-qf-rerank-001")
    assert rerank.step_type is StepType.TOOL_CALL
    assert rerank.metadata["queryforge"]["event_type"] == "rerank"
    assert rerank.tool_name == "BAAI/bge-reranker-base"

    citation = _step_by_id(record, "span-qf-cite-001")
    assert citation.step_type is StepType.EVALUATION
    assert citation.metadata["queryforge"]["event_type"] == "citation_validation"


def test_example_token_usage_mapping(example_payload):
    record = validate_queryforge_trace(example_payload).record

    model_001 = _step_by_id(record, "span-qf-model-001")
    assert model_001.token_usage is not None
    assert model_001.token_usage.input_tokens == 812
    assert model_001.token_usage.output_tokens == 64
    assert model_001.token_usage.total_tokens == 876
    assert model_001.metadata["queryforge"]["token_usage_status"] == TOKEN_STATUS_COMPLETE

    model_002 = _step_by_id(record, "span-qf-model-002")
    assert model_002.token_usage.total_tokens == 1676

    # 非 token 型事件：token_usage 必须为 None（不填 0），且标记 not_applicable
    for step_id in ("span-qf-tool-001", "span-qf-ret-001", "span-qf-rerank-001",
                    "span-qf-cite-001"):
        step = _step_by_id(record, step_id)
        assert step.token_usage is None
        assert (
            step.metadata["queryforge"]["token_usage_status"]
            == TOKEN_STATUS_NOT_APPLICABLE
        )

    # planning 允许携带 token，但示例未提供 -> unknown（不是 0）
    planning = _step_by_id(record, "span-qf-plan-001")
    assert planning.token_usage is None
    assert planning.metadata["queryforge"]["token_usage_status"] == TOKEN_STATUS_UNKNOWN
    assert record.metadata["queryforge"]["token_usage_unknown_spans"] == [
        "span-qf-plan-001"
    ]


def test_example_dag_is_buildable(example_payload):
    record = validate_queryforge_trace(example_payload).record
    dag = build_dag(record)

    assert dag.node_count == len(record.steps) == 7
    assert dag.edge_count == 6  # v1 为树：7 节点 6 条父->子边
    assert len(dag.topological_order) == dag.node_count  # 无环
    assert dag.root_nodes == ["span-qf-plan-001"]
    assert dag.leaf_nodes == ["span-qf-cite-001"]


def test_example_step_order_is_topological_and_temporal(example_payload):
    record = validate_queryforge_trace(example_payload).record
    index = {step.step_id: position for position, step in enumerate(record.steps)}

    previous_timestamp = record.steps[0].timestamp
    for step in record.steps:
        assert step.timestamp >= previous_timestamp  # 时间顺序正确
        previous_timestamp = step.timestamp
        for parent_id in step.parent_step_ids:
            assert index[parent_id] < index[step.step_id]  # 父节点先于子节点


def test_parallel_branch_dag_is_buildable():
    """同一父节点下多个并行子步骤（fan-out）可构建 DAG（v1 为树，不支持 fan-in）。"""
    payload = _base_payload()
    payload["spans"].append(
        {
            "span_id": "s3",
            "parent_span_id": "s1",  # 与 s2 并行，均依赖 s1
            "event_type": "retrieval",
            "name": "hybrid_retrieval",
            "started_at": "2026-11-25T00:00:00.150Z",
            "latency_ms": 20.0,
            "input": {"query": "q", "mode": "adaptive"},
            "output": {"effective_mode": "hybrid", "hits": []},
            "attributes": {"rrf_k": 60},
            "token_usage": None,
            "error": None,
            "status": "succeeded",
        }
    )

    record = to_trace_record(payload)
    dag = build_dag(record)

    assert dag.node_count == 3
    assert dag.edge_count == 2
    assert dag.root_nodes == ["s1"]
    assert sorted(dag.leaf_nodes) == ["s2", "s3"]
    assert len(dag.topological_order) == 3
    assert set(dag.ancestors("s2")) == {"s1"}
    assert set(dag.descendants("s1")) == {"s2", "s3"}


def test_to_trace_record_accepts_payload_and_imported(example_payload):
    imported = validate_queryforge_trace(example_payload)
    from_payload = to_trace_record(example_payload)
    from_imported = to_trace_record(imported)

    assert isinstance(from_payload, TraceRecord)
    # imported_at 是导入时间戳，两次独立校验必然不同；比较除该字段外的完整记录。
    left = from_payload.model_dump(mode="json")
    right = imported.record.model_dump(mode="json")
    left["metadata"].get("queryforge", {}).pop("imported_at", None)
    right["metadata"].get("queryforge", {}).pop("imported_at", None)
    assert left == right
    assert from_imported is imported.record


def test_validate_accepts_json_string_and_bytes(example_payload):
    text = json.dumps(example_payload, ensure_ascii=False)
    assert validate_queryforge_trace(text).record.trace_id == example_payload["trace_id"]
    assert (
        validate_queryforge_trace(text.encode("utf-8")).record.trace_id
        == example_payload["trace_id"]
    )


def test_example_payload_is_clearly_marked_as_example(example_payload):
    assert example_payload["attributes"]["example"] is True
    assert example_payload["trace_id"].startswith("qf-example-")
    assert "NOT a real QueryForge run" in example_payload["attributes"]["note"]
    assert example_payload["execution_mode"] == "simulated"


# ---------------------------------------------------------------------------
# 2. 协议校验：必填 / 未知字段 / 枚举 / 时间 / 结构
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "trace_id",
        "schema_version",
        "task_id",
        "run_id",
        "execution_mode",
        "model",
        "started_at",
        "status",
        "spans",
    ],
)
def test_missing_required_field_rejected(field):
    payload = _base_payload()
    del payload[field]
    with pytest.raises(TraceValidationError):
        validate_queryforge_trace(payload)


def test_unknown_top_level_field_rejected():
    payload = _base_payload()
    payload["unknown_field"] = "x"
    with pytest.raises(TraceValidationError, match="unknown_field"):
        validate_queryforge_trace(payload)


def test_unknown_span_field_rejected():
    payload = _base_payload()
    payload["spans"][0]["unknown_span_field"] = "x"
    with pytest.raises(TraceValidationError, match="unknown_span_field"):
        validate_queryforge_trace(payload)


@pytest.mark.parametrize("version", ["1.0.0", "2.0", "v1", ""])
def test_wrong_schema_version_rejected(version):
    payload = _base_payload()
    payload["schema_version"] = version
    with pytest.raises(TraceValidationError, match="schema_version"):
        validate_queryforge_trace(payload)


@pytest.mark.parametrize("event_type", ["guardrail_check", "MEMORY_READ", ""])
def test_unknown_event_type_rejected(event_type):
    payload = _base_payload()
    payload["spans"][0]["event_type"] = event_type
    with pytest.raises(TraceValidationError, match="event_type"):
        validate_queryforge_trace(payload)


def test_invalid_execution_mode_rejected():
    payload = _base_payload()
    payload["execution_mode"] = "production"
    with pytest.raises(TraceValidationError, match="execution_mode"):
        validate_queryforge_trace(payload)


@pytest.mark.parametrize(
    "bad_started_at",
    ["2026-11-25T00:00:00", "not-a-timestamp", "2026-11-25"],
)
def test_invalid_or_naive_started_at_rejected(bad_started_at):
    payload = _base_payload()
    payload["started_at"] = bad_started_at
    with pytest.raises(TraceValidationError):
        validate_queryforge_trace(payload)


def test_duplicate_span_id_rejected():
    payload = _base_payload()
    payload["spans"].append(copy.deepcopy(payload["spans"][1]))
    with pytest.raises(TraceValidationError, match="重复"):
        validate_queryforge_trace(payload)


def test_missing_parent_span_rejected():
    payload = _base_payload()
    payload["spans"][1]["parent_span_id"] = "s-missing"
    with pytest.raises(TraceValidationError, match="parent_span_id"):
        validate_queryforge_trace(payload)


def test_invalid_example_file_rejected():
    payload = json.loads(INVALID_EXAMPLE_PATH.read_text(encoding="utf-8"))
    with pytest.raises(TraceValidationError, match="parent_span_id"):
        validate_queryforge_trace(payload)


def test_self_parent_span_rejected():
    payload = _base_payload()
    payload["spans"][0]["parent_span_id"] = "s1"
    with pytest.raises(TraceValidationError, match="自引用"):
        validate_queryforge_trace(payload)


def test_span_parent_cycle_rejected():
    payload = _base_payload()
    # 让 s1 <-> s2 互相为父；同时把开始时间对齐，避免先触发「父子时间顺序」校验。
    payload["spans"][0]["parent_span_id"] = "s2"
    payload["spans"][0]["started_at"] = payload["spans"][1]["started_at"]
    with pytest.raises(TraceValidationError, match="环"):
        validate_queryforge_trace(payload)


def test_parent_span_started_after_child_rejected():
    payload = _base_payload()
    payload["spans"][1]["started_at"] = "2026-11-25T00:00:00.050Z"
    with pytest.raises(TraceValidationError, match="父子时间顺序"):
        validate_queryforge_trace(payload)


def test_span_outside_trace_window_rejected():
    payload = _base_payload()
    payload["spans"][0]["started_at"] = "2026-11-24T23:59:59Z"
    with pytest.raises(TraceValidationError, match="时间窗口"):
        validate_queryforge_trace(payload)


def test_terminal_status_requires_ended_at():
    payload = _base_payload()
    del payload["ended_at"]
    with pytest.raises(TraceValidationError, match="ended_at"):
        validate_queryforge_trace(payload)


def test_running_status_allows_null_ended_at():
    payload = _base_payload()
    payload["status"] = "running"
    del payload["ended_at"]

    imported = validate_queryforge_trace(payload)
    assert imported.record.status is TraceStatus.RUNNING
    assert imported.record.ended_at is None


def test_ended_at_before_started_at_rejected():
    payload = _base_payload()
    payload["ended_at"] = "2026-11-24T23:59:00Z"
    with pytest.raises(TraceValidationError):
        validate_queryforge_trace(payload)


@pytest.mark.parametrize(
    "bad_payload",
    [
        123,
        None,
        ["not", "a", "trace"],
        "{not-json",
    ],
)
def test_unsupported_payload_rejected(bad_payload):
    with pytest.raises(TracePayloadError):
        validate_queryforge_trace(bad_payload)


def test_non_json_serializable_value_rejected():
    """input / attributes 必须是 JSON 可序列化的值（TracePilot 存储前提）。"""
    payload = _base_payload()
    payload["spans"][0]["input"] = {"bad": object()}
    with pytest.raises(TraceValidationError, match="JSON 序列化"):
        validate_queryforge_trace(payload)


# ---------------------------------------------------------------------------
# 3. token 不伪造：缺失 / 全 0 / 部分缺失都必须保持 unknown
# ---------------------------------------------------------------------------


def test_token_usage_on_non_token_event_rejected():
    """retrieval / tool / rerank / citation_validation 携带 token_usage -> 拒绝。"""
    payload = _base_payload()
    payload["spans"][0]["event_type"] = "retrieval"
    payload["spans"][0]["name"] = "hybrid_retrieval"
    payload["spans"][0]["token_usage"] = {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
    }
    with pytest.raises(TraceValidationError, match="token_usage"):
        validate_queryforge_trace(payload)


@pytest.mark.parametrize(
    "bad_tokens",
    [
        {"input_tokens": -1},
        {"input_tokens": True},
        {"input_tokens": 1.5},
        {"output_tokens": "5"},
        # 伪 token：input/output 同时为 0（含全 0 对象），视为用 0 冒充未知用量
        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        {"input_tokens": 0, "output_tokens": 0},
    ],
)
def test_fake_token_values_rejected(bad_tokens):
    payload = _base_payload()
    payload["spans"][1]["token_usage"] = bad_tokens
    with pytest.raises(TraceValidationError):
        validate_queryforge_trace(payload)


def test_missing_token_usage_stays_unknown_not_zero(example_payload):
    payload = _payload(example_payload)
    model_span = next(s for s in payload["spans"] if s["span_id"] == "span-qf-model-001")
    del model_span["token_usage"]

    imported = validate_queryforge_trace(payload)
    step = _step_by_id(imported.record, "span-qf-model-001")

    assert step.token_usage is None  # 缺失 = unknown，不是 0
    assert step.metadata["queryforge"]["token_usage_status"] == TOKEN_STATUS_UNKNOWN
    assert "token_usage_raw" not in step.metadata["queryforge"]
    assert any("未提供 token_usage" in warning for warning in imported.warnings)
    assert "span-qf-model-001" in imported.record.metadata["queryforge"][
        "token_usage_unknown_spans"
    ]


def test_all_zero_token_usage_rejected_as_fabricated():
    """input/output 同时为 0 视为伪造未知用量，协议层拒绝（不是静默记 0）。"""
    payload = _base_payload()
    payload["spans"][1]["token_usage"] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }

    with pytest.raises(TraceValidationError, match="冒充"):
        validate_queryforge_trace(payload)


def test_all_null_token_usage_object_is_unknown_not_zero():
    """token_usage 对象存在但字段全为 null：按 unknown 处理，不补 0。"""
    payload = _base_payload()
    payload["spans"][1]["token_usage"] = {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }

    imported = validate_queryforge_trace(payload)
    step = _step_by_id(imported.record, "s2")

    assert step.token_usage is None
    assert step.metadata["queryforge"]["token_usage_status"] == TOKEN_STATUS_UNKNOWN
    assert step.metadata["queryforge"]["token_usage_raw"] == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }
    assert any("全为 null" in warning for warning in imported.warnings)


def test_partial_token_usage_not_fabricated():
    payload = _base_payload()
    payload["spans"][1]["token_usage"] = {
        "input_tokens": 100,
        "output_tokens": None,
        "total_tokens": None,
    }

    imported = validate_queryforge_trace(payload)
    step = _step_by_id(imported.record, "s2")

    assert step.token_usage is None  # 不把缺失的 output 补 0
    assert step.metadata["queryforge"]["token_usage_status"] == TOKEN_STATUS_PARTIAL
    assert step.metadata["queryforge"]["token_usage_raw"] == {
        "input_tokens": 100,
        "output_tokens": None,
        "total_tokens": None,
    }
    assert any("不完整" in warning for warning in imported.warnings)


def test_missing_total_tokens_computed_with_warning():
    payload = _base_payload()
    payload["spans"][1]["token_usage"] = {
        "input_tokens": 10,
        "output_tokens": 5,
    }

    imported = validate_queryforge_trace(payload)
    step = _step_by_id(imported.record, "s2")

    assert step.token_usage is not None
    assert step.token_usage.total_tokens == 15
    assert step.metadata["queryforge"]["token_usage_status"] == TOKEN_STATUS_COMPLETE
    assert any("total_tokens" in warning for warning in imported.warnings)


def test_inconsistent_total_tokens_preserved_with_warning():
    payload = _base_payload()
    payload["spans"][1]["token_usage"] = {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 99,
    }

    imported = validate_queryforge_trace(payload)
    step = _step_by_id(imported.record, "s2")

    assert step.token_usage is not None
    assert step.token_usage.total_tokens == 99  # 保留原值，不篡改
    assert any("不一致" in warning for warning in imported.warnings)


# ---------------------------------------------------------------------------
# 4. 错误 / 状态映射
# ---------------------------------------------------------------------------


def test_failed_span_error_mapped():
    payload = _base_payload()
    payload["status"] = "failed"
    payload["spans"][1]["status"] = "failed"
    payload["spans"][1]["error"] = {
        "type": "LLMUnavailable",
        "message": "upstream model unavailable",
        "recoverable": False,
    }

    imported = validate_queryforge_trace(payload)
    step = _step_by_id(imported.record, "s2")

    assert imported.record.status is TraceStatus.FAILED
    assert step.status is StepStatus.FAILED
    assert step.error is not None
    assert step.error.error_type == "LLMUnavailable"
    assert step.error.error_message == "upstream model unavailable"
    assert step.error.recoverable is False
    assert not any("状态不一致" in warning for warning in imported.warnings)


def test_status_inconsistency_kept_and_warned():
    payload = _base_payload()
    payload["spans"][1]["status"] = "failed"
    payload["spans"][1]["error"] = {
        "type": "ToolError",
        "message": "boom",
        "recoverable": True,
    }
    # trace.status 仍为 succeeded，与失败 span 矛盾

    imported = validate_queryforge_trace(payload)
    assert imported.record.status is TraceStatus.SUCCEEDED  # 不篡改原始状态
    assert any("状态不一致" in warning for warning in imported.warnings)


def test_failed_span_without_error_warns_and_keeps_null():
    payload = _base_payload()
    payload["spans"][1]["status"] = "failed"

    imported = validate_queryforge_trace(payload)
    step = _step_by_id(imported.record, "s2")

    assert step.error is None
    assert any("未提供 error" in warning for warning in imported.warnings)


# ---------------------------------------------------------------------------
# 5. 导入存储（全部写入 tmp_path，不污染 data/traces）
# ---------------------------------------------------------------------------


def test_import_writes_trace_and_load_trace_reads_it(tmp_path, example_payload):
    imported = import_queryforge_trace(_payload(example_payload), traces_dir=tmp_path)

    assert imported.saved_path == tmp_path / f"{example_payload['trace_id']}.json"
    assert imported.saved_path.exists()
    assert imported.record.trace_id == example_payload["trace_id"]

    # 通过现有 trace_store.load_trace 读取（测试写入只落在 tmp_path）
    loaded = load_trace(example_payload["trace_id"], traces_dir=tmp_path)
    assert loaded.trace_id == example_payload["trace_id"]
    assert loaded.steps == imported.record.steps
    assert loaded.metadata["source"] == "queryforge"
    assert loaded.metadata["queryforge"]["schema_version"] == "1.0"
    assert loaded.metadata["queryforge"]["execution_mode"] == "simulated"
    assert classify_metadata(loaded.metadata)["source_class"] == "SIMULATED"


def test_import_preserves_safe_original_trace_id(tmp_path):
    payload = _base_payload()
    imported = import_queryforge_trace(payload, traces_dir=tmp_path)

    assert imported.record.trace_id == "trace-qf-unit-001"
    assert imported.original_trace_id == "trace-qf-unit-001"
    assert imported.saved_path == tmp_path / "trace-qf-unit-001.json"
    assert imported.warnings == []


def test_import_conflict_refused_and_file_not_overwritten(tmp_path, example_payload):
    payload = _payload(example_payload)
    first = import_queryforge_trace(payload, traces_dir=tmp_path)
    original_bytes = first.saved_path.read_bytes()

    with pytest.raises(TraceIdConflictError, match="不会覆盖"):
        import_queryforge_trace(_payload(example_payload), traces_dir=tmp_path)

    # 已有文件必须保持原样，且不产生第二个文件
    assert first.saved_path.read_bytes() == original_bytes
    assert [path.name for path in tmp_path.iterdir()] == [first.saved_path.name]


@pytest.mark.parametrize(
    "unsafe_trace_id",
    ["../evil-trace", "bad/id-slash", "x" * 200, "含中文的trace"],
)
def test_import_illegal_trace_id_generates_safe_id(tmp_path, unsafe_trace_id):
    payload = _base_payload()
    payload["trace_id"] = unsafe_trace_id

    imported = import_queryforge_trace(payload, traces_dir=tmp_path)

    assert imported.record.trace_id != unsafe_trace_id
    assert _SAFE_TRACE_ID.fullmatch(imported.record.trace_id)
    assert imported.record.trace_id.startswith("trace-")
    assert imported.original_trace_id == unsafe_trace_id
    assert imported.saved_path.parent == tmp_path  # 文件不会逃逸到目录外
    assert [path.name for path in tmp_path.iterdir()] == [imported.saved_path.name]
    assert any("已生成" in warning for warning in imported.warnings)

    # 所有步骤的 trace_id 反向引用同步更新；metadata 保留原值
    assert all(step.trace_id == imported.record.trace_id for step in imported.record.steps)
    loaded = load_trace(imported.record.trace_id, traces_dir=tmp_path)
    assert loaded.metadata["queryforge"]["original_trace_id"] == unsafe_trace_id
    assert loaded.metadata["queryforge"]["trace_id"] == imported.record.trace_id


def test_import_does_not_pollute_default_traces_dir(tmp_path, example_payload):
    from backend.app.services.trace_interop import DEFAULT_TRACES_DIR

    before = {path.name for path in DEFAULT_TRACES_DIR.glob("*.json")}
    import_queryforge_trace(_payload(example_payload), traces_dir=tmp_path)
    after = {path.name for path in DEFAULT_TRACES_DIR.glob("*.json")}

    assert before == after
    assert not (DEFAULT_TRACES_DIR / f"{example_payload['trace_id']}.json").exists()


def test_import_redacts_sensitive_values(tmp_path, example_payload):
    payload = _payload(example_payload)
    model_span = next(s for s in payload["spans"] if s["span_id"] == "span-qf-model-001")
    model_span["input"]["api_key"] = "sk-live-SECRET-should-not-be-stored"

    imported = import_queryforge_trace(payload, traces_dir=tmp_path)
    text = imported.saved_path.read_text(encoding="utf-8")

    assert "sk-live-SECRET-should-not-be-stored" not in text
    assert "[REDACTED]" in text

    loaded = load_trace(imported.record.trace_id, traces_dir=tmp_path)
    assert loaded.metadata["source"] == "queryforge"
    # token 用量不是敏感键，必须保留
    model_step = _step_by_id(loaded, "span-qf-model-001")
    assert model_step.token_usage is not None
    assert model_step.token_usage.total_tokens == 876


def test_import_failure_does_not_write_file(tmp_path):
    payload = _base_payload()
    payload["spans"][1]["parent_span_id"] = "missing-parent"
    target = tmp_path / "traces"

    with pytest.raises(TraceValidationError):
        import_queryforge_trace(payload, traces_dir=target)

    assert not target.exists()


def test_import_metadata_origin_and_warnings(tmp_path, example_payload):
    imported = import_queryforge_trace(_payload(example_payload), traces_dir=tmp_path)
    qf = imported.record.metadata["queryforge"]

    assert imported.record.metadata["source"] == "queryforge"
    assert qf["imported_at"] == imported.imported_at.isoformat()
    assert qf["warnings"] == imported.warnings
    assert qf["span_count"] == 7


def test_import_governance_markers(tmp_path):
    """execution_mode 映射到治理口径；real_api 声明不自动升级为 REAL_API。"""
    cases = [
        ("real_api", "UNKNOWN", "data_source"),
        ("mock", "MOCK", "data_source"),
        ("simulated", "SIMULATED", "data_source"),
        ("replay", "REPLAY", "derivation"),
    ]
    for mode, expected_class, metadata_key in cases:
        payload = _base_payload()
        payload["trace_id"] = f"trace-qf-mode-{mode}"
        payload["execution_mode"] = mode
        imported = import_queryforge_trace(payload, traces_dir=tmp_path)

        if metadata_key == "derivation":
            assert imported.record.metadata["derivation"] == "replay"
        else:
            assert imported.record.metadata["data_source"] == mode
        assert classify_metadata(imported.record.metadata)["source_class"] == expected_class

    # real_api 导入必须给出「不可自证」告警
    payload = _base_payload()
    payload["execution_mode"] = "real_api"
    payload["trace_id"] = "trace-qf-real-api-warning"
    imported = validate_queryforge_trace(payload)
    assert any("real_api_verified" in warning for warning in imported.warnings)


# ---------------------------------------------------------------------------
# 6. JSON Schema 文件与 Pydantic 模型一致
# ---------------------------------------------------------------------------


def test_json_schema_file_is_valid_and_matches_model():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert schema.pop("$id") == "queryforge_trace_v1.schema.json"
    # 文件必须与模型生成结果完全一致（模型演进时需同步重新生成 schema）
    assert schema == QueryForgeTraceV1.model_json_schema()


def test_json_schema_required_fields_and_event_enum():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "trace_id",
        "schema_version",
        "task_id",
        "run_id",
        "execution_mode",
        "model",
        "started_at",
        "status",
        "spans",
    }
    span_schema = schema["$defs"]["QueryForgeSpan"]
    assert span_schema["additionalProperties"] is False
    # 需求要求的 span 字段一个不少
    assert set(span_schema["properties"]) == {
        "span_id",
        "parent_span_id",
        "event_type",
        "name",
        "started_at",
        "latency_ms",
        "input",
        "output",
        "attributes",
        "token_usage",
        "error",
        "status",
    }
    assert set(span_schema["required"]) == {
        "span_id",
        "event_type",
        "name",
        "started_at",
        "status",
    }
    token_schema = schema["$defs"]["QueryForgeTokenUsage"]
    assert set(token_schema["properties"]) == {
        "input_tokens",
        "output_tokens",
        "total_tokens",
    }
    assert set(schema["$defs"]["QueryForgeEventType"]["enum"]) == {
        "planning",
        "model_call",
        "tool_call",
        "retrieval",
        "rerank",
        "citation_validation",
        "reflection",
    }


def test_model_rejects_unknown_field_directly():
    """直接使用协议模型也保持严格校验（JSON Schema 与模型行为一致）。"""
    payload = _base_payload()
    payload["extra"] = 1
    with pytest.raises(ValidationError, match="extra"):
        QueryForgeTraceV1.model_validate(payload)


def test_interop_errors_are_value_errors():
    """所有导入错误都是 ValueError 子类，便于 API 层统一处理。"""
    assert issubclass(TracePayloadError, TraceInteropError)
    assert issubclass(TraceValidationError, TraceInteropError)
    assert issubclass(TraceIdConflictError, TraceInteropError)


def test_runtime_fallback_schema_matches_docs_file():
    """部署镜像可能不含 docs/，此时由模型即时生成 schema，结果必须与文件一致。"""
    generated = QueryForgeTraceV1.model_json_schema()
    generated["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    generated["$id"] = "queryforge_trace_v1.schema.json"
    assert generated == json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_schema_endpoint_is_public_and_returns_protocol_v1():
    from fastapi.testclient import TestClient

    from backend.app.main import app

    client = TestClient(app)
    response = client.get("/api/interop/queryforge/schema")
    assert response.status_code == 200
    body = response.json()
    assert body["protocol_version"] == "1.0"
    assert body["schema"]["title"] == "QueryForgeTraceV1"
    assert body["schema"]["$id"] == "queryforge_trace_v1.schema.json"
    assert issubclass(TraceInteropError, ValueError)
