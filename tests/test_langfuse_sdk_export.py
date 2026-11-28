"""LangFuse 官方 SDK 导出通道测试：真实 SDK + 本地 mock 服务（无外网、无真实 LangFuse）。

验证：
1. SDK 可被识别/加载；
2. 类型化 OtelResourceSpan 转换正确（traceId/spanId/父子关系/属性）；
3. 通过 SDK 的 OpenAPI 接口发送到 mock LangFuse，路径/鉴权头正确；
4. 增量状态生效（第二次跳过）；
5. 5xx 失败不推进状态；
6. 缺少 key / 缺少 SDK 时给出明确错误。
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("langfuse")

from backend.app.services import langfuse_export as lf  # noqa: E402
from backend.app.services import langfuse_sdk_export as sdk_export  # noqa: E402
from tests.test_langfuse_export import TRACE_ID, make_trace  # noqa: E402


class _MockLangfuse:
    """模拟 LangFuse 的 OTel 接收端点。"""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = list(statuses or [200])
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                status = server.statuses.pop(0) if server.statuses else 200
                server.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "raw": body,
                    }
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def otel_url(self) -> str:
        return self.host + "/api/public/otel/v1/traces"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def mock_langfuse():
    server = _MockLangfuse()
    yield server
    server.stop()


def _config(tmp_path: Path, server: _MockLangfuse, **overrides):
    params = {
        "enabled": True,
        "endpoint": server.otel_url,
        "langfuse_host": server.host,
        "public_key": f"pk-{uuid.uuid4().hex}",
        "secret_key": "sk-test",
        "state_path": tmp_path / "sdk-state.json",
        "traces_dir": tmp_path / "traces",
        "timeout": 10.0,
        "max_retries": 0,
        "batch_traces": 20,
    }
    params.update(overrides)
    return lf.build_export_config(**params)


def test_sdk_available_and_version() -> None:
    assert sdk_export.sdk_available() is True
    version = sdk_export.sdk_version()
    assert version and version != "unknown"


def test_build_sdk_resource_spans_conversion(tmp_path: Path, mock_langfuse: _MockLangfuse) -> None:
    config = _config(tmp_path, mock_langfuse)
    spans = sdk_export.build_sdk_resource_spans([make_trace()], config)
    assert len(spans) == 1
    flat = [
        span
        for resource_span in spans
        for scope_span in (resource_span.scope_spans or [])
        for span in (scope_span.spans or [])
    ]
    assert len(flat) == 4
    root = flat[0]
    assert len(root.trace_id) == 32
    assert len(root.span_id) == 16
    by_name = {span.name: span for span in flat}
    assert "agent.run" in by_name
    assert by_name["gen_ai.chat"].parent_span_id == lf._span_hex(TRACE_ID, "step-1")
    attrs = {item.key: item.value for item in (by_name["gen_ai.chat"].attributes or [])}
    assert int(attrs["gen_ai.usage.input_tokens"].int_value) == 10


def test_export_via_sdk_hits_langfuse_otel_endpoint(tmp_path: Path, mock_langfuse: _MockLangfuse) -> None:
    config = _config(tmp_path, mock_langfuse)
    state = lf.TraceExportState(path=tmp_path / "sdk-state.json")
    result = sdk_export.export_traces_via_sdk([make_trace()], config=config, state=state)
    assert result.failed == 0 and result.exported == 1 and result.spans == 4
    assert result.batches == 1
    assert len(mock_langfuse.requests) == 1
    request = mock_langfuse.requests[0]
    assert request["path"] == "/api/public/otel/v1/traces"
    assert request["headers"]["authorization"].startswith("Basic ")
    # SDK 以 JSON 形式发送 OTLP（内部类型化序列化），负载可被解析且含关键 span 名
    body = json.loads(request["raw"].decode("utf-8"))
    text = json.dumps(body, ensure_ascii=False)
    assert "agent.run" in text or "gen_ai.chat" in text
    assert TRACE_ID in state.traces


def test_sdk_export_incremental_skip(tmp_path: Path, mock_langfuse: _MockLangfuse) -> None:
    config = _config(tmp_path, mock_langfuse)
    trace = make_trace()
    state = lf.TraceExportState(path=tmp_path / "sdk-state.json")
    first = sdk_export.export_traces_via_sdk([trace], config=config, state=state)
    second = sdk_export.export_traces_via_sdk([trace], config=config, state=state)
    assert first.exported == 1
    assert second.exported == 0 and second.skipped == 1
    assert len(mock_langfuse.requests) == 1


def test_sdk_export_failure_does_not_mark_state(tmp_path: Path) -> None:
    server = _MockLangfuse(statuses=[500] * 6)
    try:
        config = _config(tmp_path, server)
        state = lf.TraceExportState(path=tmp_path / "sdk-state.json")
        result = sdk_export.export_traces_via_sdk([make_trace()], config=config, state=state)
        assert result.failed == 1 and result.exported == 0
        assert TRACE_ID not in state.traces
        assert result.failures and "SDK 导出失败" in result.failures[0]["error"]
    finally:
        server.stop()


def test_sdk_requires_keys(tmp_path: Path, mock_langfuse: _MockLangfuse) -> None:
    config = _config(tmp_path, mock_langfuse, public_key="", secret_key="")
    state = lf.TraceExportState(path=tmp_path / "sdk-state.json")
    with pytest.raises(lf.ExportError, match="LANGFUSE_PUBLIC_KEY"):
        sdk_export.export_traces_via_sdk([make_trace()], config=config, state=state)
