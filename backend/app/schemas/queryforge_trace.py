"""QueryForge Trace 互操作协议 v1 的数据模型。

本模块定义 TracePilot 从 QueryForge（本地 RAG 发布版）接入真实 Trace 时使用的
**版本化 JSON 协议 v1**。协议独立于 QueryForge 实现（只按协议导出，不 import
QueryForge 代码），也独立于 TracePilot 内部 Trace Schema（映射逻辑见
``backend/app/services/trace_interop.py``）。

协议设计原则：
1. ``schema_version`` 必填且严格等于 ``"1.0"``；未知版本直接拒绝，不做猜测性解析；
2. 顶层与 span 均 ``extra="forbid"``：未知字段必须显式拒绝，避免静默丢数据；
   扩展信息统一放入 ``attributes``（键名自由，建议 ``x_`` 前缀）与 ``metadata``；
3. span 关系为树（每个 span 最多一个 ``parent_span_id``），``parent_span_id``
   必须存在、不得自引用、不得成环——保证可直接映射为 TracePilot DAG；
4. ``token_usage`` 语义为「可空、可部分缺失」：缺失一律为 ``null``/unknown，
   **禁止用 0 冒充真实用量**；负值、非整数（含 bool）直接拒绝；
5. 时间必须为带时区的 ISO 8601（``AwareDatetime``），并校验 trace / span 的
   时间窗口与父子先后顺序；
6. 模型基于项目统一的 Pydantic v2（与 ``backend/app/schemas/trace.py`` 一致）。
   这里的 "v1" 指**协议版本**，不是 Pydantic 1.x。

对应的 JSON Schema 见 ``docs/schemas/queryforge_trace_v1.schema.json``（由
``QueryForgeTraceV1.model_json_schema()`` 生成），字段说明与映射规则见
``docs/TRACE_INTEROPERABILITY.md``。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    model_validator,
)

__all__ = [
    "QUERYFORGE_SOURCE",
    "QUERYFORGE_SCHEMA_VERSION",
    "TOKEN_BEARING_EVENT_TYPES",
    "QueryForgeExecutionMode",
    "QueryForgeTraceStatus",
    "QueryForgeSpanStatus",
    "QueryForgeEventType",
    "QueryForgeTokenUsage",
    "QueryForgeSpanError",
    "QueryForgeSpan",
    "QueryForgeTraceV1",
]

#: 数据来源标识（写入 TracePilot metadata 与导入结果）。
QUERYFORGE_SOURCE = "queryforge"

#: 当前支持的协议版本；``schema_version`` 字段必须严格等于该值。
QUERYFORGE_SCHEMA_VERSION = "1.0"

class _StrictModel(BaseModel):
    """协议模型公共基类：未知字段一律拒绝。"""

    model_config = ConfigDict(extra="forbid")


class QueryForgeExecutionMode(str, Enum):
    """QueryForge 本次 trace 的执行方式。"""

    REAL_API = "real_api"      # 真实模型 API 调用
    MOCK = "mock"              # 本地 Mock / 脚本化模型实际执行
    SIMULATED = "simulated"    # 离线构造 / 模拟器输出（示例数据属于此类）
    REPLAY = "replay"          # 历史 trace 的受控重放 / 派生


class QueryForgeTraceStatus(str, Enum):
    """整条 trace 的最终状态（与 TracePilot TraceStatus 值一一对应）。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    INTERRUPTED = "interrupted"


class QueryForgeSpanStatus(str, Enum):
    """单个 span 的状态（与 TracePilot StepStatus 值一一对应）。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING = "waiting"
    INTERRUPTED = "interrupted"


class QueryForgeEventType(str, Enum):
    """QueryForge 步骤事件类型（v1 白名单）。

    未知 event_type 会被 Pydantic 直接拒绝；新增类型需要协议小版本 + 显式映射，
    不允许「按兜底类型静默接收」。
    """

    PLANNING = "planning"                        # 查询路由 / 任务规划
    MODEL_CALL = "model_call"                    # LLM 生成（含工具决策）
    TOOL_CALL = "tool_call"                      # 工具调用（如 knowledge_search）
    RETRIEVAL = "retrieval"                      # 混合检索（bm25/dense/hybrid/adaptive）
    RERANK = "rerank"                            # 交叉编码器重排
    CITATION_VALIDATION = "citation_validation"  # 引用校验 / 拒答判定
    REFLECTION = "reflection"                    # 反思 / 重写（协议保留类型）


# 允许携带 token_usage 的 event_type。
#
# 理由：token 用量是「模型调用」的计量。工具调用 / 检索 / 重排 / 引用校验属于
# 本地或非 LLM 步骤，若这些 span 携带 token_usage，多半是把别处用量错挂过来
# （即伪造）；协议 v1 直接拒绝，要求导出方把 LLM 调用拆成独立 model_call span。
TOKEN_BEARING_EVENT_TYPES = frozenset(
    {
        QueryForgeEventType.MODEL_CALL,
        QueryForgeEventType.PLANNING,
        QueryForgeEventType.REFLECTION,
    }
)


class QueryForgeTokenUsage(_StrictModel):
    """单个 span 的 token 用量；全部字段可空表示 unknown。

    不得为了「看起来完整」而填 0：缺失即 ``null``。若 ``input_tokens`` 或
    ``output_tokens`` 任一缺失，映射层会整体按 unknown 处理并保留原始值，
    不会把缺失字段补 0。
    """

    input_tokens: StrictInt | None = Field(
        default=None, ge=0, description="输入 token 数；未知为 null，禁止用 0 冒充"
    )
    output_tokens: StrictInt | None = Field(
        default=None, ge=0, description="输出 token 数；未知为 null，禁止用 0 冒充"
    )
    total_tokens: StrictInt | None = Field(
        default=None, ge=0, description="总 token 数；未知为 null。缺失时可校验为 input+output"
    )

    @model_validator(mode="after")
    def _reject_fabricated_zero_usage(self) -> "QueryForgeTokenUsage":
        """拒绝用 0 冒充未知用量。

        真实模型调用的输入 / 输出 token 不可能同时为 0；两者同时为 0（含全 0 对象）
        属于把「未知」伪造成「0」，协议 v1 直接拒绝。未知用量请整体使用
        ``token_usage: null``；只缺 total 时可由导入层按 input+output 计算。
        """
        if self.input_tokens == 0 and self.output_tokens == 0:
            raise ValueError(
                "token_usage 的 input_tokens 与 output_tokens 同时为 0："
                "禁止用 0 冒充未知用量，未知请使用 token_usage=null"
            )
        return self


class QueryForgeSpanError(_StrictModel):
    """span 上的错误信息（失败步骤建议提供）。"""

    type: str = Field(..., min_length=1, description="错误类型 / 异常类名，如 LLMUnavailable")
    message: str = Field(..., min_length=1, description="错误消息")
    recoverable: bool = Field(default=False, description="是否可通过重试 / 替代方案恢复")


class QueryForgeSpan(_StrictModel):
    """QueryForge trace 中的一个步骤（span）。"""

    span_id: str = Field(..., min_length=1, description="span 唯一 ID（trace 内唯一）")
    parent_span_id: str | None = Field(
        default=None,
        description="父 span ID；null 表示根 span。必须指向 trace 内已存在的 span，且不得成环",
    )
    event_type: QueryForgeEventType = Field(..., description="事件类型，见 QueryForgeEventType")
    name: str = Field(..., min_length=1, description="步骤名称（工具名 / 模型名 / 操作名）")
    started_at: AwareDatetime = Field(..., description="span 开始时间（必须带时区）")
    latency_ms: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        description="耗时（毫秒，允许小数）；未知为 null，禁止用 0 冒充",
    )
    input: Any | None = Field(default=None, description="步骤输入（必须可 JSON 序列化）")
    output: Any | None = Field(default=None, description="步骤输出（必须可 JSON 序列化）")
    attributes: dict[str, Any] = Field(
        default_factory=dict,
        description="扩展属性（如 retrieval_mode / index_revision / tool_call_id）；原样保留到 metadata",
    )
    token_usage: QueryForgeTokenUsage | None = Field(
        default=None,
        description="token 用量；仅 model_call / planning / reflection 允许携带，未知为 null",
    )
    error: QueryForgeSpanError | None = Field(default=None, description="错误信息；失败步骤建议提供")
    status: QueryForgeSpanStatus = Field(..., description="span 状态，见 QueryForgeSpanStatus")

    @model_validator(mode="after")
    def _check_token_usage_allowed(self) -> "QueryForgeSpan":
        """禁止非模型步骤携带 token_usage（防止把别处用量错挂成伪造数据）。"""
        if self.token_usage is not None and self.event_type not in TOKEN_BEARING_EVENT_TYPES:
            allowed = ", ".join(sorted(e.value for e in TOKEN_BEARING_EVENT_TYPES))
            raise ValueError(
                f"span {self.span_id!r} 的 event_type={self.event_type.value} 不允许携带 "
                f"token_usage（仅 {allowed} 允许）；请把 LLM 调用拆成独立 model_call span，"
                f"未知用量应为 null"
            )
        return self


class QueryForgeTraceV1(_StrictModel):
    """QueryForge Trace 协议 v1 顶层结构。

    ``model_json_schema()`` 的输出即 ``docs/schemas/queryforge_trace_v1.schema.json``
    的正文（另加 ``$schema`` / ``$id``），测试会校验两者一致。
    """

    trace_id: str = Field(
        ...,
        min_length=1,
        description="trace 唯一 ID；导入时若不能作为安全文件名，会生成新 ID 并保留原值",
    )
    schema_version: Literal["1.0"] = Field(
        ..., description="协议版本，v1 固定为 '1.0'；未知版本拒绝导入"
    )
    task_id: str = Field(..., min_length=1, description="任务 / 会话 ID（同一任务可多次运行）")
    run_id: str = Field(..., min_length=1, description="本次运行 ID")
    execution_mode: QueryForgeExecutionMode = Field(
        ..., description="执行方式：real_api / mock / simulated / replay"
    )
    model: str = Field(..., min_length=1, description="本次 trace 使用的模型名（如 deepseek-flash）")
    started_at: AwareDatetime = Field(..., description="trace 开始时间（必须带时区）")
    ended_at: AwareDatetime | None = Field(
        default=None,
        description="trace 结束时间（必须带时区）；running 状态可为 null，终态必须提供",
    )
    status: QueryForgeTraceStatus = Field(..., description="trace 最终状态")
    attributes: dict[str, Any] = Field(
        default_factory=dict,
        description="trace 级扩展属性（原样保留到 metadata.queryforge.attributes）",
    )
    spans: list[QueryForgeSpan] = Field(
        ...,
        min_length=1,
        description="步骤列表；顺序不限，导入时按依赖关系排序",
    )

    @model_validator(mode="after")
    def _check_time_window_and_spans(self) -> "QueryForgeTraceV1":
        """时间窗口、父子先后、span 关系（唯一性 / 引用 / 无环）校验。"""
        # 1. trace 时间窗口
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at 不能早于 started_at")
        if self.status is not QueryForgeTraceStatus.RUNNING and self.ended_at is None:
            raise ValueError(
                f"status={self.status.value} 为终态，必须提供 ended_at（仅 running 可为 null）"
            )

        # 2. span 时间窗口
        for span in self.spans:
            if span.started_at < self.started_at:
                raise ValueError(
                    f"span {span.span_id!r} 的 started_at 早于 trace.started_at，时间窗口非法"
                )
            if self.ended_at is not None and span.started_at > self.ended_at:
                raise ValueError(
                    f"span {span.span_id!r} 的 started_at 晚于 trace.ended_at，时间窗口非法"
                )

        # 3. span ID 唯一性 + 父引用存在性 / 自引用
        index: dict[str, QueryForgeSpan] = {}
        for span in self.spans:
            if span.span_id in index:
                raise ValueError(
                    f"span_id 重复：{span.span_id!r}（同一 trace 内必须唯一）"
                )
            index[span.span_id] = span

        for span in self.spans:
            parent_id = span.parent_span_id
            if parent_id is None:
                continue
            if parent_id == span.span_id:
                raise ValueError(f"span {span.span_id!r} 自引用：parent_span_id 不能等于自身")
            if parent_id not in index:
                raise ValueError(
                    f"span {span.span_id!r} 的 parent_span_id={parent_id!r} 不存在于本 trace"
                )
            if index[parent_id].started_at > span.started_at:
                raise ValueError(
                    f"span {span.span_id!r} 的 parent span {parent_id!r} 开始时间晚于子 span，"
                    f"父子时间顺序非法"
                )

        # 4. 依赖成环检测（Kahn）：每个 span 至多一个父节点，剩余节点即成环。
        children: dict[str, list[str]] = {sid: [] for sid in index}
        in_degree: dict[str, int] = {}
        for span in self.spans:
            if span.parent_span_id is None:
                in_degree[span.span_id] = 0
            else:
                in_degree[span.span_id] = 1
                children[span.parent_span_id].append(span.span_id)
        queue = [sid for sid, degree in in_degree.items() if degree == 0]
        processed = 0
        while queue:
            current = queue.pop()
            processed += 1
            for child_id in children[current]:
                in_degree[child_id] -= 1
                if in_degree[child_id] == 0:
                    queue.append(child_id)
        if processed != len(index):
            raise ValueError("检测到 span 依赖环（parent_span_id 链成环），无法构成 DAG")
        return self
