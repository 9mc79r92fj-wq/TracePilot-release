# REGRESSION_DETECTION · 回归检测（第 50 步）

> 输入一次「版本对比报告」（第 49 步），输出**规则命中 → 风险等级 → 质量门禁预判**。
> 全部为确定性规则运算，不输出模型生成的「回归概率」。

## 1. 模块与入口

| 类型 | 位置 |
|------|------|
| 引擎 | `backend/app/services/regression_detector.py` |
| API | `/api/regression/*`（9 个端点） |
| 前端 | `/version-compare` 页面「Regression Detection」区（风险矩阵 + 规则明细） |
| 测试 | `tests/test_regression_detection.py`（17 项） |

## 2. 规则模型

`DetectionRule` 字段：`rule_id`、`metric_name`、`direction`（`drop` / `rise` / `count`）、
`threshold`、`threshold_unit`（`percentage_point` / `ratio` / `count` / `absolute`）、
`severity`、`blocking`、`rationale`、`enabled`、`min_absolute`（噪声抑制）、`source`。

默认 8 条规则（阈值来自 `DEFAULTS_LABEL = PROJECT_DEFAULT_NOT_INDUSTRY_STANDARD`，
**项目默认演示配置，不代表行业统一标准**，可按请求覆盖）：

| rule_id | 指标 / 对象 | 方向 | 默认阈值 | 严重度 | 阻断 | 理由 |
|---------|-------------|------|----------|--------|------|------|
| `NEW_SAFETY_FAILURE` | `safety_pass_rate` | drop | 0 pp | CRITICAL | 是 | 安全通过率只要真实下降就阻断 |
| `TASK_SUCCESS_DROP` | `task_success_rate` | drop | 5 pp | HIGH | 是 | 成功率下降 ≥5pp 视为显著退化 |
| `TOOL_CORRECTNESS_DROP` | `tool_correctness_rate` | drop | 10 pp | MEDIUM | 否 | 工具层退化提示 |
| `RECOVERY_RATE_DROP` | `recovery_rate` | drop | 10 pp | MEDIUM | 否 | 恢复能力退化提示 |
| `LATENCY_INCREASE` | `average_duration_ms` | rise | 25%（相对） | LOW | 否 | 平均耗时上升 ≥25%，且绝对增量 ≥5ms |
| `COST_INCREASE` | `total_tokens` | rise | 25%（相对） | MEDIUM | 否 | Token 成本上升 ≥25%，且绝对增量 ≥1 |
| `CASE_REGRESSION` | 案例级 PASS→FAIL | count | 1 例 | HIGH | 是 | 存在案例回归即阻断 |
| `NEW_FAILURE_PATTERN` | 失败模式指纹 | count | 1 个 | MEDIUM | 否 | 出现新失败模式 |

## 3. 规则求值语义

每条规则产生一个事件，状态为 5 类之一：

| status | 含义 |
|--------|------|
| `TRIGGERED` | 满足方向且超过阈值 |
| `NOT_TRIGGERED` | 可求值且未超阈值 |
| `NOT_EVALUABLE` | 证据缺失（指标为 null / 对比报告缺该指标），**不放行** |
| `NOT_APPLICABLE` | 该维度不适用（如两侧恢复率分母均为 0），允许跳过 |
| `DISABLED` | 调用方显式禁用该规则（报告中如实列出，门禁不受影响但会注明） |

重要细节（均由测试锁定）：

- **严格方向**：`threshold=0` 时「相等」不触发（避免 `NEW_SAFETY_FAILURE` 在无变化时误报）；
- **噪声抑制**：相对变化型规则（耗时/成本）同时要求绝对增量 ≥ `min_absolute`，
  例如 +30% 但仅 +3ms 的耗时波动不触发；
- **基线为 0**：相对变化无定义时按绝对变化判定，并在 `reason` 中明示「相对变化无定义」；
- **禁用 ≠ 缺失**：显式禁用记为 `DISABLED`，不计入 `NOT_EVALUABLE`；若**全部规则**被禁用
  → 风险 `UNKNOWN`、门禁 `INCONCLUSIVE`，不因「用户关掉了检查」而给 PASS；
- 案例级规则只统计 `PASS_TO_FAIL`；失败模式规则只统计新增指纹。

## 4. 风险等级

| risk_level | 条件 |
|-----------|------|
| `CRITICAL` / `HIGH` / `MEDIUM` / `LOW` | 取**已触发规则中的最高严重度**；无命中时 LOW |
| `UNKNOWN` | 版本不可比 / 全部规则被禁用 / 所有规则均无法求值 |

`risk_reason` 说明命中规则或无法判定的原因；**不含概率、置信度或模型生成内容**。

## 5. 质量门禁

| gate_status | 条件（按顺序判定） |
|-------------|-------------------|
| `INCONCLUSIVE` | 版本不可比（存在 CRITICAL 可比性问题）或风险为 UNKNOWN |
| `BLOCK` | 命中任一 `blocking=true` 规则 |
| `WARN` | 命中非阻断规则，**或**存在 `NOT_EVALUABLE` 规则（证据缺失不放行） |
| `PASS` | 无命中且证据完整（允许 `NOT_APPLICABLE` 跳过与显式 `DISABLED`，并在 `gate_reason` 中注明） |

`gate_reason` 明确写出 `INCONCLUSIVE 不等同于 PASS`。

## 6. 报告结构

```json
{
  "regression_report_id": "reg-<uuid>",
  "comparison_id": "cmp-<uuid>",
  "risk_level": "HIGH",
  "risk_reason": "最高严重度规则命中：TASK_SUCCESS_DROP（HIGH）",
  "gate_status": "BLOCK",
  "gate_reason": "触发阻断规则：TASK_SUCCESS_DROP",
  "triggered_rules": [{"rule_id": "...", "observed": {"change_pp": -37.5}, ...}],
  "not_evaluable_rules": [{"rule_id": "...", "reason": "..."}],
  "not_applicable_rules": [],
  "disabled_rules": [],
  "regressed_cases": [{"case_id": "...", "trace_id_baseline": "...", "trace_id_candidate": "..."}],
  "rule_events": [{"rule_id": "...", "status": "TRIGGERED", "evidence_refs": [...]}],
  "metric_deltas": {"task_success_rate": {"numerators_denominators": {...}}},
  "failure_pattern_changes": {"new_patterns": [], "resolved_patterns": [], "rows": []},
  "evidence_refs": [{"kind": "comparison_report", "ref_id": "cmp-..."}],
  "limitations": ["...", "INCONCLUSIVE 表示无法判定，绝不视为 PASS"],
  "threshold_source": "PROJECT_DEFAULT_NOT_INDUSTRY_STANDARD",
  "paid_api_called": false
}
```

报告落盘 `data/regression/reports/reg-*.json`（gitignored）。

## 7. API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/regression/rules` | 默认规则 + 阈值来源 + 门禁说明 + 规则状态枚举 |
| POST | `/api/regression/runs` | 创建检测：`comparison_id` 或 `(baseline_run_id, candidate_run_id)`，可选 `rules` 覆盖 |
| GET | `/api/regression/reports` | 历史报告列表 |
| GET | `/api/regression/reports/{id}` | 完整报告 |
| GET | `/api/regression/reports/{id}/rules` | 触发规则 + 全部规则事件 + 不可求值/不适用/禁用 |
| GET | `/api/regression/reports/{id}/cases` | PASS→FAIL 案例 + 指标差异 |
| GET | `/api/regression/reports/{id}/risk` | 风险等级与触发规则 |
| GET | `/api/regression/reports/{id}/gate` | 门禁状态与说明 |
| GET | `/api/regression/reports/{id}/trace` | 回归案例两侧 Trace 可读性 |
| GET | `/api/regression/reports/{id}/evidence` | 规则 → 观测值 → 证据引用 |

规则覆盖示例：`{"rule_id":"TASK_SUCCESS_DROP","threshold":20,"enabled":true,"severity":"HIGH","blocking":true}`。
错误：未知规则/非法阈值/缺少运行 → 400；报告不存在 → 404；阈值 <0 等请求体校验 → 422。

## 8. 前端风险矩阵

`/version-compare` 的「Regression Detection」区：

- 风险横幅：风险等级徽标 + 门禁徽标 + 两条判定理由 + 「INCONCLUSIVE 绝不视为 PASS」声明；
- **回归风险矩阵**：行 = 风险维度（质量 / 安全 / 性能 / 成本 / 案例 / 失败模式），
  列 = 严重度（LOW / MEDIUM / HIGH / CRITICAL），单元格渲染对应规则及其状态，
  颜色区分 已触发 / 未触发 / 无法求值 / 不适用或禁用；
- 规则求值明细表（规则 / 指标 / 阈值 / 严重度 / 阻断 / 状态 / 判定说明 + 观测值）；
- 触发规则、无法求值、不适用/已禁用三栏明细 + 证据引用列表。

## 9. 实测

### 真实运行（同版本两次独立运行，禁用耗时/成本规则以隔离计时噪声）

| 指标 | 值 |
|------|-----|
| 可比性 | `COMPARABLE` |
| 风险 / 门禁 | `LOW` / `PASS` |
| 触发规则 | 0 |
| 不适用 | `RECOVERY_RATE_DROP`（恢复率分母为 0） |
| 禁用 | `LATENCY_INCREASE`、`COST_INCREASE`（如实列出） |
| 付费 API | 0 次 |

### 真实运行（跨数据集）

`risk_level=UNKNOWN`、`gate_status=INCONCLUSIVE`、`risk_reason` 以「版本不可比」开头，
`limitations` 列出 CRITICAL 可比性问题码。

### 受控场景（测试注入内存 Store 的合成报告，用于稳定覆盖阈值语义）

| 场景 | 结果 |
|------|------|
| 全部指标无变化 | `LOW / PASS`，8 条规则均 `NOT_TRIGGERED` |
| 成功率 7/8 → 4/8（-37.5pp） | `TASK_SUCCESS_DROP` 命中 → `HIGH / BLOCK` |
| 安全通过率 8/8 → 7/8（-12.5pp） | `NEW_SAFETY_FAILURE` 命中 → `CRITICAL / BLOCK` |
| 工具正确率 8/8 → 6/8（-25pp） | `TOOL_CORRECTNESS_DROP` 命中 → `MEDIUM / WARN` |
| 耗时 100→200ms、Token 1000→1400 | 两条规则命中 → `MEDIUM / WARN` |
| 耗时 10→13ms（+30%，但 +3ms < 5ms） | 噪声抑制 → `NOT_TRIGGERED`，`PASS` |
| 恢复率 value 为 null（分母 1） | `NOT_EVALUABLE` → 门禁 `WARN`（不放行） |
| 缺少耗时/成本指标 | 两条规则 `NOT_EVALUABLE` → `WARN` |
| 聚合指标改善但 1 例 PASS→FAIL | `CASE_REGRESSION` 命中 → `HIGH / BLOCK`（聚合改善不能掩盖单案例回归） |
| 阈值改为 20pp 后同一下降 | `NOT_TRIGGERED` → `LOW / PASS`（阈值可配置、有报告留痕） |
| 全部规则禁用 | `UNKNOWN / INCONCLUSIVE` |
| 新失败模式指纹 | `NEW_FAILURE_PATTERN` 命中 → `MEDIUM / WARN`，证据引用指向指纹 |

## 10. 限制与诚实性

- 阈值是**项目默认值**，不是行业标准；不同项目应重新标定并保留 `threshold_source`；
- 风险等级由确定性规则得出，不预测真实用户影响，也不输出概率；
- `INCONCLUSIVE` 表示无法判定，**永远不等于 PASS**；`WARN` 表示存在未验证风险；
- 规则只覆盖已知指标/案例/指纹维度，未覆盖的退化类型不会被发现（归入未来规则）；
- 依赖评测运行记录（内存保留 20 条），重启后历史运行不可查询；
- 真实耗时/成本受环境波动影响，已用 `min_absolute` 抑制，但仍建议在稳定环境执行门禁。
