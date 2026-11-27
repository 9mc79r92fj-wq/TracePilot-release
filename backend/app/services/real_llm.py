"""V2 真实模型调用助手：统一预算扣减、Token 统计与 JSON 输出。

所有 V2 实验（Benchmark 归因、Judge 校准之外的实验辅助调用）都应经过这里，
保证“按每一次真实上游调用扣减预算”的口径一致。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from backend.app.services.real_api_budget import (
    RealCallBudgetExceeded,
    record_real_usage,
    reserve_real_call,
)
from backend.app.services.request_context import get_client_key

__all__ = ["RealLLMError", "call_json"]

DEFAULT_MODEL = "deepseek-flash"


class RealLLMError(RuntimeError):
    pass


def _client():
    from openai import OpenAI

    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        raise RealLLMError("未配置 DEEPSEEK_API_KEY")
    return OpenAI(
        api_key=key,
        base_url=(os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/"),
        timeout=90.0,
        max_retries=0,
    )


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise RealLLMError("模型未返回可解析 JSON")


def call_json(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens: int = 1200,
    timeout: float = 90.0,
    kind: str = "v2_experiment",
) -> dict[str, Any]:
    """调用真实模型并返回 {data, usage, latency_ms, model}。

    Raises:
        RealLLMError: 未配置、预算不足或调用/解析失败。
    """
    model = (model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL).strip()
    try:
        reserve_real_call(kind=kind, model=model, visitor_key=get_client_key())
    except RealCallBudgetExceeded as exc:
        raise RealLLMError(f"真实调用预算不足：{exc}") from exc
    started = time.perf_counter()
    try:
        response = _client().chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.0,
            max_tokens=max_tokens,
            stream=False,
            timeout=timeout,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
    except Exception as exc:  # noqa: BLE001
        raise RealLLMError(f"真实模型调用失败: {type(exc).__name__}: {exc}") from exc
    usage = None
    if getattr(response, "usage", None) is not None:
        u = response.usage
        usage = {
            "input_tokens": getattr(u, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(u, "completion_tokens", 0) or 0,
            "total_tokens": getattr(u, "total_tokens", 0) or 0,
        }
        record_real_usage(
            kind=kind,
            model=model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            visitor_key=get_client_key(),
        )
    content = (response.choices[0].message.content or "") if response.choices else ""
    finish_reason = (response.choices[0].finish_reason if response.choices else None)
    latency = round((time.perf_counter() - started) * 1000, 3)
    try:
        data = _extract_json(content)
    except Exception as exc:  # noqa: BLE001
        raise RealLLMError(
            f"JSON 解析失败: {exc}; finish_reason={finish_reason!r}; "
            f"usage={usage!r}; raw={content[:300]!r}"
        ) from exc
    return {"data": data, "usage": usage, "latency_ms": latency, "model": model, "raw": content}
