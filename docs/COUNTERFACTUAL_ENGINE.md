# Counterfactual Engine 反事实实验引擎

- 对应代码：`backend/app/services/counterfactual_engine.py`、`backend/app/services/experiment_store.py`
- 前端页面：`frontend/src/pages/CounterfactualLab.tsx`（路由 `/counterfactual`）
- API：
  - `POST /api/counterfactual/experiments`（创建实验，返回 experiment_id）
  - `GET  /api/counterfactual/experiments`（实验列表）
  - `GET  /api/counterfactual/experiments/{experiment_id}`（实验状态）
  - `GET  /api/counterfactual/experiments/{experiment_id}/results`（实验结果）
  - `GET  /api/counterfactual/intervention-types`（干预白名单）
- 引擎版本：`0.1.0`
- 状态：引擎 + Lab API + 前端已实现，13 项 API 测试 / 14 项引擎测试通过（模拟环境，未接入真实模型）

---

## 1. 产品目标

回答「如果改变 Agent 执行的某一步，结果会不会不同」这一核心问题：对已有故障 Trace 施加受控干预（关闭故障 / 替换工具结果 / 调整超时重试），在隔离环境重跑并比较观察结果与干预结果，为故障归因与最小修复验证提供**受控实验证据**。

## 2. 实验流程

```
读取原始故障 Trace（只读）
  → 提取任务信息与故障注入配置
  → 校验干预类型（白名单，否则 400 拒绝）
  → 生成独立 experiment_id
  → 施加干预并重跑受控 Agent 流程（模拟模型 + 白名单本地工具，有界重试）
  → 生成独立反事实 Trace（新 trace_id / run_id）
  → TraceComparator 对比原始 vs 反事实
  → 输出：observed_outcome / intervention_outcome / observed_difference
          / causal_evidence_level / limitations
```

## 3. 干预类型（仅三种白名单）

| 类型 | 说明 | 参数 |
|------|------|------|
| `remove_fault` | 关闭原始 Trace 中的全部注入故障 | — |
| `replace_tool_result` | 替换白名单工具（calculator / text_analyzer）的返回结果 | `tool`、`value`（≤4KB JSON） |
| `adjust_timeout_retry` | 调整受控执行的重试配置 | `timeout_ms`（1..60000）、`max_retries`（0..5） |

不支持的干预（如 `run_shell`、`exec_code`、非白名单工具）一律抛 `UnsupportedInterventionError` → HTTP 400。

## 4. 控制变量

| 控制项 | 取值 |
|--------|------|
| 场景 | 固定白名单 3 个（calculator_add / calculator_div_zero / text_analyzer） |
| 模型 | 固定模拟模型 `simulated-scripted-model`（无随机采样） |
| 工具 | 仅 `calculator`、`text_analyzer`（本地无副作用） |
| 干变量 | 单一干预变量，其余故障配置保持与原始一致 |
| 重试 | 上限 5 次 |
| 重放次数 | 1..5 |
| 执行路径 | 规划 → 工具 → 结论（简化路径，与原始完整计划不同） |

## 5. 实验可信度

输出中严格区分四个概念：

- **observed_outcome**：原始 Trace 的观察记录（`executed: false`，未重跑）；
- **intervention_outcome**：施加干预后新 Trace 的结果；
- **observed_difference**：两者的可观察差异（状态变化、故障集合变化、对比摘要）；
- **causal_evidence_level**：因果证据强度分级。

| 等级 | 含义 |
|------|------|
| `none` | 干预后未观察到任何差异 |
| `weak` | 观察到差异，但为有限次重放 / 模拟环境 |
| `moderate` | 观察到差异 + 多次重放可复现 + 最终状态确有变化 |
| `strong` | **本引擎永不输出**（需真实模型 + 真实随机重放 + 外部效度验证） |

并显式输出 `confounders`（混杂因素）与 `limitations`（限制）：模拟模型与真实模型差异、原始模型版本/温度未知、执行路径简化、工具为简化实现等。

## 6. 数据结构（实验报告关键字段）

```json
{
  "experiment_id": "exp-<uuid>",
  "engine_version": "0.1.0",
  "source_trace_id": "trace-...",
  "scenario": "calculator_add",
  "intervention": {"type": "remove_fault"},
  "replays": 3,
  "reproducible": true,
  "observed_outcome": {"trace_id": "...", "status": "succeeded", "fault_types": ["tool_timeout"], "executed": false},
  "intervention_outcome": {"trace_id": "trace-cf-...", "run_id": "run-cf-...", "status": "succeeded", "fault_types": [], "attempts": 1, "simulated": true},
  "observed_difference": {"status_changed": false, "fault_types_removed": ["tool_timeout"], "fault_types_added": [], "comparison_summary": {...}},
  "causal_evidence_level": "weak",
  "causal_evidence_note": "...",
  "environment": {"model": {...}, "tools": [...], "fault_scenarios": [...], "bounded": {...}},
  "control_conditions": [...],
  "confounders": [...],
  "limitations": [...],
  "original_trace_modified": false
}
```

## 7. 安全边界

- 原始 Trace **只读**（`original_trace_modified: false`，测试断言字节级不变）；
- 每次实验生成独立 `experiment_id` / `trace_id` / `run_id`，结果不落盘覆盖原始数据；
- 禁止任意 Shell / Python 代码执行、任意外部 URL 访问；
- 禁止修改真实业务数据库；
- 默认模拟模型，**不调用付费 API**（`paid_api_called: false`）；
- 有界重试（≤5）、有界重放（≤5）、`value` 体积上限 4KB。

## 8. 已知限制

- 模型响应为固定脚本模拟，不代表真实模型行为；
- 执行路径简化为「规划 → 工具 → 结论」，未复现原始完整计划与多轮工具链；
- 干预类型仅 3 种，不支持组合干预（如同时移除故障 + 改超时）；
- 未覆盖真实 API 随机性与真实环境故障；
- 最多 `moderate` 级证据，不产生 `strong` 因果结论；
- 真实原始 Trace 的状态可能已是 succeeded（Agent 已恢复），此时「移除故障」不改变最终状态，差异仅体现在故障集合——引擎如实反映这一点而不夸大。

## 9. 后续最小修复如何复用

1. 用 `remove_fault` 验证「移除该故障后任务恢复」→ 得到候选根因的干预证据；
2. 用 `replace_tool_result` 验证「注入正确工具返回值可修复」→ 形成最小修复建议；
3. 用 `adjust_timeout_retry` 验证「提升重试/超时能否规避」→ 评估配置类修复；
4. 输出的 `observed_difference` 与 `causal_evidence_level` 可直接作为评测报告中「修复成功率」与「归因置信度」的输入；
5. 多次 `replays` 用于可复现性判定，服务创新点 #3（不确定性感知门禁）的置信度计算。

---

## 10. Counterfactual Lab 产品化（第 32 步）

### 10.1 实验登记与资源控制（experiment_store.py）

| 机制 | 取值 | 说明 |
|------|------|------|
| 并发上限 | 2 | 固定线程池，超出返回 HTTP 429 |
| 单次执行超时 | 20s | 超时标记 `failed`，不阻塞请求 |
| 重复提交抑制 | 60s 窗口 | 相同 (trace_id, intervention, scenario, replays) 指纹直接复用，`deduplicated: true` |
| 内存保留上限 | 50 条 | 防无界增长；进程重启清空 |

实验状态机：`running → succeeded` 或 `running → failed`（失败保留 `error` 供排查）。

### 10.2 API 契约

- **创建** `POST /api/counterfactual/experiments` → `{experiment_id, status, deduplicated, source_trace_id, intervention, links}`
  - 干预配置先同步校验（非法立即 400，不产生实验记录）
  - Trace 不存在 404；Pydantic 校验失败 422；并发满 429
- **状态** `GET .../{id}` → 状态、干预、重放次数、elapsed_ms、error、关联 links
- **结果** `GET .../{id}/results` → 引擎原始报告字段（observed/intervention outcome、差异、证据等级、控制条件、混杂因素、限制），不做任何加工或美化

### 10.3 前端 Evidence Strength 证据强度

由**真实实验元数据**推导，绝不以"实验成功/失败"宣称因果：

| 卡片等级 | 判定条件 |
|----------|----------|
| `Inconclusive` | `causal_evidence_level == none`（干预无可区分差异） |
| `Repeated` | 多次重放且 `reproducible == true` |
| `Controlled` | `weak` 且存在受控条件（单一变量干预） |
| `Exploratory` | 其余（探索性，存在未控因素） |

页面同时展示：已控制条件、未控制/潜在混杂因素、是否可复现、是否使用模拟模型、完整限制说明，并提供「查看 DAG」跳转与原始/干预双轨对照（状态、故障、重试次数、差异节点、Trace Diff）。
