# 自动化 Bad Case 归因（Automated Bad Case Attribution）

- 代码：`backend/app/services/bad_case_attribution.py`
- API：`/api/attribution/*`（9 个端点）
- 前端：`frontend/src/components/AttributionPanel.tsx`（证据图 / 抽屉 / 批量面板）
- 引擎版本：`1.0.0`｜报告目录：`data/attribution/reports/`

---

## 1. 归因入口

| 入口 | 说明 |
|------|------|
| 评测运行（批量） | `POST /api/attribution/runs`：读取该运行的所有 Bad Case 逐一归因 |
| 单案例 | `POST /api/attribution/evaluations/{run_id}/cases/{case_id}`：仅 Bad Case，非 Bad Case 返回 409 |
| 单 Trace | `POST /api/attribution/traces/{trace_id}`：无评测上下文时直接归因 |

## 2. 归因类型（10 类）

`TOOL_TIMEOUT`｜`TOOL_EXCEPTION`｜`INVALID_ARGUMENTS`｜`INVALID_TOOL_RESULT`｜`EMPTY_RETRIEVAL`｜
`PERMISSION_DENIED`｜`TASK_INCOMPLETE`｜`OUTPUT_MISMATCH`｜`PROCESS_VIOLATION`｜`UNKNOWN`

- **允许一个 Bad Case 有多条归因**（不同 cause_type 并存）；
- `root_cause_status ∈ {MULTI_ANOMALY, SINGLE_CONFIRMED, INTERVENTION_SUPPORTED_ONLY, UNKNOWN}`；
- **不输出单一 root_cause 断言**（测试断言报告中不存在 `root_cause` 字段）。

## 3. 证据等级（5 级）

| 等级 | 含义 | 判定来源 |
|------|------|---------|
| `CONFIRMED_EVENT` | Trace 直接记录的异常 | `step.metadata.fault_injection`、`step.error`、工具失败状态 |
| `OBSERVED_DIFFERENCE` | 观察到的差异 | 评测指标 FAIL、与基线 Trace 的对比结果 |
| `POTENTIAL_CAUSE` | 推断（不构成因果） | **DAG 可达性**、异常与失败的先后共存 |
| `INTERVENTION_SUPPORTED` | 有干预证据 | **真实 experiment_id** 引用 + 控制条件 |
| `UNKNOWN` | 证据不足 | 无故障注入 / 无错误 / 无指标差异 / 无实验 |

规则落实：
1. DAG 后继节点 → 一律 `POTENTIAL_CAUSE`，且 `uncertainty` 明写"仅为依赖关系，不构成因果"，`is_conclusive=false`；
2. 反事实实验 → `INTERVENTION_SUPPORTED`，必须带 `experiment_refs`（含 `control_conditions` / `replays` / 引擎自评因果等级）；
3. **不生成置信概率**：只有 `confidence_basis`（文字依据），无 `confidence` / `probability` / `score` 数值（有测试断言）。

## 4. 分析流程

```
评测运行 → Bad Case 列表
   ↓
案例指标 → failed_metrics / unknown_metrics（UNKNOWN 不计入通过或失败）
   ↓
Trace（load_trace）→ observed_events：
   · fault_injection（注入故障）
   · step_error（异常类名 / 消息）
   · tool_failed（工具失败但无 error）
   ↓
候选归因：
   1) 故障注入类型 → 对应 TOOL_* / PERMISSION_DENIED 等（CONFIRMED_EVENT）
   2) step.error 类名映射（CONFIRMED_EVENT）
   3) 指标 FAIL → OUTPUT_MISMATCH（OBSERVED_DIFFERENCE）
   4) tool_correctness FAIL → PROCESS_VIOLATION 或 TASK_INCOMPLETE（按证据文本区分）
   5) 故障 + 任务失败共存 → TASK_INCOMPLETE（POTENTIAL_CAUSE）
   6) DAG 后继节点 → UNKNOWN/POTENTIAL_CAUSE（依赖关系）
   7) 同任务基线对比 → OUTPUT_MISMATCH（OBSERVED_DIFFERENCE）
   8) 反事实实验 → INTERVENTION_SUPPORTED
   9) 以上皆无 → UNKNOWN
   ↓
按 (cause_type, evidence_level) 合并 → AttributionReport → 持久化
```

## 5. 报告结构（持久化到 `data/attribution/reports/attr-*.json`）

`attribution_id`、`evaluation_run_id`、`dataset_id`、`case_id`、`trace_id`、
`failed_metrics`、`unknown_metrics`、`observed_events`、`candidate_causes`、
`evidence_refs`、`evidence_levels`、`affected_steps`、`experiment_refs`、
`root_cause_status`、`ambiguity_notes`、`limitations`、`generated_at`、`notes`

## 6. API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/attribution/types` | 归因类型 / 证据等级 / 等级规则 / 限制 |
| POST | `/api/attribution/runs` | 批量归因（`evaluation_run_id`、可选 `case_ids`、`max_cases` ≤20） |
| POST | `/api/attribution/evaluations/{run_id}/cases/{case_id}` | 单案例归因（非 Bad Case → 409） |
| POST | `/api/attribution/traces/{trace_id}` | 单 Trace 归因 |
| GET | `/api/attribution/reports` | 已持久化报告列表 |
| GET | `/api/attribution/reports/{id}` | 报告详情 |
| GET | `/api/attribution/reports/{id}/evidence` | 证据详情（按等级分组） |
| GET | `/api/attribution/reports/{id}/trace` | 关联 Trace（含 trace/dag/lineage 链接；缺失返回 NOT_AVAILABLE） |
| GET | `/api/attribution/reports/{id}/experiments` | 引用的真实实验（逐条解析状态，未找到不捏造） |

限制：单次批量 ≤20；`confidence_probability = not_emitted`；`paid_api_called=false`。

## 7. 前端

**Evaluation Center**：
1. 结果页新增「自动归因（Automated Bad Case Attribution）」卡片 → 「运行归因」→ 展示 Bad Case 数、已归因数、证据等级分布、逐案例根因状态与异常类型；
2. Bad Case 证据抽屉新增「自动归因」按钮 → 归因抽屉：失败指标表、异常事件表、**归因证据图**（按 5 类关系分层：直接证据 / 观察差异 / 依赖关系 / 干预证据 / 未知）、候选节点（依据 + 不确定性 + 可展开证据引用 + 关联实验控制条件）、Trace/DAG 跳转、限制说明；
3. 通过归因按钮仅对 Bad Case 显示（非 Bad Case 不显示，有 e2e 断言）。

## 8. 受控失败数据集

`data/evaluation/dataset_fault_v1.json`（`tracepilot-fault-v1`，8 案例，`data_source=synthetic`）：

| 案例 | 设计 | 实测结果 |
|------|------|---------|
| fail-01-timeout | tool_timeout 永久故障 | FAIL → TOOL_TIMEOUT |
| fail-02-exception | tool_exception 永久故障 | FAIL → TOOL_EXCEPTION |
| fail-03-invalid-args | 非法表达式 + invalid_arguments | FAIL → INVALID_ARGUMENTS |
| fail-04-invalid-result | 结果被篡改 | UNKNOWN → 计入 unknown_metrics |
| fail-05-empty-retrieval | 检索为空 | UNKNOWN → 计入 unknown_metrics |
| fail-06-permission | permission_denied | FAIL → PERMISSION_DENIED |
| fail-07-wrong-tool | 应 text_analyzer 却用 calculator | FAIL → PROCESS_VIOLATION |
| fail-08-answer-mismatch | 期望值不匹配 | FAIL → OUTPUT_MISMATCH |

## 9. 实测结果（单次批量归因）

| 指标 | 值 |
|------|-----|
| Bad Case 数 | 6（另 2 个 UNKNOWN 不进 Bad Case） |
| 已归因 | 6 |
| 证据等级分布 | CONFIRMED_EVENT 9 / POTENTIAL_CAUSE 4 / OBSERVED_DIFFERENCE 1 |
| 根因状态 | MULTI_ANOMALY 4 / SINGLE_CONFIRMED 1 / UNKNOWN 1 |
| 归因类型 | TOOL_TIMEOUT, TOOL_EXCEPTION, INVALID_ARGUMENTS, PERMISSION_DENIED, TASK_INCOMPLETE, PROCESS_VIOLATION, OUTPUT_MISMATCH, UNKNOWN |
| 批量耗时 | 冷启动 1.36s / 热态 0.05s（含索引缓存） |
| 付费 API | 0 次 |

## 10. 已知限制

1. **无基线时**只能给出 OBSERVED_DIFFERENCE / CONFIRMED_EVENT，无法给出因果比较；
2. **DAG 依赖不等于因果**，只能作为 POTENTIAL_CAUSE；
3. 归因规则基于确定性启发式（故障类型映射、错误类名映射、指标状态），**未做语义级根因推理**；
4. 反事实实验引用依赖实验记录仍在内存登记表（重启后实验不可查询 → 报告保留 ID 但标记不可解析）；
5. 归因报告只读历史证据，不重新执行任何案例（因此不会产生新 Trace）；
6. `INTERVENTION_SUPPORTED` 仅在存在真实实验时出现，当前受控失败数据集未跑实验，故实测中该等级为 0；
7. Trace 索引缓存 TTL 30s，新增 Trace 可能短暂不可见。
