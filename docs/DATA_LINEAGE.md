# 数据血缘与一致性（Data Lineage & Consistency）

- 对应代码：`backend/app/services/evidence_lineage.py`、`backend/app/services/derived_trace.py`
- API：`/api/lineage/*`（5 个只读端点）
- 前端：`frontend/src/components/LineagePanel.tsx`（分层证据链面板 / 抽屉）
- 引擎版本：`1.0.0`

---

## 1. 数据对象与 ID 规则（第 44 步实测）

| 对象 | 主键 | 关联字段 | 实际存储位置 |
|------|------|---------|------------|
| Agent Run | `run_id`（`run-<32hex>`） | `trace_id` | **内存**（`agent_runner`，上限 30 条，重启丢失） |
| Trace | `trace_id`（`trace-<32hex>`） | `task_id`、`run_id` | **文件**：`data/traces/{trace_id}.json` |
| 评测 Trace | `trace_id`（`trace-eval-<32hex>`） | `task_id` | **文件**：`data/evaluation/traces/{trace_id}.json` |
| 派生 Trace | `trace_id`（`trace-cf-<32hex>`） | `metadata.derived_from` | **文件**：`data/traces/`（第 44 步起落盘） |
| Trace Step | `step_id`（`{trace_id}-sNNN`） | `trace_id`（必须等于所属 Trace） | Trace 内嵌 |
| DAG Node | `step_id`（与 Step 一一对应） | `parent_step_ids` / `child_step_ids` | 由 Trace 动态构建，不单独存储 |
| Fault Record | 无独立 ID | `step.metadata.fault_injection`、`trace.metadata.fault_injections` | Trace 内嵌 |
| Comparison Report | 无独立 ID | `reference_trace_id` / `target_trace_id` | 不存储（即时计算返回） |
| Counterfactual Experiment | `experiment_id`（`exp-<32hex>`） | `source_trace_id`；结果含 `intervention_outcome.trace_id` | **内存**（`experiment_store`，重启丢失）；派生 Trace 已落盘 |
| Repair Report | `report_id`（`report-<32hex>`） | `source_trace_id`；候选含 `repair_id`、`experiment_id`、`repair_trace_id` | **内存**（`repair_store`）；修复 Trace 已落盘 |
| Repair Candidate | `repair_id`（`repair-<12hex>`） | `repair_trace_id`、`experiment_id`、`target_step_id` | 报告内嵌 |
| Evaluation Run | `evaluation_run_id`（`eval-<32hex>`） | `dataset_id`、`dataset_version` | **内存**（`evaluation_store`） |
| Evaluation Case | `case_id`（`case-NN-*`） | `trace_id`、`data_source`、`judge` | 报告内嵌 |
| Judge Result | 无独立 ID | 挂在 case 的 `judge` 字段 | 报告内嵌 |
| Calibration Report | 无独立 ID | `dataset.calibration_id` | 即时返回（不存储） |
| Multi-Judge Report | `run_id`（`mj-<32hex>`） | `judges[]`、`cases[].judges` | **内存**（`multi_judge` store） |

### 关键结论（不得假设 ID 统一）

1. **每种对象的 ID 前缀与长度不同**：`trace-` / `trace-eval-` / `trace-cf-` / `run-` / `run-cf-` / `exp-` / `report-` / `repair-` / `eval-` / `mj-`。
2. **Trace 是唯一跨模块的"证据锚点"**：评测、实验、修复、对比、DAG 全部通过 `trace_id` 关联到 Trace。
3. **`case_id` 与 `trace_id` 不是同一命名空间**：`case-01-calc-exact` 属于数据集，`trace-eval-*` 属于该次执行。
4. **只有 Trace 是持久化的**；Run / Experiment / Repair / Evaluation / Multi-Judge 的**登记表都在内存**。
   → 因此血缘中"登记表"节点一律标记 `storage = MEMORY`，且不因内存丢失而判定"数据不存在"。

---

## 2. 数据来源标记（正交双维度）

血缘节点同时携带两个**互相独立**的字段：

### 2.1 `origin` —— 数据从哪来

| 取值 | 判定规则（严格依据元数据） |
|------|--------------------------|
| `REAL_API` | `metadata.data_source == "real_api"` **且** `simulated != true` |
| `MOCK` | `metadata.data_source == "mock"` |
| `SIMULATED` | `metadata.data_source == "simulated"`；或缺失 `data_source` 但 `simulated == true` |
| `UNKNOWN` | 无任何来源证据；或字段自相矛盾（如 `real_api` + `simulated=true`） |

**禁止**：根据页面名称、ID 前缀、模型名推断真实性。缺 `data_source` 时**绝不**推断为 `REAL_API`。

### 2.2 `storage` —— 是否已落盘

| 取值 | 含义 |
|------|------|
| `RECORDED` | 已保存为文件（`data/traces/` 或 `data/evaluation/traces/`） |
| `MEMORY` | 仅进程内登记表（重启丢失） |
| `NOT_AVAILABLE` | 无法获取 |

> **`RECORDED` 不代表来自真实 API**。它只说明"有保存的历史记录"。真实来源看 `origin`。
> 例如评测 Trace 为 `RECORDED + SIMULATED`，历史 mock 运行为 `RECORDED + MOCK`。

### 2.3 派生 Trace 的来源保留

派生 Trace（反事实变体、修复验证）由 `derived_trace.py` 落盘，元数据强制包含：

| 字段 | 值 |
|------|-----|
| `data_source` | `"simulated"`（派生执行恒为模拟环境重放） |
| `simulated` | `true` |
| `derived_from` | 原始 Trace ID |
| `derivation` | `counterfactual` / `repair_validation` |
| `source_data_source` | 原始 Trace 的**规范来源**（`REAL_API`/`MOCK`/`SIMULATED`/`UNKNOWN`） |
| `source_data_source_raw` | 原始 `metadata.data_source` 原值（保留未加工证据） |
| `source_task_id` / `source_run_id` | 原始 Trace 的 task/run |

**派生结果永不标记为 `real_api`**（有测试与一致性检查双重保证）。

---

## 3. 证据追溯路径

```
Evaluation Run ──contains_case──► Evaluation Case ──evaluated_trace──► Trace
                                                                        ├──executed_by──► Agent Run
                                                                        └──contains──► Step
                                                                                        └──produced──► Tool Result
                                                                        Tool Call ──produced──► Tool Result

Counterfactual Experiment ──based_on──► Source Trace
                          └─produced_variant──► Variant Trace ──derived_from──► Source Trace

Repair Report ──based_on──► Source Trace
              └─contains_candidate──► Repair Candidate ──validated_by_trace──► Repair Trace ──derived_from──► Source Trace
```

### 3.1 评测 → Trace → Agent Run → Step → Tool（核心路径）

1. `evaluation_store` 报告内 `case_results[]` 提供 `case_id` + `trace_id`；
2. `load_trace(trace_id)` 读取 Trace（主目录或评测目录回退）；
3. `trace.run_id` → `agent_runner.get_run(run_id)`（内存；不在则 NOT_AVAILABLE 但 Trace 仍可追溯）；
4. Trace 内 `steps[]` 逐个成为节点；`tool_call` 步骤派生 `tool_call` 与 `tool_result` 节点。

### 3.2 缺失证据的诚实表达

任何解析失败的关联都生成 `status = NOT_AVAILABLE` 的节点，并在 `note` 给出原因，例如：

| 场景 | 节点状态与说明 |
|------|--------------|
| 案例声明 `trace_id` 但文件缺失 | `NOT_AVAILABLE`：「案例声明了 trace_id，但未找到已保存的 Trace 文件（关联断裂）」 |
| Run 记录已因重启丢失 | `NOT_AVAILABLE`：「运行记录不在当前进程登记表中…Trace 仍可追溯」 |
| 历史实验的变体 Trace 未落盘 | `NOT_AVAILABLE`：「变体 Trace 未落盘（历史实验），无法查询」 |
| `tool_call` 无 `tool_result` | `NOT_AVAILABLE`：「该 tool_call 未记录 tool_result」 |

**绝不伪造缺失关系**（例如不会"猜"一个 Trace ID 填进去）。

---

## 4. API（全部只读）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/lineage/trace/{trace_id}` | Trace → Run → Step → Tool |
| GET | `/api/lineage/evaluation/{run_id}/cases/{case_id}` | 评测运行 → 案例 → Trace → Run → Step → Tool |
| GET | `/api/lineage/experiment/{experiment_id}` | 实验 → 原始 Trace / 变体 Trace |
| GET | `/api/lineage/repair/{report_id}` | 报告 → 候选 → 修复 Trace → 原始 Trace |
| GET | `/api/lineage/consistency?limit=N` | 只读一致性检查（step/trace ID、DAG 节点、派生来源） |

响应统一结构：

```
{ lineage_engine_version, root_kind, root_id,
  nodes: [{node_id, kind, label, status, origin, storage, detail, note, source_api}],
  edges: [{from, to, relation}],
  summary: {node_count, available_count, not_available_count, origin_counts,
            has_real_api_evidence, has_missing_evidence},
  origin_vocabulary, storage_vocabulary, notes }
```

**安全**：不接受路径参数（只接受 ID）、不做任何写操作、不触发 Agent/模型调用、响应不含密钥。

---

## 5. 前端入口

| 页面 | 入口 | 内容 |
|------|------|------|
| Trace 详情 `/traces/:id` | 页面底部「证据血缘（Evidence Lineage）」卡片（自动加载） | 完整分层链 |
| Evaluation Center 案例抽屉 | 「查看证据血缘」按钮 → 抽屉 | 评测运行→案例→Trace→Run→Step→Tool |
| Counterfactual Lab | 结果区下方血缘卡片 | 实验→原始/变体 Trace |
| Repair Studio | 页面底部血缘卡片（验证后出现） | 报告→候选→修复 Trace→原始 Trace |

面板特性：
- 按**分层**渲染（结果来源 / 案例·候选 / Trace / Agent Run / 执行步骤 / 工具调用与结果 / 一致性告警）；
- 每节点显示 `origin` 徽标（REAL API / MOCK / SIMULATED / UNKNOWN）与 `storage` 徽标（已保存 / 内存登记 / 不可用）；
- 缺失证据红色高亮 + `NOT_AVAILABLE` + 原因；
- 节点可展开查看 `detail` JSON、来源接口路径，Trace 节点可直接跳 Trace / DAG；
- 「查看关系边」可展开完整边表；刷新后重新从后端读取，不依赖前端状态。

---

## 6. 一致性验证

### 6.1 `consistency_check` 检查项

| 检查 | 说明 |
|------|------|
| `step_trace_id_mismatch` | `step.trace_id` 必须等于所属 Trace 的 `trace_id` |
| `duplicate_step_id` | 同一 Trace 内 `step_id` 唯一 |
| `dag_step_mismatch` | DAG 节点集合必须与步骤集合一致 |
| `dag_build_error` | DAG 构建异常 |
| `broken_derived_from` | 派生 Trace 的 `derived_from` 必须指向存在的 Trace |
| `derived_marked_real_api` | 派生 Trace 不得标记为 `real_api` |

实测：488 份 Trace 全量检查 **0 issue**。

### 6.2 第 44 步修复的一致性问题

| # | 问题 | 根因 | 修复 | 状态 |
|---|------|------|------|------|
| **A** | 实验出现两套 `experiment_id`（登记表 `exp-a5…` vs 引擎结果 `exp-c6…`） | `experiment_store` 生成 ID 后未传给引擎，引擎自行再生成 | 引擎新增 `experiment_id` 参数；`_run_with_timeout` 透传 | FIXED |
| **B** | 反事实变体 Trace `trace-cf-*` 查询 **404** | 派生 Trace 只存在于内存返回值，从未落盘 | 新增 `derived_trace.save_derived_trace`，引擎在 replays 后落盘 | FIXED |
| **C** | 修复候选的 `repair_trace_id` 查询 **404** | 同 B（修复验证复用反事实引擎） | 同 B；并区分 `derivation=repair_validation` | FIXED |
| **D** | 评测 Trace `metadata` 无 `data_source` | `evaluation_engine._persist_trace` 直接写原 Trace | 落盘时补 `data_source=simulated` + `simulated=true` + `origin_note` | FIXED |
| **E** | 派生结果可能被误标为真实数据 | 无统一来源标记约定 | `derived_trace` 强制 `data_source=simulated` + 保留 `source_data_source` | FIXED |

---

## 7. 已知限制

1. **登记表仅内存**：Run / Experiment / Repair / Evaluation / Multi-Judge 记录进程重启后丢失；Trace 与派生 Trace 已落盘，因此"证据"不会丢失，但"运行登记"会。
   → 血缘对这类节点标记 `storage = MEMORY` 并给出说明（技术债 P2-10）。
2. **历史数据不可追溯**：第 44 步之前生成的 `trace-cf-*` 从未落盘，血缘会显示 `NOT_AVAILABLE`（诚实标注，不补造）。
3. **旧 Trace 缺 `data_source`**：早期数据集 Trace 只有 `simulated=true`，血缘归为 `SIMULATED` 并注明"缺少 data_source"，不推断为真实。
4. **Comparison / Calibration 无 ID**：即时计算产物不存储，无法作为血缘根节点（仅可作为过程展示）。
5. **未提供图数据库/跨 run 全局血缘视图**：目前按需从单个根节点展开。
6. **`initial_run` 与 `case` 的 judge 明细**未拆成独立节点（挂在案例节点 detail 中）。
7. 前端血缘面板**未做虚拟滚动**，超大 Trace（>200 步）展开会较慢。
