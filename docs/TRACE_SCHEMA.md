# Trace Schema 设计文档

- 对应代码：`backend/app/schemas/trace.py`
- Schema 版本：`0.1.0`
- 依据：《总方案》六（Trace Schema 定义）、PRD 第 4.2 / 5.2 节
- 状态：设计完成，尚未接入 Agent 执行引擎（阶段5 开发时落地）

> 本 Schema 用于统一 Agent 执行轨迹的数据结构，是后续 DAG 构建、故障归因与反事实实验的数据基础。所有字段均可 JSON 序列化与反序列化。

---

## 1. 数据模型总览

| 模型 | 用途 | 粒度 |
|------|------|------|
| `TraceRecord` | 一次完整的 Agent 执行（一条 Trace） | 任务运行 |
| `TraceStep` | Trace 中的一个执行步骤 | 步骤 |
| `TokenUsage` | 单次模型调用的 Token 消耗 | 步骤内指标 |
| `TraceError` | 一次执行错误 | 步骤内错误 |

外加三个枚举：`TraceStatus`（Trace 状态）、`StepStatus`（步骤状态）、`StepType`（步骤类型）。

---

## 2. 枚举定义

### 2.1 TraceStatus（Trace 状态）

| 值 | 含义 |
|----|------|
| `running` | 执行中 |
| `succeeded` | 全部步骤成功 |
| `failed` | 任务失败 |
| `partial` | 部分步骤失败，但整体有产出 / 降级完成 |
| `interrupted` | 被人工中断或取消 |

### 2.2 StepStatus（步骤状态）

| 值 | 含义 |
|----|------|
| `pending` | 已创建，未执行 |
| `running` | 执行中 |
| `succeeded` | 成功 |
| `failed` | 失败 |
| `skipped` | 因剪枝 / 短路 / 条件分支未执行 |
| `waiting` | 等待人工输入（HITL） |
| `interrupted` | 被中断 / 取消 |

### 2.3 StepType（步骤类型）

| 值 | 语义 | 对应能力（PRD 第 6 节） |
|----|------|--------------------------|
| `planning` | 规划 | #1 规划 |
| `model_call` | 模型调用（含可解释性决策输出） | #2 工具调用、#8 可解释性 |
| `tool_call` | 工具调用 | #2 工具调用 |
| `skill` | 复合技能 | #3 技能 |
| `retrieval` | RAG 检索 | #4 RAG |
| `memory_read` / `memory_write` | 记忆读取 / 写入 | #5 记忆 |
| `reflection` | 反思 / 重写 | #7 反思 |
| `human_confirmation` | 人工确认 | #9 人机协同（HITL） |
| `evaluation` | 执行评估 | #10 执行评估 |
| `safety_check` | 安全过滤 | #11 安全 |
| `error_handling` | 故障检测与处理 | #12 故障检测 |
| `context_review` | 上下文审查 | #20 上下文审查 |
| `context_injection` | 上下文注入 | #6 上下文工程 |
| `subprocess` | 子进程 / 环境管理 | #13 执行环境管理 |
| `intervention` | 反事实干预节点（实验用） | 创新点 #1–#4 |
| `generic` | 扩展兜底 | 其余能力 |

> 记账计费（#14）、版本控制（#15）、自动化测试（#16）、部署（#17）、性能监控（#18）、成本优化（#19）等能力不产生独立"步骤"，而是体现在 `token_usage`、`duration_ms` 与 `metadata` 中。

---

## 3. 字段说明

### 3.1 TraceRecord

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `trace_id` | `str` | ✅ | Trace 唯一 ID |
| `task_id` | `str` | ✅ | 所属任务 ID |
| `run_id` | `str` | ✅ | 本次运行 ID；同一任务可多次运行（含反事实干预重放） |
| `started_at` | `AwareDatetime` | ✅ | 开始时间（必须带时区，推荐 UTC） |
| `ended_at` | `AwareDatetime \| None` | ❌ | 结束时间；运行中为 `null` |
| `status` | `TraceStatus` | ✅ | Trace 状态 |
| `steps` | `list[TraceStep]` | ❌（默认 `[]`） | 步骤列表（按追加顺序） |
| `metadata` | `dict[str, Any]` | ❌（默认 `{}`） | 扩展信息 |

### 3.2 TraceStep

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `step_id` | `str` | ✅ | 步骤唯一 ID（trace 内唯一） |
| `trace_id` | `str` | ✅ | 所属 trace 的 ID（反向引用） |
| `parent_step_ids` | `list[str]` | ❌（默认 `[]`） | 前置步骤 ID 列表；空 = 根步骤 |
| `step_type` | `StepType` | ✅ | 步骤类型 |
| `timestamp` | `AwareDatetime` | ✅ | 步骤发生时间（UTC） |
| `input` | `Any \| None` | ❌ | 步骤输入（JSON 可序列化） |
| `output` | `Any \| None` | ❌ | 步骤输出（JSON 可序列化） |
| `tool_name` | `str \| None` | ❌ | 工具名称 |
| `tool_arguments` | `dict[str, Any] \| None` | ❌ | 工具调用参数（JSON 对象） |
| `tool_result` | `Any \| None` | ❌ | 工具返回结果 |
| `status` | `StepStatus` | ✅ | 步骤状态 |
| `error` | `TraceError \| None` | ❌ | 错误信息（失败时存在） |
| `duration_ms` | `int \| None` | ❌ | 耗时（毫秒，`ge=0`） |
| `token_usage` | `TokenUsage \| None` | ❌ | Token 消耗（模型调用时存在） |
| `metadata` | `dict[str, Any]` | ❌（默认 `{}`） | 扩展信息 |

### 3.3 TokenUsage

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `input_tokens` | `int` | ❌（默认 `0`，`ge=0`） | 输入 token 数 |
| `output_tokens` | `int` | ❌（默认 `0`，`ge=0`） | 输出 token 数 |
| `total_tokens` | `int` | ❌（默认 `0`，`ge=0`） | 总 token 数；未显式提供时自动按 `input + output` 计算 |

### 3.4 TraceError

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `error_type` | `str` | ✅ | 错误类型 / 异常类名 |
| `error_message` | `str` | ✅ | 错误消息 |
| `recoverable` | `bool` | ❌（默认 `False`） | 是否可恢复（重试 / 替代方案） |

---

## 4. JSON 示例

```json
{
  "trace_id": "trace-20261118-0001",
  "task_id": "task-001",
  "run_id": "run-001",
  "started_at": "2026-11-18T09:00:00Z",
  "ended_at": "2026-11-18T09:00:12Z",
  "status": "failed",
  "metadata": {
    "schema_version": "0.1.0"
  },
  "steps": [
    {
      "step_id": "s1",
      "trace_id": "trace-20261118-0001",
      "parent_step_ids": [],
      "step_type": "planning",
      "timestamp": "2026-11-18T09:00:01Z",
      "input": {"task": "查询并汇总本月销售数据"},
      "output": {"plan": ["s2", "s3"]},
      "status": "succeeded",
      "duration_ms": 420,
      "token_usage": {"input_tokens": 180, "output_tokens": 90, "total_tokens": 270},
      "metadata": {}
    },
    {
      "step_id": "s2",
      "trace_id": "trace-20261118-0001",
      "parent_step_ids": ["s1"],
      "step_type": "tool_call",
      "timestamp": "2026-11-18T09:00:03Z",
      "input": {"query": "SELECT SUM(amount) FROM sales"},
      "output": null,
      "tool_name": "sql_query",
      "tool_arguments": {"sql": "SELECT SUM(amount) FROM sales WHERE month = 10"},
      "tool_result": [{"SUM(amount)": 12800}],
      "status": "succeeded",
      "duration_ms": 150,
      "metadata": {}
    },
    {
      "step_id": "s3",
      "trace_id": "trace-20261118-0001",
      "parent_step_ids": ["s1"],
      "step_type": "retrieval",
      "timestamp": "2026-11-18T09:00:04Z",
      "input": {"query": "销售报表格式规范"},
      "output": null,
      "tool_name": "knowledge_search",
      "tool_arguments": {"query": "销售报表格式规范"},
      "tool_result": {"hits": ["无关文档A", "无关文档B"]},
      "status": "succeeded",
      "duration_ms": 90,
      "metadata": {"fault_injection": {"kind": "semantic_noise", "gold_root_cause": true}}
    },
    {
      "step_id": "s4",
      "trace_id": "trace-20261118-0001",
      "parent_step_ids": ["s2", "s3"],
      "step_type": "model_call",
      "timestamp": "2026-11-18T09:00:06Z",
      "input": {"messages": [{"role": "user", "content": "请根据工具结果生成报表"}]},
      "output": {"report": "错误报表内容"},
      "status": "failed",
      "error": {"error_type": "AssertionError", "error_message": "报表与预期不符", "recoverable": true},
      "duration_ms": 1200,
      "token_usage": {"input_tokens": 500, "output_tokens": 200, "total_tokens": 700},
      "metadata": {}
    }
  ]
}
```

说明：`s4` 依赖 `s2` 与 `s3` 两个前置步骤（`parent_step_ids: ["s2","s3"]`），演示了多依赖收敛；`s3` 的 `metadata` 标注了故障注入与金标准根因，供归因评测使用。

---

## 5. 设计理由

1. **Pydantic v2 与强类型**：字段类型明确（ID 用 `str`、时间用 `AwareDatetime`、状态用枚举、计数用 `int`），保证结构统一、可验证、可静态检查。
2. **枚举约束状态**：`TraceStatus` / `StepStatus` / `StepType` 使用 `str, Enum` 派生，JSON 中呈现为字符串，反序列化时自动校验合法值，杜绝脏数据。
3. **多前置依赖（DAG）**：`parent_step_ids` 为列表，天然支持一个步骤依赖多个前置步骤（并行分支收敛）。DAG 的边由 `(parent_step_id → step_id)` 构成，可据此做拓扑排序与依赖分析。
4. **扩展性**：所有模型 `extra="forbid"` 严格校验未知字段，扩展信息统一走 `metadata: dict[str, Any]`，配合 `schema_version` 实现字段演进与数据迁移，兼容后续 20 项能力。
5. **反事实实验友好**：一次干预重放 = 新的 `run_id`；被固定输出的节点用 `step_type="intervention"` 标记；干预来源、目标与效果记录在 `metadata`，便于对比"干预前 / 干预后"结果分布。
6. **隐私与安全**：不记录隐藏 Chain-of-Thought；`metadata` / `tool_arguments` 等扩展字段约定禁止存放 API Key、密码、token 等敏感信息（采集前脱敏）。
7. **成本与性能可度量**：`TokenUsage`（自动补全 total）与 `duration_ms` 支撑记账计费、性能监控、平均诊断成本等指标。

---

## 6. 与 DAG 及反事实实验的关系

### 6.1 DAG 构建

- 节点 = `TraceStep`，边 = `parent_step_ids` 指向本步骤；
- 根节点 = `parent_step_ids == []` 的步骤；
- 并发分支 = 多个步骤共享同一父节点（fan-out）；多个步骤指向同一子节点（fan-in）；
- 拓扑排序以 `parent_step_ids` 为准，`steps` 列表顺序仅作为采集顺序参考。

### 6.2 反事实干预

- 干预定义"do 操作"：固定某步骤输出 / 替换工具返回值 / 修改参数；
- 每次干预产生**新的 TraceRecord**（新的 `run_id`），保留原 Trace 便于对比；
- 干预节点使用 `step_type="intervention"`，并在 `metadata` 记录：
  - `intervention.target_step_id`：被干预的原始步骤；
  - `intervention.kind`：`fixed_output` / `replace_tool_result` / `modify_argument`；
  - `intervention.value`：注入的假设值。
- 多次重放同一干预以获得结果分布与置信区间，服务创新点 #3（不确定性感知门禁）。

---

## 7. 预留 metadata 键（约定）

| 键 | 位置 | 用途 |
|----|------|------|
| `schema_version` | TraceRecord | Schema 版本号，用于迁移 |
| `experiment` | TraceRecord | 实验标记（如 `counterfactual`、`baseline`） |
| `intervention.*` | TraceRecord / TraceStep | 反事实干预信息 |
| `fault_injection.*` | TraceStep | 故障注入类型与金标准标注 |
| `overridden` | TraceStep | 标记该步输出被干预固定 |
| `evaluation` | TraceStep / TraceRecord | 执行评估结果 |

> 以上为建议键，非强制。所有 `metadata` 值必须可 JSON 序列化且不含敏感信息。

---

## 8. JSON 序列化 / 反序列化用法（Pydantic v2）

```python
from backend.app.schemas.trace import TraceRecord

# 反序列化
record = TraceRecord.model_validate_json(json_str)
record = TraceRecord.model_validate(dict_obj)

# 序列化
json_str = record.model_dump_json()      # JSON 字符串（枚举→str，时间→ISO8601）
data = record.model_dump(mode="json")    # JSON 兼容的 dict
```
