# Multi-Judge Arbitration 多评测器仲裁

- 对应代码：`backend/app/services/multi_judge.py`
- API：`/api/evaluation/multi-judge/configs`、`/runs`、`/runs/{id}`、`/scores`、`/disagreement`、`/arbitration`
- 前端：`/multi-judge`（Disagreement Explorer），Evaluation Center 顶部有入口
- 引擎版本：`1.0.0`｜状态：已实现，29 项模块测试 + 20 项 API 测试通过（全 Mock）

---

## 1. 目标

单一 Judge 的评分无法自证可靠。多 Judge 仲裁通过**独立评分 → 分歧检测 → 聚合仲裁**回答两个问题：

1. 不同评审器对该案例的判断**是否一致**（一致才可提高可信度）
2. 不一致时**是否应该拒绝给结论**（而不是强行平均）

## 2. 三种 Judge 关系的区分（核心诚实性）

| 关系 | 判定 | 独立性等级 | 含义 |
|------|------|-----------|------|
| 不同模型 Judge | `(provider_kind, model_name)` 不同 | `independent_models` | 真正的模型级独立（需真实模型） |
| 同模型不同 Prompt | 模型相同、`prompt_version` 不同 | `prompt_variants_only` | 分歧来自规则差异 |
| 同模型重复采样 | 模型与 Prompt 均相同 | `correlated` | 方差无信息量 |

### Mock 变体的特殊规则（关键）

多个 Mock 变体**不是**独立真实模型。系统强制：

- 所有 `mode=mock` 的 Judge 身份**归一到** `(mock, mock-rule-based-judge)`，忽略 variant 后缀；
- 因此 Mock 组合最高只能得到 **`mock_prompt_variants`**，**永不**判定为 `independent_models`；
- 同时输出 `not_independent: true`、`all_mock: true` 与 `disclaimer`（"不得作为真实模型一致性证据"）。

> 修复记录：初版因 variant 后缀被误判为 `independent_models`，已修正并加测试 `test_independence_mock_variants_not_independent` 锁定。

### Mock 变体必须真的不同

为避免"同一规则复制三份"，`MockJudgeProvider` 支持三个**规则不同**的变体：

| variant | 规则差异 | prompt_version |
|---------|---------|----------------|
| `base` | 原始规则 | `judge-prompt-v1` |
| `strict` | 证据 < 2 条 → UNKNOWN；SCORED 分数 −1（下限 1） | `judge-prompt-v1-strict` |
| `lenient` | SCORED 分数 +1（上限 5） | `judge-prompt-v1-lenient` |

测试断言三者在同一 Trace 上给出**互不相同的分数**，且 `strict < base < lenient`。

## 3. 预设配置组

| config_id | Judge 数 | 预期关系 | 限制说明 |
|-----------|---------|---------|---------|
| `mock-mixed-variants` | 3（base/strict/lenient） | prompt_variant | 共享同一规则基数，非独立真实模型 |
| `mock-resampling` | 3（同变体 ×3） | resampling | 确定性评审下方差恒为 0，仅演示 correlated 识别 |
| `mock-dual` | 2（base/strict） | prompt_variant | 快速演示两评审器分歧 |

每组都强制声明 `limitation` 字段（测试校验）。

## 4. 分歧检测指标

逐案例计算（**仅基于 SCORED 评分**）：

| 指标 | 说明 |
|------|------|
| `max_diff` | 最大分差（1~5 分制，最大值 4） |
| `stdev` / `variance` | 总体标准差 / 方差 |
| `mean` / `median` | 均值 / 中位数 |
| `agreement` | `1 − max_diff/4`（1 表示完全一致） |
| `distribution` | 归一化到 1~5 整级后的评分分布 |
| `disagreement_flagged` | `max_diff ≥ threshold`（默认 2） |
| `severe` | `max_diff ≥ severe_threshold`（默认 3） |
| `evidence_conflicts` | 同一维度下各 Judge 引用证据**完全不重叠**时记录冲突 |
| `reason_summary` | 各 Judge 评分理由摘要（前 200 字符） |
| `human_review_required` | 分歧 / 严重 / 有效评分不足 / 存在 UNKNOWN |
| `sufficient_for_conclusion` | `SCORED ≥ 2` |

**缺失评分不按 0 处理**：UNKNOWN Judge 只记入 `unknown_judges`，不参与任何统计量计算（测试 `test_unknown_not_treated_as_zero` 断言分布中不存在 0 分档）。

## 5. 仲裁策略

| 策略 | 计算 | 前置条件 |
|------|------|---------|
| `median` | 可用评分的中位数 | 至少 2 个 SCORED |
| `mean` | 可用评分的算术平均 | 至少 2 个 SCORED |
| `calibration_weighted` | 加权平均（按 judge_id 权重） | **必须有真实校准依据** |

### 加权聚合的硬约束

`calibration` 必须提供 `{weights: {judge_id: float}, source: str}`：

- 缺少 `weights` → `MissingCalibrationError`
- 权重为空或全 0 → `MissingCalibrationError`
- 缺少 `source`（无法追溯依据）→ `MissingCalibrationError`

**没有有效校准数据时绝不编造可靠性权重。** 报告中原样回显 `calibration.source` / `weights` / `evidence` 供审计。

### INCONCLUSIVE 规则

以下任一情况 → `status = INCONCLUSIVE`，`aggregated_score = null`：

1. 有效评分不足（`SCORED < 2`）
2. 严重分歧（`max_diff ≥ severe_threshold`）
3. 加权聚合时没有任何 Judge 具备校准权重

**不强行输出确定性结论**——这是诚实性要求，不是失败。

## 6. 独立错误处理

单个 Judge 抛异常时：

- 该 Judge 记录 `status="ERROR"`、`score=null`、`error` 文本；
- 其他 Judge 结果正常保留；
- 全局 `errors[]` 记录 `{case_id, judge_id, error}`；
- 仲裁时该 Judge 视同未参与（不伪造分数）。

测试 `test_judge_error_isolation` 注入必然失败的 Provider 验证：失败 Judge 无分数、有效分不足 → INCONCLUSIVE。

## 7. API 契约

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/evaluation/multi-judge/configs` | 预设配置组（含 limitation） |
| POST | `/api/evaluation/multi-judge/runs` | 创建异步运行（默认 Mock） |
| GET | `/api/evaluation/multi-judge/runs` | 运行列表 |
| GET | `/api/evaluation/multi-judge/runs/{id}` | 状态 + 摘要 + 独立性 |
| GET | `/api/evaluation/multi-judge/runs/{id}/scores` | 各 Judge 独立评分（含证据/理由/版本） |
| GET | `/api/evaluation/multi-judge/runs/{id}/disagreement` | 分歧报告 |
| GET | `/api/evaluation/multi-judge/runs/{id}/arbitration` | 仲裁结果 |

**安全**：
- 真实 Judge 预检：`mode=deepseek` 在**创建阶段**即校验授权，未授权 → **403**，绝不进入队列；
- 数据集白名单（非白名单 404）、配置组白名单（未知 404）；
- 案例 ≤ 30、Judge ≤ 5（超限 422）；
- 并发 1、超时 120s、内存保留 20 条；
- 响应不含任何 API Key（测试断言）。

## 8. 前端 Disagreement Explorer（`/multi-judge`）

| 区块 | 内容 |
|------|------|
| 配置区 | 配置组选择、仲裁策略、案例数、运行按钮、组说明与 `limitation` 醒目提示 |
| **独立性声明** | 等级徽标（independent_models / prompt_variants_only / mock_prompt_variants / correlated）+ 不构成独立评测标记 + 全部 Mock 标记 + disclaimer |
| 指标卡 | 分歧案例数、可得出结论数、INCONCLUSIVE 数、需人工复核数 |
| Judge 身份表 | 每个 Judge 的模式、变体、模型、Prompt 版本、MOCK/REAL 徽标 |
| 案例选择 | chip 列表，严重分歧标 ⚠、分歧标 ! |
| **并列 Judge 卡片** | 每 Judge 一卡：分数徽标、模型/Prompt、UNKNOWN 维度、逐维度得分表、评分理由、**引用证据**（按维度列出） |
| 分歧指标 | 最大分差、标准差、方差、一致性、均值/中位数、有效评分数、评分分布条、UNKNOWN Judge、**证据冲突**（按维度展开双方证据）、理由差异摘要 |
| 仲裁结果 | 状态徽标、聚合分（INCONCLUSIVE 显示"不输出确定性结论"）、需人工复核标记、参与聚合的评分、未参与的 Judge、人工复核建议、说明 |
| 评测限制 | limitations 列表 |

## 9. 已知限制

1. **全部预设配置为 Mock**，无法提供真实模型间分歧证据；`independent_models` 等级仅在真实模式下可达。
2. Mock 变体差异是**人为规则调整**，不是真实 Prompt 的效果差异。
3. `mock-resampling` 场景在确定性 Mock 下方差恒为 0，仅用于验证 correlated 识别逻辑。
4. `calibration_weighted` 的策略实现已就绪，但**当前没有真实校准权重数据**，API 层未开放传入自定义校准（避免任意权重注入）；如需使用须通过代码/服务层调用并提供可追溯 `source`。
5. 未实现多轮仲裁（如分歧后追加 Judge）与主动学习式评审器选择。
6. 未实现 Judge 与确定性验证器的交叉分歧率（技术债 P3-8 仍开放）。
7. 运行结果仅驻留内存，进程重启丢失。
