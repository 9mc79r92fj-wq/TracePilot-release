# 失败模式挖掘（Failure Pattern Mining）

- 代码：`backend/app/services/failure_pattern_mining.py`
- API：`/api/failure-patterns/*`（8 个端点）
- 前端：`frontend/src/components/FailurePatternExplorer.tsx`（嵌入 Evaluation Center）
- 引擎版本：`1.0.0`｜报告目录：`data/failure_patterns/reports/`

---

## 1. 流程

```
Evaluation Run
  → Bad Case（已执行且 is_bad_case）
  → 归因报告（复用已持久化的；缺失则实时计算并落盘）
  → 结构化失败特征（5 维）
  → Failure Fingerprint（内容哈希，可复现）
  → 按指纹分组
  → 统计（数量 / 占比 / 代表性案例 / Trace 关联 / 证据关联）
  → 失败模式报告（持久化）
  → 两次运行对比（按 fingerprint_id 对齐）
```

## 2. 指纹字段（5 维，允许缺失）

| 字段 | 来源 | 缺失规则 |
|------|------|---------|
| `failure_type` | 归因候选（证据等级优先 → 具体类型优先） | 无候选时为 `UNKNOWN` |
| `tool_name` | Trace 中**状态为 failed** 的 `tool_call` 工具名（多个用 `+` 连接） | 无失败工具 → `null` |
| `failed_metric` | 归因 `failed_metrics`（确定性验证器 FAIL 的指标） | 无失败指标 → `null` |
| `recovery_status` | `recovered` / `not_recovered` / `no_tool_failure` / `unknown`，依据 `metadata.recovery_observed` 或**步骤顺序**（失败后是否仍有成功调用） | 无 Trace → `unknown` |
| `scenario_type` | 评测案例声明的场景类型 | 案例未声明 → `null` |

- 指纹 = `fp-` + `sha256(canonical_json)[:12]`（同输入 → 同 ID，可复现）；
- `fingerprint_label` 用 `-` 表示缺失维度；
- **缺失字段绝不猜测填充**（`missing_fields` 显式列出）。

### 失败类型优先级（避免泛化类型吞掉具体类型）

`TOOL_TIMEOUT` → `TOOL_EXCEPTION` → `INVALID_ARGUMENTS` → `INVALID_TOOL_RESULT` →
`EMPTY_RETRIEVAL` → `PERMISSION_DENIED` → `PROCESS_VIOLATION` → `OUTPUT_MISMATCH` →
`TASK_INCOMPLETE` → `UNKNOWN`

证据等级优先：`CONFIRMED_EVENT` > `INTERVENTION_SUPPORTED` > `OBSERVED_DIFFERENCE` > `POTENTIAL_CAUSE` > `UNKNOWN`。

## 3. 统计与分母（透明）

| 分母 | 含义 |
|------|------|
| `total_cases_in_run` | 本次运行全部案例数 |
| `executed_cases` | 实际执行完成数（**未执行不计入任何模式**） |
| `bad_cases` | 评测判定为 Bad Case 的数量 |
| `analyzed_cases` | 本次分析的 Bad Case 数（受 `max_cases` ≤50 限制） |
| `unknown_cases` | 归因未找到可确认失败类型的案例数 |
| `share_of_bad_cases` | 该模式案例数 / `analyzed_cases` |

自洽约束（测试断言）：
- 各模式 `case_count` 之和 == `analyzed_cases`；
- 各模式 `share_of_bad_cases` 之和 ≈ 1.0；
- 模式按 `case_count` 降序（同数按指纹 ID 稳定排序）。

**UNKNOWN 处理**：单独计数，不并入任何失败类型；仍保留在指纹分组中（`failure_type=UNKNOWN` 的模式），并在报告中列出 `unknown_cases[]`。

## 4. API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/failure-patterns/types` | 指纹字段 / 失败类型 / 恢复状态 / 策略 / 限制 |
| POST | `/api/failure-patterns/runs` | 创建模式分析（`evaluation_run_id`、`max_cases`≤50、`strategy`） |
| GET | `/api/failure-patterns/reports` | 报告列表 |
| GET | `/api/failure-patterns/reports/{id}` | 报告详情（模式 + 分母 + 限制） |
| GET | `/api/failure-patterns/reports/{id}/patterns/{pid}` | 模式详情（含成员案例指纹/证据等级） |
| GET | `/api/failure-patterns/reports/{id}/patterns/{pid}/cases` | 关联 Bad Case（含 trace/dag/attribution 链接） |
| GET | `/api/failure-patterns/reports/{id}/patterns/{pid}/trace` | 关联 Trace（逐条校验可读性，不可读标 `NOT_AVAILABLE`） |
| POST | `/api/failure-patterns/compare` | 两次运行对比（`run_a`/`run_b`，同运行 → 400） |

## 5. 前端 Failure Pattern Explorer

位于 Evaluation Center（评测完成后自动挂载）：
1. **选择评测运行**：随评测结果自动选择当前运行；对比区可选择历史运行；
2. **失败模式分布图**：横向占比条（点击下钻）；宽度 = 案例数/分析数；
3. **高频问题排行榜**：Top 5，显示指纹标签与缺失字段；
4. **按失败类型筛选**（`failure_type` 下拉）；
5. **按工具名称筛选**（含「无失败工具」选项）；
6. **代表性案例**：每个模式记录 `representative_case`（优先有 CONFIRMED_EVENT 证据）；
7. **归因证据**：案例表「证据」→ 模式抽屉内 Evidence Drill-down（完整归因面板：失败指标、异常事件、证据图、Trace/DAG 跳转）；
8. **Trace / DAG 跳转**；
9. **两个运行对比**：新增 / 消失 / 增加 / 减少 / 不变 五类状态 + 逐指纹 Δ 表；
10. **分母透明展示**：页面顶部固定展示统计分母与口径说明。

## 6. 实测（受控失败数据集 `tracepilot-fault-v1`）

| 指标 | 值 |
|------|-----|
| 模式数量 | **6** |
| 分析 Bad Case | **6**（运行 8 例，其中 2 例 UNKNOWN 非 Bad Case） |
| 分母 | total 8 / executed 8 / bad 6 / analyzed 6 / unknown 0 |
| 模式构成（各 1 例，16.7%） | TOOL_TIMEOUT·calculator·tool_correctness·not_recovered·tool_timeout；TOOL_EXCEPTION·calculator·…；INVALID_ARGUMENTS·calculator·…；PERMISSION_DENIED·sqlite_query·…·recovered；PROCESS_VIOLATION·-·…·wrong_tool；OUTPUT_MISMATCH·-·task_success·…·answer_mismatch |
| 对比（同数据集两次运行） | 新增 0 / 消失 0 / 不变 6 → 确定性可复现 |
| 对比（故障集 vs 正常集） | 故障集 6 模式全部在正常集「消失」（正常集无 Bad Case） |
| 付费 API | 0 次 |

## 7. 扩展接口（为语义聚类预留）

```python
from backend.app.services.failure_pattern_mining import register_cluster_strategy

def semantic_key(feature) -> str:
    """返回分组键：例如基于 embedding 的聚类标签。"""
    ...

register_cluster_strategy("semantic", semantic_key)
analyze_run(run_id, strategy="semantic")   # 立即生效
```

未注册的策略会抛 `FailurePatternError`（API 400）。

## 8. 已知限制

1. **相同指纹 ≠ 相同根因**：指纹只表示 5 维失败特征相似，报告与 UI 均明示该限制；
2. 指纹基于确定性规则，**尚未实现语义/embedding 聚类**（仅预留接口）；
3. `tool_name` 只取「记录为 failed」的工具步骤；若执行器未标记 failed，则该维度为 `null`（不猜测）；
4. `recovery_status` 在缺少 `recovery_observed` 且只有 1 次工具调用时判为 `unknown`（避免把单次失败当"未恢复"）；
5. 分析上限 50 个案例，超出部分不参与统计（分母 `analyzed_cases` 已体现）；
6. 跨运行的占比变化需结合两侧分母解读（报告两侧分母均已列出）；
7. 归因报告与模式报告均写入 `data/`，长期累积需清理（P3 技术债）。
