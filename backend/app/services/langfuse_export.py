"""TracePilot Trace → LangFuse（自托管）增量导出，基于 OpenTelemetry OTLP/HTTP 标准。

设计目标（安全第一）：
- **零新增运行时依赖**：只用 Python 标准库 + 项目已有的 Pydantic 模型，
  因此 `requirements.txt` / 生产 Dockerfile 无需修改，魔搭线上镜像不受影响；
- **默认关闭**：未显式配置 ``TRACEPILOT_LANGFUSE_ENABLED=1`` 且未传 ``--yes`` 时只做 dry-run；
- **增量 + 幂等**：以 Trace 内容指纹记录导出状态，未变化的 Trace 不重复导出；
  OTel ``traceId``/``spanId`` 由 TracePilot ID 确定性派生，重复导出不会产生随机新链路；
- **可审计**：不打印 API Key；状态文件原子写入；失败批次不推进状态，便于重试。

OTLP 负载遵循 OpenTelemetry Protocol (OTLP) v1.0.0 HTTP/JSON 编码：
``{"resourceSpans":[{"resource":{...},"scopeSpans":[{"scope":{...},"spans":[...]}]}]}``

LangFuse 侧约定（v3 的 OTLP 入口，路径默认 ``/api/public/otel/v1/traces``）：
- 通用 OTel 属性：``service.name``、``gen_ai.*``；
- LangFuse 增强属性（可选但推荐）：``langfuse.observation.type`` /
  ``langfuse.observation.input`` / ``langfuse.observation.output`` /
  ``langfuse.trace.*``。不同 LangFuse 版本对增强属性的支持可能不同，
  通用 OTLP 字段始终可被任何 OTLP 后端消费。

使用方式::

    # 1) dry-run：只生成 OTLP JSON，便于审阅，不发网络请求
    python scripts/export_langfuse.py --dry-run --limit 10

    # 2) 真实导出（自托管 LangFuse，需显式 --yes）
    LANGFUSE_HOST=http://localhost:3000 \\
    LANGFUSE_PUBLIC_KEY=pk-... LANGFUSE_SECRET_KEY=sk-... \\
    python scripts/export_langfuse.py --yes

无真实 LangFuse 实例时，可用任意 OTLP/HTTP collector（如 otel-collector 的
``otlphttp`` 接收器）做协议级验证；本仓库测试使用内置 mock OTLP 服务。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from backend.app.schemas.trace import StepStatus, StepType, TraceRecord, TraceStep

__all__ = [
    "ExportConfig",
    "ExportError",
    "ExportStateError",
    "ExportLockError",
    "TraceExportState",
    "OTLP_CONTENT_TYPE",
    "OTLP_JSON_VERSION",
    "build_export_config",
    "load_traces_for_export",
    "trace_to_otlp",
    "build_otlp_payload",
    "export_traces",
    "export_to_langfuse",
    "describe_export_status",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TRACES_DIR = PROJECT_ROOT / "data" / "traces"
DEFAULT_STATE_PATH = PROJECT_ROOT / "data" / "observability" / "langfuse_export_state.json"
OTLP_CONTENT_TYPE = "application/json"
OTLP_JSON_VERSION = "1.0.0"
EXPORTER_NAME = "tracepilot.otlp-exporter"
EXPORTER_VERSION = "1.0.0"
DEFAULT_MAX_ATTR_CHARS = 4000
STATE_VERSION = 1
LOCK_STALE_SECONDS = 3600


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ExportConfig:
    """导出配置（参数 > 环境变量 > 默认值）。"""

    enabled: bool = False
    endpoint: str = ""
    langfuse_host: str = ""
    public_key: str = ""
    secret_key: str = ""
    bearer_token: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    service_name: str = "tracepilot"
    service_version: str = "2.0"
    deployment_environment: str = "local"
    traces_dir: Path = DEFAULT_TRACES_DIR
    state_path: Path = DEFAULT_STATE_PATH
    timeout_seconds: float = 15.0
    max_retries: int = 3
    batch_traces: int = 20
    max_attr_chars: int = DEFAULT_MAX_ATTR_CHARS
    user_agent: str = f"TracePilot-OTLP/{EXPORTER_VERSION}"

    @property
    def configured(self) -> bool:
        return bool(self.endpoint.strip())

    @property
    def auth_mode(self) -> str:
        if self.public_key and self.secret_key:
            return "basic"
        if self.bearer_token:
            return "bearer"
        return "none"

    def safe_endpoint(self) -> str:
        """返回可打印的 endpoint（去掉 URL 中的 userinfo/query，避免泄露凭据）。"""
        parsed = urllib.parse.urlsplit(self.endpoint)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        path = parsed.path or ""
        return urllib.parse.urlunsplit((parsed.scheme, host, path, "", ""))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


def _parse_headers(raw: str) -> dict[str, str]:
    """解析 ``KEY=VALUE,KEY2=VALUE2`` 形式的自定义请求头。"""
    headers: dict[str, str] = {}
    for part in (raw or "").split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key, value = key.strip(), value.strip()
        if key:
            headers[key] = value
    return headers


def build_export_config(
    *,
    endpoint: str | None = None,
    public_key: str | None = None,
    secret_key: str | None = None,
    bearer_token: str | None = None,
    langfuse_host: str | None = None,
    extra_headers: dict[str, str] | None = None,
    state_path: str | Path | None = None,
    traces_dir: str | Path | None = None,
    enabled: bool | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    batch_traces: int | None = None,
    max_attr_chars: int | None = None,
) -> ExportConfig:
    """从参数与环境变量构造配置（不发起任何网络请求）。"""
    host = (os.environ.get("LANGFUSE_HOST") or "").strip().rstrip("/")
    endpoint_env = (os.environ.get("TRACEPILOT_OTEL_ENDPOINT") or "").strip()
    if endpoint_env:
        resolved_endpoint = endpoint_env
    elif host:
        resolved_endpoint = f"{host}/api/public/otel/v1/traces"
    else:
        resolved_endpoint = ""
    deployment = (
        os.environ.get("TRACEPILOT_LANGFUSE_ENVIRONMENT")
        or os.environ.get("TRACEPILOT_ENV")
        or "local"
    )
    return ExportConfig(
        enabled=_env_bool("TRACEPILOT_LANGFUSE_ENABLED") if enabled is None else bool(enabled),
        endpoint=(endpoint or resolved_endpoint).strip(),
        langfuse_host=(langfuse_host if langfuse_host is not None else host).strip(),
        public_key=(public_key if public_key is not None else os.environ.get("LANGFUSE_PUBLIC_KEY", "")).strip(),
        secret_key=(secret_key if secret_key is not None else os.environ.get("LANGFUSE_SECRET_KEY", "")).strip(),
        bearer_token=(
            bearer_token if bearer_token is not None else os.environ.get("TRACEPILOT_OTEL_TOKEN", "")
        ).strip(),
        extra_headers=(
            dict(extra_headers)
            if extra_headers is not None
            else _parse_headers(os.environ.get("TRACEPILOT_OTEL_HEADERS", ""))
        ),
        service_name=(os.environ.get("TRACEPILOT_OTEL_SERVICE_NAME") or "tracepilot").strip(),
        service_version=(os.environ.get("TRACEPILOT_OTEL_SERVICE_VERSION") or "2.0").strip(),
        deployment_environment=str(deployment).strip() or "local",
        traces_dir=Path(traces_dir) if traces_dir else DEFAULT_TRACES_DIR,
        state_path=Path(state_path) if state_path else DEFAULT_STATE_PATH,
        timeout_seconds=_env_float("TRACEPILOT_OTEL_TIMEOUT", 15.0) if timeout is None else float(timeout),
        max_retries=_env_int("TRACEPILOT_OTEL_MAX_RETRIES", 3) if max_retries is None else int(max_retries),
        batch_traces=_env_int("TRACEPILOT_OTEL_BATCH_TRACES", 20) if batch_traces is None else int(batch_traces),
        max_attr_chars=(
            _env_int("TRACEPILOT_OTEL_MAX_ATTR_CHARS", DEFAULT_MAX_ATTR_CHARS)
            if max_attr_chars is None
            else int(max_attr_chars)
        ),
    )


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class ExportError(RuntimeError):
    """导出流程的可预期失败（配置/网络/协议）。"""


class ExportStateError(ExportError):
    """状态文件损坏或版本不支持（fail-closed，不静默重置）。"""


class ExportLockError(ExportError):
    """同一状态文件已有导出进程在运行。"""


# ---------------------------------------------------------------------------
# 增量状态
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class TraceExportState:
    """导出状态（按 Trace 内容指纹做增量判断）。"""

    path: Path
    traces: dict[str, dict[str, Any]] = field(default_factory=dict)
    updated_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None

    @classmethod
    def load(cls, path: Path) -> "TraceExportState":
        if not path.is_file():
            return cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExportStateError(f"导出状态文件不可读或不是合法 JSON: {path} ({exc})") from exc
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            raise ExportStateError(
                f"导出状态文件版本不受支持（需要 version={STATE_VERSION}）: {path}"
            )
        traces = data.get("traces")
        if not isinstance(traces, dict):
            raise ExportStateError(f"导出状态缺少 traces 映射: {path}")
        return cls(
            path=path,
            traces=traces,
            updated_at=data.get("updated_at"),
            last_success_at=data.get("last_success_at"),
            last_error=data.get("last_error"),
        )

    def save(self, *, mark_success: bool = False, error: str | None = None) -> None:
        now = _utc_now_iso()
        self.updated_at = now
        if mark_success:
            self.last_success_at = now
            self.last_error = None
        elif error:
            self.last_error = error[:500]
        payload = {
            "version": STATE_VERSION,
            "updated_at": self.updated_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "traces": self.traces,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def mark_exported(self, trace_id: str, fingerprint: str, spans: int) -> None:
        self.traces[trace_id] = {
            "fingerprint": fingerprint,
            "exported_at": _utc_now_iso(),
            "spans": spans,
        }

    def is_exported(self, trace_id: str, fingerprint: str) -> bool:
        item = self.traces.get(trace_id)
        return isinstance(item, dict) and item.get("fingerprint") == fingerprint


class _StateLock:
    """基于 O_EXCL 的跨进程锁（避免两个导出器同时写状态）。"""

    def __init__(self, state_path: Path, stale_seconds: int = LOCK_STALE_SECONDS) -> None:
        self.path = state_path.with_name(state_path.name + ".lock")
        self.stale_seconds = stale_seconds
        self.acquired = False

    def __enter__(self) -> "_StateLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                age = 0
            if age > self.stale_seconds:
                try:
                    self.path.unlink()
                except OSError:
                    pass
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ExportLockError(
                f"已有导出进程在运行（锁文件 {self.path}）；如确认无进程在跑，可删除该锁文件后重试"
            ) from exc
        try:
            os.write(fd, f"pid={os.getpid()}\nat={_utc_now_iso()}\n".encode("utf-8"))
        finally:
            os.close(fd)
        self.acquired = True
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
        self.acquired = False


# ---------------------------------------------------------------------------
# 时间 / 属性工具
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_nanos(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return str(int(value.timestamp() * 1_000_000_000))


def _add_ms(value: datetime | str | None, duration_ms: int | float | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = int(duration_ms or 0) / 1000.0
    return str(int((value.timestamp() + delta) * 1_000_000_000))


def _attr(key: str, value: Any) -> dict[str, Any] | None:
    """把 Python 值转成 OTLP AnyValue；None 直接省略（不伪造缺失值）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if isinstance(value, (list, tuple)):
        values = [item for item in (_any_value(v) for v in value) if item is not None]
        return {"key": key, "value": {"arrayValue": {"values": values}}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _any_value(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        values = [item for item in (_any_value(v) for v in value) if item is not None]
        return {"arrayValue": {"values": values}}
    return {"stringValue": str(value)}


def _attributes(items: Iterable[tuple[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key, value in items:
        converted = _attr(key, value)
        if converted is not None:
            result.append(converted)
    return result


def _truncate_json(value: Any, max_chars: int) -> str | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json")
        except Exception:  # noqa: BLE001 - 序列化失败退回字符串
            value = str(value)
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > max_chars:
        return text[:max_chars] + f"…[truncated {len(text) - max_chars} chars]"
    return text


def _fingerprint(trace: TraceRecord) -> str:
    payload = json.dumps(trace.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _trace_hex(trace_id: str) -> str:
    """确定性 32 位十六进制 OTel traceId（非零）。"""
    return hashlib.sha256(f"tracepilot:trace:{trace_id}".encode("utf-8")).hexdigest()[:32]


def _span_hex(trace_id: str, step_id: str) -> str:
    """确定性 16 位十六进制 OTel spanId（非零）。"""
    return hashlib.sha256(f"tracepilot:span:{trace_id}:{step_id}".encode("utf-8")).hexdigest()[:16]


def _error_text(error: Any) -> str | None:
    """把 TraceStep.error / metadata.error 统一转成可读文本。"""
    if error is None:
        return None
    if hasattr(error, "error_message"):
        return str(getattr(error, "error_message"))
    if isinstance(error, dict):
        return str(error.get("error_message") or error.get("message") or error)
    return str(error)


def _is_error_status(status: str | None) -> bool:
    return str(status or "").lower() in {StepStatus.FAILED.value, "error", "timeout"}


def _span_status(status: str | None, message: str | None = None) -> dict[str, Any]:
    code = 2 if _is_error_status(status) else (1 if str(status or "").lower() == "succeeded" else 0)
    result: dict[str, Any] = {"code": code}
    if message:
        result["message"] = message[:500]
    return result


def _span_kind(step_type: StepType) -> int:
    """OTLP span kind：1=INTERNAL 2=SERVER 3=CLIENT 4=PRODUCER 5=CONSUMER。"""
    if step_type in {StepType.TOOL_CALL, StepType.RETRIEVAL, StepType.MODEL_CALL}:
        return 3  # CLIENT：向工具/模型/检索后端发起调用
    return 1


def _observation_type(step_type: StepType) -> str:
    if step_type in {StepType.MODEL_CALL}:
        return "generation"
    if step_type in {StepType.TOOL_CALL}:
        return "tool"
    if step_type in {StepType.RETRIEVAL}:
        return "retriever"
    if step_type in {StepType.PLANNING, StepType.REFLECTION}:
        return "agent"
    return "span"


def _step_name(step: TraceStep) -> str:
    mapping = {
        StepType.PLANNING: "agent.plan",
        StepType.MODEL_CALL: "gen_ai.chat",
        StepType.TOOL_CALL: f"tool.{step.tool_name or 'unknown'}",
        StepType.RETRIEVAL: "retrieval.search",
        StepType.REFLECTION: "agent.reflect",
        StepType.HUMAN_CONFIRMATION: "human.confirmation",
        StepType.EVALUATION: "evaluation.run",
        StepType.SAFETY_CHECK: "safety.check",
        StepType.ERROR_HANDLING: "error.handling",
    }
    return mapping.get(step.step_type, f"step.{step.step_type.value}")


# ---------------------------------------------------------------------------
# Trace → OTLP spans
# ---------------------------------------------------------------------------

def _root_span(trace: TraceRecord, max_attr_chars: int) -> dict[str, Any]:
    trace_id_hex = _trace_hex(trace.trace_id)
    root_span_id = _span_hex(trace.trace_id, "__root__")
    task = _truncate_json((trace.metadata or {}).get("task") or (trace.metadata or {}).get("task_description"), max_attr_chars)
    metadata = trace.metadata or {}
    attrs = _attributes(
        [
            ("langfuse.trace.name", _truncate_json(metadata.get("task") or trace.task_id, 200)),
            ("langfuse.observation.type", "span"),
            ("langfuse.observation.input", task),
            ("langfuse.trace.metadata.trace_id", trace.trace_id),
            ("langfuse.trace.metadata.run_id", trace.run_id),
            ("langfuse.trace.metadata.task_id", trace.task_id),
            ("langfuse.trace.metadata.execution_mode", metadata.get("execution_mode")),
            ("langfuse.trace.metadata.data_source", metadata.get("data_source")),
            ("tracepilot.trace_id", trace.trace_id),
            ("tracepilot.run_id", trace.run_id),
            ("tracepilot.task_id", trace.task_id),
            ("tracepilot.step_count", len(trace.steps or [])),
            ("tracepilot.status", trace.status.value if hasattr(trace.status, "value") else trace.status),
            ("tracepilot.execution_mode", metadata.get("execution_mode")),
            ("tracepilot.data_source", metadata.get("data_source")),
            ("tracepilot.model", metadata.get("model") or metadata.get("model_name")),
            ("tracepilot.token_usage.total_tokens", _trace_total_tokens(trace)),
        ]
    )
    events: list[dict[str, Any]] = []
    error = metadata.get("error")
    if error:
        events.append(
            {
                "timeUnixNano": _to_nanos(trace.ended_at or trace.started_at) or "0",
                "name": "exception",
                "attributes": _attributes(
                    [
                        ("exception.type", "TraceError"),
                        ("exception.message", _truncate_json(_error_text(error), 500)),
                    ]
                ),
            }
        )
    span: dict[str, Any] = {
        "traceId": trace_id_hex,
        "spanId": root_span_id,
        "name": "agent.run",
        "kind": 1,
        "startTimeUnixNano": _to_nanos(trace.started_at) or "0",
        "endTimeUnixNano": _to_nanos(trace.ended_at) or _to_nanos(trace.started_at) or "0",
        "attributes": attrs,
        "status": _span_status(
            trace.status.value if hasattr(trace.status, "value") else trace.status,
            _truncate_json(_error_text(error), 500) if error else None,
        ),
    }
    if events:
        span["events"] = events
    return span


def _trace_total_tokens(trace: TraceRecord) -> int | None:
    total = 0
    found = False
    for step in trace.steps or []:
        usage = step.token_usage
        if usage is not None and usage.total_tokens is not None:
            total += int(usage.total_tokens)
            found = True
    return total if found else None


def _step_span(
    trace: TraceRecord,
    step: TraceStep,
    *,
    max_attr_chars: int,
) -> dict[str, Any]:
    span_id = _span_hex(trace.trace_id, step.step_id)
    parents = [
        _span_hex(trace.trace_id, parent_id)
        for parent_id in (step.parent_step_ids or [])
        if parent_id
    ]
    if not parents:
        parents = [_span_hex(trace.trace_id, "__root__")]
    parent_span_id = next((p for p in parents if p and p != span_id), "")
    start = _to_nanos(step.timestamp) or "0"
    end = _add_ms(step.timestamp, step.duration_ms) or start
    usage = step.token_usage
    attrs = _attributes(
        [
            ("langfuse.observation.type", _observation_type(step.step_type)),
            ("langfuse.observation.input", _truncate_json(step.input, max_attr_chars)),
            ("langfuse.observation.output", _truncate_json(step.output, max_attr_chars)),
            ("langfuse.observation.level", "ERROR" if _is_error_status(step.status.value) else "DEFAULT"),
            ("tracepilot.trace_id", trace.trace_id),
            ("tracepilot.step_id", step.step_id),
            ("tracepilot.step_type", step.step_type.value),
            ("tracepilot.status", step.status.value),
            ("tracepilot.duration_ms", step.duration_ms),
            ("tracepilot.tool_name", step.tool_name),
            ("tracepilot.tool_arguments", _truncate_json(step.tool_arguments, max_attr_chars)),
            ("tracepilot.tool_result", _truncate_json(step.tool_result, max_attr_chars)),
            ("gen_ai.operation.name", "chat" if step.step_type is StepType.MODEL_CALL else None),
            (
                "gen_ai.system",
                (step.metadata or {}).get("usage_meta", {}).get("provider")
                if isinstance((step.metadata or {}).get("usage_meta"), dict)
                else None,
            ),
            (
                "gen_ai.request.model",
                (step.metadata or {}).get("usage_meta", {}).get("model")
                if isinstance((step.metadata or {}).get("usage_meta"), dict)
                else None,
            ),
            ("gen_ai.usage.input_tokens", int(usage.input_tokens) if usage and usage.input_tokens is not None else None),
            ("gen_ai.usage.output_tokens", int(usage.output_tokens) if usage and usage.output_tokens is not None else None),
            ("tracepilot.token_usage.input_tokens", usage.input_tokens if usage else None),
            ("tracepilot.token_usage.output_tokens", usage.output_tokens if usage else None),
            ("tracepilot.token_usage.total_tokens", usage.total_tokens if usage else None),
        ]
    )
    events: list[dict[str, Any]] = []
    if step.error:
        events.append(
            {
                "timeUnixNano": end,
                "name": "exception",
                "attributes": _attributes(
                    [
                        ("exception.type", "StepError"),
                        ("exception.message", _truncate_json(_error_text(step.error), 500)),
                    ]
                ),
            }
        )
    span: dict[str, Any] = {
        "traceId": _trace_hex(trace.trace_id),
        "spanId": span_id,
        "name": _step_name(step),
        "kind": _span_kind(step.step_type),
        "startTimeUnixNano": start,
        "endTimeUnixNano": end,
        "attributes": attrs,
        "status": _span_status(step.status.value, _truncate_json(_error_text(step.error), 500) if step.error else None),
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    if events:
        span["events"] = events
    return span


def trace_to_otlp(
    trace: TraceRecord,
    *,
    max_attr_chars: int = DEFAULT_MAX_ATTR_CHARS,
) -> dict[str, Any]:
    """把一条 Trace 转成 OTLP ``scopeSpans`` 结构（不含 resource/scope 包装）。"""
    spans = [_root_span(trace, max_attr_chars)]
    spans.extend(_step_span(trace, step, max_attr_chars=max_attr_chars) for step in (trace.steps or []))
    return spans


def build_otlp_payload(
    traces: Sequence[TraceRecord],
    config: ExportConfig,
) -> dict[str, Any]:
    """构造完整 OTLP/HTTP JSON 负载（多 Trace 合批，共享 resource/scope）。"""
    resource_attrs = _attributes(
        [
            ("service.name", config.service_name),
            ("service.version", config.service_version),
            ("deployment.environment", config.deployment_environment),
            ("telemetry.sdk.name", EXPORTER_NAME),
            ("telemetry.sdk.version", EXPORTER_VERSION),
            ("telemetry.sdk.language", "python"),
        ]
    )
    all_spans: list[dict[str, Any]] = []
    for trace in traces:
        all_spans.extend(trace_to_otlp(trace, max_attr_chars=config.max_attr_chars))
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource_attrs},
                "scopeSpans": [
                    {
                        "scope": {"name": "tracepilot.agent", "version": config.service_version},
                        "spans": all_spans,
                    }
                ],
            }
        ]
    }


# ---------------------------------------------------------------------------
# HTTP 发送
# ---------------------------------------------------------------------------

def _auth_headers(config: ExportConfig) -> dict[str, str]:
    headers = {
        "Content-Type": OTLP_CONTENT_TYPE,
        "Accept": OTLP_CONTENT_TYPE,
        "User-Agent": config.user_agent,
    }
    if config.auth_mode == "basic":
        token = base64.b64encode(f"{config.public_key}:{config.secret_key}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    elif config.auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {config.bearer_token}"
    headers.update(config.extra_headers)
    return headers


def _post_otlp(config: ExportConfig, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        config.endpoint,
        data=body,
        headers=_auth_headers(config),
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(max(1, config.max_retries) + 1):
        try:
            with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
                if 200 <= response.status < 300:
                    return
                raise ExportError(f"OTLP 导出失败：HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(400).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 读取错误响应体失败不应掩盖原始错误
                detail = ""
            last_error = ExportError(f"OTLP 导出失败：HTTP {exc.code} {detail[:200]}")
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt >= config.max_retries:
                raise last_error from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = ExportError(f"OTLP 网络错误：{type(exc).__name__}: {exc}")
            if attempt >= config.max_retries:
                raise last_error from exc
        time.sleep(min(2.0 * (2**attempt), 8.0))
    raise last_error or ExportError("OTLP 导出失败：未知错误")


# ---------------------------------------------------------------------------
# 增量导出主流程
# ---------------------------------------------------------------------------

def load_traces_for_export(
    *,
    traces_dir: Path | None = None,
    limit: int | None = None,
    trace_id: str | None = None,
) -> list[TraceRecord]:
    """从本地 Trace 目录读取 Trace（只读，不修改原始数据）。"""
    directory = Path(traces_dir) if traces_dir else DEFAULT_TRACES_DIR
    if trace_id:
        from backend.app.services.trace_store import load_trace

        try:
            return [load_trace(trace_id, traces_dir=directory)]
        except Exception as exc:  # noqa: BLE001 - 统一转导出错误
            raise ExportError(f"读取 Trace 失败：{trace_id} ({exc})") from exc
    if not directory.is_dir():
        return []
    from backend.app.services.trace_store import load_trace  # 延迟导入，避免测试环境副作用

    traces: list[TraceRecord] = []
    for path in sorted(directory.glob("*.json")):
        try:
            traces.append(load_trace(path.stem, traces_dir=directory))
        except Exception:  # noqa: BLE001 - 单条损坏不阻断整批导出
            continue
        if limit is not None and len(traces) >= limit:
            break
    return traces


@dataclass(slots=True)
class ExportResult:
    scanned: int = 0
    eligible: int = 0
    exported: int = 0
    skipped: int = 0
    failed: int = 0
    spans: int = 0
    batches: int = 0
    dry_run: bool = False
    state_file: str = ""
    endpoint: str = ""
    payloads: list[Path] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "eligible": self.eligible,
            "exported": self.exported,
            "skipped": self.skipped,
            "failed": self.failed,
            "spans": self.spans,
            "batches": self.batches,
            "dry_run": self.dry_run,
            "state_file": self.state_file,
            "endpoint": self.endpoint,
            "payloads": [str(p) for p in self.payloads],
            "failures": self.failures,
        }


def _chunks(items: Sequence[TraceRecord], size: int) -> Iterator[Sequence[TraceRecord]]:
    size = max(1, int(size))
    for index in range(0, len(items), size):
        yield items[index : index + size]


def export_traces(
    traces: Sequence[TraceRecord],
    *,
    config: ExportConfig,
    state: TraceExportState,
    dry_run: bool = True,
    force: bool = False,
    output_dir: Path | None = None,
) -> ExportResult:
    """导出给定 Trace 集合（网络或 dry-run），成功后推进增量状态。"""
    result = ExportResult(
        scanned=len(traces),
        dry_run=dry_run,
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

    if dry_run:
        out = output_dir or (PROJECT_ROOT / "data" / "observability" / "dry_run")
        out.mkdir(parents=True, exist_ok=True)
        for trace in pending:
            payload = build_otlp_payload([trace], config)
            target = out / f"{trace.trace_id}.otlp.json"
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            result.payloads.append(target)
            result.exported += 1
            result.spans += len(payload["resourceSpans"][0]["scopeSpans"][0]["spans"])
        return result

    if not config.configured:
        raise ExportError("未配置 OTLP endpoint：请设置 LANGFUSE_HOST 或 TRACEPILOT_OTEL_ENDPOINT")

    for batch in _chunks(pending, config.batch_traces):
        payload = build_otlp_payload(batch, config)
        span_count = len(payload["resourceSpans"][0]["scopeSpans"][0]["spans"])
        try:
            _post_otlp(config, payload)
        except ExportError as exc:
            result.failed += len(batch)
            result.failures = [*result.failures, *[{"trace_id": t.trace_id, "error": str(exc)} for t in batch]]
            continue
        result.batches += 1
        result.exported += len(batch)
        result.spans += span_count
        for trace in batch:
            state.mark_exported(trace.trace_id, fingerprints[trace.trace_id], span_count // max(1, len(batch)))
    return result


def export_to_langfuse(
    *,
    config: ExportConfig | None = None,
    dry_run: bool | None = None,
    force: bool = False,
    limit: int | None = None,
    trace_id: str | None = None,
    traces: Sequence[TraceRecord] | None = None,
    output_dir: Path | None = None,
    yes: bool = False,
    progress: Any = None,
) -> ExportResult:
    """完整导出流程：读 Trace → 增量判断 → 发送/dry-run → 原子推进状态。

    Args:
        config: 配置；None 时从环境变量构造。
        dry_run: None 表示按 ``config.enabled`` 决定；显式 True 强制 dry-run。
        force: 忽略增量状态重新导出。
        limit: 最多检查多少条 Trace。
        trace_id: 只导出指定 Trace。
        traces: 直接传入 Trace 列表（自动化/测试用；给定时不再读磁盘）。
        output_dir: dry-run 负载输出目录。
        yes: 真实发送必须显式确认（或环境变量 enabled=1）。
        progress: 可选回调 ``callable(result)``，用于 CLI 进度输出。
    """
    cfg = config or build_export_config()
    effective_dry_run = (not cfg.enabled) if dry_run is None else bool(dry_run)
    if not effective_dry_run and not yes and not cfg.enabled:
        raise ExportError(
            "真实导出需要显式确认：请加 --yes，或设置 TRACEPILOT_LANGFUSE_ENABLED=1（默认只做 dry-run）"
        )
    with _StateLock(cfg.state_path):
        state = TraceExportState.load(cfg.state_path)
        selected = (
            list(traces)
            if traces is not None
            else load_traces_for_export(traces_dir=cfg.traces_dir, limit=limit, trace_id=trace_id)
        )
        try:
            result = export_traces(
                selected,
                config=cfg,
                state=state,
                dry_run=effective_dry_run,
                force=force,
                output_dir=output_dir,
            )
        except ExportError as exc:
            state.save(error=str(exc))
            raise
        # dry-run 不代表真实导出成功：不推进增量状态，避免后续真实导出被误跳过。
        if not effective_dry_run:
            if result.failed:
                state.save(error=f"{result.failed} 条 Trace 导出失败")
            else:
                state.save(mark_success=True)
        if progress is not None:
            progress(result)
        return result


# ---------------------------------------------------------------------------
# 只读状态（供 API / 运维查看，不包含任何密钥）
# ---------------------------------------------------------------------------

def describe_export_status(config: ExportConfig | None = None) -> dict[str, Any]:
    cfg = config or build_export_config()
    state_info: dict[str, Any] = {
        "state_file": str(cfg.state_path),
        "exported_traces": 0,
        "last_success_at": None,
        "last_error": None,
    }
    try:
        state = TraceExportState.load(cfg.state_path)
        state_info.update(
            exported_traces=len(state.traces),
            last_success_at=state.last_success_at,
            last_error=state.last_error,
        )
    except ExportStateError as exc:
        state_info["last_error"] = f"状态文件异常：{exc}"
    return {
        "enabled": bool(cfg.enabled),
        "configured": cfg.configured,
        "endpoint": cfg.safe_endpoint(),
        "auth_mode": cfg.auth_mode,
        "service_name": cfg.service_name,
        "traces_dir": str(cfg.traces_dir),
        "state": state_info,
        "note": "只读状态：不发起网络请求、不返回任何密钥",
    }
