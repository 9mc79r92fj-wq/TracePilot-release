# -*- coding: utf-8 -*-
"""本地脚本化大模型服务（OpenAI Chat Completions 兼容）。

用途：在**不调用任何付费模型 API** 的前提下，让 TracePilot 的真实 Agent 执行引擎
（LangGraph + 规划/工具循环 + 真实本地工具）完整跑起来，从而生成可复核的执行轨迹。

证据边界（必须保留在界面与报告中）：
- 模型响应是本地脚本生成的（simulated），不是 DeepSeek 等真实模型输出；
- 工具调用是真实本地工具执行（calculator / sqlite_query / text_analyzer / 知识检索等）；
- Token 用量不返回 Provider usage，执行引擎会按估算值进行预算控制，界面标注为估算/未知；
- Trace metadata 由生成脚本统一标注 data_source=mock / simulated=true / model_provider=local-scripted-llm。

实现：stdlib HTTP 服务，无第三方依赖；由 scripts/generate_evidence_runs.py 在进程内启动。
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TASK_PREFIX = "任务："
PLAN_SPLIT = "\n执行计划："
UNDERSTAND_SPLIT = "\n任务理解："


def _normalize_task(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _extract_task(user_content: str, *, understanding: bool = False) -> str:
    text = (user_content or "").strip()
    if text.startswith(TASK_PREFIX):
        text = text[len(TASK_PREFIX):]
    cut = UNDERSTAND_SPLIT if understanding else PLAN_SPLIT
    idx = text.find(cut)
    if idx >= 0:
        text = text[:idx]
    return _normalize_task(text)


class ScriptedModel:
    """按任务文本匹配预置执行脚本。"""

    def __init__(self, scripts: list[dict[str, Any]]):
        self.by_task: dict[str, dict[str, Any]] = {}
        for item in scripts:
            self.by_task[_normalize_task(item["task"])] = item

    def plan_for(self, task: str) -> dict[str, Any] | None:
        return self.by_task.get(_normalize_task(task))

    # ---------------------------------------------------------------- 请求处理
    def handle(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        messages = body.get("messages") or []
        system = ""
        user = ""
        for msg in messages:
            if msg.get("role") == "system" and not system:
                system = str(msg.get("content") or "")
            elif msg.get("role") == "user" and not user:
                user = str(msg.get("content") or "")

        if "任务分析助手" in system:
            phase, task = "understand", _extract_task(user)
        elif "任务规划助手" in system:
            phase, task = "plan", _extract_task(user, understanding=True)
        elif "执行助手" in system:
            phase, task = "agent", _extract_task(user)
        else:
            return 400, {"error": {"message": "无法识别的系统提示，拒绝生成", "type": "invalid_request_error"}}

        plan = self.plan_for(task)
        if plan is None:
            return 404, {"error": {"message": f"没有为该任务预置脚本: {task[:60]}", "type": "invalid_request_error"}}

        if plan.get("provider_error_phase") == phase:
            return 500, {"error": {"message": "模拟模型服务异常（用于验证失败链路）", "type": "server_error"}}

        delay = float(plan.get(f"delay_ms_{phase}") or plan.get("delay_ms") or 0) / 1000.0
        if delay > 0:
            time.sleep(delay)

        if phase == "understand":
            content = plan.get("understanding") or f"任务目标：{task[:60]}"
            return 200, self._completion(content)
        if phase == "plan":
            steps = plan.get("plan") or ["理解任务", "调用工具", "汇总结论"]
            text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
            return 200, self._completion(text)

        # agent 阶段：按已执行的工具轮次返回下一批工具调用或最终结论
        rounds = plan.get("rounds") or []
        done_rounds = sum(
            1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls")
        )
        if done_rounds < len(rounds):
            batch = rounds[done_rounds]
            tool_calls = []
            for i, call in enumerate(batch):
                args = call.get("arguments")
                raw_args = call.get("raw_arguments")
                if raw_args is None:
                    raw_args = json.dumps(args or {}, ensure_ascii=False)
                tool_calls.append({
                    "id": f"call_{done_rounds}_{i}_{uuid.uuid4().hex[:6]}",
                    "type": "function",
                    "function": {"name": call["name"], "arguments": raw_args},
                })
            return 200, self._completion(None, tool_calls=tool_calls)

        template = plan.get("final_template")
        if template:
            final = self._format_template(template, messages)
        else:
            final = plan.get("final") or self._derive_final(messages, plan)
        return 200, self._completion(final)

    @staticmethod
    def _format_template(template: str, messages: list[dict[str, Any]]) -> str:
        """用最后一次真实工具返回填充模板占位符（如 {result} / {last}），避免编造数值。"""
        payload: dict[str, Any] = {}
        last_raw = ""
        for msg in reversed(messages):
            if msg.get("role") != "tool":
                continue
            last_raw = str(msg.get("content") or "")
            try:
                data = json.loads(last_raw)
            except Exception:  # noqa: BLE001
                data = {}
            if isinstance(data, dict):
                payload = data
                if "rows" in data and isinstance(data["rows"], list) and data["rows"]:
                    first = data["rows"][0]
                    if isinstance(first, (list, tuple)) and len(first) == 1:
                        payload.setdefault("row_count_value", first[0])
                    if isinstance(first, (list, tuple)):
                        payload.setdefault("first_row", " | ".join(str(x) for x in first))
            break
        values = {k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool))}
        values["last"] = last_raw
        values.setdefault("result", payload.get("result", ""))
        values.setdefault("row_count", payload.get("row_count", ""))
        values.setdefault("conclusion", payload.get("conclusion", ""))
        values.setdefault("average_amount", payload.get("average_amount", ""))

        def repl(match: "re.Match[str]") -> str:
            key = match.group(1).strip()
            return str(values.get(key, match.group(0)))

        return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, template)

    @staticmethod
    def _derive_final(messages: list[dict[str, Any]], plan: dict[str, Any]) -> str:
        """根据真实工具返回内容推导最终答复（失败时如实给出错误信息）。"""
        tool_payloads: list[str] = []
        for msg in messages:
            if msg.get("role") == "tool":
                tool_payloads.append(str(msg.get("content") or ""))
        errors: list[str] = []
        for payload in tool_payloads:
            try:
                data = json.loads(payload)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, dict) and data.get("error"):
                errors.append(str(data["error"]))
        if errors:
            detail = "；".join(errors[:2])
            return plan.get("final_on_error") or f"任务失败：工具调用返回错误（{detail}）"
        last = tool_payloads[-1] if tool_payloads else ""
        return plan.get("final") or f"任务完成，最后一次工具返回：{last[:160]}"

    @staticmethod
    def _completion(content: str | None, *, tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": content}
        finish = "stop"
        if tool_calls:
            message["tool_calls"] = tool_calls
            finish = "tool_calls"
        return {
            "id": f"chatcmpl-mock-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "mock-scripted-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        }


class _Handler(BaseHTTPRequestHandler):
    server_version = "TracePilotMockLLM/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 静默访问日志
        return

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._send(404, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            status, payload = self.server.model.handle(body)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            status, payload = 500, {"error": {"message": f"mock server error: {exc}", "type": "server_error"}}
        self._send(status, payload)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # 客户端超时主动断开（如超时场景）属于预期行为，不打印噪音日志
            return


class MockLLMServer:
    """在后台线程中运行脚本化模型服务。"""

    def __init__(self, scripts: list[dict[str, Any]], host: str = "127.0.0.1", port: int = 0):
        self.model = ScriptedModel(scripts)
        self.httpd = ThreadingHTTPServer((host, port), _Handler)
        self.httpd.model = self.model  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.host, self.port = self.httpd.server_address

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def start(self) -> "MockLLMServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def load_scripts(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("tasks") or []
    return list(data)
