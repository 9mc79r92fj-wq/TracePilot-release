# EVALUATION_VERSION_COMPARISON · 评测版本对比（第 49 步）

> 目标：回答「候选版本相对基线版本，指标、案例、失败模式发生了什么变化」，并把
> **不可比**、**UNKNOWN**、**分母口径**、**缺失案例** 等容易造成误读的因素显式暴露。

## 1. 模块与入口

| 类型 | 位置 |
|------|------|
| 引擎 | `backend/app/services/evaluation_version_comparison.py` |
| API | `/api/version-comparison/*`（7 个只读/创建端点） |
| 前端 | `/version-compare`（`frontend/src/pages/VersionComparison.tsx`） |
| 测试 | `tests/test_evaluation_version_comparison.py`（11 项）+ `frontend/e2e/version-comparison.spec.ts` |

数据来源：**真实评测运行记录**（`evaluation_store`，仅内存保留最近 20 条）与真实数据集文件；
不根据前端选择拼装版本元数据，不调用付费 API（`paid_api_called=false`）。

## 2. 版本元数据（Version Descriptor）

`build_version_descriptor(run_id)` 从运行记录 + 数据集文件派生：

| 字段 | 来源 | 缺失时 |
|------|------|--------|
| `evaluation_version_id` | `ev-ver-` + 版本身份字段内容哈希（sha256 前 12 位） | — |
| `evaluation_run_id` | 运行记录 | — |
| `agent_version` | 受控执行器**没有**该字段 | `null` + `agent_version_note`（明示「未伪造」） |
| `prompt_version` | 仅 Judge 启用时存在 | `null` + 说明 |
| `model_name` / `model_simulated` | 报告 `environment.model` | `null` |
| `tool_config_version` | 工具列表 sha256 前 12 位 | 无工具时 `null` |
| `evaluator_version` | 报告 `engine_version` | `null` |
| `judge_mode` / `rubric_id` / `rubric_version` | 运行记录 + Rubric 文件 | `none` 时 Rubric 为 `null` |
| `dataset_id` / `dataset_version` / `dataset_fingerprint` / `dataset_case_count` | 报告字段 + 数据集文件 sha256 | 数据集不在白名单时 `null` |
| `execution_config` | `data_source`、`judge_mode`、`max_cases`、`execution_engine`、`paid_api_called` | — |
| `identity_hash` | 版本身份字段的完整 sha256 | — |

关键语义：

- **同版本 ≠ 同一次运行**：同一版本的两次独立运行 `evaluation_run_id` 不同，但
  `evaluation_version_id` / `identity_hash` 相同（版本对比要求「同版本两次运行」，因此
  评测存储新增 `force_new` 关闭 60s 去重）。
- 无法核对的字段保持 `null`，不猜测填充。

## 3. 可比性检查（Comparability）

`check_comparability(a, b)` 返回三档判定：

| verdict | 含义 |
|---------|------|
| `COMPARABLE` | 无任何 CRITICAL/WARNING 问题 |
| `COMPARABLE_WITH_WARNINGS` | 仅有 WARNING（如案例集合不同、工具配置不同） |
| `NOT_COMPARABLE` | 存在 CRITICAL（数据集/Rubric/执行模式不一致） |

问题码：

| code | 严重度 | 判定条件 |
|------|--------|----------|
| `DATASET_ID_MISMATCH` | CRITICAL | 数据集 ID 不同 |
| `DATASET_VERSION_MISMATCH` | CRITICAL | 数据集版本不同 |
| `DATASET_FINGERPRINT_MISMATCH` | CRITICAL | 数据集文件内容哈希不同 |
| `EVALUATOR_VERSION_MISMATCH` | WARNING | 评测器版本不同 |
| `JUDGE_MODE_MISMATCH` | WARNING | Judge 模式不同 |
| `RUBRIC_ID_MISMATCH` / `RUBRIC_VERSION_MISMATCH` | CRITICAL | 双方均启用 Judge 时 Rubric 不同 |
| `TOOL_CONFIG_MISMATCH` | WARNING | 工具配置哈希不同 |
| `EXECUTION_MODE_MISMATCH` | CRITICAL | 执行模式（模拟/真实）不同 |
| `PAID_API_INVOLVED` | WARNING | 任一侧调用过付费 API（成本口径不同） |
| `CASE_COUNT_MISMATCH` | CRITICAL | 数据集案例数不同 |
| `CASE_SET_MISMATCH` | WARNING | 实际案例集合不同（补充检查） |
| `MISSING_DATA` / `EXECUTION_ERRORS` | WARNING | 存在未执行/执行异常案例 |

Rubric 检查的**不适用**处理：双方都未启用 Judge 时，Rubric 不参与评分，跳过检查并记录
`skipped_checks=[{code:"RUBRIC_CHECK_SKIPPED", ...}]`，不报成「元数据缺失」（否则确定性评测
会永远被误判为「有警告不可比」）。

不可比结果**必须**明确：`note` + 前端红色提示「不得解释为性能提升或退化」；指标差异仍然
保留，但只作为诊断线索。

## 4. 指标差异

每个指标输出：

```json
{
  "metric": "task_success_rate",
  "baseline":  {"value": 0.5,  "numerator": 4, "denominator": 8},
  "candidate": {"value": 0.25, "numerator": 2, "denominator": 8},
  "delta": -25.0,
  "delta_unit": "percentage_point",
  "relative_change": -0.5,
  "relative_change_unit": "ratio_of_baseline",
  "status": "REGRESSED",
  "denominator_definition": "已实际执行的案例数（不含未执行/执行异常）",
  "shape": "ratio",
  "note": null
}
```

规则：

- **pp 与相对变化分开**：比率指标 `delta` 单位是百分点（`percentage_point`），
  `relative_change` 是相对基线的比例（`ratio_of_baseline`），禁止混用。
- **保留分子/分母**：比率指标必须带 `numerator` / `denominator` 与 `denominator_definition`；
  标量指标（`average_duration_ms`、`total_tokens`）`shape="scalar"`，分子分母如实为 `null`
  （不编造）。
- **UNKNOWN 不当 0**：任一侧 `value` 为 `null` → `delta=null`、`status="UNKNOWN"`，并附说明。
- 方向：`average_duration_ms` / `total_tokens` 越低越好（`LOWER_IS_BETTER_METRICS`），
  其余比率越高越好；`status ∈ {IMPROVED, REGRESSED, UNCHANGED, UNKNOWN}`。
- 维度差异（`dimension_deltas`）：来自 `metrics.dimension_breakdown`，保留 pass/fail/unknown/
  applicable_denominator 与 `pass_rate`，`delta` 为 pp。
- `bad_case_diff`：失败案例数、UNKNOWN 案例数分别对比；UNKNOWN 单独计数，不并入失败。

## 5. Case-level Diff

仅对**共同案例**做配对比较（按 `case_id` 对齐）：

| transition | 含义 |
|------------|------|
| `FAIL_TO_PASS` | 改善 |
| `PASS_TO_FAIL` | 回归（唯一会被回归检测计为案例回归的迁移） |
| `PASS_TO_PASS` | 稳定通过 |
| `FAIL_TO_FAIL` | 稳定失败 |
| `UNKNOWN_INVOLVED` | 任一侧为 UNKNOWN / NOT_APPLICABLE（不计入通过或失败） |

- 缺失案例（仅 A / 仅 B）单独列出，**不参与**状态变化统计；
- 每行保留两侧 `trace_id` 与 `links`（`/api/traces/{id}`、`/api/traces/{id}/dag`），
  支持前端跳转 Trace/DAG；
- `counts` 同时给出 common/improved/regressed/stable_pass/stable_fail/unknown_involved/
  only_in_baseline/only_in_candidate。

## 6. 失败模式分布变化

复用第 48 步 `failure_pattern_mining.analyze_run()`：

- 按 `fingerprint_id` 对齐两侧模式，输出 `NEW / RESOLVED / INCREASED / DECREASED / UNCHANGED`；
- 保留两侧 `denominators`（total/executed/bad/analyzed/unknown），分母透明；
- 明示限制：「指纹相同不代表根因相同；分布变化只作为诊断线索」；
- 分析不可用时返回 `status=NOT_AVAILABLE` + 原因，不伪造空结果。

## 7. API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/version-comparison/runs` | 列出可比较运行及版本描述（`limit` 1..100） |
| POST | `/api/version-comparison/runs` | 创建对比：`{baseline_run_id, candidate_run_id, max_cases?}` |
| GET | `/api/version-comparison/reports` | 历史对比报告（磁盘持久化） |
| GET | `/api/version-comparison/reports/{comparison_id}` | 完整报告 |
| GET | `/api/version-comparison/reports/{id}/metrics` | 指标/维度/失败数差异 |
| GET | `/api/version-comparison/reports/{id}/cases` | Case Diff，可按 `transition` 筛选 |
| GET | `/api/version-comparison/reports/{id}/failure-patterns` | 失败模式分布变化 |
| GET | `/api/version-comparison/reports/{id}/traces` | 逐条校验两侧 Trace 可读性（AVAILABLE / NOT_AVAILABLE） |

错误：非法运行/相同运行/max_cases 越界 → 400；报告不存在或 ID 非法（含路径穿越）→ 404；
请求体校验失败 → 422。报告落盘 `data/version_comparison/reports/cmp-*.json`（gitignored）。

## 8. 前端 `/version-compare`

- 顶部版本选择（基线 → 候选）+ 「开始对比」「运行回归检测」；
- 可比性横幅（问题码列表、跳过检查、不可比红条警告）；
- 双版本元数据卡（agent_version 缺失原因、Rubric 不适用、数据指纹、工具配置、执行模式）；
- 指标差异卡（分子/分母、pp、相对变化、UNKNOWN 说明）；
- 维度通过率差异表；
- Case-level Diff 表 + 迁移筛选（全部/5 类迁移）+ 缺失案例 + 基线/候选 Trace、DAG 跳转；
- 失败模式分布变化表；
- 回归检测区（风险矩阵、规则明细、证据引用、限制说明），详见 `REGRESSION_DETECTION.md`。

## 9. 实测（真实受控评测，`tracepilot-fault-v1`，8 案例）

| 场景 | 结果 |
|------|------|
| 同版本两次独立运行 | `verdict=COMPARABLE`、`issue_count=0`、`skipped_checks=[RUBRIC_CHECK_SKIPPED]` |
| Case Diff | `common=8 / stable_fail=6 / unknown_involved=2 / improved=0 / regressed=0`，缺失 0 |
| 指标 | `task_success_rate 0.0→0.0`、`tool_correctness_rate 0.375→0.375`、`safety_pass_rate 1.0→1.0`、`recovery_rate null（分母 0）`、`total_tokens 0.0→0.0`；耗时为标量指标（真实抖动，不参与通过率） |
| 失败模式 | 两侧各 6 个模式（bad_cases=6），新增/消失均 0 |
| 跨数据集（fault vs eval-v1） | `NOT_COMPARABLE`：`DATASET_ID_MISMATCH`、`DATASET_FINGERPRINT_MISMATCH`、`CASE_COUNT_MISMATCH`、`CASE_SET_MISMATCH`；`common=0`，仅 A 8 / 仅 B 13 |
| 对比耗时 | 首次约 3.0s（含失败模式分析），热态约 0.5s |
| 付费 API | 0 次 |

## 10. 限制与诚实性

- 评测运行记录**仅内存**（重启后不可比），可比较的仅是当前进程内保留的运行；
- 耗时/成本受机器负载影响，属于真实波动，不能当作质量退化证据（第 50 步用 `min_absolute` 抑制噪声）；
- 跨数据集/跨 Rubric/跨执行模式一律 `NOT_COMPARABLE`，共同案例子集仅供诊断；
- `agent_version` 缺失来自受控执行器本身，报告显式声明而非编造；
- UNKNOWN 既不当作通过也不当作失败；缺失指标差异为 `null` 而非 0；
- 版本元数据全部来自真实运行记录与数据集文件哈希，前端选择不产生元数据。
