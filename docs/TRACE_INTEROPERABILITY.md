# QueryForge Trace 互操作（标准数据接入）v1

- 协议版本：**1.0**（`schema_version = "1.0"`）
- 对应代码：`backend/app/schemas/queryforge_trace.py`、`backend/app/services/trace_interop.py`
- JSON Schema：`docs/schemas/queryforge_trace_v1.schema.json`
- 示例：`examples/interop/queryforge_trace_example.json`（合法）、`examples/interop/queryforge_trace_invalid_example.json`（非法）
- 测试：`tests/test_trace_interop.py`
- 状态：数据接入层完成（校验 / 映射 / 导入存储）；未改 `main.py`，未接 API 路由

> 说明：需求中的「Pydantic v1 模型」在本仓库按**「协议 v1 的 Pydantic 模型」**落地。
> TracePilot 现有 `backend/app/schemas/trace.py` 与依赖 `pydantic==2.14.3` 均使用
> Pydantic v2，为保持单一技术栈与「扩展而不是创建不兼容重复协议」，本协议同样基于
> Pydantic v2 实现（`model_json_schema()` 即协议 JSON Schema）。这里的 **v1 指数据
> 协议版本**，不是 Pydantic 1.x。

---

## 1. 目标与边界

### 1.1 目标

把本地另一个 RAG 项目 **QueryForge** 的真实 Trace/步骤概念（检索、重排、生成、
工具调用、引用校验）以**版本化、可校验、可审计**的方式接入 TracePilot：

1. 定义 QueryForge Trace 导出协议 v1（JSON Schema + Pydantic 模型）；
2. 严格校验后映射为 TracePilot 原生 `TraceRecord`，保证 DAG 可直接构建、时间顺序正确；
3. 提供导入存储服务，把结果写入 `data/traces`，不覆盖已有 Trace；
4. 缺失 token 用量保持 `null/unknown`，绝不以 0 冒充。

### 1.2 边界（重要）

- **不修改 QueryForge 项目，也不 import 其代码**：本目录只定义「接入协议」，QueryForge
  需要按 v1 协议导出 JSON（导出器属于 QueryForge 侧工作）。
- 字段形状参考 QueryForge 发布版后端的真实响应结构（`SearchResponse` / `SearchHit`、
  rerank 结果、tool_chat 的工具调用轨迹、`ChatResponse` 引用校验结果、
  `usage_metadata` token 用量），但示例文件是**构造的 shape-only 数据**，不冒充真实运行。
- 本步只新增互操作文件，不改动 `backend/app/main.py`、`tests/conftest.py` 与现有测试；
  未来可在 API 层直接调用 `import_queryforge_trace` / `validate_queryforge_trace`。

---

## 2. 协议 v1 顶层字段（JSON 对象）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `trace_id` | `str`（非空） | ✅ | Trace 唯一 ID。导入时若不能作为安全文件名会生成新 ID，原值保留在 metadata |
| `schema_version` | `"1.0"` | ✅ | 协议版本；仅接受 `"1.0"`，未知版本拒绝 |
| `task_id` | `str`（非空） | ✅ | 任务 / 会话 ID（建议用 QueryForge session_id 或业务任务号） |
| `run_id` | `str`（非空） | ✅ | 单次运行 ID（同一 task 可多次运行） |
| `execution_mode` | enum | ✅ | `real_api` / `mock` / `simulated` / `replay` |
| `model` | `str`（非空） | ✅ | 本次 trace 使用的模型名，如 `deepseek-flash` |
| `started_at` | ISO 8601（带时区） | ✅ | Trace 开始时间 |
| `ended_at` | ISO 8601（带时区）\| `null` | 条件 | `running` 可为 null；其余终态必填，且不得早于 `started_at` |
| `status` | enum | ✅ | `running` / `succeeded` / `failed` / `partial` / `interrupted`（与 TracePilot `TraceStatus` 值一致） |
| `attributes` | `dict[str, Any]` | ❌（默认 `{}`） | Trace 级扩展属性，原样保留到 `metadata.queryforge.attributes` |
| `spans` | `list[Span]`（至少 1 个） | ✅ | 步骤列表；顺序不限（导入时按依赖拓扑排序） |

顶层对象 `additionalProperties: false`（Pydantic `extra="forbid"`）：**未知字段一律拒绝**。

---

## 3. Span 字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `span_id` | `str`（非空） | ✅ | trace 内唯一；重复直接拒绝 |
| `parent_span_id` | `str \| null` | ❌（默认 null） | 父 span；必须指向本 trace 内已存在的 span，不得自引用、不得成环；null = 根 |
| `event_type` | enum | ✅ | v1 白名单：`planning` / `model_call` / `tool_call` / `retrieval` / `rerank` / `citation_validation` / `reflection` |
| `name` | `str`（非空） | ✅ | 操作名 / 工具名 / 模型名（如 `hybrid_retrieval`、`knowledge_search`） |
| `started_at` | ISO 8601（带时区） | ✅ | 必须落在 `[trace.started_at, trace.ended_at]` 内 |
| `latency_ms` | `float \| null`，`>= 0` | ❌ | 耗时（毫秒，允许小数）；未知为 null，禁止用 0 冒充 |
| `input` | 任意 JSON \| `null` | ❌ | 步骤输入 |
| `output` | 任意 JSON \| `null` | ❌ | 步骤输出 |
| `attributes` | `dict[str, Any]` | ❌（默认 `{}`） | 扩展属性（如 `mode` / `index_revision` / `tool_call_id`）；原样保留 |
| `token_usage` | 对象 \| `null` | ❌ | 仅 `model_call` / `planning` / `reflection` 允许携带，详见第 5 节 |
| `error` | 对象 \| `null` | ❌ | `{ "type": str, "message": str, "recoverable": bool }` |
| `status` | enum | ✅ | `pending` / `running` / `succeeded` / `failed` / `skipped` / `waiting` / `interrupted`（与 TracePilot `StepStatus` 值一致） |

Span 对象同样 `additionalProperties: false`。

`token_usage` 子对象字段：`input_tokens` / `output_tokens` / `total_tokens`，均为
`int | null` 且 `>= 0`；仅接受 JSON 整数（bool / 小数 / 字符串直接拒绝）。

---

## 4. 校验规则（`validate_queryforge_trace`）

| # | 规则 | 处理 |
|---|------|------|
| 1 | payload 必须为 dict / JSON 字符串 / bytes / 已校验模型 | 否则 `TracePayloadError` |
| 2 | 必填字段齐全、类型正确 | 否则 `TraceValidationError` |
| 3 | 顶层 / span 未知字段 | 拒绝（`extra="forbid"`），避免静默丢数据 |
| 4 | `schema_version` 必须为 `"1.0"` | 未知版本拒绝，不做猜测性解析 |
| 5 | 时间必须带时区（`AwareDatetime`） | 无时区 / 非 ISO 8601 拒绝 |
| 6 | `ended_at >= started_at`；终态必须有 `ended_at` | 违反拒绝 |
| 7 | span `started_at` 必须落在 trace 时间窗口内 | 违反拒绝 |
| 8 | 子 span 的开始时间不得早于其父 span | 违反拒绝（保证时间顺序） |
| 9 | `span_id` 在 trace 内唯一 | 重复拒绝 |
| 10 | `parent_span_id` 必须存在 | 不存在拒绝 |
| 11 | 不得自引用 / 不得成环（Kahn 检测） | 违反拒绝（保证可构建 DAG） |
| 12 | 未知 `event_type` | 拒绝（v1 白名单，不做兜底映射） |
| 13 | 非 token 型事件携带 `token_usage` | 拒绝（防止用量错挂 / 伪造） |
| 14 | token 字段为负 / bool / 小数 / 字符串 | 拒绝 |
| 15 | `input_tokens` 与 `output_tokens` 同时为 0（含全 0 对象） | 拒绝：视为用 0 冒充未知用量 |
| 16 | `input` / `output` / `attributes` 含不可 JSON 序列化的值 | 拒绝（TracePilot 存储前提） |
| 17 | `trace.status` 与 span 状态矛盾 | 不拒绝，保留原值并写入 `warnings` |

校验错误只回显字段位置与错误消息（`TraceValidationError.errors` 包含 `loc/msg/type`），
**不回显原始输入值**，避免把疑似凭据写进日志或响应。

---

## 5. token_usage 语义：缺失必须 `null/unknown`，禁止 0 冒充

| payload 中的 `token_usage` | 含义 | 导入后 `TraceStep.token_usage` | `metadata.queryforge.token_usage_status` | `metadata.queryforge.token_usage_raw` |
|---------------------------|------|-------------------------------|------------------------------------------|----------------------------------------|
| 字段缺失 / `null` | unknown | `None` | `unknown`（model_call 会附 warning） | 不写 |
| 对象字段全 `null` | unknown | `None` | `unknown` + warning | 原样保留 |
| `input_tokens` 与 `output_tokens` 同时为 0 | **伪造** | **拒绝导入** | — | — |
| `input_tokens` 或 `output_tokens` 缺失 | 部分未知 | `None`（不补 0） | `partial` + warning | 原样保留 |
| input/output 完整，`total_tokens` 缺失 | 可计算 | 按 `input+output` 计算 | `complete` + warning | 不写 |
| input/output 完整，`total_tokens` 不一致 | 保留原值 | 使用 payload 中的 total | `complete` + warning | 不写 |
| input/output 完整且一致 | 已知 | 原值映射 | `complete` | 不写 |
| 非 token 型事件（retrieval / rerank / tool_call / citation_validation） | 无 token 概念 | `None` | `not_applicable` | 不写 |

设计理由：TracePilot 的 `TokenUsage` 字段为整数且默认 0，如果导入时把「未知」直接
构造成 `TokenUsage()`，会把未知伪装成 0，污染 Token 成本统计。因此映射层对任何
不完整 / 未知用量一律映射为 `None`，并把原始对象（若有）存入 metadata 供审计回溯。

---

## 6. event_type -> TracePilot StepType 映射

| QueryForge `event_type` | TracePilot `StepType` | 说明 |
|-------------------------|-----------------------|------|
| `planning` | `planning` | 查询路由 / 任务规划 |
| `model_call` | `model_call` | LLM 生成（含工具决策） |
| `tool_call` | `tool_call` | 工具调用（如 `knowledge_search`） |
| `retrieval` | `retrieval` | 混合检索（bm25 / dense / hybrid / adaptive） |
| `rerank` | `tool_call` | 交叉编码器重排视为检索管线中的本地处理步骤；**原始类型保留在 metadata**，避免被统计为模型调用 token |
| `citation_validation` | `evaluation` | 引用校验 / 拒答判定，属于生成后评估 |
| `reflection` | `reflection` | 反思 / 重写（协议保留类型） |

映射是**固定、显式、双向可追溯**的：下游始终可从
`step.metadata["queryforge"]["event_type"]` 取回原始事件类型；测试
`test_event_type_mapping_table_is_explicit` 锁定该映射表。

步骤字段映射：

| QueryForge span | TracePilot TraceStep |
|-----------------|----------------------|
| `span_id` | `step_id` |
| `parent_span_id` | `parent_step_ids`（0 或 1 个） |
| `event_type` | `step_type`（按上表）+ `metadata.queryforge.event_type` |
| `started_at` | `timestamp` |
| `input` / `output` | `input` / `output` |
| `latency_ms` | `duration_ms`（四舍五入为整数）；原始小数保留在 `metadata.queryforge.latency_ms` |
| `attributes` | `metadata.queryforge.attributes`（原样） |
| `name` | `tool_name`（tool_call / retrieval 等工具类步骤）+ metadata |
| `error` | `TraceError`（`type` -> `error_type`、`message` -> `error_message`） |
| `status` | `status`（枚举值一一对应） |
| `token_usage` | `token_usage`（按第 5 节规则） |

`tool_result` 不重复写入（原始结果只在 `output` 中保留一份，避免同一数据双份）；
`tool_arguments` 在 `input` 为 JSON 对象且步骤为 tool_call / retrieval 时原样映射。

**排序**：`TraceRecord.steps` 按「依赖拓扑优先 + 同层 `started_at`/`step_id` 确定性排序」，
因此父步骤一定在子步骤之前、时间非递减，DAG 可直接构建（v1 为树：n 个 span 有
n-1 条父子边）。

---

## 7. 导入流程与存储

### 7.1 API

```python
import json
from pathlib import Path

from backend.app.services.trace_interop import (
    validate_queryforge_trace,   # 纯校验：payload -> ImportedTrace（无 I/O）
    to_trace_record,             # payload / ImportedTrace -> TraceRecord
    import_queryforge_trace,     # 校验 + 映射 + 写入 Trace 目录
)

payload = json.loads(
    Path("examples/interop/queryforge_trace_example.json").read_text(encoding="utf-8")
)

imported = validate_queryforge_trace(payload)
print(imported.record.trace_id, imported.source, imported.schema_version, imported.warnings)

record = to_trace_record(payload)          # TracePilot 原生 TraceRecord

saved = import_queryforge_trace(payload)   # 默认写入 data/traces/{trace_id}.json
print(saved.saved_path, saved.warnings)
```

`import_queryforge_trace(payload, traces_dir=tmp_path)` 支持指定目录，测试一律使用
`tmp_path`，不污染 `data/traces`。

### 7.2 trace_id 规则

| 情形 | 行为 |
|------|------|
| 原 trace_id 合法（`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` 且不含 `..`，与 `trace_store` 规则一致）且目标目录无同名文件 | **保留原名** |
| 合法但目标目录已存在同名文件 | 抛 `TraceIdConflictError`，**拒绝导入且不覆盖原文件** |
| 不能作为安全文件名（非法字符 / 路径穿越 / 过长 / 非 ASCII） | 生成 `trace-<uuid>` 新 ID，原值保留在 `metadata.queryforge.original_trace_id`，warnings 说明 |

### 7.3 写入安全

1. 目录创建后，用 `open("x")` **独占创建**（同时承担并发竞态下的不覆盖保证）；
2. 写入前对整条记录做递归脱敏：复用 `trace_collector.redact`（`api_key` / `authorization` /
   `password` / `secret` / `access_token` 等键值替换为 `[REDACTED]`）；
3. 写入后立即用 `TraceRecord.model_validate_json` 回读自检，失败则删除半成品并报错，
   保证 `trace_store.load_trace(trace_id)` 一定能读取导入结果；
4. 复用现有接口：`trace_collector.DEFAULT_TRACES_DIR`（`data/traces`）、`new_trace_id()`、
   `redact()`、`trace_store.load_trace()`。**不直接调用 `TraceCollector.save()`**，因为其
   `write_text` 会覆盖同名文件，与「不得覆盖已有文件」的要求冲突。

### 7.4 写入后的 metadata 结构

```json
{
  "schema_version": "0.1.0",
  "source": "queryforge",
  "imported": true,
  "data_source": "simulated",
  "queryforge": {
    "schema_version": "1.0",
    "trace_id": "qf-example-trace-0001",
    "original_trace_id": "qf-example-trace-0001",
    "execution_mode": "simulated",
    "model": "deepseek-flash",
    "status": "succeeded",
    "span_count": 7,
    "attributes": {"example": true, "note": "..."},
    "imported_at": "2026-11-25T08:00:05+00:00",
    "warnings": [],
    "token_usage_unknown_spans": ["span-qf-plan-001"]
  }
}
```

- `source=queryforge` + `imported=true` 标注来源（需求要求）；
- `schema_version` 是 **TracePilot 内部 Schema 版本**；来源协议版本在
  `queryforge.schema_version`，两者分开，避免混淆；
- 每个步骤的 `metadata.queryforge` 包含：`event_type`、`span_id`、`parent_span_id`、
  `name`、`latency_ms`（原始小数）、`attributes`、`token_usage_status`，以及可选
  `token_usage_raw`（仅 unknown/partial 且 payload 提供了对象时）。

### 7.5 数据治理口径（不提升来源等级）

`execution_mode` 映射到 `data_governance.classify_metadata` 的输入：

| execution_mode | metadata 标记 | 分类结果 |
|----------------|---------------|----------|
| `mock` | `data_source="mock"` | `MOCK` |
| `simulated` | `data_source="simulated"` | `SIMULATED` |
| `replay` | `derivation="replay"` | `REPLAY` |
| `real_api` | `data_source="real_api"`（不写 `real_api_verified`） | `UNKNOWN`（保守，不自动升级） |

导入数据无法自证真实 API 调用，因此 `real_api` 声明只作为来源声明记录，并写入一条
warning 提示：缺少 TracePilot 独立 `real_api_verified` 标记时不会计入真实 API KPI。

---

## 8. 版本兼容策略

1. **单一版本入口**：`schema_version` 必须严格等于 `"1.0"`。未知版本（含 future
   `1.1` / `2.0`）一律 `TraceValidationError` 拒绝，绝不猜测性解析。
2. **v1 冻结**：`1.0` 的字段、枚举、校验规则冻结；已有 1.0 导出数据在 TracePilot
   后续版本中保持可导入。
3. **演进方式**：新增能力优先通过 **可选字段 + `attributes`** 承载，升 `1.1` 时导入层
   按版本号分派适配器（版本分发表），不改变 1.0 语义；破坏性变更必须升 `2.0` 并
   提供独立入口，禁止原地修改 1.0 含义。
4. **模型是唯一事实源**：`docs/schemas/queryforge_trace_v1.schema.json` 由
   `QueryForgeTraceV1.model_json_schema()` 生成，测试
   `test_json_schema_file_is_valid_and_matches_model` 强制两者一致；模型变更必须同步
   重新生成 Schema 并更新本文档。
5. **来源可追溯**：导入记录的 `metadata.queryforge.schema_version` 记录来源协议版本，
   便于后续迁移 / 审计。

重新生成 JSON Schema（在项目根目录，使用 `.venv` Python）：

```bash
.venv\Scripts\python.exe - <<'PY'
import json
from pathlib import Path
from backend.app.schemas.queryforge_trace import QueryForgeTraceV1

schema = QueryForgeTraceV1.model_json_schema()
schema = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "queryforge_trace_v1.schema.json",
    **schema,
}
Path("docs/schemas/queryforge_trace_v1.schema.json").write_text(
    json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY
```

---

## 9. 未知字段 / 未知事件类型的扩展策略

1. **未知字段（顶层 / span）**：拒绝（`extra="forbid"`）。理由：静默丢弃字段会让
   「导入了什么」不可审计，宁可让导出方显式升级协议。
2. **扩展信息**：使用 `attributes`（trace 级 / span 级）承载业务扩展，导入后原样保留在
   `metadata.queryforge.attributes`；建议键名使用 `x_` 前缀或命名空间（如
   `queryforge.index_revision`）以减少碰撞。不要为了放扩展字段而修改协议核心字段。
3. **未知 event_type**：拒绝，不做 `generic` 兜底映射。新增事件类型必须同时：
   - 加入 `QueryForgeEventType` 枚举（提议协议小版本）；
   - 在 `EVENT_TYPE_TO_STEP_TYPE` 显式声明映射（或说明为何拒绝）；
   - 更新本文档、JSON Schema 与测试；重新生成 Schema。
4. **未知执行模式**：`execution_mode` 枚举固定 4 值，未知值拒绝；新增模式需同步
   `data_source/derivation` 治理映射。

---

## 10. 示例文件

### 10.1 合法示例

`examples/interop/queryforge_trace_example.json`：模拟一次 QueryForge RAG 问答的 7 个 span：

| span_id | event_type | 映射 StepType | token |
|---------|------------|----------------|-------|
| `span-qf-plan-001` | planning | planning | null（unknown） |
| `span-qf-model-001` | model_call | model_call | 812 / 64 / 876 |
| `span-qf-tool-001` | tool_call | tool_call | null（not_applicable） |
| `span-qf-ret-001` | retrieval | retrieval | null（not_applicable） |
| `span-qf-rerank-001` | rerank | tool_call | null（not_applicable） |
| `span-qf-model-002` | model_call | model_call | 1440 / 236 / 1676 |
| `span-qf-cite-001` | citation_validation | evaluation | null（not_applicable） |

- 顶层 `attributes.example = true`、`trace_id` 以 `qf-example-` 开头、
  `execution_mode = "simulated"`，明确标注**示例数据，不是真实 QueryForge 运行**；
- 字段形状对齐 QueryForge 发布版真实响应（检索 hit 的 `document_id` / `chunk_id` /
  `score`、rerank 分数、工具调用 `tool_call_id` / `source_ids`、引用校验 `refused` /
  `citations`、模型 `usage_metadata` token 等）。

### 10.2 非法示例

`examples/interop/queryforge_trace_invalid_example.json`：子 span 的 `parent_span_id`
指向不存在的 `span-qf-does-not-exist`，用于验证「非法父节点被拒」。

---

## 11. 测试与验收

```bash
.venv\Scripts\python.exe -m pytest -q tests/test_trace_interop.py
```

覆盖（83 个用例）：

- 合法示例通过、event_type 映射、metadata 来源标注、token 完整映射；
- 非法父节点 / 重复 span_id / 自引用 / 成环 / 时间窗口 / 父子顺序 / 未知 event_type /
  未知字段 / 错误版本 / 错误 token 类型 / 伪 token（0 冒充）被拒；
- token 缺失 / 全 null / 部分缺失不补 0（映射 None、留档 raw、warnings）；
- 多分支 DAG 可构建、步骤父先于子且时间非递减；
- 导入写入 `tmp_path` 后可通过 `trace_store.load_trace` 读取，metadata 标注
  `source=queryforge`；同名冲突拒绝且不覆盖；非法 trace_id 安全重命名；
- 导入不写入默认 `data/traces`（隔离验证）；
- JSON Schema 文件与 Pydantic 模型一致。

---

## 12. 限制与已知边界

1. **QueryForge 需按 v1 协议导出**：本仓库只提供协议与导入器，不能直接把 QueryForge
   任意内部结构当 v1 数据读取。
2. **不修改 / 不 import QueryForge**：仅在只读检查其发布版代码后对齐字段形状；示例数据
   为构造数据。
3. **v1 为单父树**：TracePilot 原生 `parent_step_ids` 支持多父 DAG（fan-in），但 QueryForge
   span 模型是树；多父信息需协议 v1.1 扩展（如增加 `parent_span_ids`）。
4. **span 无结束时间**：v1 只记录 `started_at` + `latency_ms`；TracePilot `duration_ms` 取整，
   原始小数保留在 `metadata.queryforge.latency_ms`。
5. **token 语义边界**：token 仅为模型调用计量；非模型事件携带 token_usage 会被拒绝
   （应拆成独立 `model_call` span）。`usage_metadata` 缺失时保持 unknown，不做估算。
6. **来源等级**：`execution_mode=real_api` 不会自动升级为 `REAL_API`；仍需要
   TracePilot 侧独立验证标记。
7. **未接入 API / 前端**：本步限制文件范围（不改 `main.py`），服务可直接被后续 API 层
   调用；导入为单文件写入，不产生评测/派生记录。
8. **JSON Schema 与模型强绑定**：Schema 由模型生成，测试强制一致；升级模型时必须同步
   重新生成 Schema 与本文档。

---

## 13. 变更记录

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0 | 2026-11-27 | 首个版本：协议 v1 模型 + JSON Schema；严格校验；TraceRecord 映射与 DAG 顺序；导入存储（不覆盖 / 脱敏 / trace_id 规则）；83 个测试用例 |
