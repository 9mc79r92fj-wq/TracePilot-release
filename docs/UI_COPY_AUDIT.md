# 前端用户可见文案审计（UI Copy Audit）

- 审计对象：`frontend/src/pages/*.tsx`（20 个已挂载路由页面 + 1 个未挂载占位页）与 `frontend/src/components/*.tsx`（10 个组件）。
- 审计范围：**仅统计最终渲染到界面的文案**（JSX 文本、`title`/`placeholder`/`aria-label`、常量表里的可见标签、`EmptyState` 文案、`<option>` 文本）。代码注释中的「第 53 步」「第 55-A 步治理接口」「第 49/50 步」等开发阶段编号**不可见**，本次不作为问题条目，仅在文末说明。
- 判定口径：
  - **类型1**：开发阶段说明 / 步骤号 / 内部验收 / 内部治理口径（如「只读派生」「口径」「不自动升级」「为什么可用 / 不可用」）。
  - **类型2**：重复解释实现过程、冗长括号解释、模板化提示、教学式段落、技术名词堆砌。
  - **类型3**：明显 AI 生成式 / 汇报式文案（如「属诚实的不确定」「绝不…」）。
  - **类型4**：与功能不符的宣传语、占位内容、原始字段名 / 枚举 / 英文占位符直出、无意义提示。
  - **类型5**：多行、信息密度低的说明段落（实际渲染 2 行及以上；超过 2 行者为本类重点）。
- 行号来自对当前工作区文件的逐行读取，未做任何源码修改、未执行 git 操作。

## 汇总

| 指标 | 数值 |
| --- | --- |
| 审计条目数 | **114** |
| 有条目的文件数 | **29**（21 个 pages + 8 个 components） |
| 已通读但无问题的文件 | 2（`components/TraceFlowNode.tsx`、`components/ui.tsx`） |
| 类型1（开发阶段 / 内部口径 / 步骤号） | 25 |
| 类型2（冗长解释 / 括号解释 / 模板化 / 技术名词堆砌） | 24 |
| 类型3（AI / 汇报语气） | 2 |
| 类型4（占位 / 原始字段与枚举直出 / 表述与功能不符） | 25 |
| 类型5（多行低信息密度说明，2 行及以上） | 39 |

> 说明：分类可重叠，`NotImplemented.tsx` 1 条同时计入类型1与类型4；类型5 统计口径为“实际渲染 2 行及以上的可压缩说明”。

### 全局性问题（优先统一处理）

1. **中英混排不统一**：页面标题全英文（Overview / Fault Lab / Evaluation Center…），而侧栏又是中文（「轨迹浏览」「Judge校准」「Pareto分析」）；徽标、指标名、标签中英重复（「决策解释（Evidence-based Explainability）」「证据血缘（Evidence Lineage）」）。
2. **原始字段名 / 枚举直出**：`Not Recorded`、`NOT_AVAILABLE`、`paid_api_called=false`、`prompt_injection`、`simulation/real` 开关值等直接显示给用户。
3. **「口径 / 不伪造 / 不自动升级 / 绝不」类内部治理语气**在 8 个页面反复出现，属于内部验收语言外溢。
4. **同一句免责声明在 4–5 处重复**（「模拟数据不代表真实模型性能」「成本为价格表估算，不是 Provider 实际账单」），建议收敛为统一的页脚一行。

---

## 按文件明细

### frontend/src/pages/Overview.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 65 | 面向长程 AI Agent 的执行轨迹分析、故障归因与最小修复验证平台 | 类型4（说明书式宣传语） | Agent 轨迹分析、故障归因与修复验证 |
| 81 | 主 Trace 目录 Trace | 类型4（词语重复、术语生硬） | 主目录 Trace 数 |
| 105–120 | Overview / Trace Explorer：只统计主 Trace 目录（Agent 运行记录），当前 N 条。Cost Analytics：统计全库（主 Trace 目录 + 评测 Trace 目录）… | 类型5（4 条列表、约 8 行） | 统计范围：Overview / Trace Explorer 只统计主 Trace 目录（当前 N 条）；Cost Analytics 统计全库（含评测 Trace，不重复计数）。 |
| 114 | 两个口径没有重复计入：同一 trace_id 只计一次；差异来自“评测 Trace 是否纳入”。 | 类型1（内部验收口径） | 同一 trace_id 不重复计数。 |
| 120 | （未标记 real_api_verified 的历史记录一律归入 UNKNOWN，不自动升级。） | 类型1（内部规则外溢） | 缺少 real_api_verified 标记的记录计为 UNKNOWN。 |
| 129 | 数据来源分类（全库，只读派生） | 类型1（「只读派生」） | 数据来源分类（全库） |
| 176–177 | 失败 Trace 并未丢失：它们保存在主 Trace 目录，可在 Trace Explorer 用「状态=失败 / 仅看故障」筛选；评测 Trace（SIMULATED）不在该列表内，需按 trace_id 下钻。 | 类型5（3 行辩解式解释） | 失败 Trace 保存在主 Trace 目录，可在 Trace Explorer 按「状态=失败」筛选。 |
| 219 | 暂无 Trace 数据，请先通过 CLI 生成 | 类型1（开发期指引） | 暂无 Trace 数据 |

### frontend/src/pages/TraceExplorer.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 85 | 筛选条件（服务端过滤，口径与 Overview 一致） | 类型1（实现细节 + 口径） | 筛选条件 |

### frontend/src/pages/TraceDetail.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 139 | 数据来源分类（只读派生，不修改 Trace） | 类型1（「只读派生」） | 数据来源分类 |
| 141 | 来源分类不可用（不影响 Trace 详情展示） | 类型2（冗长括号解释） | 来源分类暂不可用 |
| 163 | 受控实验资格（为什么可用 / 不可用） | 类型1（把验收问句写进标题） | 受控实验资格 |
| 244 | 决策解释（Evidence-based Explainability） | 类型2（中英重复） | 决策解释 |
| 246 | 解释不可用（仅基于已记录证据，不保存模型思维链） | 类型5 | 解释暂不可用：仅使用已记录证据 |
| 253 | 不是思维链，仅为证据化理由 | 类型2（否定式解释） | 证据化理由（非思维链） |

### frontend/src/pages/DagAnalysis.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 379 | 点击节点查看证据；依赖边 ≠ 已证明因果 | 类型2（符号替代文字） | 点击节点查看证据；依赖边不等同因果 |
| 383 | Fault Focus：高亮 {id} 及其下游依赖（下游依赖不代表故障实际传播） | 类型5（括号重复解释） | 聚焦 {id} 及下游依赖（不代表故障传播） |
| 395 | 请从 Trace Explorer 重新选择（不会静默回退到其他 Trace）。 | 类型1（内部行为说明） | 请从 Trace Explorer 重新选择。 |
| 441 | Trace Time Travel · 进度 {pct}% | 类型4（功能名与产品不符） | Trace 回放 · 进度 {pct}% |
| 504 | 注意：{n} 个步骤缺少可用时间戳，已排除在时间轴之外（未推断其顺序）。 | 类型2（冗长括号解释） | {n} 个步骤缺少时间戳，未纳入时间轴。 |

### frontend/src/pages/FaultLab.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 113 | Fault Lab · Failure Replay | 类型4（双重英文命名） | 故障重放 |
| 169 | 安全说明：默认使用模拟模型与白名单无副作用本地工具；不调用付费 API；不执行任意系统命令；不修改原始 Trace。 | 类型5（4 个分号句） | 默认使用模拟模型与白名单本地工具：不调用付费 API、不执行系统命令、不修改原始 Trace。 |

### frontend/src/pages/CounterfactualLab.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 256–257 | 在受控条件下对故障 Trace 施加单一干预并重跑，对比观察结果与干预结果。实验使用模拟模型与白名单本地工具，不调用付费 API；结果不写入原始 Trace。 | 类型5（2 行副标题） | 对故障 Trace 施加单一干预后重跑，对比观察结果。使用模拟模型与白名单工具，写入独立 Trace。 |
| 302 | 来自 DAG 节点上下文：{stepId}（反事实实验以整条 Trace 为单位，节点仅作定位） | 类型2（括号解释） | 来源节点：{stepId}（实验以整条 Trace 为单位） |
| 375–378 | 实验控制条件：固定白名单场景，无自由输入执行 / 模拟模型（无随机采样），工具仅限 calculator / text_analyzer / 单一干预变量，其余故障配置保持原始 / 有界重试（≤5）与有界重放（≤5） | 类型5（4 条列表，与配置区重复） | 固定白名单场景；模拟模型（calculator / text_analyzer）；单变量干预；重试与重放均 ≤5 次。 |
| 74 | Inconclusive / Repeated / Controlled / Exploratory（证据强度徽标） | 类型4（英文枚举直出） | 无结论 / 可复现 / 受控 / 探索性 |
| 90 | 单一变量受控干预，但为有限次重放且使用模拟模型 | 类型2（解释实现过程） | 单变量受控干预，有限次重放（模拟模型） |

### frontend/src/pages/RepairStudio.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 95–105 | verification_status / experiment_count / modification_scope / latency_cost / token_cost / tool_call_cost / risk_level / evidence_limitations（表格行标签原样输出） | 类型4（snake_case 字段名直出） | 验证状态 / 实验次数 / 修改范围 / 延迟成本 / Token 成本 / 工具调用成本 / 风险等级 / 证据限制 |
| 110 | Repair Confidence Breakdown | 类型2（英文标题无中文） | 修复候选评估 |
| 122 | 以上为受控模拟环境下的验证元数据，不是成功率，不代表真实生产结果。 | 类型5（双重免责） | 以上为模拟环境下的验证元数据，不代表生产结果。 |
| 277–278 | 基于故障 Trace 生成最小修复候选，并通过受控反事实实验验证有效性。原始 Trace 只读；验证在模拟模型与白名单本地工具下完成，不代表真实生产结果。 | 类型5（2 行副标题） | 生成最小修复候选，并用受控反事实实验验证。原始 Trace 只读，结果为模拟环境。 |
| 308–309 | 来自 DAG 节点上下文：{stepId}（修复候选以整条 Trace 为单位；可在候选详情中定位目标步骤） | 类型2（括号解释） | 来源节点：{stepId}（候选以整条 Trace 为单位） |

### frontend/src/pages/FaultAttribution.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 173–174 | 基于 DAG 可达性与基线对比，对故障节点的传播范围做分类归因。可达 ≠ 因果：potentially_affected 是推断范围，只有 observed_difference 才是观测到的实际差异。 | 类型5（字段名混排的长说明） | 基于 DAG 可达性与基线对比归因故障传播范围。可达不等同因果：potentially_affected 为推断范围，observed_difference 为观测差异。 |
| 218 | 提供基线可让「观测到差异」有对照依据；不提供时多数节点会归为 unknown，属诚实的不确定。 | 类型3（汇报式措辞） | 提供基线后「观测到差异」才有对照；不提供时多数节点为 unknown。 |
| 222–223 | 该 Trace 没有注入故障（fault_types 为空），Fault Attribution 需要故障 Trace：运行后多数节点会是 unaffected / unknown。请从 Trace Explorer 选择带故障的 Trace。 | 类型5（3 行警告） | 该 Trace 无注入故障，归因结果多为 unaffected / unknown。请选择带故障的 Trace。 |
| 229 | （在节点归因表中高亮；不会把可达性当作已证明因果） | 类型2（括号解释） | （已在归因表中高亮） |
| 350 | 结论口径说明 | 类型1（「口径」） | 结论说明 |

### frontend/src/pages/TraceComparison.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 147–148 | 对齐两份 Trace 的步骤，输出结构差异、最早差异点与影响推断。差异为相关性观察，不等同因果结论；因果需通过 Counterfactual Lab 的受控干预实验验证。 | 类型5（2 行副标题） | 对齐两份 Trace 的步骤，输出结构差异、最早差异点与影响推断。差异是相关性观察，不等同因果。 |
| 331 | 观察 / 推断 / 未验证（严格区分） | 类型1（内部验收要求） | 观察 / 推断 / 未验证 |

### frontend/src/pages/EvaluationCenter.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 295 | 对 Agent 执行轨迹做六维度确定性评测。全部案例为合成数据（synthetic），使用模拟模型与本地白名单工具，不调用付费 API。 | 类型5（2 行副标题） | 对 Agent 执行轨迹做六维度确定性评测。案例为合成数据，使用模拟模型与本地白名单工具，不调用付费 API。 |
| 302–303 | 用人工参考标注检验 LLM Judge 的评分一致性（Exact / MAE / Within-1 / 混淆矩阵 / 稳定性）。参考标注标记为 synthetic_reference。 | 类型5（2 行说明） | 用参考标注检验 LLM Judge 的评分一致性（Exact / MAE / Within-1 / 混淆矩阵 / 稳定性）；标注来源为 synthetic_reference。 |
| 314–315 | 多个 Judge 独立评分 → 分歧检测（最大分差/方差/分布/证据冲突）→ Median/Mean 仲裁。严重分歧返回 INCONCLUSIVE 并建议人工复核；全部为 Mock，零成本。 | 类型5（2 行说明） | 多个 Judge 独立评分后做分歧检测与 Median/Mean 仲裁；严重分歧返回 INCONCLUSIVE 并建议人工复核。全部为 Mock。 |
| 621–622 | 模型：{provider} · 模拟=true · 付费API=false | 类型4（布尔字段直出） | 模型：{provider} · 模拟：是 · 付费 API：否 |
| 545 | 失败维度列直接输出原始指标 key（如 task_success、tool_correctness） | 类型4（原始字段名直出） | 用维度中文名输出（`DIMS.find(d => d.key === k)?.label ?? k`） |

### frontend/src/pages/JudgeCalibration.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 182–183 | 用人工参考标注检验 LLM Judge 的评分一致性与稳定性。参考标注来源标注为 synthetic_reference（非独立第三方双盲标注），因此一致率不得对外宣称等同于真实人工标注一致率。 | 类型5（2 行、含内部宣称约束） | 用参考标注检验 LLM Judge 的一致性与稳定性；标注来源为 synthetic_reference（非独立双盲标注），一致率不等同真实人工标注。 |
| 205 | Real Judge（需显式授权，否则 403） | 类型4（HTTP 状态码外露） | Real Judge（需服务端授权） |
| 254 | Exact Agreement / Within-1 Agreement（指标卡标题） | 类型4（英文指标名） | 完全一致率 / 偏差 ≤1 分一致率 |
| 299 | 分母口径：{definition} | 类型1（「口径」） | 统计分母：{definition} |
| 347 | 绿色对角线为一致；红色为偏离。Mock Judge 结果为规则化模拟，不代表真实模型校准结果。 | 类型5（2 行图例说明） | 绿：一致；红：偏离。Mock Judge 为规则化模拟，不代表真实模型结果。 |

### frontend/src/pages/DisagreementExplorer.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 222–223 | 多个 Judge 独立评分 → 分歧检测 → 仲裁聚合。缺失评分不按 0 处理；严重分歧时返回 INCONCLUSIVE 而非强行结论。所有预设配置均为 Mock（零成本）。 | 类型5（2 行副标题） | 多个 Judge 独立评分后做分歧检测与仲裁；缺失评分不计 0，严重分歧返回 INCONCLUSIVE。预设配置均为 Mock。 |
| 297 | 独立性评估（关键诚实性声明） | 类型1（内部验收语言） | 独立性评估 |
| 554 | 各 Judge 分歧未达阈值，可直接参考聚合结果（仍非客观真值）。 | 类型2（括号补充） | 分歧未达阈值，可参考聚合结果（非客观真值）。 |

### frontend/src/pages/AgentRuns.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 175–177 | 提交 Agent 任务 → 后端执行（模型推理 + 工具调用）→ 采集 Trace → 保存 → 展示结果。默认为 Mock 模式（确定性脚本化模型 + 真实本地工具，零成本）；真实 DeepSeek API 需服务端开关与请求确认双重授权。 | 类型5（3 行、流程箭头堆叠） | 提交任务后由后端执行（模型推理 + 工具调用）并采集 Trace。默认 Mock 模式（零成本）；真实 API 需服务端开关与请求确认。 |
| 188 | 输入任务并运行，执行过程将记录为可追踪的 Trace。 | 类型2（教学式提示） | 输入任务并运行，执行过程会记录为 Trace。 |
| 312、316、323、347、369 | Not Recorded | 类型4（英文占位符直出） | 未记录 |
| 448、452 | `metadata.simulated=false` / `metadata.simulated=true`（反引号原样渲染） | 类型4（Markdown 标记泄漏） | metadata.simulated = false / true（去掉反引号） |
| 458 | 三者严格区分，绝不把模拟 Trace 标记为真实 Trace。 | 类型3（口号式保证） | 模拟数据不会标记为真实数据。 |

### frontend/src/pages/RedTeamLab.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 172–173 | 生成六类受控安全测试案例（确定性模板 + 种子变异），在受控环境中执行并做确定性安全判定。安全通过率与任务成功率是两个独立指标；金丝雀负控用于验证检测器本身是否有效。 | 类型5（2 行副标题） | 生成六类受控安全测试案例并做确定性安全判定。安全通过率与任务成功率独立统计；金丝雀用例用于验证检测器有效性。 |
| 310、343 | 按攻击类型统计与「类型」列直接输出原始 key（如 prompt_injection） | 类型4（原始枚举直出） | 输出中文类型名（提示注入 / 工具误用 / 权限边界 / 非法参数 / 指令冲突 / 故障恢复） |

### frontend/src/pages/AdversarialLab.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 273–274 | 受控对抗评测：生成六类攻击案例 → 选择案例执行 → 安全约束判定 → 攻击覆盖地图 → Bad Case Explorer。安全通过率与任务成功率是两个独立指标；未执行与 UNKNOWN 均不计入通过。 | 类型5（2 行副标题） | 受控对抗评测：生成攻击案例 → 执行 → 安全约束判定 → 覆盖地图 → Bad Case 列表。安全通过率与任务成功率独立统计。 |
| 281 | ① 生成配置 | 类型1（步骤号） | 生成配置 |
| 382 | ② 生成结果（{n} 例） | 类型1（步骤号） | 生成结果（{n} 例） |
| 492 | ③ Attack Coverage Map（来自实际执行数据） | 类型1（步骤号 + 括号解释） | 攻击覆盖图 |
| 559 | ④ Bad Case Explorer（{n}/{m}） | 类型1（步骤号） | Bad Case（{n}/{m}） |
| 643 | ⑤ 案例证据 | 类型1（步骤号） | 案例证据 |
| 565 | 风险类型筛选项直接渲染原始 key（prompt_injection 等） | 类型4（原始枚举直出） | 使用 `TYPE_LABEL[k]` 输出中文名 |
| 603 | 风险类型列直接渲染 {c.attack_type} 原始 key | 类型4（原始枚举直出） | 使用 `TYPE_LABEL[c.attack_type] ?? c.attack_type` |

### frontend/src/pages/EvaluationPipeline.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 466–467 | 数据集验证 → Agent 执行 → Trace 采集 → 确定性评测 → 可选 Judge / 归因 / 失败模式 / 版本对比 / 回归检测 → 质量门禁 → 报告（Mock 模式零成本、无付费 API） | 类型5（2 行流程堆叠） | 数据集验证 → Agent 执行 → Trace 采集 → 评测 → 可选归因 / 失败模式 / 版本对比 / 回归 / 门禁 → 报告。 |
| 471 | Pipeline Run Inspector | 类型2（英文标签无信息量） | 删除该徽标，或改为「运行详情」 |
| 531 | real_api（需服务端 TRACEPILOT_ALLOW_REAL_API=1） | 类型4（环境变量外露） | real_api（需服务端开启真实 API） |
| 552–553 | none（仅确定性验证器） / mock（规则化模拟，零成本） | 类型4（原始枚举直出） | 不启用（仅确定性验证器） / Mock（规则化模拟，零成本） |
| 811 | 流水线仍在运行，报告尚未生成（不展示未完成结论）。 | 类型2（括号内部约束） | 流水线仍在运行，报告尚未生成。 |
| 1018 | 限制与口径（{n}） | 类型1（「口径」） | 限制说明（{n}） |

### frontend/src/pages/CostAnalytics.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 370–372 | Token 用量来源 · 价格表成本估算 · 延迟 · 成本归因 · UNKNOWN 与缺失数据（全部本地确定性计算，不调用付费 API） | 类型5（2 行、8 个名词并列） | Token 用量、价格表成本估算、延迟与成本归因；含 UNKNOWN 与缺失数据。 |
| 407 | 暂无已验证的真实模型运行记录。历史 trace-4d3d... 缺少 real_api_verified 标记，按规则归入 UNKNOWN，不自动升级为真实 API。 | 类型1（开发期具体记录 + 内部规则） | 暂无已验证的真实模型运行记录。 |
| 463–465 | 计费口径：{disclaimer}。状态 ACTUAL_USAGE_PRICED 表示「Provider 报告用量 + 本地价格表」，仍不是 Provider 实际扣费金额；价格为空的模型为 PRICE_UNKNOWN。 | 类型5（3 行、两个枚举码） | 成本为本地价格表估算，不是 Provider 实际账单；无价格的模型标记为 PRICE_UNKNOWN。 |
| 619 | 0 次（paid_api_called=false） | 类型4（布尔字段直出） | 0 次 |
| 633 | 版本成本变化（agent_version 缺失时按模型/执行模式分组） | 类型2（实现细节写进标题） | 版本成本变化 |
| 806 | · 来源筛选 {scope} 对当前维度不生效（评测/流水线对象自身即评测数据） | 类型2（括号解释） | · 当前维度不受来源筛选影响 |
| 862 | UNKNOWN 与缺失数据（不当作 0） | 类型1（内部约定写进标题） | UNKNOWN 与缺失数据 |
| 898 | 限制与口径（{n}） | 类型1（「口径」） | 限制说明（{n}） |
| 913–915 | 成本为「Token 用量 × 本地价格表」的估算，不是 Provider 实际账单；价格缺失时返回 PRICE_UNKNOWN，不编造官方价格。 | 类型5（页脚 2 行重复声明） | 成本为估算值，不是 Provider 实际账单；无价格时显示 PRICE_UNKNOWN。 |

### frontend/src/pages/ParetoExplorer.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 527 | 质量越高、延迟越低、成本越低越好 · 非支配解（Pareto Frontier）· 交互约束 · 相似候选并列处理 | 类型5（2 行、4 个卖点并列） | 质量越高、延迟与成本越低越好；展示非支配解与并列候选。 |
| 602 | 真实/模拟混用：不得解释为真实模型性能比较 | 类型2（行政化措辞） | 含模拟数据，不可直接比较模型性能 |
| 614–617 | 候选全部来自本地评测运行；模拟执行数据不代表真实模型性能；成本为本地价格表估算，不是 Provider 实际账单。 | 类型5（3 行重复免责） | 候选来自本地评测运行；模拟数据不代表真实模型性能；成本为估算值。 |
| 732 | 候选指标缺失时显示「—」，不按 0 计；不可比候选不进入前沿。 | 类型2（实现说明） | 缺失指标显示「—」；不可比候选不进入前沿。 |
| 960 | 未提供（未伪造） | 类型1（内部立场写进字段） | 未提供 |
| 994 | 金额为本地价格表估算，不是 Provider 实际账单；PRICE_UNKNOWN / 缺失成本显示「—」。 | 类型5（2 行重复免责） | 金额为估算值；PRICE_UNKNOWN 与缺失成本显示「—」。 |

### frontend/src/pages/VersionComparison.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 622–624 | 版本元数据 · 可比性检查 · 指标差异（分子/分母、pp 与相对变化）· Case-level Diff · 失败模式变化 · 回归风险矩阵与质量门禁 | 类型5（2 行、7 个名词并列） | 版本元数据、可比性检查、指标差异、案例级 Diff、失败模式变化、回归风险矩阵与质量门禁。 |
| 645 | 暂无可比较的评测运行（仅列出已成功且报告可读的运行；运行记录保存在内存中，最多 20 条） | 类型5（内部实现细节写进空状态） | 暂无可比较的评测运行（仅列出报告可读的成功运行）。 |
| 802 | 指标差异保留原始分子/分母；pp（百分点）与相对变化分开呈现；UNKNOWN 不当作 0。 | 类型5（2 行指标说明） | pp 为百分点差；相对变化以基线为分母；UNKNOWN 不计为 0。 |
| 1129 | Regression Detection（规则命中 → 风险等级 → 质量门禁） | 类型2（括号解释实现链） | 回归检测 |
| 1147 | INCONCLUSIVE 表示无法判定，绝不视为 PASS；风险等级由规则严重度取最大值决定，不含模型生成的概率。 | 类型5（2 行说明 + 内部口吻） | INCONCLUSIVE 视为无法判定，不等同 PASS；风险等级取规则严重度最大值。 |
| 1323 | CI Quality Gate（确定性规则 · 质量门禁） | 类型2（中英重复） | 质量门禁（CI） |
| 1344–1345 | 阈值来源：{label}（配置指纹 {fp}）。INCONCLUSIVE 绝不等于 PASS；WARN 默认退出码 0（仅告警），可通过配置调整。 | 类型5（2 行说明） | 阈值来源：{label}。WARN 默认退出码 0（仅告警）。 |

### frontend/src/pages/Architecture.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 234 | 分层架构、执行链路与真实运行概览；统计数据实时读取自本机后端，不使用演示记录。 | 类型2（括号式澄清） | 分层架构、执行链路与本机运行数据概览。 |
| 17 | LangGraph 调度：规划 / 模型调用 / 工具调用 / Skill / RAG / 记忆 / 反思 / HITL / 安全钩子；故障注入接口。 | 类型2（一行堆 9 个技术名词） | Agent 执行：规划、模型调用、工具调用、RAG 与记忆、反思、HITL、安全钩子，并提供故障注入接口。 |
| 270–272 | FastAPI 服务层统一提供 Trace / DAG / 故障归因 / 反事实 / 评测接口；React 控制台按业务域组织为执行、诊断、评测、安全与成本分析模块。运行记录持久化在本地存储，模拟与真实数据在界面中按来源明确标注。 | 类型5（3 行段落） | FastAPI 统一提供 Trace / DAG / 归因 / 反事实 / 评测接口；控制台按执行、诊断、评测、安全与成本组织；数据持久化在本地，并按来源标注。 |
| 338 | Trace 总数与数据来源分布来自持久化存储；Agent / 评测运行统计为当前服务进程内可查询记录。 | 类型1（内部实现说明） | Trace 与来源分布取自持久化存储；运行统计为当前会话内记录。 |
| 443 | 评测运行与流水线使用确定性模拟执行器，成本与 Token 标注为模拟，不代表真实模型或真实计费。 | 类型5（页脚 2 行声明） | 评测与流水线为模拟执行，成本与 Token 为模拟值。 |

### frontend/src/components/Layout.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 48、53、76、77、88 | Agent执行与轨迹 / DAG分析 / Judge校准 / 多Judge分歧 / Pareto分析 | 类型4（中英混排、缺空格、术语不统一） | Agent 执行与轨迹 / DAG 分析 / Judge 校准 / 多 Judge 分歧 / Pareto 分析 |

### frontend/src/components/AttributionPanel.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 96–100 | 直接证据（Trace 记录） / 观察差异（对比/指标） / 依赖关系（DAG 可达，不构成因果） / 干预证据（反事实实验） / 未知（证据不足） | 类型2（标签内塞括号解释） | 直接证据 / 观察差异 / 依赖推断 / 干预证据 / 未知 |

### frontend/src/components/EvidenceCard.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 25–32 | 故障类型：Not Recorded / 注入标记：injected = Not Recorded / 错误信息：Not Recorded（共 5 处） | 类型4（英文占位符直出） | 未记录 |

### frontend/src/components/NodeDetailPanel.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 35 | Not Recorded（时间、耗时、Token、父节点等字段的空值占位） | 类型4（英文占位符直出） | 未记录 |

### frontend/src/components/LineagePanel.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 195 | NOT_AVAILABLE（节点状态徽标） | 类型4（枚举直出，同文件已有「不可用」映射） | 不可用 |

### frontend/src/components/FailurePatternExplorer.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 349 | 占比分母 = 本次分析的 Bad Case 数；未执行案例不计入任何模式；UNKNOWN 单独计数。 | 类型1（分母口径说明） | 占比分母为本次分析的 Bad Case 数；UNKNOWN 单独计数。 |
| 457 | UNKNOWN 案例（{n}）：证据不足未归入具体失败类型，单独计数、不并入占比 | 类型5（2 行说明） | UNKNOWN 案例（{n}）：证据不足，未计入占比 |
| 469 | 评测版本变化对比（按 fingerprint 对齐） | 类型2（实现细节写进标题） | 评测版本变化对比 |
| 587 | null（不可得） | 类型4（原始空值表示） | —（不可得） |

### frontend/src/components/RootCauseMetricsPanel.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 96–98 | 金标准来自受控故障评测集标注（合成数据）；指标用于验证归因是否命中标注根因，不代表真实生产故障分布。 | 类型5（2 行说明） | 金标准来自合成故障评测集；指标不代表真实生产故障分布。 |
| 155–156 | 基线仅说明类别分布，不代表完整模型性能对比。 | 类型5（2 行解释） | 基线仅反映类别分布。 |

### frontend/src/components/DocumentPanel.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 40 | 上传、解析、预览并引用到Agent任务 · 单文件8MB | 类型4（中英/数字缺空格、表述挤在一起） | 上传、解析与预览文件，并引用到 Agent 任务 · 单文件 8MB |
| 52 | 最多引用{max}个文件；仅解析文本，不执行文件内指令。旧版DOC需先转换为DOCX；扫描PDF需要OCR。 | 类型5（2 行、术语间距不规范） | 最多引用 {max} 个文件；仅解析文本，不执行文件内指令。旧版 DOC 请先转为 DOCX；扫描版 PDF 需 OCR。 |

### frontend/src/pages/NotImplemented.tsx

| 行号 | 当前原文（截断） | 问题类型 | 建议替换文案 |
| --- | --- | --- | --- |
| 9–12 | 该模块尚未实现 / 当前版本（V1）仅完整实现 Overview、Trace Explorer、Architecture。其余模块将在后续版本接入真实数据，不会用假数据冒充真实功能。 | 类型1 + 类型4（开发阶段说明 + 占位内容） | 该页面未被任何路由引用，建议直接删除该文件；若保留，文案改为「功能建设中」。 |

---

## 建议保留（不在整改范围）

以下文案属于真实数据来源标识、真实限制或工程上有价值的定义，建议**保留**，仅做措辞微调即可：

1. 数据来源分类与徽标：`REAL_API / MOCK / SIMULATED / REPLAY / UNKNOWN` 及其定义（TraceExplorer 11–16、CostAnalytics 157–162、Architecture 378–385、AgentRuns 76–81、LineagePanel 55–72、TraceDetail 301–328）。
2. 安全与费用限制：AgentRuns 246–276（执行边界：调用次数 / Token / 时长 / 白名单）、EvaluationPipeline 541（真实 API 付费确认）、AdversarialLab 366–368（生成/执行/并发上限）、FaultLab 169（建议按第 23 条精简但保留要点）、JudgeCalibration 205（授权限制）。
3. 指标定义与证据链：EvaluationCenter 476（六维指标表头）、VersionComparison 789–793（分子/分母与 pp 口径）、ParetoExplorer 1084（并列组说明）、FailurePatternExplorer 619（相同指纹不等于相同根因）、AttributionPanel 93–100（证据分级体系）。
4. 失败 / 不可用状态：`StatusBadge` 状态词（components/ui.tsx 23–29）、「失败统计不可用」「暂无 DAG 数据」等真实空状态。
5. 合规性免责：`PRICE_UNKNOWN`、`成本估算 ≠ 实际账单`、`INCONCLUSIVE` 语义说明——建议保留但按上文对应条目压缩为一句。

## 覆盖说明与未覆盖文件

- 已逐行通读并抽查渲染文本：20 个已挂载路由页面（Overview、TraceExplorer、TraceDetail、DagAnalysis、FaultLab、CounterfactualLab、RepairStudio、EvaluationCenter、JudgeCalibration、DisagreementExplorer、AgentRuns、TraceComparison、FaultAttribution、RedTeamLab、AdversarialLab、VersionComparison、EvaluationPipeline、CostAnalytics、ParetoExplorer、Architecture）+ 10 个组件。
- 有条目：21 个 pages + 8 个 components；已通读但无需整改：`components/TraceFlowNode.tsx`（仅「重试 #n」「超时」徽标）、`components/ui.tsx`（状态词与「暂无数据」）。
- 未纳入整改：`frontend/src/pages/AgentRuns copy.tsx` —— 未被任何路由或组件引用（全仓无 import），属历史副本，建议直接删除文件，不单独做文案整改；其文案问题与 `AgentRuns.tsx` 同源。
- 代码注释中的开发阶段编号（`CostAnalytics.tsx` 8「第 53 步」、`ParetoExplorer.tsx` 8「第 54 步」、`EvaluationPipeline.tsx` 8「第 52 步」、`VersionComparison.tsx` 8「第 49 / 50 步」、`RootCauseMetricsPanel.tsx` 6「第 55 步审计修复，原始需求创新点 6」、`CounterfactualLab.tsx` 170 / `RepairStudio.tsx` 177「第 55-A 步治理接口」）不会渲染到界面，故不计入条目；但若后续做“去开发态痕迹”清理，可一并改写为业务说明。
