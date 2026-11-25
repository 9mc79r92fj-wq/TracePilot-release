# 真实 Agent 执行链路（第 41 步）

- 对应代码：`backend/app/services/agent_runner.py`
- API：`GET /api/agent/status`、`POST /api/agent/runs`、`GET /api/agent/runs`、`GET /api/agent/runs/{run_id}`
- 前端：`/runs`（Agent Runs 运行控制台）
- 引擎版本：`1.0.0`

---

## 1. 执行链路

```
前端 /runs 输入任务
  → POST /api/agent/runs
  → agent_runner.run_agent_task()
      ├─ mock 模式：确定性脚本化模型 + 真实本地 calculator → TraceRecord
      └─ real_api 模式（需双重授权）：basic_agent.run_agent_sync（LangGraph + DeepSeek）
                                       → TraceRecord
  → TraceCollector 保存到 data/traces/{trace_id}.json
  → 硬限制事后审计（超限 → status=limit_exceeded）
  → 返回 Run 记录（含 trace_id / token_usage / final_answer）
前端展示结果 → 可跳转 Trace Explorer 与 DAG Analysis
```

## 2. 数据来源标记（强制，不可伪造）

| `data_source` | 含义 | `simulated` | Token |
|---------------|------|-------------|-------|
| `real_api` | 真实调用 DeepSeek API | `false` | 来自 API 响应，真实计量 |
| `mock` | 确定性脚本化模型 + 真实本地工具 | `true` | 恒 0，并附 `note` 说明不可当真实计量 |
| `simulated` | 纯合成轨迹（评测/故障注入产物） | `true` | 视场景 |

三条铁律：
1. mock 模式**永不**标记为 `real_api`（有测试断言）；
2. 桩测试产生的 Trace **不得**落盘为 `real_api` 证据（测试内自清理，已验证）；
3. 缺少真实 Token 时写 `null` 并说明，**不填 0 冒充真实计量**。

## 3. 安全：双重授权与硬性成本限制

### 双重授权（真实 API）

真实调用需**同时**满足：

1. 请求体 `confirm_real = true`；
2. 服务端环境变量 `TRACEPILOT_ALLOW_REAL_API=1`。

缺任一 → HTTP **403**，且**不发起任何 API 请求**（有测试覆盖）。

### 硬性限制（`RunLimits`，默认值）

| 限制 | 默认 | 说明 |
|------|------|------|
| `max_model_calls` | 4 | 模型调用轮数上限（事后审计） |
| `max_total_tokens` | 4000 | Token 总量上限（事后审计） |
| `max_tool_calls` | 3 | 工具调用次数上限（事后审计） |
| `max_wall_seconds` | 90 | 整体执行超时（硬超时，`asyncio.wait_for`） |
| `max_tool_rounds` | 3 | 工具循环轮数（传入 Agent 图） |
| `api_max_retries` | **0** | **禁止自动重试**，避免失败重试产生额外费用 |
| `max_tokens_per_call` | 512 | 单次模型调用输出上限（传入 `_chat`） |

超出上限时 `status = limit_exceeded` 并记录 `limit_violations`，不静默忽略（有测试覆盖）。

### 工具白名单

仅 `calculator` / `text_analyzer` / `sqlite_query`（均为本地无副作用），无 Shell / 网络 / 文件写入能力。

### 模型 ID 校验与回退

api.deepseek.com 官方模型白名单：`deepseek-chat`、`deepseek-reasoner`。

- 显式传入非白名单模型 → `InvalidModelError`（HTTP 400）；
- 环境变量 `DEEPSEEK_MODEL` 非白名单 → **回退到 `deepseek-chat`** 并记录
  `model_selection = {selected, fallback: true, reason, env_value, allowed_models}`，
  同时写入 `limitations`，绝不静默切换。

## 4. 真实调用验证记录（用户已批准，仅 1 次）

| 项 | 值 |
|----|-----|
| 任务 | 计算125乘以48 |
| 授权 | 用户在会话中明确批准（单次） |
| 实际模型 | `deepseek-chat`（`deepseek-v4-pro` 无效，自动回退并记录） |
| run_id | `run-2aabff766bb54da5bec02f1f6ebac5fe` |
| trace_id | `trace-4d3d82dc7a67496dbc2842cd3b31aa47` |
| 状态 | `succeeded` |
| 耗时 | 5750 ms |
| Token | input 1631 / output 221 / **total 1852**（逐步骤：64 / 129 / 764 / 895） |
| 工具调用 | 2 次 calculator：`125*48`→6000、`125*50-125*2`→6000 |
| 最终答案 | 含 `125 × 48 = 6000`（含分解法自校验） |
| data_source | `real_api`，`simulated = false` |
| 限制违反 | 无（模型调用 4/4、工具 2/3、Token 1852/4000、耗时 5.75s/90s） |
| DAG | 6 节点 6 边，拓扑合法 |
| 落盘 | `data/traces/trace-4d3d82dc7a67496dbc2842cd3b31aa47.json`（被 `.gitignore` 忽略） |

### 已知瑕疵（已定位并修复代码，历史文件未改写）

首次尝试时因 `run_agent` 为 async 却被同步调用，返回未 await 的 coroutine →
HTTP 500。**该异常发生在任何 HTTP 请求之前，未消耗任何 Token / 费用**。
修复为 `run_agent_sync` 后重试成功。

此外发现真实路径未归一化 `run_id`（`TraceRecord.run_id` 来自 basic_agent 内部
collector，与 `Run.run_id` 不同）。已修复为 `model_copy(update={"run_id": run.run_id})`，
并补充**零费用**单元测试 `test_real_path_normalizes_run_id`（桩替换网络调用）验证。
上述真实 Trace 文件保留原样（`run_id = run-e649cdd6…`，与内存 Run 记录的
`run-2aabff76…` 不一致），**不做改写以避免篡改证据**；后续真实运行已一致。

## 5. API 契约

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/agent/status` | 模式可用性、已配置模型是否为官方 ID、白名单、`paid_api_called=false`；不含密钥 |
| POST | `/api/agent/runs` | 同步执行一次任务，返回 Run 记录 |
| GET | `/api/agent/runs` | 进程内运行列表 |
| GET | `/api/agent/runs/{run_id}` | 单次运行详情 + `links.trace` / `links.dag` |

请求体（`AgentRunRequest`）：`task`(1–2000)、`mode`、`confirm_real`、`model`、
`max_model_calls`(1–10)、`max_total_tokens`(100–20000)、`max_tool_calls`(0–10)、
`max_wall_seconds`(5–300)、`max_tool_rounds`(1–8)、`max_tokens_per_call`(64–2048)。

错误码：403（未授权真实调用）、400（空任务 / 非法 mode / 非白名单模型）、422（参数越界）、404（run 不存在）。

## 6. 前端 Agent Runs 控制台（`/runs`）

| 区块 | 内容 |
|------|------|
| 运行配置 | 任务输入、执行模式（Mock 默认 / Real 需授权且服务端开启）、真实模式费用确认勾选、运行按钮 |
| 能力状态 | 模式可用性徽标、已配置模型（非官方 ID 显示警告徽标）、允许模型、工具白名单、数据来源枚举 |
| 安全提示 | 禁止自动重试、各硬限制数值、未授权不发起请求 |
| 执行结果 | 数据来源徽标（REAL / MOCK / SIMULATED）、状态、run_id、耗时、模型、Trace ID、Token、数据来源 |
| 模型选择 | 回退时的完整 `model_selection` 记录 |
| 执行计划 / 工具调用 | 计划列表；每次工具调用的名称、参数、结果 |
| 最终回答 | 真实答案文本 |
| 限制与违规 | `limit_violations` 与 `limitations` |
| 跳转 | 查看 Trace / 查看 DAG |
| 运行历史 | 表格（Run / 任务 / 来源徽标 / 状态 / 耗时 / 查看） |
| 数据来源说明 | 三种来源的含义与 `simulated` 取值说明 |

## 7. 已知限制

1. **真实 API 仅验证 1 次**（用户授权单次），不足以评估稳定性、成本分布与模型行为差异。
2. `.env` 中 `DEEPSEEK_MODEL=deepseek-v4-pro` 非官方 ID，运行器会回退到 `deepseek-chat`；建议用户修正配置（技术债 P1-6）。
3. 运行记录仅存内存（保留 30 条），进程重启丢失；Trace 本身已落盘。
4. 真实模式下 `max_model_calls` / `max_total_tokens` 为**事后审计**而非硬中断（硬中断仅 `max_wall_seconds` 与 `max_tool_rounds`），因 Token 只能从响应中得知。
5. 前端实时进度为同步请求（等待返回），未实现流式/轮询进度。
6. 浏览器交互验证仍未执行（环境无浏览器自动化）。
