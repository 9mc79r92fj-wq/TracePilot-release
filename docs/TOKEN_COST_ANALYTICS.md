# TOKEN & COST ANALYTICS（第 53 步）

Token 用量采集、价格配置、成本/延迟计算与成本归因的实现说明。

> **核心口径（必须保留）**
> 1. 所有金额均为「Token 用量 × 本地价格表」计算的**成本估算**，**不是** Provider 实际账单，
>    更不是「已扣费金额」。接口与前端均携带 `billing_disclaimer` /「成本估算 ≠ 实际账单」标识。
> 2. Provider 未返回 Token 用量 → token 字段为 `null`，状态 `UNKNOWN`，**绝不填 0**。
> 3. 缺少可信价格 → `PRICE_UNKNOWN` + `amount=null`，**不编造官方价格、不按 0 计费**。
> 4. 本模块不调用任何模型/付费 API：所有接口返回 `paid_api_called: false`。

## 1. 数据采集

### 1.1 采集位置（不修改 Trace Schema）

Trace step 的 `metadata.usage_meta` 为第 53 步新增的扩展字段（`TraceRecord`/`TraceStep`
Schema 未变更，旧 Trace 仍可解析；缺失 `usage_meta` 时按 Trace 级信息推断并标记来源）：

| 执行路径 | 写入位置 | `usage_source` | provider / model |
| --- | --- | --- | --- |
| 真实/受控 Agent（`basic_agent._chat`） | `_understand_node`、`_plan_node`、工具循环 model_call 节点的 `metadata.usage_meta` | `PROVIDER_REPORTED`（有 `response.usage`）或 `UNKNOWN`（无 usage） | `deepseek` / 实际模型名 |
| Mock Agent（`agent_runner` 模拟执行器） | 每个 model_call 步骤 | `SIMULATED` | `local-simulation` / `mock-scripted-model` |
| 确定性评测（`evaluation_engine` 模拟器） | 每案例 Trace 的 model_call 步骤 | `SIMULATED` | `local-simulation` / `simulated-model` |

`usage_meta` 字段：

```json
{
  "usage_meta": {
    "provider": "deepseek",
    "model": "deepseek-chat",
    "usage_source": "PROVIDER_REPORTED",
    "raw_usage": {"prompt_tokens": 45, "completion_tokens": 19, "total_tokens": 64},
    "recorded_at": "2026-11-19T04:39:00+00:00"
  }
}
```

- `raw_usage` 原样保留 Provider 返回的字段（含未知字段，如 `prompt_cache_hit_tokens`），
  分析器不丢弃、不破坏 Schema；缓存 Token 分桶只在配置了缓存价格时计费，否则 `PRICE_UNKNOWN`。
- `recorded_at` 记录采集时间；缺失时分析结果中为 `null`（不伪造）。

### 1.2 单次模型调用记录字段

分析结果 `model_calls[]` 包含需求要求的字段：

`run_id`、`trace_id`、`model_call_id`（step_id）、`provider`、`model_name`、
`prompt_tokens`、`completion_tokens`、`total_tokens`、`usage_source`、`recorded_at`，
另有 `step_type`、`duration_ms`、`raw_usage`、`cost`（金额/状态/价格版本/来源）。

### 1.3 用量来源语义

| 来源 | 含义 |
| --- | --- |
| `PROVIDER_REPORTED` | Provider（真实 API）返回的 usage，最可信 |
| `ESTIMATED` | 显式开启估算时用「字符数 / 4」得到的本地估算（`estimate_missing=true`），非 Provider 报告 |
| `SIMULATED` | 模拟执行器不调用真实模型，Token 0 表示未产生调用 |
| `UNKNOWN` | 无 usage 且未开启估算 → `null`，不参与平均成本 |

## 2. 价格配置

文件：`config/model_pricing.yaml`（版本化、可审计）。

### 2.1 字段

| 字段 | 说明 |
| --- | --- |
| `provider` / `model_name` | 匹配键；`model_name: "*"` 为通配（精确匹配优先） |
| `currency` | ISO 4217 三位货币代码 |
| `input_price_per_million_tokens` | 输入单价（每百万 Token），`null` = 未知 |
| `output_price_per_million_tokens` | 输出单价（每百万 Token），`null` = 未知 |
| `cache_read_price_per_million_tokens` / `cache_write_price_per_million_tokens` | 可选缓存价格 |
| `effective_date` | 价格生效日期（YYYY-MM-DD） |
| `pricing_version` | 价格表版本（每次价格变更应提升） |
| `pricing_source` | 价格来源（官方 URL/文档/`NOT_CONFIGURED`） |
| `billing_note` | 口径说明 |

顶层 `meta` 提供默认值（条目可覆盖）。加载时严格校验：必填字段、类型、非负价格、
日期格式、货币代码、YAML 可解析；非法配置抛 `PricingConfigError`（API 返回 400）。

### 2.2 当前仓库配置

- `deepseek/deepseek-chat`、`deepseek/deepseek-reasoner`：价格 **null**，
  `pricing_source: UNKNOWN_OFFICIAL_PRICE_NOT_CONFIGURED` → 成本 `PRICE_UNKNOWN`。
- `local-simulation/*`：单价 0.0，`pricing_source: SIMULATED_ZERO_COST`，
  状态 `SIMULATED`（0 表示未产生调用，**不是真实计费**）。
- 元数据：`pricing_version: 2026.11-demo-unconfigured`、`currency: CNY`、
  `pricing_source: NOT_CONFIGURED_FOR_BILLING`。

> 未配置可信官方价格是**有意的诚实性选择**：避免编造价格并把估算展示成账单。

### 2.3 价格选择规则

1. 先精确匹配 `provider` + `model_name`（大小写不敏感）；
2. 无精确匹配时使用同 provider 的 `*` 通配；
3. 同一模型多条匹配时取 `effective_date` 最新者；
4. 无任何匹配 / 单价为 null → `PRICE_UNKNOWN`。

## 3. 成本计算

### 3.1 公式（Decimal 精确计算，量化到 8 位小数）

```
cost = prompt_tokens   / 1_000_000 × input_price
     + completion_tokens / 1_000_000 × output_price
```

- 输入/输出分别计价，**不把所有 Token 按同一价格计算**；
- 若 Provider 返回缓存分桶且配置了缓存读价：缓存命中部分按缓存价，其余输入按普通输入价；
- 若存在缓存分桶但未配置缓存价格 → 整次调用 `PRICE_UNKNOWN`（无法可靠拆分，不猜测）；
- 金额使用 `Decimal`，避免浮点累积误差；输出为 8 位小数的 `float` 便于 JSON 展示。

### 3.2 单次调用状态

| 状态 | 条件 |
| --- | --- |
| `SIMULATED` | 模拟执行器（不调用付费 API），金额 0 |
| `UNKNOWN` | Token 用量未知（`null`），金额 `null` |
| `PRICE_UNKNOWN` | 无价格配置 / 价格为 null / 缓存分桶无缓存价，金额 `null` |
| `ESTIMATED` | 用量来自本地估算 + 有价格，金额为估算值 |
| `ACTUAL_USAGE_PRICED` | 用量来自 Provider + 价格来自本地价格表，金额为按价格表计算的估算值 |

> `ACTUAL_USAGE_PRICED` 仅表示「用量可信 + 有价格表可用」，**金额仍不是 Provider 账单**。

### 3.3 聚合状态合并优先级

`PRICE_UNKNOWN > UNKNOWN > ESTIMATED > SIMULATED > ACTUAL_USAGE_PRICED`

只对金额非 `null` 的调用求和；聚合结果同时返回
`complete / partial / priced_calls / estimated_calls / simulated_calls /
price_unknown_calls / unknown_calls / pricing_versions / pricing_sources / disclaimer`。
UNKNOWN / PRICE_UNKNOWN 的调用**不计为 0、不计入平均成本**。

### 3.4 各层级成本口径

| 层级 | 计算方式 |
| --- | --- |
| 单次调用 | step 级一次计价 |
| 单次 Trace | 该 Trace 内全部 model_call 步骤求和（trace 级 `token_usage` 不重复累加） |
| Agent Run | 同一 `run_id` 的所有 Trace 求和；run 级 `token_usage` 仅交叉校验，不重复计费 |
| Evaluation Run | 评测报告的 `trace_ids` 逐条分析求和（不重复执行评测） |
| Pipeline Run | 流水线引用的 `evaluation_run_id` / `trace_ids` 求和 |
| 模型对比 | 按 provider/model 聚合调用数、Token、成本、状态 |
| Agent 版本对比 | `agent_version` 缺失时按模型/执行模式分组，并显式返回 `agent_version_note`（不伪造版本） |
| 平均每任务 / 成功任务 / 失败任务 | 仅对金额非 null 的 Trace 求平均；失败任务成本单独统计 |

## 4. 延迟计算

| 字段 | 来源 |
| --- | --- |
| `model_latency_ms` | model_call / planning / reflection 步骤 `duration_ms` 求和 |
| `tool_latency_ms` | `tool_call` 步骤 `duration_ms` 求和 |
| `total_run_latency_ms` | Trace `started_at → ended_at` 差值；缺失时回退为步骤时长求和并标注 |
| `evaluation_latency_ms` | 评测记录 `elapsed_ms` 或报告时间字段 |
| `pipeline_latency_ms` | 流水线 `created_at → completed_at` |

无法可靠计算时为 `null` 并进入 `unknown_fields`，**不填 0**。

## 5. 成本归因（Cost Attribution）

层级：`model_call` / `trace` / `agent_run` / `evaluation` / `pipeline`。

- 唯一计费单元是 **model_call 步骤**（`step_id`）；上层全部由 step 级汇总而来，不重复累加；
- 每个归因行返回 `parent_refs`（trace_id / run_id / trace_ids / evaluation_run_id）与
  `allocation_basis`（分摊口径说明）；
- 流水线**不伪造阶段级成本分摊**：没有可靠 step/stage 关联时返回
  `stage_allocation_available: false` 并说明原因；
- 支持 `usage_source` 过滤（`PROVIDER_REPORTED / ESTIMATED / SIMULATED / UNKNOWN / MIXED`），
  只筛选用量来源，不改变金额口径；`MIXED` 仅可能出现在多来源聚合维度。

## 6. API

| 端点 | 说明 |
| --- | --- |
| `GET /api/cost/pricing` | 价格版本、来源、条目、缺失价格行为与免责声明 |
| `GET /api/cost/overview?recent=&refresh=&estimate_missing=` | 总览：KPI、成本趋势、模型分布、延迟、UNKNOWN 统计 |
| `GET /api/cost/models` | 模型成本分布 |
| `GET /api/cost/versions` | 版本（或模型/执行模式）成本变化 |
| `GET /api/cost/unknown` | UNKNOWN / PRICE_UNKNOWN / 缺失延迟清单（不当作 0） |
| `GET /api/cost/attribution?level=&limit=&usage_source=` | Cost Attribution Explorer |
| `GET /api/cost/traces/{trace_id}` | 单 Trace 成本 |
| `GET /api/cost/runs/{run_id}` | 单 Agent Run 成本 |
| `GET /api/cost/evaluations/{evaluation_run_id}` | 单评测成本 |
| `GET /api/cost/pipelines/{pipeline_run_id}` | 单流水线成本 |

错误约定：价格配置非法 → 400；对象不存在 → 404；其他 → 500。所有响应含
`paid_api_called: false`，成本相关响应含 `disclaimer`。

## 7. 前端 `/costs` Cost Analytics

专业浅色 SaaS 工作台（PC / 平板 / 手机三档响应式）：

- **顶部 KPI**：总 Token（输入/输出）、成本估算 + 状态徽标、平均模型延迟、数据覆盖率、UNKNOWN 计数；
- **成本口径声明**：`成本估算 ≠ 实际账单` 徽标 + 免责声明 + `PRICE_UNKNOWN` 展示；
- **中部**：成本趋势（按 Trace 时间）、模型成本分布表、延迟分析、数据来源与价格版本、
  版本成本对比（agent_version 缺失时标注按模型/执行模式分组）；
- **底部**：Cost Attribution Explorer（维度切换 + 用量来源过滤 + 关联 Trace 跳转）、
  单对象查询（Trace / Agent Run / Evaluation / Pipeline）、UNKNOWN 与缺失数据清单。

## 8. 性能与缓存

- Trace 目录签名（文件数 + 目录 mtime，30s TTL）判断是否需要刷新；
- 增量缓存：文件 mtime/size 未变的 Trace 复用已解析对象与分析结果，新增 Trace 只解析/分析新文件；
- 单飞锁（single-flight）：并发请求同时发现缓存失效时只允许一个线程重算，其余等待复用；
- 实测（9,766 Trace）：冷启动分析约 33s（首次请求），热态约 0–4s；浏览器首次页面加载
  使用 90s 断言上限，串行执行避免多 worker 同时冷启动。

## 9. 已知限制

1. **没有配置可信的官方价格**：真实 DeepSeek 调用成本为 `PRICE_UNKNOWN`，本仓库不展示任何
   「真实账单/扣费」数字；
2. 真实付费 API 仅验证过 **1 次**（任务「计算125乘以48」，Trace
   `trace-4d3d82dc7a67496dbc2842cd3b31aa47`，Provider 报告 1852 Token）；其余数据均为模拟/评测；
3. `agent_version` 字段目前未写入 Trace，版本对比只能按模型/执行模式分组，不代表真实版本对比；
4. 评测运行与流水线运行记录主要保存在内存，服务重启后部分历史不可查；
5. 缓存 Token 分桶只有在配置缓存价格时才计费，否则 `PRICE_UNKNOWN`；
6. 估算模式（`estimate_missing=true`）使用字符数/4 的粗略估算，不能用于计费；
7. 成本归因只使用可靠的 ID 关联；流水线无阶段级成本分摊（不伪造）。

## 10. 测试与验收

- 后端：`tests/test_token_analytics.py`（27 项，含用量来源、UNKNOWN 不为 0、价格版本、
  输入输出分开计价、Decimal 精度、聚合、防重复计费、失败任务成本、延迟、缺失价格、
  Trace 关联、归因过滤、配置校验、版本对比、UNKNOWN 报告、API、无付费调用、无密钥泄露、缓存刷新）；
- 浏览器：`frontend/e2e/cost-analytics.spec.ts`（3 项：核心指标+计费口径+来源/价格版本；
  归因 model_call/evaluation/pipeline；单 Trace 查询 + UNKNOWN 清单）；
- 全量回归：后端 `pytest tests -q`，浏览器 `npx playwright test --project=desktop-edge`，
  前端 `tsc -b` 构建。
