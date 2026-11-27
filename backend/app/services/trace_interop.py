"""QueryForge Trace 互操作服务：校验、映射与导入存储（协议 v1）。

本模块把 QueryForge 按 ``docs/schemas/queryforge_trace_v1.schema.json`` 导出的
版本化 JSON（协议 v1）接入 TracePilot：

1. ``validate_queryforge_trace(payload) -> ImportedTrace``
   严格校验协议 v1（必填字段、未知字段 / 未知 event_type、时间格式与窗口、
   span_id 唯一、parent 存在且无环、token 不伪造），返回 TraceRecord 与导入元数据
   （``source=queryforge``、``schema_version``、``warnings`` 等）。纯校验，无 I/O。
2. ``to_trace_record(payload) -> TraceRecord``
   校验并映射为 TracePilot 原生 ``TraceRecord``（DAG 可直接构建）。
3. ``import_queryforge_trace(payload, traces_dir=...) -> ImportedTrace``
   校验 + 映射 + 写入 Trace 目录（默认 ``data/traces``）：原 trace_id 合法且不冲突
   时保留，冲突时拒绝（不覆盖已有文件），不能作为安全文件名时生成新 ID 并告警。

映射约定（详见 docs/TRACE_INTEROPERABILITY.md）：
- ``event_type`` -> ``StepType``：planning->planning、model_call->model_call、
  tool_call->tool_call、retrieval->retrieval、rerank->tool_call、
  citation_validation->evaluation、reflection->reflection；
- ``parent_span_id`` -> ``parent_step_ids``（v1 为树，导入后按依赖拓扑排序）；
- span 的 ``attributes`` / ``event_type`` / 原始 ``latency_ms`` / 原始 token 用量
  等保留到 ``metadata["queryforge"]``；trace 级扩展保留到
  ``metadata["queryforge"]["attributes"]``；
- token 用量缺失 / 全 null / 部分缺失时**保持 unknown（None）**，绝不补 0；
  input/output 同时为 0 视为伪造，协议层直接拒绝；原始部分值保留到
  ``metadata["queryforge"]["token_usage_raw"]`` 供审计；
- 导入不提升数据来源等级：``execution_mode=real_api`` 仅写入 ``data_source``，
  因缺少 TracePilot ``real_api_verified`` 标记，数据治理仍会保守归为 UNKNOWN。

复用现有模块（已阅读后选择）：
- ``trace_collector.DEFAULT_TRACES_DIR``（``data/traces``）、``new_trace_id``、
  ``redact``；``trace_store.load_trace`` 可读取导入结果。
- 不直接调用 ``TraceCollector.save``：其 ``write_text`` 会覆盖同名文件，而导入
  要求「不得覆盖已有文件」，因此这里用独占创建（``open("x")``）保证不覆盖。
"""

from __future__ import annotations

import heapq
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from backend.app.schemas.queryforge_trace import (
    QUERYFORGE_SCHEMA_VERSION,
    QUERYFORGE_SOURCE,
    TOKEN_BEARING_EVENT_TYPES,
    QueryForgeEventType,
    QueryForgeExecutionMode,
    QueryForgeSpan,
    QueryForgeSpanStatus,
    QueryForgeTraceV1,
)
from backend.app.schemas.trace import (
    SCHEMA_VERSION,
    StepStatus,
    StepType,
    TokenUsage,
    TraceError,
    TraceRecord,
    TraceStatus,
    TraceStep,
)
from backend.app.services.trace_collector import (
    DEFAULT_TRACES_DIR,
    new_trace_id,
    redact,
)

__all__ = [
    "TraceInteropError",
    "TracePayloadError",
    "TraceValidationError",
    "TraceIdConflictError",
    "ImportedTrace",
    "EVENT_TYPE_TO_STEP_TYPE",
    "TOKEN_STATUS_COMPLETE",
    "TOKEN_STATUS_UNKNOWN",
    "TOKEN_STATUS_PARTIAL",
    "TOKEN_STATUS_NOT_APPLICABLE",
    "validate_queryforge_trace",
    "to_trace_record",
    "import_queryforge_trace",
    "save_imported_trace",
    "DEFAULT_TRACES_DIR",
]

# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------


class TraceInteropError(ValueError):
    """QueryForge Trace 互操作错误的基类。"""


class TracePayloadError(TraceInteropError):
    """payload 形态非法（非 dict / JSON 字符串 / bytes，或不是合法 JSON）。"""


class TraceValidationError(TraceInteropError):
    """协议 v1 校验失败。

    Attributes:
        errors: 结构化的错误列表（仅 loc / msg / type；不回显原始输入值，
            避免把疑似凭据写进日志或响应）。
    """

    def __init__(self, message: str, *, errors: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.errors: list[dict[str, str]] = errors or []


class TraceIdConflictError(TraceInteropError):
    """目标 trace_id 已存在（导入不覆盖已有文件）。"""


# ---------------------------------------------------------------------------
# 映射与 token 语义常量
# ---------------------------------------------------------------------------

#: event_type -> TracePilot StepType。
#:
#: rerank 视为检索管线中的本地处理步骤（交叉编码器），映射为 tool_call；
#: 原始 event_type 始终保留在 metadata["queryforge"]["event_type"]，下游可精确还原。
EVENT_TYPE_TO_STEP_TYPE: dict[QueryForgeEventType, StepType] = {
    QueryForgeEventType.PLANNING: StepType.PLANNING,
    QueryForgeEventType.MODEL_CALL: StepType.MODEL_CALL,
    QueryForgeEventType.TOOL_CALL: StepType.TOOL_CALL,
    QueryForgeEventType.RETRIEVAL: StepType.RETRIEVAL,
    QueryForgeEventType.RERANK: StepType.TOOL_CALL,
    QueryForgeEventType.CITATION_VALIDATION: StepType.EVALUATION,
    QueryForgeEventType.REFLECTION: StepType.REFLECTION,
}

#: 映射后视为「工具 / 检索类步骤」的 StepType（用于填充 tool_name / tool_arguments）。
_TOOL_LIKE_STEP_TYPES = frozenset({StepType.TOOL_CALL, StepType.RETRIEVAL})

TOKEN_STATUS_COMPLETE = "complete"            # input/output 已知，已映射为 TokenUsage
TOKEN_STATUS_UNKNOWN = "unknown"              # 缺失 / 全 null / 全 0：保持 None，不补 0
TOKEN_STATUS_PARTIAL = "partial"              # 只给了一部分：不映射，原始值留档待审计
TOKEN_STATUS_NOT_APPLICABLE = "not_applicable"  # 非 token 型事件：无 token 概念

# 与 trace_store._SAFE_ID 保持一致：导入后的文件名必须安全，防路径穿越。
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


# ---------------------------------------------------------------------------
# 导入结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportedTrace:
    """导入结果：TracePilot 记录 + 导入元数据。

    Attributes:
        record: 已通过 TraceRecord 校验的轨迹（DAG 可直接构建）。
        source: 数据来源标识，固定为 ``"queryforge"``。
        schema_version: 来源协议版本，v1 为 ``"1.0"``。
        warnings: 导入告警列表（token 未知、状态不一致、ID 重命名等）。
        original_trace_id: 原始 payload 中的 trace_id（重命名时保留原值）。
        imported_at: 导入时间（UTC）。
        saved_path: 仅 ``import_queryforge_trace`` 返回时存在，为实际写入文件路径。
    """

    record: TraceRecord
    source: str = QUERYFORGE_SOURCE
    schema_version: str = QUERYFORGE_SCHEMA_VERSION
    warnings: list[str] = field(default_factory=list)
    original_trace_id: str = ""
    imported_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    saved_path: Path | None = None

    @property
    def trace_id(self) -> str:
        """导入后的最终 trace_id（可能与 original_trace_id 不同）。"""
        return self.record.trace_id


# ---------------------------------------------------------------------------
# payload 解析与 Pydantic 校验
# ---------------------------------------------------------------------------


def _error_details(exc: ValidationError) -> list[dict[str, str]]:
    """把 Pydantic 错误转为结构化摘要（不回显 input，避免泄露疑似凭据）。"""
    details: list[dict[str, str]] = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
        details.append(
            {
                "loc": loc,
                "msg": str(err.get("msg", "validation error")),
                "type": str(err.get("type", "value_error")),
            }
        )
    return details


def _format_validation_error(exc: ValidationError) -> str:
    details = [
        f"{item['loc']}: {item['msg']}" for item in _error_details(exc)
    ]
    if len(details) > 10:
        hidden = len(details) - 10
        details = details[:10] + [f"... 其余 {hidden} 处错误已省略"]
    return "QueryForge Trace 协议 v1 校验失败：" + "；".join(details)


def _coerce_trace(payload: Any) -> QueryForgeTraceV1:
    """把 dict / JSON 字符串 / bytes / 已校验模型统一转成 QueryForgeTraceV1。"""
    if isinstance(payload, QueryForgeTraceV1):
        return payload

    raw = payload
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TracePayloadError(f"payload 不是合法 UTF-8 字节：{exc}") from exc

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TracePayloadError(
                f"payload 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}"
            ) from exc

    if not isinstance(raw, Mapping):
        raise TracePayloadError(
            "payload 类型不支持："
            f"{type(raw).__name__}；应为 dict / JSON 字符串 / bytes / QueryForgeTraceV1"
        )

    data = dict(raw)
    try:
        model = QueryForgeTraceV1.model_validate(data)
    except ValidationError as exc:
        raise TraceValidationError(
            _format_validation_error(exc), errors=_error_details(exc)
        ) from exc

    # input / output / attributes 声明为 Any：Pydantic 会放行任意 Python 对象，
    # 但 TracePilot 要求所有扩展值可 JSON 序列化（写入 Trace 文件的前提）。
    try:
        model.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 - 序列化失败统一转为协议校验错误
        raise TraceValidationError(
            f"payload 包含不可 JSON 序列化的值：{exc}"
        ) from exc
    return model


# ---------------------------------------------------------------------------
# 公开：校验 / 映射
# ---------------------------------------------------------------------------


def validate_queryforge_trace(payload: Any) -> ImportedTrace:
    """严格校验 QueryForge Trace 协议 v1，返回 ImportedTrace（纯校验，无 I/O）。

    Args:
        payload: 协议 v1 字典 / JSON 字符串 / bytes / ``QueryForgeTraceV1``。

    Returns:
        ImportedTrace: 含 TraceRecord、来源（queryforge）、协议版本与 warnings。

    Raises:
        TracePayloadError: payload 形态非法（非 JSON / 非对象 / 类型不支持）。
        TraceValidationError: 协议校验失败（未知字段 / 未知 event_type / 时间 /
            父子关系 / 重复 span_id / token 伪造等）。
    """
    model = _coerce_trace(payload)
    imported_at = datetime.now(timezone.utc)
    record, warnings = _map_trace(model, imported_at)
    return ImportedTrace(
        record=record,
        warnings=warnings,
        original_trace_id=model.trace_id,
        imported_at=imported_at,
    )


def to_trace_record(payload: Any) -> TraceRecord:
    """映射为 TracePilot 原生 TraceRecord（入参可为载荷或 ImportedTrace）。"""
    if isinstance(payload, ImportedTrace):
        return payload.record
    return validate_queryforge_trace(payload).record


# ---------------------------------------------------------------------------
# 核心映射实现
# ---------------------------------------------------------------------------


def _map_trace(
    model: QueryForgeTraceV1, imported_at: datetime
) -> tuple[TraceRecord, list[str]]:
    """把已通过协议校验的模型映射为 TraceRecord，并收集 warnings。"""
    warnings: list[str] = []
    unknown_token_spans: list[str] = []

    steps = [
        _map_span(span, trace_id=model.trace_id, warnings=warnings,
                  unknown_token_spans=unknown_token_spans)
        for span in model.spans
    ]
    ordered_steps = _order_steps_by_dependency(steps)
    trace_status = TraceStatus(model.status.value)

    # 状态一致性告警（只告警，不篡改原始状态，避免「美化」导入数据）。
    if trace_status is TraceStatus.SUCCEEDED:
        failed = [s.step_id for s in ordered_steps if s.status is StepStatus.FAILED]
        if failed:
            warnings.append(
                f"trace.status=succeeded，但存在失败 span：{', '.join(failed)}"
                f"（状态不一致，按原值保留）"
            )
    elif trace_status is TraceStatus.FAILED and not any(
        s.status is StepStatus.FAILED for s in ordered_steps
    ):
        warnings.append(
            "trace.status=failed，但没有任何 span.status=failed（状态不一致，按原值保留）"
        )

    # 数据治理：real_api 声明不可自证，保守提示（不写 real_api_verified）。
    if model.execution_mode is QueryForgeExecutionMode.REAL_API:
        warnings.append(
            "execution_mode=real_api：导入数据缺少 TracePilot real_api_verified 独立验证标记，"
            "来源分类将保守为 UNKNOWN，不计入真实 API KPI"
        )

    metadata = _build_metadata(model, imported_at, warnings, unknown_token_spans)
    record = TraceRecord(
        trace_id=model.trace_id,
        task_id=model.task_id,
        run_id=model.run_id,
        started_at=model.started_at,
        ended_at=model.ended_at,
        status=trace_status,
        steps=ordered_steps,
        metadata=metadata,
    )
    return record, warnings


def _map_span(
    span: QueryForgeSpan,
    *,
    trace_id: str,
    warnings: list[str],
    unknown_token_spans: list[str],
) -> TraceStep:
    """把单个 span 映射为 TraceStep。"""
    step_type = EVENT_TYPE_TO_STEP_TYPE[span.event_type]
    token_usage, token_status, token_warnings = _map_token_usage(span)
    warnings.extend(token_warnings)
    if span.event_type in TOKEN_BEARING_EVENT_TYPES and token_usage is None:
        unknown_token_spans.append(span.span_id)

    error: TraceError | None = None
    if span.error is not None:
        error = TraceError(
            error_type=span.error.type,
            error_message=span.error.message,
            recoverable=span.error.recoverable,
        )
        if span.status is not QueryForgeSpanStatus.FAILED:
            warnings.append(
                f"span {span.span_id!r} 提供了 error 但 status={span.status.value}，"
                f"error 原样保留"
            )
    elif span.status is QueryForgeSpanStatus.FAILED:
        warnings.append(
            f"span {span.span_id!r} status=failed 但未提供 error，TraceStep.error 为 null"
        )

    queryforge_span_meta: dict[str, Any] = {
        "event_type": span.event_type.value,
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "name": span.name,
        "latency_ms": span.latency_ms,
        "attributes": dict(span.attributes),
        "token_usage_status": token_status,
    }
    # 未能完整映射的 token 用量保留原始值，供审计（绝不补 0 后丢弃证据）。
    if span.token_usage is not None and token_status != TOKEN_STATUS_COMPLETE:
        queryforge_span_meta["token_usage_raw"] = span.token_usage.model_dump(mode="json")

    tool_like = step_type in _TOOL_LIKE_STEP_TYPES
    return TraceStep(
        step_id=span.span_id,
        trace_id=trace_id,
        parent_step_ids=[span.parent_span_id] if span.parent_span_id else [],
        step_type=step_type,
        timestamp=span.started_at,
        input=span.input,
        output=span.output,
        tool_name=span.name if tool_like else None,
        tool_arguments=dict(span.input) if tool_like and isinstance(span.input, dict) else None,
        tool_result=None,  # 原始结果保留在 output，避免同一数据写两份
        status=StepStatus(span.status.value),
        error=error,
        duration_ms=None if span.latency_ms is None else int(round(span.latency_ms)),
        token_usage=token_usage,
        metadata={"queryforge": queryforge_span_meta},
    )


def _map_token_usage(
    span: QueryForgeSpan,
) -> tuple[TokenUsage | None, str, list[str]]:
    """把协议 token_usage 诚实映射为 TracePilot TokenUsage。

    关键规则（对应需求「token 用量缺失必须 null/unknown，不能填 0 冒充」）：
    - 非 token 型事件（且 model 校验已禁止携带 token_usage）-> not_applicable；
    - 未提供 / 全 null -> unknown，映射为 None；
    - input/output 同时为 0 已在协议层拒绝（视为伪造）；
    - input/output 任一缺失 -> partial，映射为 None 并保留原始值；
    - input/output 完整 -> complete；total 缺失按 input+output 计算并告警，
      total 与 input+output 不一致时保留原值并告警。
    """
    if span.event_type not in TOKEN_BEARING_EVENT_TYPES:
        return None, TOKEN_STATUS_NOT_APPLICABLE, []

    usage = span.token_usage
    if usage is None:
        if span.event_type is QueryForgeEventType.MODEL_CALL:
            return (
                None,
                TOKEN_STATUS_UNKNOWN,
                [
                    f"span {span.span_id!r}（model_call）未提供 token_usage，"
                    f"按 unknown 处理（不填 0）"
                ],
            )
        return None, TOKEN_STATUS_UNKNOWN, []

    values = (usage.input_tokens, usage.output_tokens, usage.total_tokens)
    if all(value is None for value in values):
        return (
            None,
            TOKEN_STATUS_UNKNOWN,
            [f"span {span.span_id!r} 的 token_usage 全为 null，按 unknown 处理（不填 0）"],
        )

    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    if input_tokens is None or output_tokens is None:
        return (
            None,
            TOKEN_STATUS_PARTIAL,
            [
                f"span {span.span_id!r} 的 token_usage 不完整"
                f"（input/output 缺失），保持 unknown 并保留原始值到 metadata"
            ],
        )

    warnings: list[str] = []
    computed_total = input_tokens + output_tokens
    total_tokens = usage.total_tokens
    if total_tokens is None:
        total_tokens = computed_total
        warnings.append(
            f"span {span.span_id!r} 缺少 total_tokens，按 input+output={computed_total} 计算"
        )
    elif total_tokens != computed_total:
        warnings.append(
            f"span {span.span_id!r} 的 total_tokens={total_tokens} 与 "
            f"input+output={computed_total} 不一致，保留原值"
        )

    return (
        TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        ),
        TOKEN_STATUS_COMPLETE,
        warnings,
    )


def _order_steps_by_dependency(steps: list[TraceStep]) -> list[TraceStep]:
    """按依赖拓扑排序步骤（父节点优先），同层按 (timestamp, step_id) 确定性排序。

    协议层已保证 parent 存在且无环；此处为防御性检查（并让 TraceRecord.steps
    既符合时间顺序，又能直接交给 DAG 构建）。
    """
    index = {step.step_id: step for step in steps}
    children: dict[str, list[str]] = {sid: [] for sid in index}
    in_degree: dict[str, int] = {}
    for step in steps:
        in_degree[step.step_id] = len(step.parent_step_ids)
        for parent_id in step.parent_step_ids:
            if parent_id not in index:
                raise TraceValidationError(
                    f"内部错误：步骤 {step.step_id!r} 的父节点 {parent_id!r} 不存在"
                )
            children[parent_id].append(step.step_id)

    heap: list[tuple[datetime, str]] = [
        (index[sid].timestamp, sid) for sid, degree in in_degree.items() if degree == 0
    ]
    heapq.heapify(heap)
    ordered: list[TraceStep] = []
    while heap:
        _, sid = heapq.heappop(heap)
        ordered.append(index[sid])
        for child_id in children[sid]:
            in_degree[child_id] -= 1
            if in_degree[child_id] == 0:
                child = index[child_id]
                heapq.heappush(heap, (child.timestamp, child_id))

    if len(ordered) != len(steps):
        raise TraceValidationError("检测到循环依赖，无法构建 DAG")
    return ordered


def _build_metadata(
    model: QueryForgeTraceV1,
    imported_at: datetime,
    warnings: list[str],
    unknown_token_spans: list[str],
) -> dict[str, Any]:
    """构造 TraceRecord.metadata（来源标注 + QueryForge 原始信息 + 治理标记）。"""
    queryforge_meta: dict[str, Any] = {
        "schema_version": QUERYFORGE_SCHEMA_VERSION,
        "trace_id": model.trace_id,
        "original_trace_id": model.trace_id,
        "execution_mode": model.execution_mode.value,
        "model": model.model,
        "status": model.status.value,
        "span_count": len(model.spans),
        "attributes": dict(model.attributes),
        "imported_at": imported_at.isoformat(),
        "warnings": list(warnings),
        "token_usage_unknown_spans": list(unknown_token_spans),
    }
    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": QUERYFORGE_SOURCE,
        "imported": True,
        "queryforge": queryforge_meta,
    }

    # 映射到 TracePilot 数据治理口径（classification 为只读派生）：
    # - mock/simulated -> data_source 直接对应；
    # - replay -> derivation=replay（治理层归 REPLAY）；
    # - real_api -> 仅声明 data_source，因无 real_api_verified 仍会被保守归为 UNKNOWN。
    mode = model.execution_mode
    if mode is QueryForgeExecutionMode.REAL_API:
        metadata["data_source"] = "real_api"
    elif mode is QueryForgeExecutionMode.MOCK:
        metadata["data_source"] = "mock"
    elif mode is QueryForgeExecutionMode.SIMULATED:
        metadata["data_source"] = "simulated"
    elif mode is QueryForgeExecutionMode.REPLAY:
        metadata["derivation"] = "replay"
    return metadata


def _relabel_trace(imported: ImportedTrace, new_trace_id: str) -> ImportedTrace:
    """把 trace_id 改成新值（同步步骤反向引用与 metadata）。"""
    record = imported.record
    steps = [
        step.model_copy(update={"trace_id": new_trace_id}) for step in record.steps
    ]
    metadata = dict(record.metadata)
    queryforge_meta = dict(metadata.get("queryforge") or {})
    queryforge_meta["trace_id"] = new_trace_id
    queryforge_meta["warnings"] = list(imported.warnings)
    metadata["queryforge"] = queryforge_meta
    updated_record = record.model_copy(
        update={"trace_id": new_trace_id, "steps": steps, "metadata": metadata}
    )
    return replace(imported, record=updated_record)


# ---------------------------------------------------------------------------
# 导入存储
# ---------------------------------------------------------------------------


def _is_safe_trace_id(trace_id: str) -> bool:
    """trace_id 能否安全作为文件名（与 trace_store 的规则保持一致）。"""
    return (
        isinstance(trace_id, str)
        and bool(_SAFE_ID_PATTERN.fullmatch(trace_id))
        and ".." not in trace_id
    )


def _generate_safe_trace_id(target_dir: Path) -> str:
    """生成一个不与该目录已有文件冲突的安全 trace_id。"""
    for _ in range(8):
        candidate = new_trace_id()
        if not _is_safe_trace_id(candidate):
            continue
        if not (target_dir / f"{candidate}.json").exists():
            return candidate
    raise TraceInteropError("无法生成唯一的 trace_id，请检查 Trace 目录状态")


def import_queryforge_trace(
    payload: Any, *, traces_dir: str | Path | None = None
) -> ImportedTrace:
    """校验并导入一条 QueryForge trace，写入 Trace 目录（默认 ``data/traces``）。

    规则：
    - 原 trace_id 合法（可安全作为文件名）且目标目录无同名文件 -> 保留原名；
    - 同名文件已存在 -> 抛 ``TraceIdConflictError``（**绝不覆盖**已有 Trace）；
    - 原 trace_id 不能作为安全文件名 -> 生成新 ID，原值保留在
      ``metadata["queryforge"]["original_trace_id"]`` 并在 warnings 中说明；
    - 写入前对敏感键做递归脱敏（复用 ``trace_collector.redact``）；
    - 写入后回读校验，确保 ``trace_store.load_trace`` 可直接读取。

    Args:
        payload: 协议 v1 字典 / JSON 字符串 / bytes / ``QueryForgeTraceV1``。
        traces_dir: 目标目录（测试传 tmp_path 以保证隔离）；默认 data/traces。

    Returns:
        ImportedTrace: 含 record、warnings 与 saved_path。

    Raises:
        TracePayloadError / TraceValidationError: payload 非法。
        TraceIdConflictError: trace_id 冲突。
        TraceInteropError: 目录创建或文件写入失败。
    """
    imported = validate_queryforge_trace(payload)

    target_dir = Path(traces_dir) if traces_dir is not None else DEFAULT_TRACES_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TraceInteropError(f"无法创建 Trace 目录 {target_dir}: {exc}") from exc
    if not target_dir.is_dir():
        raise TraceInteropError(f"Trace 目录不是目录：{target_dir}")

    original_trace_id = imported.record.trace_id
    if _is_safe_trace_id(original_trace_id):
        final_trace_id = original_trace_id
        candidate_path = target_dir / f"{final_trace_id}.json"
        if candidate_path.exists():
            raise TraceIdConflictError(
                f"trace_id {final_trace_id!r} 已存在于 {candidate_path}："
                f"导入不会覆盖已有 Trace，请更换 trace_id 或目标目录"
            )
    else:
        final_trace_id = _generate_safe_trace_id(target_dir)
        rename_warning = (
            f"原 trace_id {original_trace_id!r} 不能作为安全文件名"
            f"（含非法字符 / 路径穿越风险），已生成 {final_trace_id!r}；"
            f"原值保留在 metadata.queryforge.original_trace_id"
        )
        imported = replace(imported, warnings=[rename_warning, *imported.warnings])
        imported = _relabel_trace(imported, final_trace_id)
        candidate_path = target_dir / f"{final_trace_id}.json"

    # 写入：model_dump(mode="json") -> 递归脱敏 -> 独占创建（不覆盖已有文件）。
    payload_text = json.dumps(
        redact(imported.record.model_dump(mode="json")),
        ensure_ascii=False,
        indent=2,
    )
    try:
        with candidate_path.open("x", encoding="utf-8") as file_handle:
            file_handle.write(payload_text)
    except FileExistsError as exc:
        raise TraceIdConflictError(
            f"trace_id {final_trace_id!r} 已存在于 {candidate_path}："
            f"导入不会覆盖已有 Trace，请更换 trace_id 或目标目录"
        ) from exc
    except OSError as exc:
        raise TraceInteropError(f"写入 Trace 文件失败 {candidate_path}: {exc}") from exc

    # 回读自检：保证 trace_store.load_trace 能直接读取；失败则清理半成品。
    try:
        TraceRecord.model_validate_json(candidate_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 任何回读失败都不应留下坏文件
        candidate_path.unlink(missing_ok=True)
        raise TraceInteropError(
            f"写入后回读校验失败，已删除不完整文件 {candidate_path}: {exc}"
        ) from exc

    return replace(imported, saved_path=candidate_path)


#: 语义别名：明确「保存导入结果」意图时使用。
save_imported_trace = import_queryforge_trace
