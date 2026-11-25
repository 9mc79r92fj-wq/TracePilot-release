"""TracePilot 执行轨迹数据结构（Trace Schema）。

本模块定义 Agent 执行轨迹的标准化、可验证、可扩展数据模型，用于后续的
Trace 采集、DAG 构建、故障归因与反事实实验。

设计约束（详见 docs/TRACE_SCHEMA.md）：
1. 基于 Pydantic v2，支持 JSON 序列化 / 反序列化；
2. 状态与步骤类型使用枚举约束；
3. 一个步骤可通过 ``parent_step_ids`` 依赖多个前置步骤（DAG 收敛）；
4. 扩展信息统一放入 ``metadata``，避免破坏既有字段；
5. 不记录模型隐藏 Chain-of-Thought；
6. 不保存 API Key、密码等敏感信息（采集前必须脱敏）。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "SCHEMA_VERSION",
    "TraceStatus",
    "StepStatus",
    "StepType",
    "TokenUsage",
    "TraceError",
    "TraceStep",
    "TraceRecord",
]

# Trace Schema 版本号。建议写入 TraceRecord.metadata["schema_version"]，
# 供后续字段演进与数据迁移使用。
SCHEMA_VERSION = "0.1.0"


class TraceStatus(str, Enum):
    """一次完整 Agent 执行（Trace）的最终状态。"""

    RUNNING = "running"          # 执行中
    SUCCEEDED = "succeeded"      # 全部步骤成功
    FAILED = "failed"            # 任务失败
    PARTIAL = "partial"          # 部分步骤失败，但整体有产出 / 降级完成
    INTERRUPTED = "interrupted"  # 被人工中断或取消


class StepStatus(str, Enum):
    """单个执行步骤的状态。"""

    PENDING = "pending"          # 已创建，未执行
    RUNNING = "running"          # 执行中
    SUCCEEDED = "succeeded"      # 成功
    FAILED = "failed"            # 失败
    SKIPPED = "skipped"          # 因剪枝 / 短路 / 条件分支未执行
    WAITING = "waiting"          # 等待人工输入（HITL）
    INTERRUPTED = "interrupted"  # 被中断 / 取消


class StepType(str, Enum):
    """执行步骤的类型，覆盖 20 项 Agent 核心能力对应的步骤语义。"""

    PLANNING = "planning"                      # 规划
    MODEL_CALL = "model_call"                  # 模型调用（含可解释性决策输出）
    TOOL_CALL = "tool_call"                    # 工具调用
    SKILL = "skill"                            # 复合技能
    RETRIEVAL = "retrieval"                    # RAG 检索
    MEMORY_READ = "memory_read"                # 记忆读取
    MEMORY_WRITE = "memory_write"              # 记忆写入
    REFLECTION = "reflection"                  # 反思 / 重写
    HUMAN_CONFIRMATION = "human_confirmation"  # 人工确认（HITL）
    EVALUATION = "evaluation"                  # 执行评估
    SAFETY_CHECK = "safety_check"              # 安全过滤
    ERROR_HANDLING = "error_handling"          # 故障检测与处理
    CONTEXT_REVIEW = "context_review"          # 上下文审查
    CONTEXT_INJECTION = "context_injection"    # 上下文注入
    SUBPROCESS = "subprocess"                  # 子进程 / 环境管理
    INTERVENTION = "intervention"              # 反事实干预节点（实验用）
    GENERIC = "generic"                        # 扩展兜底类型


class _TraceModel(BaseModel):
    """所有 Trace 模型的公共基类。

    严格校验：未知字段报错（``extra="forbid"``），以保证 Trace 数据的
    统一与可验证；所有扩展信息必须通过 ``metadata`` 承载。
    """

    model_config = ConfigDict(extra="forbid")


class TokenUsage(_TraceModel):
    """记录单次模型调用的 Token 消耗。"""

    input_tokens: int = Field(default=0, ge=0, description="输入 token 数")
    output_tokens: int = Field(default=0, ge=0, description="输出 token 数")
    total_tokens: int = Field(
        default=0,
        ge=0,
        description="总 token 数；未显式提供时按 input_tokens + output_tokens 自动计算",
    )

    @model_validator(mode="after")
    def _fill_total_tokens(self) -> "TokenUsage":
        """未显式传入 total_tokens 时，按输入 + 输出自动计算。"""
        if "total_tokens" not in self.model_fields_set:
            self.total_tokens = self.input_tokens + self.output_tokens
        return self


class TraceError(_TraceModel):
    """记录一次执行错误。"""

    error_type: str = Field(..., description="错误类型 / 异常类名，如 ValueError、ToolTimeout、HTTPError")
    error_message: str = Field(..., description="错误消息")
    recoverable: bool = Field(
        default=False,
        description="是否可恢复：True 表示 Agent 可通过重试或替代方案恢复，False 表示不可恢复",
    )


class TraceStep(_TraceModel):
    """代表一次 Agent 执行步骤（模型调用、工具调用、规划、检索等）。

    与 DAG / 反事实实验的关系：
    - ``parent_step_ids`` 记录本步骤依赖的全部前置步骤，是 DAG 边（入边）的来源；
      允许多个前置步骤，即支持并行分支的收敛（fan-in）。
    - 反事实干预会生成新的 Trace（新的 ``run_id``），被"固定输出"的节点可用
      ``step_type="intervention"`` 标记，并在 ``metadata`` 中记录干预来源与目标。
    """

    step_id: str = Field(..., description="步骤唯一 ID（trace 内唯一）")
    trace_id: str = Field(..., description="所属 trace 的 ID（反向引用）")
    parent_step_ids: list[str] = Field(
        default_factory=list,
        description="前置步骤 ID 列表；空列表表示根步骤。支持一个步骤依赖多个前置步骤",
    )
    step_type: StepType = Field(..., description="步骤类型，见 StepType 枚举")
    timestamp: AwareDatetime = Field(..., description="步骤发生时间（必须带时区，推荐 UTC）")

    input: Any | None = Field(default=None, description="步骤输入（必须可 JSON 序列化）")
    output: Any | None = Field(default=None, description="步骤输出（必须可 JSON 序列化）")

    tool_name: str | None = Field(default=None, description="工具名称；tool_call / skill 等类型使用")
    tool_arguments: dict[str, Any] | None = Field(
        default=None,
        description="工具调用参数（JSON 对象）；tool_call 类型使用",
    )
    tool_result: Any | None = Field(default=None, description="工具返回结果（必须可 JSON 序列化）")

    status: StepStatus = Field(..., description="步骤状态，见 StepStatus 枚举")
    error: TraceError | None = Field(default=None, description="错误信息；仅在失败时存在")

    duration_ms: int | None = Field(default=None, ge=0, description="步骤耗时（毫秒）；运行中可为 null")
    token_usage: TokenUsage | None = Field(default=None, description="Token 消耗；模型调用时存在")

    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="扩展信息。不得存放 API Key、密码等敏感数据；"
        "预留键见 docs/TRACE_SCHEMA.md",
    )


class TraceRecord(_TraceModel):
    """代表一次完整的 Agent 执行（一条 Trace）。"""

    trace_id: str = Field(..., description="Trace 唯一 ID")
    task_id: str = Field(..., description="所属任务 ID")
    run_id: str = Field(..., description="本次运行 ID；同一任务可多次运行（含反事实干预重放）")
    started_at: AwareDatetime = Field(..., description="开始时间（必须带时区，推荐 UTC）")
    ended_at: AwareDatetime | None = Field(default=None, description="结束时间（必须带时区）；运行中为 null")
    status: TraceStatus = Field(..., description="Trace 状态，见 TraceStatus 枚举")
    steps: list[TraceStep] = Field(default_factory=list, description="执行步骤列表（按追加顺序）")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="扩展信息：schema_version、实验 / 干预标记、金标准标注等；"
        "不得存放敏感数据",
    )
