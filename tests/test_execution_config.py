"""统一执行配置测试（本地/生产预算、与模型约束的边界、前端超时余量）。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.execution_config import (  # noqa: E402
    LOCAL_LIMITS,
    PRODUCTION_LIMITS,
    get_execution_limits,
)

# DeepSeek Chat Completions 官方约束（2026-11 核对）：max_tokens 1..393216，
# 上下文长度远大于本项目预算。这里锁定“不越界”的上限断言。
DEEPSEEK_MAX_OUTPUT_TOKENS = 393_216
DEEPSEEK_MIN_CONTEXT_TOKENS = 64_000


def test_local_profile_is_looser_but_retries_stay_zero(monkeypatch):
    monkeypatch.setenv("TRACEPILOT_ENV", "local")
    limits = get_execution_limits()
    assert limits.profile == "local"
    assert limits.agent_max_total_tokens >= 12_000
    assert limits.agent_max_output_tokens >= 2_048
    assert limits.agent_max_tool_rounds >= 6
    assert limits.agent_max_tool_calls >= 8
    assert limits.agent_max_wall_seconds >= 180
    assert limits.agent_max_document_refs >= 5
    # 放宽预算不等于放宽安全：付费调用自动重试恒为 0
    assert limits.agent_api_max_retries == 0


def test_production_profile_is_stricter(monkeypatch):
    monkeypatch.setenv("TRACEPILOT_ENV", "production")
    prod = get_execution_limits()
    assert prod.profile == "production"
    assert prod.agent_max_total_tokens < LOCAL_LIMITS.agent_max_total_tokens
    assert prod.agent_max_output_tokens < LOCAL_LIMITS.agent_max_output_tokens
    assert prod.agent_max_wall_seconds < LOCAL_LIMITS.agent_max_wall_seconds
    assert prod.agent_api_max_retries == 0


def test_unknown_or_missing_profile_defaults_to_production(monkeypatch):
    monkeypatch.delenv("TRACEPILOT_ENV", raising=False)
    monkeypatch.delenv("TRACEPILOT_PROFILE", raising=False)
    assert get_execution_limits().profile == "production"
    monkeypatch.setenv("TRACEPILOT_ENV", "staging-unknown")
    assert get_execution_limits().profile == "production"


def test_limits_stay_within_deepseek_model_constraints():
    for limits in (LOCAL_LIMITS, PRODUCTION_LIMITS):
        assert 64 <= limits.agent_max_output_tokens <= DEEPSEEK_MAX_OUTPUT_TOKENS
        assert limits.agent_max_total_tokens < DEEPSEEK_MIN_CONTEXT_TOKENS
        # 前端请求超时必须大于后端任务墙钟，并预留网络/序列化余量
        assert limits.frontend_agent_timeout_ms > int(limits.agent_max_wall_seconds * 1000)
        # 文件上传保持 8MB；解析上限为正
        assert limits.document_upload_max_bytes == 8 * 1024 * 1024
        assert limits.document_parse_max_chars > 0
        # 文档注入预算不得超过总 Token 预算的保守比例（避免第一次调用就耗尽）
        assert limits.document_context_max_chars <= limits.agent_max_total_tokens * 2


def test_public_dict_has_no_secret_or_path_fields():
    public = LOCAL_LIMITS.to_public_dict()
    assert public["profile"] == "local"
    assert "agent_max_total_tokens" in public
    for key in public:
        lowered = key.lower()
        assert "key" not in lowered
        assert "path" not in lowered
        assert "secret" not in lowered
