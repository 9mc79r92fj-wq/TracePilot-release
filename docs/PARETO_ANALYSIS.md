# COST–LATENCY–QUALITY PARETO ANALYSIS（第 54 步）

在既有 EvaluationEngine / EvaluationVersionComparison / CostAnalytics / RegressionDetector /
QualityGate 之上实现的**成本–延迟–质量三维帕累托分析**。

> **诚实性硬约束**
> 1. 候选全部来自本进程内**真实执行过的评测运行**（`evaluation_store`），不伪造 Agent 版本；
>    `agent_version` 缺失时输出 `null` 并注明原因。
> 2. 缺失指标一律为 `null`，**绝不把 UNKNOWN 当 0**；缺失任一维度的候选不进入帕累托前沿，
>    而是列入排除清单并写明原因。
> 3. **真实 API 数据与模拟数据不混合比较**：同一比较组内混用时判定 `NOT_COMPARABLE`，
>    不生成前沿，并明确「不得解释为真实模型性能比较」。
> 4. 成本来自 CostAnalytics：金额是本地价格表估算，**不是 Provider 实际账单**；
>    `PRICE_UNKNOWN` 时平均成本为 `null`（不按 0 计费）。
> 5. 全部分析只读本地数据，不调用任何付费 API（响应均含 `paid_api_called: false`）。

## 1. 三维指标

| 维度 | 字段 | 方向 | 定义 |
| --- | --- | --- | --- |
| 质量 | `task_success_rate` | 越高越好（max） | 评测报告：整体 PASS 案例数 / 已实际执行案例数 |
| 延迟 | `average_latency_ms` | 越低越好（min） | 评测报告 `metrics.average_duration_ms`（每案例 Trace 执行时长均值） |
| 成本 | `average_cost` | 越低越好（min） | CostAnalytics 评测总成本 / 已执行案例数；价格表估算，不是账单 |

候选结构（需求第二部分）：

```json
{
  "candidate_id": "cand-<evaluation_run_id>",
  "agent_version": null,
  "agent_version_note": "受控执行器无独立 agent_version 字段，未伪造",
  "evaluation_run_id": "eval-...",
  "dataset_id": "tracepilot-eval-v1",
  "dataset_version": "1.0.0",
  "dataset_group_key": "tracepilot-eval-v1@1.0.0",
  "evaluator_version": "1.0.0",
  "task_success_rate": 1.0,
  "average_latency_ms": 1.0,
  "average_cost": 0.0,
  "currency": "CNY",
  "sample_size": 8,
  "data_source": "simulated",
  "comparability_status": "COMPARABLE",
  "safety_pass_rate": 1.0,
  "safety_gate_status": null,
  "cost_status": "SIMULATED",
  "pricing_versions": ["2026.11-demo-simulated-zero"],
  "missing_fields": [],
  "trace_ids": ["trace-eval-..."]
}
```

安全评测结果存在时同时记录 `safety_pass_rate`；`safety_gate_status` 仅从已落盘的
质量门禁报告读取（`data/quality_gate/reports/qg-*.json`），没有记录时为 `null`，不伪造。

## 2. 支配关系定义

对候选 A、B（三维指标全部已知）：

**A 支配 B** 当且仅当：

```
A.quality >= B.quality 且
A.latency <= B.latency 且
A.cost    <= B.cost    且
至少一个维度严格更优（> / <）
```

- 完全相同的三项指标：**互不支配**，并列进入帕累托前沿（`tie_group` 标记）；
- 任一指标缺失的候选：不参与支配计算（避免把未知当 0）；
- 不可比 / 被约束排除的候选：不参与前沿计算。

输出中的支配关系包含 `strict_axes`（严格更优维度）与 `margins`（质量差/延迟差/成本差）。

## 3. 非支配解计算

1. 读取候选（按 `evaluation_store` 中已完成评测运行构建）；
2. 可比性检查（见第 4 节），不可比候选标 `NOT_COMPARABLE`、缺失指标标 `INSUFFICIENT_DATA`；
3. 应用交互约束（见第 5 节），记录每个候选的 `exclusion_reasons`；
4. 对可进入前沿的候选两两比较支配关系；
5. `dominates` / `dominated_by` 列表 + 全局 `dominance_relations`；
6. 非支配解 = 没有任何候选支配它的候选 → `frontier`（按质量降序、延迟升序、成本升序排名）；
7. 被支配解 = 至少被一个候选支配 → `dominated`；
8. 并列组 = 三项指标完全相同的非支配候选 → `tie_groups`（互不支配，同时进入前沿）。

状态：

| 状态 | 含义 |
| --- | --- |
| `OK` | 存在满足条件的可比候选，前沿已计算 |
| `NO_FEASIBLE_CANDIDATE` | 约束筛选后没有候选满足条件 |
| `NO_COMPARABLE_CANDIDATE` | 没有可比候选（数据集/执行模式/币种/评分规则不一致等） |
| `INSUFFICIENT_DATA` | 所有候选都缺失三维指标 |
| `NO_CANDIDATE` | 没有可用评测候选（尚未运行评测） |

## 4. 可比性要求

比较前执行 9 项检查：

| # | 检查 | 规则 | 严重度 |
| --- | --- | --- | --- |
| 1 | 数据集一致 | 按 `dataset_id + dataset_version` 分组；分析自动选择候选数最多的可比组，其他组候选排除 | 警告（组外候选逐条不可比） |
| 2 | 评分规则一致 | Rubric 版本不一致 → 不可比；Judge 模式不一致 → 警告 | 严重 / 警告 |
| 3 | 指标定义一致 | 三维指标固定使用 `task_success_rate` / `average_duration_ms` / 平均成本 | 信息 |
| 4 | 执行模式一致 | 同一组内真实 API 与模拟混合 → `NOT_COMPARABLE`，不生成前沿 | 严重 |
| 5 | 成本币种一致 | 币种不同 → 不可比；币种缺失 → 警告 | 严重 / 警告 |
| 6 | 价格版本一致 | 价格版本不同 → 成本估算口径可能不同，警告 | 警告 |
| 7 | 样本数量合理 | `sample_size < min_sample_size`（默认 3）→ 统计稳定性警告 | 警告 |
| 8 | 数据来源兼容 | 来源未知 → 警告；真实+模拟混用 → 严重 | 严重 / 警告 |
| 9 | 缺失指标 | 缺失三维任一指标 → 候选 `INSUFFICIENT_DATA`，排除出前沿 | 警告 |

输出包含 `verdict`（COMPARABLE / COMPARABLE_WITH_WARNINGS / NOT_COMPARABLE）、问题列表
（含 `candidate_ids`）、真实/模拟候选计数与 `mixed_real_and_simulated` 标记。

> 跨数据集候选被排除出比较组时，组内仍可比，但整体给出明确警告；
> 真实/模拟混用则**不生成前沿**，禁止解释为真实模型性能比较。

## 5. 约束筛选（交互约束）

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `minimum_quality` | 0..1 | 最低任务成功率（含等号） |
| `maximum_latency_ms` | >=0 | 最大平均延迟 |
| `maximum_cost` | >=0 | 最大平均成本（币种一致前提下） |
| `required_safety_gate` | PASS/WARN/INCONCLUSIVE/BLOCK | 至少达到该安全门禁等级；无门禁记录的候选不满足 |
| `dataset_version` | 字符串 | 限定数据集（支持 `dataset_id`、版本号或 `dataset_id@version` 组合键） |

用户修改约束后可重新筛选：API 返回新的候选集合、`feasible_count` 与重算后的前沿；
无候选满足时状态为 `NO_FEASIBLE_CANDIDATE`。未知字段/越界值返回 400。

## 6. API

| 端点 | 说明 |
| --- | --- |
| `GET /api/pareto/candidates?limit=` | 查询候选版本（含可比性标注与数据集分组） |
| `GET /api/pareto/analyses` | 历史分析列表 |
| `POST /api/pareto/analyses` | 创建分析（body: `constraints` / `min_sample_size` / `limit`），返回完整报告 |
| `GET /api/pareto/analyses/{analysis_id}` | 查询报告 |
| `GET /api/pareto/analyses/{analysis_id}/frontier` | 查询非支配解 |
| `GET /api/pareto/analyses/{analysis_id}/dominated` | 查询被支配解 |
| `GET /api/pareto/analyses/{analysis_id}/dominance` | 查询支配关系 |
| `POST /api/pareto/analyses/{analysis_id}/filter` | 按新约束重新筛选并重算前沿（不修改原报告） |
| `GET /api/pareto/analyses/{analysis_id}/candidates/{candidate_id}/evaluation` | 查询关联 Evaluation Run |
| `GET /api/pareto/analyses/{analysis_id}/candidates/{candidate_id}/traces` | 查询关联 Trace |

报告持久化：`data/pareto/reports/pareto-<id>.json`（gitignored 运行数据）；
内存保留最近 100 份。错误约定：参数非法 → 400，报告/候选不存在 → 404。

## 7. 前端 `/pareto`

Interactive Pareto Explorer（专业浅色 SaaS 风格，PC/平板/手机）：

- **左侧约束面板**：评测数据集、最低成功率、最大延迟、最大成本、安全门禁；
  「应用约束」（重算前沿）/「生成新分析」/「重置」；数据来源计数与诚实性提示。
- **中部散点图**：自绘 SVG 三维等距投影，支持拖拽旋转、旋转/俯仰滑杆、悬停提示、
  点击选中；二维投影切换（质量-延迟 / 质量-成本 / 延迟-成本）适配移动端。
  非支配解绿色、被支配解灰色、约束排除浅灰。
- **右侧候选详情**：三维指标、Agent 版本（缺失显示「未提供（未伪造）」）、数据集、
  评测器版本、样本数、安全通过率/门禁、成本状态/币种/价格版本、被支配原因、
  可比性问题；「查看 Evaluation Run」内联展示运行摘要；「查看关联 Trace」跳转 Trace 详情。
- **底部**：Pareto Frontier 表（排名/数值/并列组/详情）、被支配解表（含被谁支配）、
  支配关系表（严格更优维度与差值）、排除候选与可比性警告列表。
- 顶部 KPI：候选版本、非支配解、被支配解、支配关系数量；状态横幅展示
  `NO_FEASIBLE_CANDIDATE` / `NO_COMPARABLE_CANDIDATE` / `INSUFFICIENT_DATA` / `NO_CANDIDATE`。

## 8. 数据来源与实测

- 候选来源：`evaluation_store`（内存中最近 20 条评测运行，`succeeded` 且有报告）；
  质量/延迟来自评测报告，成本来自 CostAnalytics（读取评测 Trace 文件 + 本地价格表）。
- 全量浏览器验收后本地实测（`evaluation_store` 最近 20 条真实评测运行）：

| 项 | 值 |
| --- | --- |
| 候选总数 | 20（`tracepilot-eval-v1@1.0.0` 组 16 个 + `tracepilot-fault-v1@1.0.0` 组 4 个） |
| 分析可比组 | `tracepilot-eval-v1@1.0.0`（16 个候选；fault 组 4 个被排除） |
| 满足约束候选 | 16 |
| 非支配解 | 1（质量 100%、延迟约 0.46ms、成本 0，安全门禁 `INCONCLUSIVE`） |
| 被支配解 | 15 |
| 支配关系 | 100（16 候选中有大量指标相同的候选互不支配） |
| 约束筛选 | `dataset_version=tracepilot-fault-v1 + minimum_quality=1` → `NO_FEASIBLE_CANDIDATE`；`required_safety_gate=PASS` → 可行 1 |
| 可比性 | `COMPARABLE_WITH_WARNINGS`（存在跨数据集候选，已排除并警告） |
| 数据来源 | 全部为受控模拟评测（Task Success 来自确定性模拟模型，不代表真实模型性能） |
| 成本 | 模拟执行器 0 成本（`SIMULATED_ZERO_COST`，不是真实计费）；真实 API 评测运行不存在 |
| 付费 API | 0 次 |

> 该实测仅用于验证管线正确性，**不是真实 Agent 版本评测结论**，不得作为产品性能对比宣传。

## 9. 已知限制

1. 候选池依赖进程内 `evaluation_store`：服务重启后候选为空（已落盘分析报告仍可查询，
   但关联 Evaluation Run / Trace 可能 404）；
2. 当前评测全部为受控模拟执行器，真实 API 只有 1 条历史 Trace 且没有对应评测运行，
   因此**不存在真实模型候选**，前沿不具备真实性能含义；
3. `agent_version` / `prompt_version` 缺失：候选只能以评测运行 ID 区分，不是真正的
   Agent 版本对比；
4. 模拟执行器 Token 恒为 0 → 成本恒为 0，三维帕累托的成本维在当前数据上退化；
5. 安全门禁状态只来自已落盘的门禁报告；多数候选没有门禁记录，门禁筛选会严格排除；
6. 三维散点为自绘等距投影（非 WebGL），无坐标轴刻度/缩放；移动端以二维投影为主；
7. 样本量小（评测案例数 ≤50）时统计稳定性有限，仅以警告标注，不做显著性检验；
8. 帕累托前沿仅表示候选集合内的非支配关系，不代表绝对最优，也不含概率/置信区间。

## 10. 测试

- 后端：`tests/test_pareto_analysis.py`（20 项：支配/非支配/多候选/并列/单一/缺失三维/
  数据集不同/币种不同/价格版本不同/真实与模拟混用/无可行候选/约束/安全门禁/前沿正确性/API/无付费调用）；
- 浏览器：`frontend/e2e/pareto.spec.ts`（3 项：KPI+可比性+散点+前沿；约束与
  NO_FEASIBLE_CANDIDATE+安全门禁；候选详情+Evaluation Run+Trace 跳转+悬停提示）；
- 全量：后端 `pytest tests -q`，浏览器 `npx playwright test --project=desktop-edge`，
  前端 `npm run build`。
