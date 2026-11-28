"""LangFuse 官方 SDK 导出通道（与 OTLP 通道二选一，共用增量状态）。

为什么同时保留两条通道：
- **SDK 通道（本文件）**：使用官方 ``langfuse`` SDK 的类型化 OTLP 导出接口
  （``client.api.opentelemetry.export_traces``），满足"引入 SDK"的工程要求，
  由 SDK 负责鉴权、重试与协议序列化；
- **OTLP 通道（``langfuse_export.py``）**：零依赖、可用 ``urllib`` 直接对接任意 OTLP 后端，
  在没有安装 SDK 的环境（如最小化镜像/离线环境）仍可工作。

两条通道使用同一份增量状态文件（内容指纹 + 确定性 traceId/spanId），因此可以混用：
例如先 dry-run 审阅、再用 SDK 发送、最后用 OTLP 通道补发到另一个 collector。

SDK 的 ``export_traces`` 接受类型化的 ``OtelResourceSpan``，这里把 OTLP dict 显式转换为
SDK 的 Pydantic 模型（属性只保留标量；数组/嵌套结构会降级为 JSON 字符串，避免静默丢数据）。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from backend.app.schemas.trace import TraceRecord
from backend.app.services.langfuse_export import (
    ExportConfig,
    ExportError,
    ExportResult,
    TraceExportState,
    _chunks,  # noqa: PLC2701 - 同包内部复用批次工具
    _fingerprint,  # noqa: PLC2701
    build_otlp_payload,
)

__all__ = [
    "sdk_available",
    "sdk_version",
    "export_traces_via_sdk",
    "build_sdk_resource_spans",
]


def sdk_available() -> bool:
    try:
        import langfuse  # noqa: F401
    except Exception:  # noqa: BLE001 - 缺少 SDK 时优雅降级
        return False
    return True


def sdk_version() -> str | None:
    try:
        import langfuse

        return str(getattr(langfuse, "__version__", "unknown"))
    except Exception:  # noqa: BLE001
        return None


def _attr_scalar(value: dict[str, Any]) -> dict[str, Any]:
    """把 OTLP AnyValue dict 转成 SDK OtelAttributeValue 的 snake_case 字段。"""
    mapping = {
        "stringValue": "string_value",
        "intValue": "int_value",
        "doubleValue": "double_value",
        "boolValue": "bool_value",
    }
    for camel, snake in mapping.items():
        if camel in value:
            raw = value[camel]
            if camel in {"intValue", "doubleValue"} and isinstance(raw, (int, float)):
                raw = str(raw) if camel == "intValue" else float(raw)
            return {snake: raw}
    # 数组/嵌套值：SDK 类型只支持标量，降级为 JSON 字符串（可审计，不静默丢弃）
    return {"string_value": json.dumps(value, ensure_ascii=False, default=str)}


def build_sdk_resource_spans(traces: Sequence[TraceRecord], config: ExportConfig) -> list[Any]:
    """把 Trace 转成 SDK 的 ``OtelResourceSpan`` 列表。"""
    from langfuse.api.opentelemetry.types.otel_attribute import OtelAttribute, OtelAttributeValue
    from langfuse.api.opentelemetry.types.otel_resource import OtelResource
    from langfuse.api.opentelemetry.types.otel_resource_span import OtelResourceSpan
    from langfuse.api.opentelemetry.types.otel_scope import OtelScope
    from langfuse.api.opentelemetry.types.otel_scope_span import OtelScopeSpan
    from langfuse.api.opentelemetry.types.otel_span import OtelSpan

    payload = build_otlp_payload(traces, config)
    result: list[Any] = []
    for resource_span in payload.get("resourceSpans", []):
        resource = resource_span.get("resource") or {}
        resource_attributes = [
            OtelAttribute(key=item["key"], value=OtelAttributeValue(**_attr_scalar(item["value"])))
            for item in (resource.get("attributes") or [])
        ]
        scope_spans = []
        for scope_span in resource_span.get("scopeSpans") or []:
            scope = scope_span.get("scope") or {}
            spans = []
            for span in scope_span.get("spans") or []:
                spans.append(
                    OtelSpan(
                        trace_id=span.get("traceId"),
                        span_id=span.get("spanId"),
                        parent_span_id=span.get("parentSpanId"),
                        name=span.get("name"),
                        kind=span.get("kind"),
                        start_time_unix_nano=span.get("startTimeUnixNano"),
                        end_time_unix_nano=span.get("endTimeUnixNano"),
                        attributes=[
                            OtelAttribute(key=item["key"], value=OtelAttributeValue(**_attr_scalar(item["value"])))
                            for item in (span.get("attributes") or [])
                        ],
                        status=span.get("status"),
                    )
                )
            scope_spans.append(
                OtelScopeSpan(
                    scope=OtelScope(
                        name=scope.get("name"),
                        version=scope.get("version"),
                    ),
                    spans=spans,
                )
            )
        result.append(OtelResourceSpan(resource=OtelResource(attributes=resource_attributes), scope_spans=scope_spans))
    return result


def _build_client(config: ExportConfig) -> Any:
    from langfuse import Langfuse

    host = (config.langfuse_host or "").rstrip("/")
    if not host:
        host = config.endpoint.rstrip("/")
        marker = "/api/public/otel"
        if marker in host:
            host = host.split(marker, 1)[0]
    if not host:
        raise ExportError("SDK 通道需要 LANGFUSE_HOST 或 TRACEPILOT_OTEL_ENDPOINT（用于推导 host）")
    if not (config.public_key and config.secret_key):
        raise ExportError("SDK 通道需要 LANGFUSE_PUBLIC_KEY 与 LANGFUSE_SECRET_KEY")
    # tracing_enabled=False：只用 SDK 的类型化导出 API，避免 SDK 后台 exporter 重复发送
    return Langfuse(
        public_key=config.public_key,
        secret_key=config.secret_key,
        host=host,
        timeout=int(config.timeout_seconds),
        environment=config.deployment_environment,
        release=config.service_version,
        tracing_enabled=False,
    )


def export_traces_via_sdk(
    traces: Sequence[TraceRecord],
    *,
    config: ExportConfig,
    state: TraceExportState,
    force: bool = False,
) -> ExportResult:
    """通过官方 LangFuse SDK 增量导出（成功后推进状态；失败不推进）。"""
    if not sdk_available():
        raise ExportError("未安装 langfuse SDK：请 pip install -r requirements.txt（含 langfuse==5.2.0）")
    result = ExportResult(
        scanned=len(traces),
        dry_run=False,
        state_file=str(state.path),
        endpoint=config.safe_endpoint(),
    )
    pending: list[TraceRecord] = []
    fingerprints: dict[str, str] = {}
    for trace in traces:
        fingerprint = _fingerprint(trace)
        fingerprints[trace.trace_id] = fingerprint
        if not force and state.is_exported(trace.trace_id, fingerprint):
            result.skipped += 1
            continue
        pending.append(trace)
    result.eligible = len(pending)
    if not pending:
        return result

    client = _build_client(config)
    try:
        for batch in _chunks(pending, config.batch_traces):
            resource_spans = build_sdk_resource_spans(batch, config)
            span_count = sum(
                len(scope_spans.spans or [])
                for resource_span in resource_spans
                for scope_spans in (resource_span.scope_spans or [])
            )
            try:
                client.api.opentelemetry.export_traces(resource_spans=resource_spans)
            except Exception as exc:  # noqa: BLE001 - SDK 异常统一转导出错误
                result.failed += len(batch)
                result.failures = [
                    *result.failures,
                    *[{"trace_id": t.trace_id, "error": f"SDK 导出失败：{type(exc).__name__}: {exc}"} for t in batch],
                ]
                continue
            result.batches += 1
            result.exported += len(batch)
            result.spans += span_count
            per_trace_spans = span_count // max(1, len(batch))
            for trace in batch:
                state.mark_exported(trace.trace_id, fingerprints[trace.trace_id], per_trace_spans)
    finally:
        try:
            client.shutdown()
        except Exception:  # noqa: BLE001 - 关闭失败不影响已发送结果
            pass
    return result
