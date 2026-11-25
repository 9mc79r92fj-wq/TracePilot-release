"""统一执行限额配置（LOCAL / DEVELOPMENT 与 PRODUCTION）。

本模块是 Agent 执行、文件引用、评测/流水线超时与前端请求预算的**单一事实来源**。
禁止在其他模块再次硬编码这些限额（历史分散常量已改为从这里读取）。

环境选择：
- `TRACEPILOT_ENV=local` / `development` / `dev` → 宽松本地预算；
- 其他值 / 未设置 → 严格生产预算（默认更安全，避免公网误用宽松额度）。

DeepSeek 模型约束（核对官方 Chat Completions 文档，2026-11-20）：
- `max_tokens` 允许 1..393216（384K）；未设置时非思考模式默认 8K；
- 上下文长度上限远大于这里的 24,000 Token 预算；
- 因此本地 `max_output_tokens=2048` 与总预算 12,000 均在模型支持范围内，
  不会因为参数越界导致真实 API 400。
参考：https://api-docs.deepseek.com/api/create-chat-completion/

注意：本模块不读取密钥、不暴露密钥；`to_public_dict()` 只用于前端展示限制值。
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

__all__ = [
    "PROFILE_ENV",
    "PUBLIC_DEMO_ENV",
    "PROFILE_LOCAL",
    "PROFILE_PRODUCTION",
    "ExecutionLimits",
    "LOCAL_LIMITS",
    "PRODUCTION_LIMITS",
    "active_profile",
    "get_execution_limits",
    "public_demo_enabled",
]

PROFILE_ENV = "TRACEPILOT_ENV"
PUBLIC_DEMO_ENV = "TRACEPILOT_PUBLIC_DEMO"
PROFILE_LOCAL = "local"
PROFILE_PRODUCTION = "production"
_LOCAL_ALIASES = {"local", "dev", "development"}


def public_demo_enabled() -> bool:
    """公共演示模式：禁止匿名真实 API 调用（即使误设服务端开关也不放行）。"""
    return os.environ.get(PUBLIC_DEMO_ENV, "").strip().lower() in {"1", "true", "yes", "on"}



@dataclass(frozen=True)
class ExecutionLimits:
    """一组完整的执行限额（字段均不含敏感信息）。"""

    profile: str

    # --- Agent 执行预算 ---
    agent_max_total_tokens: int          # 单次 Agent 任务输入+输出总 Token 预算
    agent_max_output_tokens: int         # 单次模型调用最大输出 Token
    agent_max_model_calls: int           # 模型调用次数上限（事后审计，预算预检查为主）
    agent_max_tool_calls: int            # 工具调用次数上限（执行前检查，避免超限）
    agent_max_tool_rounds: int           # 工具循环轮数上限
    agent_max_wall_seconds: float        # 单次 Agent 任务墙钟上限
    agent_api_max_retries: int           # 付费 API 自动重试次数（恒为 0）
    agent_min_output_tokens: int         # Token 预算不足时允许继续调用所需的最小输出额度

    # --- 文件引用 / 解析 ---
    agent_max_document_refs: int         # 单次 Agent 任务最多引用文件数
    document_context_max_chars: int      # 注入模型上下文的文档总字符预算
    document_per_file_max_chars: int     # 单个文件注入模型上下文的最大字符数
    document_upload_max_bytes: int       # 上传文件大小上限（本地保持 8MB）
    document_parse_max_chars: int        # 单文件解析文本长度上限（预览/存储）

    # --- 评测 / 批量评测 / 流水线 ---
    evaluation_max_run_seconds: float    # 单次评测执行超时（秒）
    evaluation_max_cases: int            # 单次/单数据集评测案例上限
    pipeline_max_runtime_seconds: float  # 单次自动评测流水线硬超时（秒）
    pipeline_default_runtime_seconds: float  # 流水线未显式传入时的默认超时（秒）

    # --- 前端请求预算（后端下发，前端不硬编码） ---
    frontend_agent_timeout_ms: int       # Agent Runs 同步请求超时（任务墙钟 + 网络余量）

    def to_public_dict(self) -> dict[str, int | float | str]:
        """仅返回可公开给前端的限制值（无密钥、无路径）。"""
        return asdict(self)


# 本地开发：允许真实 API 多轮 + 文件分析的合理预算，同时保留硬上限与零重试。
LOCAL_LIMITS = ExecutionLimits(
    profile=PROFILE_LOCAL,
    agent_max_total_tokens=24_000,  # 12000 建议值在多轮文件任务中因上下文重复计入不足；仍远低于模型上下文
    agent_max_output_tokens=2_048,
    agent_max_model_calls=9,          # 工具轮 6 时最多 6+3 次模型调用
    agent_max_tool_calls=8,
    agent_max_tool_rounds=6,
    agent_max_wall_seconds=180.0,
    agent_api_max_retries=0,
    agent_min_output_tokens=256,
    agent_max_document_refs=5,
    document_context_max_chars=6_000,
    document_per_file_max_chars=3_000,
    document_upload_max_bytes=8 * 1024 * 1024,
    document_parse_max_chars=120_000,
    evaluation_max_run_seconds=300.0,
    evaluation_max_cases=50,
    pipeline_max_runtime_seconds=1_800.0,
    pipeline_default_runtime_seconds=900.0,
    frontend_agent_timeout_ms=210_000,
)

# 生产环境：沿用更严格的既有预算，避免公网部署时成本失控。
PRODUCTION_LIMITS = ExecutionLimits(
    profile=PROFILE_PRODUCTION,
    agent_max_total_tokens=4_000,
    agent_max_output_tokens=512,
    agent_max_model_calls=4,
    agent_max_tool_calls=3,
    agent_max_tool_rounds=3,
    agent_max_wall_seconds=90.0,
    agent_api_max_retries=0,
    agent_min_output_tokens=128,
    agent_max_document_refs=3,
    document_context_max_chars=6_000,
    document_per_file_max_chars=1_800,
    document_upload_max_bytes=8 * 1024 * 1024,
    document_parse_max_chars=120_000,
    evaluation_max_run_seconds=120.0,
    evaluation_max_cases=50,
    pipeline_max_runtime_seconds=300.0,
    pipeline_default_runtime_seconds=120.0,
    frontend_agent_timeout_ms=120_000,
)


def active_profile() -> str:
    """返回当前生效的环境名称（未设置或非法值 → production）。"""
    raw = (
        os.environ.get(PROFILE_ENV)
        or os.environ.get("TRACEPILOT_PROFILE")
        or ""
    ).strip().lower()
    return PROFILE_LOCAL if raw in _LOCAL_ALIASES else PROFILE_PRODUCTION


def get_execution_limits() -> ExecutionLimits:
    """获取当前环境限额（调用时读取环境变量，便于测试覆盖）。"""
    return LOCAL_LIMITS if active_profile() == PROFILE_LOCAL else PRODUCTION_LIMITS
