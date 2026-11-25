# Judge Calibration 评测器校准

- 对应代码：`backend/app/services/judge_calibration.py`
- 校准数据集：`data/evaluation/calibration/calibration_set_v1.json`（22 案例）
- API：`GET /api/evaluation/calibration/datasets`、`POST /api/evaluation/calibration/runs`
- 前端：`/calibration`（Evaluation Center 内有入口）
- 校准引擎版本：`1.0.0`｜状态：已实现，**Mock 路径已验证（23+12 项测试）**；Real Judge 未验证

---

## 1. 目标

用人工参考标注检验 LLM Judge 的**评分一致性**与**稳定性**，量化"Judge 到底有多可信"，而不是仅凭主观宣称。

## 2. 参考标注来源（关键声明）

校准集 `annotation_source = synthetic_reference`，含义：

- `human_score` 由开发者依据 Rubric 规则**人工给定**；
- 但**不是**独立第三方标注，也**不是**双盲多人标注流程产物；
- 因此本校准得到的一致率**不得对外宣称等同于真实人工标注一致率**。

若将来引入真实标注，将 `annotation_source` 改为 `human_double_blind` 并重跑即可。**禁止**把程序生成的分数写入 `human_score` 字段冒充人工标注（模块不提供任何自动生成参考分的路径）。

## 3. 校准数据集

`data/evaluation/calibration/calibration_set_v1.json`，**22 条案例**（19 条 SCORED + 3 条 UNKNOWN）。

| 类别 | 案例数 | 说明 |
|------|--------|------|
| correct_answer | 2 | 完全正确回答 |
| partially_correct | 3 | 部分完成 |
| factual_error | 3 | 数值错误 / 自相矛盾 |
| missing_information | 2 | 遗漏关键要求 |
| instruction_violation | 3 | 违反格式/禁止工具约束 |
| unreasonable_tool_use | 3 | 工具选错 / 参数错 / 结果被无视 |
| insufficient_evidence | 3 | 无工具证据 / 空答案 / 无材料 → **UNKNOWN** |
| ambiguous_boundary | 3 | 极简、冗余、浮点取整等边界 |

每条案例字段：`case_id`、`category`、`task`、`candidate_answer`、`reference_material`、`rubric_id`、`human_score`、`human_status`、`annotation_rationale`、`annotation_source`。

跨 Rubric 覆盖：`answer_quality`（13）、`tool_usage`（3）、`instruction_following`（3）。

## 4. 案例 → Trace 映射

Judge 的输入是 `TraceRecord`，因此校准模块把案例映射为最小 Trace：

| 案例字段 | 映射为 |
|---------|--------|
| `task` | planning 步骤的 `input.task` |
| `reference_material.tool_name/arguments/result` | tool_call 步骤的对应字段 |
| `candidate_answer` | 最后一个 model_call 的 `output` |
| `reference_material.notes` | planning 步骤 `output.notes`（**不伪装成工具返回**） |

**不编造**：案例未提供 `tool_name` 时不生成任何 tool_call 步骤（已测试断言）。

## 5. 指标定义

| 指标 | 定义 | 分子/分母 |
|------|------|----------|
| **Exact Agreement** | 归一化到 1~5 整级后 Judge 总分 == 参考分 | 一致案例 / 可评估案例 |
| **MAE** | \|Judge 总分 − 参考分\| 的均值（未归一化，保留连续值） | — |
| **Within-1 Agreement** | \|Judge 总分 − 参考分\| ≤ 1 | 满足案例 / 可评估案例 |
| **Confusion Matrix** | 行 = 参考分（1–5），列 = Judge 归一化分（1–5） | 各格计数 |
| **偏差分析** | 高估（Δ≥1）/ 低估（Δ≤−1）/ 一致（\|Δ\|<1） | 三者和==可评估案例数 |

**可评估分母**：参考标注与 Judge 均为 SCORED 的案例数。参考标注为 UNKNOWN 的案例**不进入分母**（已测试断言）。Judge 给出 UNKNOWN 的案例按缺失处理，不臆造分值。

归一化规则：`clamp(round(judge_overall), 1, 5)`。Judge 总分本身是 Rubric 加权平均（连续值），归一化是为了可比性，属有损映射，已在限制中说明。

## 6. 稳定性分析

同一案例重复评分 `repeats` 次（1–5），记录：

- `per_case_scores`：每案例的逐次分值
- `per_case_std`：**同一案例内**逐次分值的总体标准差
- `mean_within_case_std` / `max_within_case_std`：逐案例标准差均值/最大值（越接近 0 越稳定）
- `exact_stable`：逐次分值完全相同
- `normalized_stable` / `consistent`：归一化到 1~5 整级后一致

> 修复记录：初版误将"跨案例均值标准差"当作稳定性，已修正为"同一案例内重复评分标准差"，并新增 `test_stability_with_unstable_provider` 用不稳定 Provider 验证指标确实能反映波动。

## 7. Mock / Real 严格隔离

| 项 | Mock | Real |
|----|------|------|
| 默认 | ✅（`judge_mode="mock"`） | ❌ |
| 成本 | 0（`total_tokens=0`） | 按量付费 |
| 确定性 | 完全确定（std=0） | 受采样影响 |
| 标记 | `is_mock=true`、`simulated=true`、`provider_kind="mock"` | `is_mock=false` |
| API | 200 | 未授权 → **403** |

所有报告都带 `disclaimer`："Mock Judge 结果为规则化模拟，不得冒充真实模型校准结果"，并有测试断言该字符串存在。

## 8. API 契约

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/evaluation/calibration/datasets` | 校准集列表（含 `annotation_source` / `annotation_note` / 类别） |
| POST | `/api/evaluation/calibration/runs` | `{dataset_id, judge_mode, max_cases?, repeats?}` → 完整校准报告 |

**安全**：仅接受白名单字符的 `dataset_id`（防路径穿越，非法 → 404）；`max_cases ≤ 50`、`repeats ≤ 5`（超限 422）；真实 Judge 未授权 → 403 且不产生任何付费调用。

报告结构：

```
calibration_engine_version
dataset{calibration_id, version, rubric_id, annotation_source, annotation_note, total_cases, evaluated_cases}
judge{provider_kind, model_name, is_mock, simulated, rubric_id, rubric_version, prompt_version,
      judge_engine_version, repeats}
metrics{evaluable_cases, exact_agreement, exact_agreement_count, mae,
        within_1_agreement, within_1_agreement_count, confusion_matrix, denominator_definition}
bias{overestimated[], underestimated[], aligned[]}
stability{repeats, per_case_scores, per_case_std, mean_within_case_std, max_within_case_std,
          exact_stable, normalized_stable, consistent, note}
differences[]  cases[]{...evidence, dimension_scores, repeat_scores, unknown_dimensions, limitations}
unknown{reference_unknown_cases[], judge_unknown_cases[], execution_errors[], note}
disclaimer  limitations[]
```

## 9. 前端页面（`/calibration`）

- **配置区**：校准集选择（显示版本/案例数/标注来源徽标）、Judge 模式、重复次数、运行按钮
- **指标卡**：Exact Agreement / MAE / Within-1 / 稳定性（均附分子分母）
- **版本信息**：Mock/Real 徽标、provider、模型、Rubric/Prompt/Judge Engine/校准引擎/数据集版本、分母口径、付费调用标记
- **Confusion Matrix**：5×5 热力图（绿色对角=一致，红色=偏离），含 hover 提示与图例说明
- **差异分析**：高估 / 低估 / 全部差异 / UNKNOWN 四个 Tab；表格含案例、类别、参考分、Judge 分、Δ、标注理由
- **证据抽屉**：逐案例的标注理由、重复评分、UNKNOWN 维度、逐维度得分、**Judge 引用证据原文**
- **限制与声明**：醒目提示 disclaimer 与 limitations 列表
- Evaluation Center 顶部新增「Judge Calibration」入口卡片

## 10. 已知限制

1. **Real Judge 未验证**：本校准仅覆盖 Mock Judge；真实模型的一致性/稳定性数据为空。
2. **Mock Judge 校准结果较差**（MAE 1.40、Exact 15.8%、11 高估 vs 1 低估）——这是**真实发现**，说明规则化 Mock 不适宜作为质量代理，仅用于验证功能链路；未对 Mock 做"拟合校准集"的调参（那等价于伪造质量）。
3. **样本量小**（19 条可评估）→ 指标存在抽样波动，不能代表总体性能。
4. **参考标注非独立双盲**（`synthetic_reference`）。
5. **归一化有损**：连续加权平均 → 1~5 整级，边界案例可能因取整改变一致性判定。
6. 未实现 Judge 与确定性验证器的分歧率交叉统计、也未实现多标注者间一致性（Cohen's κ / Krippendorff's α）。

## 11. 与最小修复 / 反事实实验的关系

校准结果回答"Judge 是否可信"，从而决定 Judge 能否作为**反事实实验结果质量**与**最小修复建议合理性**的辅助评估信号：

- 若未来真实 Judge 校准显示 `Within-1 ≥ 0.8` 且稳定性 std < 0.5，可作为修复建议排序的**辅助**权重；
- 无论校准结果多好，Judge 分数**始终不得覆盖**确定性验证器结论；
- 校准集可随修复场景扩充（新增 `reference_material` 为修复前后的对比证据）。
