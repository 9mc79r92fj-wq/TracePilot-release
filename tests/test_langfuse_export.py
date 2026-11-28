"""LangFuse/OTLP 增量导出测试（全程本地 mock，无外网、无真实 LangFuse 依赖）。

覆盖：
1. OTLP 负载结构（traceId/spanId、父子关系、resource/scope）；
2. OTel GenAI + LangFuse 观测属性映射；
3. 错误步骤的 status/exception event；
4. dry-run 只写文件、不发网络；
5. 增量跳过 / 内容变化重导 / --force；
6. Basic 鉴权头与自定义 header；
7. 5xx 重试、4xx 不重试、失败不推进状态；
8. 无 --yes 的确认保护、并发锁、损坏状态 fail-closed；
9. 密钥不进入负载/状态输出；
10. CLI 与只读状态 API。
"""

from __future__ import annotations

import json
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from backend.app.schemas.trace import (
    StepStatus,
    StepType,
    TokenUsage,
    TraceError,
    TraceRecord,
    TraceStatus,
)
from backend.app.services import langfuse_export as lf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TRACE_ID = "trace-abc1234567890abc"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 构造测试数据
# ---------------------------------------------------------------------------

def make_step(
    step_id: str,
    *,
    step_type: StepType = StepType.MODEL_CALL,
    status: StepStatus = StepStatus.SUCCEEDED,
    parent_step_ids: list[str] | None = None,
    tool_name: str | None = None,
    token_usage: TokenUsage | None = None,
    error: TraceError | None = None,
    seconds: int = 0,
    duration_ms: int = 100,
) -> object:
    return {
        "step_id": step_id,
        "trace_id": TRACE_ID,
        "parent_step_ids": parent_step_ids or [],
        "step_type": step_type.value,
        "timestamp": (START + timedelta(seconds=seconds)).isoformat(),
        "input": {"task": "计算 1+1"},
        "output": "结果 2",
        "tool_name": tool_name,
        "tool_arguments": {"expression": "1+1"} if tool_name else None,
        "tool_result": {"value": 2} if tool_name else None,
        "status": status.value,
        "error": (
            {"error_type": error.error_type, "error_message": error.error_message, "recoverable": error.recoverable}
            if error
            else None
        ),
        "duration_ms": duration_ms,
        "token_usage": (
            {
                "input_tokens": token_usage.input_tokens,
                "output_tokens": token_usage.output_tokens,
                "total_tokens": token_usage.total_tokens,
            }
            if token_usage
            else None
        ),
        "metadata": {"usage_meta": {"provider": "deepseek", "model": "deepseek-flash"}} if step_type is StepType.MODEL_CALL else {},
    }


def make_trace(
    *,
    trace_id: str = TRACE_ID,
    status: StepStatus = StepStatus.SUCCEEDED,
    steps: list[dict] | None = None,
    execution_mode: str = "mock",
) -> TraceRecord:
    return TraceRecord.model_validate(
        {
            "trace_id": trace_id,
            "task_id": "task-1",
            "run_id": "run-1",
            "started_at": START.isoformat(),
            "ended_at": (START + timedelta(seconds=5)).isoformat(),
            "status": TraceStatus.SUCCEEDED.value if status is StepStatus.SUCCEEDED else TraceStatus.FAILED.value,
            "steps": steps
            if steps is not None
            else [
                make_step("step-1", step_type=StepType.PLANNING, seconds=0),
                make_step(
                    "step-2",
                    step_type=StepType.MODEL_CALL,
                    parent_step_ids=["step-1"],
                    token_usage=TokenUsage(input_tokens=10, output_tokens=5),
                    seconds=1,
                ),
                make_step(
                    "step-3",
                    step_type=StepType.TOOL_CALL,
                    tool_name="calculator",
                    parent_step_ids=["step-2"],
                    seconds=2,
                ),
            ],
            "metadata": {
                "task": "计算 1+1",
                "execution_mode": execution_mode,
                "data_source": execution_mode,
                "model": "deepseek-flash",
            },
        }
    )


def make_config(tmp_path: Path, **overrides) -> lf.ExportConfig:
    params = {
        "enabled": True,
        "endpoint": "http://127.0.0.1:9/otlp/v1/traces",
        "public_key": "pk-test",
        "secret_key": "sk-test",
        "state_path": tmp_path / "state.json",
        "traces_dir": tmp_path / "traces",
        "timeout": 2.0,
        "max_retries": 1,
        "batch_traces": 20,
    }
    params.update(overrides)
    return lf.build_export_config(**params)


# ---------------------------------------------------------------------------
# Mock OTLP 服务
# ---------------------------------------------------------------------------

class _MockOtlpServer:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = list(statuses or [200])
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - HTTP 方法名
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                status = server.statuses.pop(0) if server.statuses else 200
                server.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(body.decode("utf-8")),
                    }
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args) -> None:  # 静默测试服务器日志
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/public/otel/v1/traces"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def otlp_server():
    server = _MockOtlpServer()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "LANGFUSE_HOST",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "TRACEPILOT_OTEL_ENDPOINT",
        "TRACEPILOT_OTEL_TOKEN",
        "TRACEPILOT_OTEL_HEADERS",
        "TRACEPILOT_LANGFUSE_ENABLED",
        "TRACEPILOT_OTEL_SERVICE_NAME",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# 1. 负载结构 / 映射
# ---------------------------------------------------------------------------

def test_otlp_payload_structure_and_ids(tmp_path: Path) -> None:
    trace = make_trace()
    config = make_config(tmp_path)
    payload = lf.build_otlp_payload([trace], config)

    resource_spans = payload["resourceSpans"]
    assert len(resource_spans) == 1
    attrs = {item["key"]: item["value"] for item in resource_spans[0]["resource"]["attributes"]}
    assert attrs["service.name"]["stringValue"] == "tracepilot"

    spans = resource_spans[0]["scopeSpans"][0]["spans"]
    assert len(spans) == 4  # 1 个 agent.run + 3 个步骤
    trace_ids = {span["traceId"] for span in spans}
    assert trace_ids == {lf._trace_hex(TRACE_ID)}
    assert re.fullmatch(r"[0-9a-f]{32}", spans[0]["traceId"])
    assert all(re.fullmatch(r"[0-9a-f]{16}", span["spanId"]) for span in spans)
    assert len({span["spanId"] for span in spans}) == len(spans)

    root = spans[0]
    assert root["name"] == "agent.run"
    assert "parentSpanId" not in root
    # 无显式父节点的步骤挂根 span；有父节点的步骤挂到对应步骤
    assert spans[1]["parentSpanId"] == root["spanId"]          # planning 无父节点
    assert spans[2]["parentSpanId"] == lf._span_hex(TRACE_ID, "step-1")
    assert spans[3]["parentSpanId"] == lf._span_hex(TRACE_ID, "step-2")


def test_step_parent_relationships_and_order(tmp_path: Path) -> None:
    trace = make_trace()
    config = make_config(tmp_path)
    spans = lf.build_otlp_payload([trace], config)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    by_name = {span["name"]: span for span in spans}
    assert by_name["agent.plan"]["parentSpanId"] == spans[0]["spanId"]
    assert by_name["gen_ai.chat"]["parentSpanId"] == lf._span_hex(TRACE_ID, "step-1")
    assert by_name["tool.calculator"]["parentSpanId"] == lf._span_hex(TRACE_ID, "step-2")
    # 时间戳递增、纳秒字符串
    assert int(by_name["tool.calculator"]["startTimeUnixNano"]) > int(by_name["agent.plan"]["startTimeUnixNano"])


def test_genai_and_langfuse_attributes(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    trace = make_trace()
    spans = lf.build_otlp_payload([trace], config)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    chat = next(s for s in spans if s["name"] == "gen_ai.chat")
    attrs = {item["key"]: item["value"] for item in chat["attributes"]}
    assert attrs["gen_ai.system"]["stringValue"] == "deepseek"
    assert attrs["gen_ai.request.model"]["stringValue"] == "deepseek-flash"
    assert attrs["gen_ai.usage.input_tokens"]["intValue"] == "10"
    assert attrs["gen_ai.usage.output_tokens"]["intValue"] == "5"
    assert attrs["langfuse.observation.type"]["stringValue"] == "generation"
    assert "计算 1+1" in attrs["langfuse.observation.input"]["stringValue"]
    assert chat["kind"] == 3
    tool = next(s for s in spans if s["name"] == "tool.calculator")
    tool_attrs = {item["key"]: item["value"] for item in tool["attributes"]}
    assert tool_attrs["langfuse.observation.type"]["stringValue"] == "tool"
    assert tool_attrs["tracepilot.tool_name"]["stringValue"] == "calculator"


def test_error_step_has_error_status_and_exception_event(tmp_path: Path) -> None:
    trace = make_trace(
        status=StepStatus.FAILED,
        steps=[
            make_step(
                "step-1",
                step_type=StepType.TOOL_CALL,
                status=StepStatus.FAILED,
                tool_name="calculator",
                error=TraceError(error_type="ToolTimeout", error_message="工具超时"),
                duration_ms=1500,
            )
        ],
    )
    config = make_config(tmp_path)
    spans = lf.build_otlp_payload([trace], config)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    failed = next(s for s in spans if s["name"] == "tool.calculator")
    assert failed["status"]["code"] == 2
    assert failed["status"]["message"] == "工具超时"
    assert failed["events"][0]["name"] == "exception"
    assert len(failed["events"][0]["attributes"]) >= 2


def test_deterministic_ids_are_stable(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    a = lf.build_otlp_payload([make_trace()], config)
    b = lf.build_otlp_payload([make_trace()], config)
    assert a == b


def test_large_fields_truncated(tmp_path: Path) -> None:
    trace = make_trace(steps=[make_step("step-1", step_type=StepType.PLANNING)])
    trace.steps[0].output = "x" * 50_000
    config = make_config(tmp_path, max_attr_chars=100)
    spans = lf.build_otlp_payload([trace], config)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    output = {i["key"]: i["value"] for i in spans[0]["attributes"]}["langfuse.observation.input"]
    assert "truncated" not in output["stringValue"]  # input 很小
    step_attrs = {i["key"]: i["value"] for i in spans[1]["attributes"]}
    assert "truncated" in step_attrs["langfuse.observation.output"]["stringValue"]


# ---------------------------------------------------------------------------
# 2. dry-run / 增量
# ---------------------------------------------------------------------------

def test_dry_run_writes_payload_without_network(tmp_path: Path) -> None:
    trace = make_trace()
    config = make_config(tmp_path, endpoint="")  # 即便未配置 endpoint 也能 dry-run
    state = lf.TraceExportState(path=tmp_path / "state.json")
    result = lf.export_traces([trace], config=config, state=state, dry_run=True, output_dir=tmp_path / "out")
    assert result.dry_run is True
    assert result.exported == 1
    assert result.spans == 4
    payload_file = tmp_path / "out" / f"{TRACE_ID}.otlp.json"
    assert payload_file.is_file()
    assert "resourceSpans" in json.loads(payload_file.read_text(encoding="utf-8"))
    assert result.batches == 0


def test_incremental_export_skips_unchanged(tmp_path: Path, otlp_server: _MockOtlpServer) -> None:
    trace = make_trace()
    config = make_config(tmp_path, endpoint=otlp_server.url)
    first = lf.export_to_langfuse(config=config, dry_run=False, yes=True, traces=[trace])
    assert first.exported == 1 and first.skipped == 0 and first.failed == 0
    assert len(otlp_server.requests) == 1
    state = lf.TraceExportState.load(tmp_path / "state.json")
    assert TRACE_ID in state.traces
    assert state.last_success_at

    second = lf.export_to_langfuse(config=config, dry_run=False, yes=True, traces=[trace])
    assert second.exported == 0 and second.skipped == 1
    assert len(otlp_server.requests) == 1  # 增量命中：没有再次发送


def test_changed_trace_is_reexported_and_force_works(tmp_path: Path, otlp_server: _MockOtlpServer) -> None:
    config = make_config(tmp_path, endpoint=otlp_server.url)
    lf.export_to_langfuse(config=config, dry_run=False, yes=True, traces=[make_trace()])

    changed = make_trace()
    changed.steps[1].output = "新的模型输出"
    result = lf.export_traces(
        [changed],
        config=config,
        state=lf.TraceExportState.load(tmp_path / "state.json"),
        dry_run=False,
    )
    assert result.exported == 1
    forced = lf.export_to_langfuse(config=config, dry_run=False, yes=True, force=True, traces=[changed])
    assert forced.exported == 1
    assert len(otlp_server.requests) == 3


def test_batches_multiple_traces(tmp_path: Path, otlp_server: _MockOtlpServer) -> None:
    config = make_config(tmp_path, endpoint=otlp_server.url, batch_traces=2)
    traces = [make_trace(trace_id=f"trace-batch{i:03d}") for i in range(5)]
    result = lf.export_to_langfuse(config=config, dry_run=False, yes=True, traces=traces)
    assert result.exported == 5
    assert result.batches == 3
    assert len(otlp_server.requests) == 3


# ---------------------------------------------------------------------------
# 3. 鉴权 / 重试 / 错误
# ---------------------------------------------------------------------------

def test_basic_auth_header_and_custom_headers(tmp_path: Path, otlp_server: _MockOtlpServer) -> None:
    config = make_config(
        tmp_path,
        endpoint=otlp_server.url,
        public_key="pk-123",
        secret_key="sk-456",
        extra_headers={"X-Scope": "tracepilot"},
    )
    lf.export_to_langfuse(config=config, dry_run=False, yes=True, traces=[make_trace()])
    headers = otlp_server.requests[0]["headers"]
    assert headers["content-type"] == "application/json"
    assert headers["authorization"].startswith("Basic ")
    assert headers["x-scope"] == "tracepilot"


def test_retry_on_5xx(tmp_path: Path) -> None:
    server = _MockOtlpServer(statuses=[503, 200])
    try:
        config = make_config(tmp_path, endpoint=server.url, max_retries=2)
        result = lf.export_to_langfuse(config=config, dry_run=False, yes=True, traces=[make_trace()])
        assert result.failed == 0 and result.exported == 1
        assert len(server.requests) == 2
    finally:
        server.stop()


def test_no_retry_on_401_and_state_not_advanced(tmp_path: Path) -> None:
    server = _MockOtlpServer(statuses=[401])
    try:
        config = make_config(tmp_path, endpoint=server.url, max_retries=3)
        result = lf.export_to_langfuse(config=config, dry_run=False, yes=True, traces=[make_trace()])
        assert result.failed == 1 and result.exported == 0
        assert len(server.requests) == 1
        state = lf.TraceExportState.load(tmp_path / "state.json")
        assert TRACE_ID not in state.traces
        assert "失败" in (state.last_error or "")
    finally:
        server.stop()


def test_requires_explicit_yes(tmp_path: Path) -> None:
    config = make_config(tmp_path, enabled=False)
    with pytest.raises(lf.ExportError, match="--yes"):
        lf.export_to_langfuse(config=config, dry_run=False, yes=False, traces=[make_trace()])


def test_concurrent_lock(tmp_path: Path) -> None:
    config = make_config(tmp_path, enabled=True)
    lock = tmp_path / "state.json.lock"
    lock.write_text("pid=1", encoding="utf-8")
    with pytest.raises(lf.ExportLockError):
        lf.export_to_langfuse(config=config, dry_run=True, traces=[make_trace()])
    lock.unlink()
    result = lf.export_to_langfuse(config=config, dry_run=True, traces=[make_trace()])
    assert result.dry_run is True


def test_corrupt_state_fails_closed(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(lf.ExportStateError):
        lf.export_to_langfuse(config=make_config(tmp_path), dry_run=True, traces=[make_trace()])


# ---------------------------------------------------------------------------
# 4. 密钥不泄露 / 状态接口 / CLI
# ---------------------------------------------------------------------------

def test_secret_never_in_payload_or_status(tmp_path: Path, monkeypatch, otlp_server: _MockOtlpServer) -> None:
    secret = "sk-super-secret-value-123456"
    config = make_config(
        tmp_path,
        endpoint=otlp_server.url,
        public_key="pk-public",
        secret_key=secret,
    )
    lf.export_to_langfuse(config=config, dry_run=False, yes=True, traces=[make_trace()])
    assert secret not in json.dumps(otlp_server.requests[0]["body"])
    status = lf.describe_export_status(config)
    assert secret not in json.dumps(status, ensure_ascii=False)
    assert status["auth_mode"] == "basic"
    assert status["enabled"] is True


def test_describe_status_default_disabled(tmp_path: Path) -> None:
    status = lf.describe_export_status(make_config(tmp_path, enabled=False, endpoint=""))
    assert status["enabled"] is False
    assert status["configured"] is False
    assert status["state"]["exported_traces"] == 0
    assert "密钥" in status["note"] or "secret" in status["note"].lower()


def test_readonly_status_api() -> None:
    from fastapi.testclient import TestClient

    from backend.app.main import app

    response = TestClient(app).get("/api/observability/langfuse")
    assert response.status_code == 200
    body = response.json()
    assert "enabled" in body and "state" in body
    assert "sdk" in body and "available" in body["sdk"]
    assert "secret_key" not in json.dumps(body, ensure_ascii=False).lower()


def test_cli_dry_run_and_status(tmp_path: Path, caplog, capsys) -> None:
    from scripts import export_langfuse as cli

    trace = make_trace()
    traces_dir = tmp_path / "traces"
    traces_dir.mkdir()
    (traces_dir / f"{TRACE_ID}.json").write_text(
        json.dumps(trace.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    exit_code = cli.main(
        [
            "--dry-run",
            "--traces-dir",
            str(traces_dir),
            "--state-file",
            str(tmp_path / "state.json"),
            "--out",
            str(tmp_path / "out"),
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["exported"] == 1 and payload["dry_run"] is True
    assert (tmp_path / "out" / f"{TRACE_ID}.otlp.json").is_file()

    status_code = cli.main(["--status", "--json", "--traces-dir", str(traces_dir), "--state-file", str(tmp_path / "s2.json")])
    assert status_code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["enabled"] is False


def test_dry_run_does_not_advance_incremental_state(tmp_path: Path, otlp_server: _MockOtlpServer) -> None:
    """dry-run 只是预览：不能写入增量状态，否则真实导出会被误跳过。"""
    config = make_config(tmp_path, endpoint=otlp_server.url)
    preview = lf.export_to_langfuse(config=config, dry_run=True, traces=[make_trace()])
    assert preview.exported == 1
    assert not (tmp_path / "state.json").exists()

    real = lf.export_to_langfuse(config=config, dry_run=False, yes=True, traces=[make_trace()])
    assert real.exported == 1 and real.skipped == 0
    assert len(otlp_server.requests) == 1
    assert (tmp_path / "state.json").is_file()
