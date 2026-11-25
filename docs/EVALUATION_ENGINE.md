# Agent Evaluation Engine 评测引擎

- 对应代码：`backend/app/services/evaluation_engine.py`
- 评测集：`data/evaluation/dataset_v1.json`（13 个受控案例，全部 `synthetic`）
- API：`GET /api/evaluation/datasets`、`POST /api/evaluation/runs`
- 状态：已实现，16 项引擎测试 + 6 项 API 测试通过（确定性模拟环境）

---

## 1. 评测指标（六大维度）

每个指标固定包含 `metric_name` / `metric_value` / `evaluation_method` / `evidence` / `confidence_or_limitations` / `status`。

| 维度 | 检查内容 | 判定方法 |
|------|----------|----------|
| **Task Success** | 最终答案正确性 | exact / numeric(±tolerance) / contains 三类匹配 |
| **Tool Correctness** | 必需工具、禁止工具、最少调用次数、错误期望 | 规则比对 Trace 中的 tool_call 步骤 |
| **Process Quality** | 步骤数区间、DAG 拓扑完整性、规划步骤存在性 | 结构规则 + `build_dag` 校验 |
| **Recovery** | 故障后是否恢复并完成任务 | 失败工具步骤后是否存在成功工具步骤 |
| **Efficiency** | 耗时、Token、工具调用次数 | 墙钟时间 + Trace 中 token_usage |
| **Safety** | 危险工具、敏感串、代码执行痕迹 | 白名单比对 + 正则扫描 + 痕迹扫描 |

### 状态语义

| 状态 | 含义 |
|------|------|
| `PASS` | 有充分证据表明满足要求 |
| `FAIL` | 有充分证据表明违反要求 |
| `UNKNOWN` | **证据不足**，不可解读为通过或失败 |
| `NOT_APPLICABLE` | 该维度对本案不适用（如无故障案例的 recovery） |

### 整体状态聚合规则

- 任一维度 `FAIL` → 整体 `FAIL`（并计入 Bad Case）
- 全部 `NOT_APPLICABLE` → 整体 `NOT_APPLICABLE`
- 存在 `UNKNOWN` → 整体 `UNKNOWN`
- 其余 → 整体 `PASS`

## 2. 指标计算口径

所有汇总指标显式携带 `numerator` / `denominator` / `denominator_definition`：

| 指标 | 分子 | 分母 |
|------|------|------|
| `task_success_rate` | 整体 PASS 的案例数 | **已实际执行**的案例数 |
| `tool_correctness_rate` | 工具维度 PASS | 工具维度可判定（PASS/FAIL）案例数 |
| `recovery_rate` | 恢复维度 PASS | 恢复维度可判定案例数 |
| `safety_pass_rate` | 安全维度 PASS | 安全维度可判定案例数 |

恒等式（报告中可校验）：

```
total_cases = executed_cases + execution_error_cases + not_executed_cases
executed_cases = passed + failed + unknown + not_applicable
```

**未执行案例与执行异常案例绝不进入成功率分子，也绝不被算作成功。**

## 3. 评测数据集

`data/evaluation/dataset_v1.json`，13 个案例覆盖：

| # | case_id | 场景 |
|---|---------|------|
| 1 | case-01-calc-exact | 数值计算（精确） |
| 2 | case-02-calc-tolerance | 数值计算（容差 0.1+0.2） |
| 3 | case-03-text-analyzer | 文本处理 |
| 4 | case-04-sql-readonly | SQL 只读查询（独立评测库 `eval_sales.db`） |
| 5 | case-05-tool-argument-error | 工具参数错误 |
| 6 | case-06-tool-timeout | 工具超时 |
| 7 | case-07-tool-exception | 工具异常 |
| 8 | case-08-empty-retrieval | 空检索结果 |
| 9 | case-09-invalid-tool-result | 错误工具结果 |
| 10 | case-10-permission-denied | 权限拒绝 |
| 11 | case-11-fault-recovery | 故障恢复 |
| 12 | case-12-multi-step-dependency | 多步骤依赖 |
| 13 | case-13-safety-constraints | 安全约束 |

字段：`case_id` / `task_description` / `expected_outcome` / `expected_tool_behavior` / `evaluation_rules` / `scenario_type` / `data_source`（+ `execution` 受控执行定义）。

**数据来源标记**：数据集与每个案例均标注 `data_source`（`synthetic` / `recorded` / `real_api`），当前全部为 `synthetic`，并在 `data_source_note` 中显式声明「不是真实用户数据」。

## 4. 确定性验证器

10 个确定性验证器，全部基于规则/数值比较，不使用 LLM 判断：

1. 最终答案精确匹配（exact）
2. 数值结果容差验证（numeric ± tolerance）
3. 必需工具调用验证
4. 禁止工具调用验证
5. 执行步骤数量检查（min/max）
6. 工具调用错误检查（可区分「预期出错」与「非预期出错」）
7. 故障恢复状态检查
8. 执行耗时统计（墙钟）
9. Token 使用量统计
10. 安全约束检查（危险工具名 / 敏感串 / `subprocess`、`os.system`、`eval(`、`__import__` 痕迹）

**三条硬约束**：
- 没有可靠证据 → `UNKNOWN`，不填 0；
- 答案正确 **不** 自动代表过程正确（process_quality 独立判定）；
- 没有错误日志 **不** 代表安全（safety 主动扫描）。

## 5. 批量评测流程

```
加载评测集（结构校验）
  → 生成 evaluation_run_id
  → 逐案例受控执行（确定性模拟模型 + 本地白名单工具 + FaultInjector）
  → 采集 TraceRecord（每次执行独立 trace_id / run_id）
  → 六维度确定性验证
  → 汇总指标（含分母口径）
  → 收集 Bad Case / 执行异常
  → 输出 EvaluationReport
```

执行异常（如非白名单工具）单独计入 `execution_errors`，不进入 `case_results` 的成功统计。

## 6. Bad Case 定义

`overall_status == FAIL` 的案例即为 Bad Case，记录：

- `case_id`、`trace_id`
- `failed_dimensions`（失败维度列表）
- `unknown_dimensions`（证据不足维度列表）

`UNKNOWN` 案例**不**计入 Bad Case，但也不计入通过。

## 7. UNKNOWN 处理

| 情形 | 处理 |
|------|------|
| 缺失结束时间 | efficiency → `UNKNOWN`（不填 0） |
| 无法提取最终答案 | task_success → `UNKNOWN` |
| 答案中无数值但要求数值匹配 | task_success → `UNKNOWN` |
| 无故障但要求恢复判定 | recovery → `NOT_APPLICABLE` |
| 案例未定义答案规则 | task_success → `NOT_APPLICABLE` |

## 8. 安全限制

- 仅允许白名单工具：`calculator` / `text_analyzer` / `sqlite_query`
- SQL 只读查询指向 `data/evaluation/eval_sales.db`（独立评测库，**不触碰真实业务库**）
- 不调用任何付费模型 API（`paid_api_called: false`）
- 不执行任意 Shell / Python / 外部网络请求
- 评测结果不修改原始 Trace，不写入业务数据库
- 单次评测案例上限 50

## 9. 后续 LLM-as-a-Judge 扩展

当前全部为确定性验证器。后续可在保持确定性基线的前提下增加：

1. **语义正确性 Judge**：对开放式答案做语义等价判断（需标注为「非确定性」，单独归入 `judge_metrics`，不混入确定性指标）；
2. **过程合理性 Judge**：评估规划步骤语义质量（当前只有结构性检查）；
3. **置信度校准**：用多条 Judge 结果投票 + 与确定性基线对照，输出 judge 与规则的分歧率；
4. **成本**：Judge 需真实 API，必须显式开关并计入成本统计，默认关闭；
5. **红线**：Judge 结果永远不能覆盖确定性验证器的 `FAIL`，且必须标注证据来源。

---

## 10. Evaluation Center 产品化（第 36 步）

### 10.1 异步运行与资源控制（evaluation_store.py）

| 机制 | 取值 | 说明 |
|------|------|------|
| 并发上限 | 1 | 评测为 CPU 密集型批处理，超出返回 HTTP 429 |
| 单次执行超时 | 120s | 超时标记 `failed` |
| 重复提交抑制 | 60s 窗口 | 相同 (dataset_id, max_cases) 直接复用 run_id |
| 内存保留上限 | 20 条 | 防无界增长；进程重启清空 |
| 数据集白名单 | `tracepilot-eval-v1` | 仅允许受控目录内的固定文件，不接受客户端路径 |

### 10.2 API 契约

| 方法与路径 | 说明 |
|-----------|------|
| `GET /api/evaluation/datasets` | 数据集列表（元信息） |
| `GET /api/evaluation/datasets/{dataset_id}` | 数据集详情（版本/案例数/来源/案例元信息）；非白名单 404 |
| `GET /api/evaluation/runs` | 运行列表 |
| `POST /api/evaluation/runs` | 创建异步运行 → `run_id` + 状态 + links；非法数据集 404、并发满 429、参数非法 422 |
| `GET /api/evaluation/runs/{run_id}` | 状态 + 完整报告（指标、案例、Trace 关联、环境、限制） |
| `GET /api/evaluation/runs/{run_id}/bad-cases` | Bad Case + UNKNOWN 案例 + 执行异常 + 限制 |
| `GET /api/evaluation/runs/{run_id}/cases/{case_id}` | 单案例六维度证据 + Trace/DAG 关联链接 |

### 10.3 证据下钻链（Evaluation Evidence Drill-down）

```
指标卡 / 六维指标表  →（点击 FAIL 数）→ 维度下钻抽屉：该维度失败案例列表
      → 打开案例证据抽屉：六维度 metric + evidence + limitations
      → 查看 Trace（/traces/{trace_id}）→ 查看 DAG（/dag/{trace_id}）
```

- 关联关系由后端真实数据保证：`case_results[].trace_id` 即该案例实际执行产生的 Trace；
- 评测 Trace 持久化到 **独立目录** `data/evaluation/traces/`（已 gitignore），
  不进入主 Trace 列表，但 `load_trace` 会回退查找，因此按 ID 可正常查看 Trace 与 DAG；
- 所有指标统计值均由 `case_results` 实时可复算（维度计数与案例状态严格对齐）。

### 10.4 前端页面

`frontend/src/pages/EvaluationCenter.tsx`，路由 `/evaluation`：

- 数据集选择（显示版本/案例数/来源/synthetic 声明）
- 案例上限配置 + 运行评测 + 执行进度
- 指标卡：任务成功率 / 工具正确率 / 故障恢复率 / 平均耗时（均显示分子/分母）
- 运行概览：total / executed / passed / failed / unknown / N·A / 执行异常 / 未执行
- 六维指标表：PASS / FAIL（可点击下钻）/ UNKNOWN / N·A / 可判定分母 / 通过率
- 案例结果表 + 筛选（全部 / Bad Case / UNKNOWN / PASS）
- 案例证据抽屉：六维度证据 + Trace / DAG 跳转
- 评测环境与限制说明
