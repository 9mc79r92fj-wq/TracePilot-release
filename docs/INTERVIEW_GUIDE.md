# TracePilot 面试讲解指南（V2.0）

本指南用于面试时讲清这个项目做了什么、怎么做的、哪些结论有数据支撑、哪些地方还不能吹。
所有数字都来自本仓库实际运行产物（`docs/v2_results/`、`data/v2_experiments/`），可现场复现。

## 1. 一句话定位

TracePilot 是一个**面向长程 Agent 的执行过程评测平台**：把 Agent 的一次运行记录成结构化 Trace，
在此基础上做过程质量评测、故障注入与回放、根因归因、反事实重放和最小修复验证，
并接入公开 Agent 故障 Benchmark（Microsoft AgentRx）做对照实验。

不是"又一个 Agent 聊天壳"，也不是"LLM 打分器"：**评测对象是过程，不是最终答案**。

## 2. 架构与数据流

```
用户任务 ──> Agent Runner ──> Trace（模型调用/工具调用/状态/Token/延迟）
                                  │
        ┌─────────────────────────┼──────────────────────────┐
        ▼                         ▼                          ▼
  过程评测引擎              DAG + 证据血缘              反事实引擎
  (规则 / Judge / 多Judge)        │                    (干预→重放→对比)
        │                         ▼                          │
        │                   根因候选排序               最小修复验证
        │                         │                    (真实重放)
        └──────────► 报告 / 前端展示 ◄─────────────────────┘
```

关键设计取舍：

1. **Trace 是一等公民**：所有子系统（评测、归因、反事实、互操作、成本分析）都读同一份 Trace Schema，
   避免"每个功能一套数据"；`docs/TRACE_SCHEMA.md` 是唯一契约。
2. **真实/模拟必须显式区分**：Trace 上带 `execution_mode`（`real_api` / `mock` / `simulated` / `replay`），
   `metadata.data_source` 参与校验；反事实模块**拒绝**非 `real_api` 的结果冒充真实实验。
3. **确定性优先，LLM 兜底**：能规则算的（DAG、证据一致性、Token 成本、Trace 结构）不交给模型；
   模型只做判断类工作（归因裁决、Judge），且每次调用都受预算与超时约束。
4. **失败可归因、可重放**：故障注入是可审计的（注入点/注入类型/参数），反事实重放复用同一执行链路。

## 3. 真实 API 工程化（面试重点）

真实 DeepSeek 调用不是"配个 key 就完事"，本项目的处理：

| 问题 | 处理 |
|---|---|
| 模型名漂移 | 以 2026-11-27 实际 `/models` 为准：`deepseek-flash`、`deepseek-v4-pro`；旧名只做兼容映射，不对外宣称 |
| 隐藏思考消耗 | `deepseek-flash` 默认 thinking + high effort，单次调用可能空返回；统一 `extra_body={"thinking":{"type":"disabled"}}` |
| 空返回 | `max_tokens` 过小（700）导致空 content；JSON 任务提到 1500–3000，并校验 `finish_reason` |
| SDK 偷偷重试 | `max_retries=0`，每次上游调用单独计费扣减，重试也计数 |
| 预算失控 | 持久化 JSON 预算（文件锁 + 原子替换），**调用前 reserve、返回后记录**；总量 + 访客日额度；损坏状态 fail-closed；管理员可调但落盘 |
| 未测量不填 0 | Token 拿不到时写 `null`/`unknown`，并有测试禁止用 0 冒充 |

验收（实际执行，不是 mock）：

- 真实 Agent 多步执行：`trace-63d1ae6a…`，4 段 model_call，Provider 上报 3,202 input / 628 output / 3,830 total，
  真实调用 calculator 工具，`real_api_verified=true`；
- 真实受控反事实：`remove_fault` 重放成功（`trace-03544cd2…`，3,626 tokens，7.3s）；
- 真实最小修复验证：`INCREASE_TIMEOUT` 与 `LIMITED_RETRY` 两个候选均真实重放成功。

## 4. Benchmark 与三组对照（怎么讲才不夸大）

数据：Microsoft AgentRx 公开 GitHub 子集（MIT），73 条案例、3,265 条消息、334 条失败标注；
按 seed=42 的稳定哈希划分 **dev 38 / test 35**，真实对照只跑 test 的 25 条（14 tau + 11 magentic）。
标签只用于打分，Prompt 输入经 `case_without_labels()` 构造并有测试防止泄漏。

| 指标（25 条真实调用） | Baseline B 规则 | Baseline A 直接 LLM | TracePilot V2 |
|---|---:|---:|---:|
| Root Cause Top-1 | 0.04 | **0.32** | 0.20 |
| Root Cause Top-3 | 0.08 | 0.32 | **0.40** |
| Failure Step Acc | 0.08 | **0.48** | 0.36 |
| Category Macro-F1 | 0.0351 | 0.0873 | **0.0926** |
| 平均延迟 | 0 ms | 1,293.9 ms | 1,596.6 ms |

**必须主动说明的三点（体现诚实与判断力）**：

1. **V2 没有超过 Baseline A 的 Top-1**：0.20 vs 0.32。不能宣称架构更优。
2. Top-3 的 0.32→0.40 有**结构性不对等**：Baseline A 的 Prompt 只要求一个答案（其 `top3_steps` 长度恒为 1），
   而 V2 要求输出 3 个候选，所以 Top-3 对 A 退化为 Top-1。这条优势不作为独立证据。
3. v2.1 的 Prompt 是在同一 25 条案例上事后调的，**没有独立验证集**，有过拟合风险，只作为版本记录。

Bad Case 分析（`docs/V2_BAD_CASE_ANALYSIS.md`）：V2 的 20 个 Top-1 错误中偏早 9 / 偏晚 8，
说明不是"系统性找症状"，而是**归因粒度冲突**——AgentRx 标注的是"具体失败实例"，V2 输出的是"最早异常"，
两者语义不同。另有 4/25 案例 V2 完全照抄确定性候选，且 4 次全错。

## 5. 反事实与最小修复（怎么保证不是模型自说自话）

流程：注入/定位故障 → 生成干预候选（修正参数、替换工具结果、调整重试/超时、修正上下文、重跑节点）
→ 受控重放 → **对比重放前后 Trace 的工具返回、状态、最终结果** → 只有真实重放通过才计"修复成功"。

- 候选生成、干预成本函数（干预节点数、额外 Token、额外延迟）、搜索边界、终止条件都在
  `backend/app/services/real_counterfactual.py` 与 `counterfactual_engine` 中显式定义；
- 沙箱限制：只允许白名单工具（calculator / text_analyzer），禁止 subprocess/文件/网络；
- 结论等级：本次真实实验的因果证据等级是 **weak**（单案例、单次重放），不宣称"生产普遍因果"。

## 6. LLM Judge 校准（不说"人工标注"）

- 校准集 22 条，实际评估 16 条，13 条可评估（Judge 与参考标签都给出有效分数）；
- 参考标签来源标为 `synthetic_reference`：开发者按 Rubric 规则给出的确定性参考，**不是**独立第三方人工标注，
  更不是双盲专家标注流程；
- 结果：exact agreement **0.3077**、MAE **0.9192**、within-1 **0.6154**；
- 主要偏差：Judge 对"工具参数错误/工具结果被忽视/格式违规"严重高估（6 条高估，0 条低估），
  即模型倾向于给"看起来说得通"的回答打高分，不完全遵守证据链；
- 因 Rubric 是有序等级、样本量小，未计算 Cohen's Kappa（需要独立双标注），如实记录样本量。

## 7. QueryForge Trace 互操作（体现协议设计能力）

- 版本化 JSON 协议 v1：`trace_id / schema_version / span_id / parent_span_id / event_type / status /
  model_call / retrieval / tool_call / rerank / citation_validation / token_usage / latency_ms / error /
  execution_mode`；
- 7 种 `event_type`（planning / model_call / tool_call / retrieval / rerank / citation_validation / reflection）、
  4 种 `execution_mode`、7 种 span 状态；未知字段保留、未知事件类型显式失败而不是静默丢弃；
- `token_usage` 缺失必须 `null`，禁止 0 冒充；
- 提供 JSON Schema、示例、Pydantic 校验、导入接口（不覆盖已存在 trace_id）、前端导入入口；
- 83 项测试覆盖校验规则、映射、冲突、幂等。

## 8. 高频问答（准备口径）

**Q：你怎么保证真实评测不是假的？**
A：三层：Trace 上带 `execution_mode` 与 `metadata.data_source`；真实执行记录 Provider 上报的 usage
与模型名；反事实模块在代码层面拒绝非 `real_api` 结果参与"修复成功"判定。另有真实 API Smoke 脚本，
但默认测试不调用付费接口，真实测试独立运行并设预算。

**Q：为什么 V2 比直接问 LLM 还差？**
A：如实回答：在这批数据上没有超过。原因是归因粒度语义不同 + 规则先验噪音 + 单次调用。
进一步工作：标签粒度双输出、独立验证集、多次采样投票（受预算限制未做）。

**Q：你的预算怎么防止超支？**
A：调用前 reserve（持久化计数，跨进程文件锁），返回后按实际 usage 记账；总量 + 访客日额度；
损坏状态直接拒绝调用（fail-closed）；管理员改额度也落盘且下次启动仍生效；实验前估算最大开销。

**Q：Benchmark 数据合法吗？**
A：Microsoft AgentRx，MIT，保留 LICENSE，记录 commit 与逐文件 sha256；HF 版本是 gated 的，没拿到授权，
所以明确写"公开 GitHub 子集 73 条，非 HF 完整 170 条"。

**Q：反事实重放怎么防止把"模型声称修好"当成功？**
A：只认重放产生的 Trace：对比工具调用返回、节点状态、最终输出是否满足原任务断言；
不读模型自述；结论标注证据等级（本次为 weak）。

**Q：如果换一个模型/一组案例，结论会变吗？**
A：会。样本只有 25 条、单一模型、单次采样，没有显著性检验；任何"普遍更优"的说法都不成立。

**Q：你这个项目的工程量体现在哪？**
A：721 项旧测试基线修复到全量通过；新增真实 API 预算、管理员安全、反事实、Benchmark、互操作等模块；
前端新增 V2 实验页；CI 门禁；ModelScope 原空间原地升级并做真实浏览器验收。

## 9. 简历可用条目（3–5 条，均为真实结果）

1. 独立开发 Agent 过程评测平台 TracePilot：以结构化 Trace 为核心，覆盖过程评测、故障注入/回放、
   根因归因、反事实重放与最小修复验证，FastAPI + React + SQLite，本地与 ModelScope 双环境部署。
2. 打通 DeepSeek 真实 API 全链路（Agent 多步执行、Function Calling、LLM Judge、反事实重放），
   设计**按上游调用扣减的持久化预算**（总量 + 访客日额度、损坏 fail-closed），完成 142 次真实调用、
   约 47 万 Token、估算成本 <¥0.5 的受控实验，无超支。
3. 接入 Microsoft AgentRx 公开 Benchmark（73 案例 / 334 条标注，seed 固定划分 dev/test，
   标签与输入严格隔离），实现规则、直接 LLM、规则+DAG+证据+LLM 三组真实对照，并如实报告
   V2 Top-1 0.20 未超过 Baseline A 0.32，给出 Bad Case 与过拟合风险分析。
4. 实现受控反事实与最小修复验证：真实重放 `remove_fault` 与两种重试/超时修复候选，均通过真实
   Provider Token 与 Trace 验证，结论按证据等级（weak）标注，不依赖模型自述。
5. 设计并落地 QueryForge Trace 互操作协议 v1（JSON Schema + Pydantic 校验 + 导入 + 前端展示，83 项测试），
   打通 RAG 系统与 Agent 评测平台的跨系统 Trace 互通。

## 10. 不宣称事项（面试中主动划边界）

- 不宣称 V2 根因归因优于直接 LLM 诊断；不宣称修复成功率可泛化到生产；
- 不宣称 Judge 校准结果等于人工一致率（参考标签是 `synthetic_reference`）；
- 不宣称成本数字等于 Provider 账单（余额接口不可靠，按 Token × 配置单价估算）；
- 不宣称覆盖 AgentRx 官方完整 170 条（HF gated，未获授权）；
- 不把模拟/回放数据描述为真实 API 执行。
