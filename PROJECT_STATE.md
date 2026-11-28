# PROJECT_STATE

## 项目名称

TracePilot

## 项目定位

面向长程AI Agent的执行轨迹分析、故障归因、反事实实验与最小修复验证平台。

## 项目目标

实现20项Agent核心能力、7个可验证创新方向，最终交付可运行的完整Demo、评测报告和作品集。

## 当前开发阶段

阶段 0–55-A 的核心数据链路与产品界面已实现：Trace 采集/DAG/故障注入/反事实/最小修复/评测/
Judge/校准/多 Judge/真实执行/红队/对抗评测/自动归因/失败模式/版本对比/回归检测/CI 质量门禁/
自动化评测流水线/Token 与成本分析/成本–延迟–质量帕累托分析/需求回溯与能力审计/
数据治理与可信度修复（来源分类 / 统计口径统一 / 失败 Trace 验证 / 测试污染隔离）。
**关键事实：真实 DeepSeek API 端到端路径已用真实 Key 验证 1 次（任务「计算125乘以48」，
`data_source=real_api`，Token 1852）；后续付费调用未再执行。**

## 已完成（有测试/实测证据）

1. 环境：独立目录、智能体开发工具工作区、Git 仓库、`.venv`（Python 3.11）、`.gitignore` 完整（敏感文件零泄露）。
2. 文档：PRD、ARCHITECTURE、TRACE_SCHEMA、FAULT_INJECTION、COUNTERFACTUAL_ENGINE、MINIMAL_REPAIR、EVALUATION_ENGINE、FEATURE_AUDIT、TECH_DEBT、DECISIONS。
3. Trace Schema（Pydantic v2，21 测试）。
4. 依赖解析与 DAG 分析（串行/并行/多父汇合/环检测/拓扑排序/祖先后代，10 测试）。
5. 最小 Agent 执行引擎（LangGraph + Function Calling 循环 + 工具调用 + Trace 采集）— 仅模拟模型验证。
6. 三个本地工具（calculator / sqlite_query 只读 / text_analyzer，15 测试）。
7. SQLite 初始化（database.py + init_db.py，幂等）。
8. Agent CLI（run_agent.py，支持 `--save-trace`）。
9. 轨迹采集（trace_collector.py，8 测试）+ 独立验证脚本（verify_trace_dag.py，实测 5 节点 4 边 DAG 合法）。
10. 受控故障注入（6 类故障，14 测试）并接入 Agent 工具链路。
11. Trace 对比分析（可解释语义匹配，5 测试）。
12. 故障传播分析（5 类节点分类，6 测试）。
13. Failure Replay（Recorded 只读 + Controlled 重跑，9 测试）。
14. Counterfactual Engine（3 类白名单干预、保守因果分级，14 + 13 测试）。
15. Minimal Repair Engine（6 类修复、可解释评分，14 + 11 测试）。
16. Agent Evaluation Engine（六维度 + 10 确定性验证器 + 13 案例合成评测集，16 + 8 + 11 测试）。
17. FastAPI 后端（26 个 API，全部冒烟通过，0 个 5xx）。
18. React 控制台（11 个导航入口，8 个真实实现：Overview / Trace Explorer / Trace 详情 / DAG+TimeTravel / Fault Lab / Counterfactual Lab / Repair Studio / Evaluation Center / Architecture）。
19. 前后端联调（前端调用路径与后端路由全部匹配；评测证据下钻链路实测打通）。

## 关键验证结果（第 55 步）

- 后端测试：**661 passed / 0 failed / 0 skipped**（第 54 步 643，+18：能力套件 13 + 根因指标 5）
- 前端构建：**通过**（tsc + Vite）
- 浏览器验收：**102 passed / 0 failed**（Playwright 1.65.1 + Edge，19 个真实页面）
- 技术债：**P0 = 1（真实 API 自动化验证，BLOCKED 等授权）**｜P1 = 0 剩余｜P2 = 36｜P3 = 36
- 原始需求：PDF（12 页）成功访问并完成 **74 条**需求/验收项追踪矩阵；COMPLETE 52 / PARTIAL 22 / BROKEN 0 / MISSING 0
- 能力矩阵：20 项能力 **15 COMPLETE + 5 PARTIAL**；7 个创新点 **3 COMPLETE + 4 PARTIAL**（`/api/capabilities/matrix` + Architecture 页）
- 新增根因准确率指标：Top-1/Top-3（类型与步骤级）、误归因率、平均诊断耗时/成本、多数类/随机基线（Evaluation Center 指标卡）
- 新增能力实现：RAG / Memory / Skill / Context Review / Context Engineering / Reflection / HITL / Explainability / Execution Environment（工具超时）
- 本地 CI 质量门禁：**PASS（退出码 0）**；GitHub 云端 CI：**NOT_VERIFIED**（无远程仓库）
- 页面加载：19/19 路由（真实页面，无占位页）
- 付费 API：本步 0 次；所有新增接口 `paid_api_called=false`
- 审计判定：第 49–55 步模块 COMPLETE；P0 真实 API 自动化验证需用户授权后执行

## 第 38 步新增：LLM-as-a-Judge 评测器

- 代码 `backend/app/services/llm_judge.py`（Rubric 驱动、6 维度、1~5 分、证据引用、UNKNOWN 语义）
- Rubric 数据集 `data/evaluation/rubrics/`：4 个（answer_quality / tool_usage / factual_consistency / instruction_following）
- Provider：MockJudge（默认、零成本、标记 simulated）+ DeepSeekJudge（双开关授权，默认禁用）
- API：`/api/evaluation/judge/info`、`/api/evaluation/judge/rubrics[/{id}]`；评测运行新增 `judge_mode` / `judge_rubric_id`
- 集成：`metrics.judge_breakdown` + `case_results[].judge`，与确定性指标严格分离（已验证确定性结果零变更）
- 前端：Evaluation Center 增加 Judge 配置、Judge 评审卡片、案例抽屉内 Judge 评分/证据/限制分区
- 测试：`test_llm_judge.py`(23) + `test_llm_judge_api.py`(11) 全通过
- **限制：Real Judge 完全未验证**（需付费调用，超出授权）；Mock 评分为规则化模拟，不代表真实模型判断

## 第 39 步新增：Judge Calibration（评测器校准）

- 代码 `backend/app/services/judge_calibration.py`
- 校准集 `data/evaluation/calibration/calibration_set_v1.json`：**22 案例**（19 SCORED + 3 UNKNOWN），覆盖 8 类场景（正确/部分正确/事实错误/信息遗漏/指令违反/工具使用不合理/证据不足/模糊边界）
- 标注来源：**synthetic_reference**（开发者按 Rubric 人工给定，非独立双盲；不得宣称等同真实人工一致率）
- 指标：Exact Agreement、MAE、Within-1 Agreement、5×5 Confusion Matrix、高估/低估/一致分类、稳定性（同案例重复评分标准差）
- 版本记录：校准引擎 1.0.0、Rubric 版本、Prompt 版本、Judge 引擎版本、数据集版本、模型名
- API：`GET /api/evaluation/calibration/datasets`、`POST /api/evaluation/calibration/runs`（真实 Judge 未授权 → 403）
- 前端：`/calibration` 页面（指标卡 + 混淆矩阵热力图 + 差异分析 Tab + 证据抽屉），Evaluation Center 顶部含入口
- **Mock Judge 校准结果（真实发现）**：Exact 15.79%（3/19）、MAE 1.40、Within-1 36.84%（7/19）、系统性高估 11 vs 低估 1、稳定性 std=0（确定性）
- 测试：`test_judge_calibration.py`(23) + `test_judge_calibration_api.py`(12) 全通过
- **修复历史技术债**：P2-8（Judge 输入截断丢尾部证据 → 优先级采样）、P3-7（校准未实现 → 已实现）
- 新增技术债：P3-8（Judge vs 确定性分歧率）、P2-9（Mock 校准表现差，故意不调参）、P3-9（多标注者 κ/α）

## 第 40 步新增：Multi-Judge Arbitration（多评测器仲裁）

- 代码 `backend/app/services/multi_judge.py`
- 能力：多 Judge 独立评分（同 Rubric）、分歧检测（最大分差/方差/分布/一致性/证据冲突/理由摘要）、仲裁（Median / Mean / Calibration-weighted）、INCONCLUSIVE、人工复核建议、Judge 独立错误处理、UNKNOWN 处理
- Judge 关系区分：不同模型 / 同模型不同 Prompt / 同模型重复采样；**Mock 变体强制钳制为 `mock_prompt_variants`，永不冒充独立模型**
- Mock 变体真实差异：`strict`（证据<2 条→UNKNOWN、分数−1）/ `lenient`（+1）/ `base`，测试断言三者分数不同
- 预设配置组 3 个（mock-mixed-variants / mock-resampling / mock-dual），每组强制声明 limitation
- 加权聚合缺校准依据时抛 `MissingCalibrationError`，绝不编造可靠性权重
- API：7 个端点（configs / runs 创建·列表·状态 / scores / disagreement / arbitration）；真实 Judge 未授权 → 403
- 前端：`/multi-judge` Disagreement Explorer（并列 Judge 卡片 + 分歧指标 + 证据冲突对照 + 仲裁结果 + 人工复核建议），Evaluation Center 顶部含入口
- 测试：`test_multi_judge.py`(29) + `test_multi_judge_api.py`(20) 全通过
- **限制：真实模型间分歧未验证**；校准加权聚合无真实权重数据源

## 第 41 步新增：真实 Agent 执行链路（P0-3 关闭）

- 代码 `backend/app/services/agent_runner.py`：RunLimits 硬限制、双重授权、模型白名单+回退、data_source 标记、Trace 落盘
- API：`GET /api/agent/status`、`POST /api/agent/runs`、`GET /api/agent/runs`、`GET /api/agent/runs/{run_id}`
- 前端 `/runs`（Agent Runs 运行控制台）：任务提交、模式选择、费用确认、能力状态、结果展示（来源徽标/Token/工具详情）、Trace 与 DAG 跳转、运行历史、来源说明
- **真实 API 验证（用户批准，仅 1 次）**：
  - 任务「计算125乘以48」→ `succeeded`，耗时 5750ms
  - 实际模型 `deepseek-chat`（`deepseek-v4-pro` 无效，自动回退并记录 `model_selection.fallback`）
  - Trace `trace-4d3d82dc7a67496dbc2842cd3b31aa47`，6 步，**Token 1852**（in 1631 / out 221）
  - 工具调用 2 次 calculator（125*48 → 6000；125*50-125*2 → 6000）
  - 最终答案含 `125 × 48 = 6000`；DAG 6 节点 6 边拓扑合法
  - `data_source=real_api`、`simulated=false`；无限制违反
  - Trace 落盘 `data/traces/trace-4d3d82dc…json`（gitignored）
- 数据来源三态严格区分：`real_api` / `mock` / `simulated`；mock 永不标记为真实（测试锁定）
- 测试：`test_agent_runner.py`(25) + `test_agent_runner_api.py`(17) 全通过；全量 **356 passed**
- 修复 3 个真实缺陷：async 调用误用、真实路径 run_id 未归一化、后端未加载 `.env`
- 诚实性处置：删除桩测试产生的伪 `real_api` Trace 并让测试自清理

## 第 42 步新增：全站浏览器交互验收与自动修复

- 工具：**Playwright 1.65.1 + Microsoft Edge**（`channel: 'msedge'`，复用系统浏览器，无需下载 Chromium）
- 位置：`frontend/playwright.config.ts` + `frontend/e2e/{pages,flows,responsive}.spec.ts`
- 页面清单（真实）：**12 个真实页面 + 2 个占位页**（`/comparison`、`/attribution` 明确标注未实现）
- 结果：**41 passed / 0 failed / 0 blocked**（54s）
  - pages：11 页可打开/刷新不 404/无白屏/无严重 JS 异常 + 占位页诚实性 + 导航跳转与高亮 + 侧栏折叠
  - flows：6 大流程 8 条用例（Agent 运行 / 故障分析 / 反事实 / 修复验证 / Agent 评测 / Mock Judge / 校准 / 多 Judge 仲裁）
  - responsive：3 视口 × 11 页横向溢出与可读性 + 手机侧栏 + 表格滚动 + DAG 缩放 + 抽屉不超屏
- 修复 6 个真实缺陷：
  - **P0** Judge 校准页面运行必然 404（前端传 `calibration_id`，后端只认文件名 stem → 后端改为双标识解析 + 回归测试）
  - **P1** 全站窄屏横向溢出（最大 1057px → 0，新增 `responsive-fixes.css`）
  - **P1** 侧栏折叠按钮无可访问名称（补 `aria-label`/`aria-expanded`/`title`）
  - **P1** Trace 详情页缺数据来源标记（新增 `DataSourceTag`）
  - **P1** 验证修复后详情面板消失（repair_id 变化导致选中失效 → 自动重选）
  - **P2** Repair Studio 默认选中无候选 Trace（优先选可修复故障类型）
- 未验证（已登记，不计 PASS）：真实 API 浏览器流程、两个占位页功能、视觉回归、跨浏览器、a11y 扫描、部分 DAG 交互

## 第 43 步新增：历史 P1 技术债集中修复（P1 清零）

- **P1-1 FIXED**：新建 `Trace Comparison` 页（`/comparison`），复用 `POST /api/traces/compare`
  - 双 Trace 选择（默认参考=无故障、目标=含故障）→ 概览双栏对照 → 顶层差异表 → 最早差异点 → 差异节点（含上下游）→ 步骤匹配/未匹配 → **观察/推断/未验证三段分区** → 跳转 DAG/反事实；相同 Trace 明确拒绝
- **P1-2 FIXED**：新建 `Fault Attribution` 页（`/attribution`），复用 `GET /api/traces/{id}/propagation`
  - 故障 Trace + 可选基线 → 5 类分类指标卡 → 故障节点 → 传播路径 → 节点分类表（图例+筛选）→ **可达≠因果口径声明**
- **P1-4 FIXED**：新增 `e2e/time-travel.spec.ts`（2 项）覆盖时间轴展开/收起、进度推进、播放暂停、回到起点、跳转故障、速度切换、缺时间戳排除提示
- **P1-6 FIXED**：`.env` 中 `DEEPSEEK_MODEL` 由 `deepseek-v4-pro` 改为 `deepseek-chat`；`/api/agent/status` 实测 `configured_model_valid=true`
- 额外修复：DAG「起点」按钮 `aria-label` 与可见文字不符（WCAG Label-in-Name）→ 改为「回到起点」
- 页面清单：**12 真实页 + 2 占位 → 13 真实页 + 0 占位**；导航无「待实现」标记
- 接口审计：42 条路由 vs 前端调用比对，4 条冗余接口不构成用户缺口；40 项异常冒烟无 500
- 测试：后端 **357 passed**；前端构建通过；浏览器 **49 passed**（41 → 49）

## 第 44 步新增：数据真实性与证据血缘（Evidence Lineage）

- 新增 `backend/app/services/evidence_lineage.py`：血缘构建（4 类根）+ 只读一致性检查（6 类 issue）
- 新增 `backend/app/services/derived_trace.py`：派生 Trace 落盘 + 诚实来源标记
- 新增 `/api/lineage/trace|evaluation|experiment|repair|consistency` 5 个只读端点
- 新增前端 `LineagePanel` / `LineageDrawer`，接入 Trace 详情、Evaluation Center 案例抽屉、Counterfactual Lab、Repair Studio
- **数据来源双维度**：`origin`（REAL_API / MOCK / SIMULATED / UNKNOWN）× `storage`（RECORDED / MEMORY / NOT_AVAILABLE）
  - 严格依据 metadata 判定，不依据页面名/ID 前缀；缺 data_source 时**绝不**推断 REAL_API
  - `RECORDED` 只表示已落盘，不代表真实 API
- **修复 4 个真实一致性问题（全部 FIXED）**：
  - P1-7 实验两套 `experiment_id` → 引擎接收登记表 ID
  - P1-8 派生 Trace `trace-cf-*` 404 → 落盘（`data_source=simulated` + `derived_from`）
  - P1-9 修复候选 `repair_trace_id` 404 → 同 P1-8 + `derivation=repair_validation`
  - P1-10 评测 Trace 缺 `data_source` → 落盘时补来源元数据
- 一致性实测：488 份 Trace 全量检查 **0 issue**
- 文档 `docs/DATA_LINEAGE.md`（对象/ID 规则/来源标记/追溯路径/一致性/限制）
- 测试：后端 **385 passed**（+28）；浏览器 **53 passed**（+4）

## 第 45 步新增：Red Team 测试生成器

- 新增 `backend/app/services/red_team_generator.py`：六类受控安全测试生成 + 确定性判定
- 六类：`prompt_injection` / `tool_misuse` / `permission_boundary` / `invalid_arguments` / `conflicting_instructions` / `fault_recovery`（10 个模板）
- 变体机制：输入位置（prefix/middle/suffix）× 输入格式（plain/quoted/json/markdown）× 冲突形式 × 故障形态；`random.Random(seed)` 决定，**同种子完全复现**（case_id 由内容哈希派生）
- 生成阶段安全校验：拒绝真实凭据 / 项目外路径 / Shell 执行结构；允许「看起来危险但只是文本」的载荷（只作数据）
- 受控执行：复用白名单本地工具 + `FaultInjector` + **模拟权限系统**；三道门（权限/白名单/参数键）；不可信 payload 只记入 `untrusted_material`
- **安全与任务两指标严格分离**：`safety_pass_rate` 与 `task_success_rate` 独立统计，报告明示不得互相替代
- **金丝雀负控**（2 个）：伪造「已放行」的越权执行，必须被判 UNSAFE，用于证明检测器非空转 → 实测 2/2 检出
- 集成：数据集 `data/evaluation/redteam/redteam_v1.json` 可被 `evaluation_engine.load_dataset` 直接读取；每案例生成独立 Trace（`trace-rt-*`，`data_source=simulated`），可查 DAG/血缘
- API：`/api/redteam/{types,generate,dataset,runs,runs/{id}}` 6 端点
- 前端：`/redteam` Red Team Lab（类型选择/变体/种子/金丝雀开关、4 指标卡、按类型统计、案例详情与约束逐条结果、Trace/DAG 跳转）
- **实测（seed=42, variations=2）**：28 案例（26 攻击 + 2 金丝雀）、安全 26/26、任务 16/26、检测器已验证、0 执行错误、0 付费调用
- 修复真实缺陷 3 个：P1-11（前置故障从未触发）、P2-15（金丝雀 Trace 未落盘）、P2-16（安全判定 3 类系统性误报，通过率 46%→100%）
- 文档 `docs/RED_TEAM_GENERATOR.md`
- 测试：后端 **423 passed**（+38）；浏览器 **57 passed**（+4）

## 第 46 步新增：Adversarial Evaluation Lab

- 后端 `/api/red-team/*` 9 个交互接口（类型/生成/运行/详情/案例/单案例/Bad Case/覆盖率/列表）
- 安全限制：生成 ≤120、执行 ≤60、变体 ≤4、载荷 ≤1200 字符、并发 1（超出 429）、受控沙盒、付费 API 禁用
- 生成器扩展：难度筛选（`difficulties`）、按 `case_ids` 选择执行、`compute_coverage()` 覆盖率地图
- **覆盖率严格口径**：`passed + failed + unknown == executed`；未执行与 UNKNOWN 均不并入通过/失败；`coverage_status ∈ {FULL, PARTIAL, NOT_COVERED}`
- 前端 `/adversarial` Adversarial Evaluation Lab：类型/难度复选、变体数、种子、生成、全选/清空、运行所选、4 指标卡、**覆盖热力地图 + 七列明细**、**Bad Case Explorer（风险类型/执行状态/安全状态/失败原因四维筛选）**、证据面板、Trace/DAG 跳转、数据来源标记
- 修复缺陷 P2-18：`case_count` 与案例列表口径不一致 → 统一为已执行总数并单列 attack/canary 计数
- 测试：后端 **448 passed**（+25）；浏览器 **61 passed**（+4）；前端构建通过
- 文档 `docs/ADVERSARIAL_LAB.md`
## 第 47 步新增：自动化 Bad Case 归因（Automated Bad Case Attribution）

- 新增 `backend/app/services/bad_case_attribution.py`
  - 10 类归因：TOOL_TIMEOUT / TOOL_EXCEPTION / INVALID_ARGUMENTS / INVALID_TOOL_RESULT / EMPTY_RETRIEVAL / PERMISSION_DENIED / TASK_INCOMPLETE / OUTPUT_MISMATCH / PROCESS_VIOLATION / UNKNOWN
  - 5 级证据：CONFIRMED_EVENT / OBSERVED_DIFFERENCE / POTENTIAL_CAUSE / INTERVENTION_SUPPORTED / UNKNOWN
  - 支持批量（≤20）、单案例（仅 Bad Case，否则 409）、单 Trace 三种入口
  - 报告持久化 `data/attribution/reports/attr-*.json`（可查询）
  - **诚实性**：DAG 依赖强制 POTENTIAL_CAUSE 且注明不构成因果；不输出单一 root_cause；不生成数字置信概率；实验引用必须真实 ID + 控制条件；无证据返回 UNKNOWN
- `/api/attribution/*` 9 个端点（类型/批量/单案例/单Trace/报告列表/详情/证据/关联Trace/关联实验）
- **受控失败数据集** `tracepilot-fault-v1`（8 案例，synthetic）用于产生可归因 Bad Case；已加入白名单
- 前端：`AttributionPanel.tsx`（证据图 5 类关系分层 + 归因抽屉 + 批量面板），接入 Evaluation Center（批量卡片 + Bad Case 抽屉「自动归因」按钮）
- 实测：6 Bad Case 全部归因；证据等级 CONFIRMED 9 / POTENTIAL 4 / OBSERVED 1；根因状态 MULTI_ANOMALY 4 / SINGLE_CONFIRMED 1 / UNKNOWN 1；批量 1.36s（冷）/ 0.05s（热）
- 修复 3 个真实缺陷：P1-12（数据集端点硬编码）、P1-13（归因全量扫描 Trace 极慢）、P2-20（非 Bad Case 也能归因）
- 文档 `docs/BAD_CASE_ATTRIBUTION.md`
- 测试：后端 **479 passed**（+31）；浏览器 **65 passed**（+4）；前端构建通过
## 第 48 步新增：Failure Pattern Mining（失败模式挖掘）

- 新增 `backend/app/services/failure_pattern_mining.py`
  - **5 维指纹**：failure_type / tool_name / failed_metric / recovery_status / scenario_type（缺失为 null，不猜测；`fp-` + sha256[:12] 可复现）
  - 失败类型优先级：具体故障类型优先于泛化 TASK_INCOMPLETE
  - 恢复状态依据 `metadata.recovery_observed` 或**步骤顺序证据**（失败后是否有成功调用）
  - 统计分母显式记录（total/executed/bad/analyzed/unknown）；未执行不计入；UNKNOWN 独立计数
  - 运行对比（按 fingerprint_id 对齐：NEW/RESOLVED/INCREASED/DECREASED/UNCHANGED）
  - 语义聚类扩展接口 `register_cluster_strategy()`
- `/api/failure-patterns/*` 8 个端点（类型/创建/报告列表/报告/模式详情/模式案例/模式Trace/对比）
- 前端 `FailurePatternExplorer.tsx` 嵌入 Evaluation Center：分布图、高频排行、类型/工具双筛选、代表性案例、**归因证据下钻**、Trace/DAG 跳转、双运行对比、分母透明声明
- 实测：6 模式 / 分析 6 Bad Case（total 8, executed 8, bad 6）；同数据集两次运行对比 6 项不变（确定性可复现）；付费 API 0
- 修复 3 个真实缺陷：P1-14（归因未落盘导致证据下钻 404）、P2-22（分析在运行完成前触发且不重试）、P2-23（证据按钮不打开抽屉）
- 文档 `docs/FAILURE_PATTERN_MINING.md`
- 测试：后端 **505 passed**（+26）；浏览器 **69 passed**（+4）；前端构建通过
## 第 49 步新增：Evaluation Version Comparison（评测版本对比）

- 新增 `backend/app/services/evaluation_version_comparison.py`
  - 版本描述：`ev-ver-` + 内容哈希；`agent_version` 缺失显式说明（不伪造）；数据集文件 sha256 指纹
  - 三档可比性：`COMPARABLE` / `COMPARABLE_WITH_WARNINGS` / `NOT_COMPARABLE`（13 类问题码；双方未启用 Judge 时 Rubric 记为不适用）
  - 指标差异：保留分子/分母与口径；**pp（百分点）与相对变化分开**；标量指标分子分母为 null；`UNKNOWN` 不当 0
  - Case-level Diff：FAIL→PASS / PASS→FAIL / PASS→PASS / FAIL→FAIL / UNKNOWN_INVOLVED；缺失案例单列不配对
  - 失败模式分布变化（复用第 48 步）+ Trace/DAG 链接
- `/api/version-comparison/*` 7 端点；报告持久化 `data/version_comparison/reports/`
- 前端 `/version-compare`：版本选择、可比性横幅、双版本元数据、指标差异卡、维度差异表、Case Diff（含迁移筛选/缺失案例/Trace 跳转）、失败模式变化
- 修复 3 个真实缺陷：P2-25（标量指标崩溃）、P2-26（同版本两次运行被去重 → `force_new`）、P2-27（Rubric 不适用误报为缺失）
- 实测：同版本两次运行 `COMPARABLE` 0 issue；case counts common 8 / stable_fail 6 / unknown 2；跨数据集 `NOT_COMPARABLE`；对比首次约 3.0s；付费 API 0
- 文档 `docs/EVALUATION_VERSION_COMPARISON.md`
- 测试：后端 **536 passed**（+31：版本对比 11 + 回归检测 17 + Trace 列表缓存 3）；浏览器 **80 passed**（+11）；前端构建通过

## 第 50 步新增：Regression Detection（回归检测）

- 新增 `backend/app/services/regression_detector.py`
  - 8 条默认规则：NEW_SAFETY_FAILURE / TASK_SUCCESS_DROP / TOOL_CORRECTNESS_DROP / RECOVERY_RATE_DROP / LATENCY_INCREASE / COST_INCREASE / CASE_REGRESSION / NEW_FAILURE_PATTERN
  - 阈值来源显式标注 `PROJECT_DEFAULT_NOT_INDUSTRY_STANDARD`（项目默认、非行业标准，可覆盖）
  - 规则状态 5 类：TRIGGERED / NOT_TRIGGERED / NOT_EVALUABLE / NOT_APPLICABLE / DISABLED；噪声抑制（`min_absolute`）；严格方向避免阈值 0 误触发
  - 风险等级取最高严重度；不可比 → UNKNOWN；**不含概率**
  - 质量门禁：`BLOCK`（阻断规则）/ `WARN`（非阻断退化或证据缺失）/ `PASS` / `INCONCLUSIVE`（不可比、全禁用、全不可求值）——INCONCLUSIVE 绝不等于 PASS
- `/api/regression/*` 9 端点；报告持久化 `data/regression/reports/`
- 前端 `/version-compare` 回归区：**回归风险矩阵（6 风险维度 × 4 严重度）** + 规则求值明细 + 触发/不可求值/禁用三栏 + 证据引用
- 修复 3 个真实缺陷：P2-30（禁用规则误记为不可求值）、P2-31（阈值 0 误触发）、P2-32（基线为 0 的相对变化静默跳过）
- 实测：真实同版本运行（隔离计时噪声）`LOW/PASS`；跨数据集 `UNKNOWN/INCONCLUSIVE`；受控场景覆盖 -37.5pp→BLOCK、安全 -12.5pp→CRITICAL、聚合改善但单案例回归→BLOCK、全禁用→INCONCLUSIVE；付费 API 0
- 文档 `docs/REGRESSION_DETECTION.md`
- 测试：后端 **536 passed**；浏览器 **80 passed**；前端构建通过
- 浏览器全量验收附带修复 P2-34：`/api/traces` 4300+ Trace 时请求超时（4.1s→缓存后冷启 5.2s/热态 1.5s），`trace_store.list_traces()` 增加目录签名缓存，新增 `test_trace_store_cache.py` 3 项

## 第 51 步新增：CI/CD Quality Gate（CI 质量门禁）

- 新增 `backend/app/services/quality_gate.py`：确定性门禁引擎
  - 输入：评测报告（真实受控运行）+ 版本对比报告（第 49 步）+ 回归检测结果（第 50 步）+ YAML 配置
  - 11 项检查：任务成功率阈值、工具正确率阈值、**新增安全失败**（分子分母计算）、案例级回归、数据完整性、版本可比性、必需数据集版本、必需 Rubric 版本、耗时上升、Token 成本上升、回归阻断规则
  - 检查项均含 `rule_id / status / severity / threshold{value,unit,calculation} / actual / evidence_refs`
  - 四态：`PASS / WARN / BLOCK / INCONCLUSIVE`，合并优先级 BLOCK > INCONCLUSIVE > WARN > PASS
  - **证据缺失不自动放行**（`fail_on_missing_evidence`：true→BLOCK，false→WARN）；版本不可比 → INCONCLUSIVE
- 新增可版本管理配置 `config/quality_gate.yaml`：9 个需求阈值 + 判定下限 + `warn_exit_code`；阈值来源标注 `PROJECT_DEMO_DEFAULT_NOT_INDUSTRY_STANDARD`；配置内容哈希指纹写入报告；配置错误识别（未知字段/类型/范围/YAML/文件不存在）
- 新增 `/api/quality-gate/*` 6 端点（config / runs / reports / report / markdown / checks）
- 新增 `scripts/ci_quality_gate.py` 本地/CI 运行器：固定数据集 → 确定性 Mock 评测 → 版本对比 → 回归检测 → 门禁 → JSON+Markdown 报告 → 退出码；整体超时与数据集白名单限制
- 新增 `.github/workflows/agent-evaluation.yml`（checkout / Python / 后端测试 / Node / 前端构建 / 确定性评测 / 版本对比 / 回归检测 / 门禁 / 保存报告 / CI 摘要）
  - **云端 CI 状态：NOT_VERIFIED**——仓库未配置 GitHub 远程仓库（`git remote -v` 为空），工作流未实际执行，仅完成 YAML 语法校验
- 前端复用 `/version-compare`：新增「CI Quality Gate」卡片（状态/退出码/CI Run ID/触发规则/阈值与实际值/关联评测/证据入口/限制声明）
- 修复 2 个真实缺陷：P2-36（CLI 超时判定混用墙钟与单调钟导致误报超时）、P2-37（不可比场景数据完整性重复判 BLOCK，改为可比性 INCONCLUSIVE）
- 本地实测：默认命令 `PASS`（退出码 0，JSON+Markdown 落盘）；`tracepilot-fault-v1` + 阈值 0.90 → `BLOCK`（退出码 1，触发 `GATE_TASK_SUCCESS_BELOW_THRESHOLD`）；配置错误 → 退出码 3；浏览器门禁面板 PASS/BLOCK/INCONCLUSIVE 均已验证
- 文档 `docs/CI_QUALITY_GATE.md`
- 测试：后端 **565 passed**（+29：门禁 25 + 本地 CI 脚本 4）；浏览器 **82 passed**（+2）；前端构建通过；付费 API 0 次

## 第 52 步新增：Automated Evaluation Pipeline（自动化评测流水线）

- 新增 `backend/app/services/evaluation_pipeline.py`：11 阶段确定性编排（复用既有引擎，不重复开发）
  - 阶段：数据集验证 → Agent 执行 → Trace 采集 → 确定性评测 →（可选）Judge / Bad Case 归因 / 失败模式挖掘 / 版本对比 / 回归检测 → 质量门禁 → 报告生成
  - 状态 7 类：PENDING / RUNNING / SUCCEEDED / FAILED / SKIPPED / BLOCKED / CANCELLED；**SKIPPED 绝不标记 SUCCEEDED**；依赖失败 → BLOCKED；失败/取消/超时仍生成报告
  - 可复现性：pipeline_run_id / dataset_version / agent_version / prompt_version / evaluator_version / rubric_version / execution_mode / random_seed / config_fingerprint / created_at（缺失为 null + 原因）
  - 数据一致性：真实引用 evaluation_run / agent_run（real_api）/ trace / bad_case / attribution / pattern / comparison / regression / quality_gate；不修改原始 Trace
  - 执行控制：创建 / 启动 / 状态 / 阶段 / 报告 / 取消 / 历史；有限重试（≤2，不无限重试）
  - 安全限额：案例 ≤50、并发 1、队列 ≤10、运行时长 ≤300s、输入长度限制；默认 Mock，real_api 需双授权（未授权 403、零调用）
- 抽取 `evaluation_engine.build_evaluation_report_from_results()` + `persist_trace()`（无行为变更，既有测试回归通过）
- `evaluation_store` 新增 `register_report()` / `update_report()`：登记流水线真实产生的评测报告（不重新执行）
- 新增 `/api/pipelines/*` 8 端点（meta / create / start / cancel / list / get / stages / report）
- 新增前端 `/pipelines` Evaluation Pipeline（Pipeline Run Inspector：左侧配置 / 中部可交互阶段流程图 / 右侧阶段详情与日志 / 底部最终报告与证据入口 + 历史流水线；PC/平板/手机）
- 修复 2 个真实缺陷：P2-40（终态在 finalize 完成前发布，查询方可能看到「成功但报告路径为空」的竞态）、P2-41（未选中流水线时历史列表被报告条件隐藏）
- 实测：本地实际运行 **179 条**流水线（覆盖成功/失败/超时/取消/重试场景）；HTTP 最终验证 3 条全部 SUCCEEDED（无基线 / 带基线 PASS / 故障集归因与模式），付费 API 0 次
- 文档 `docs/AUTOMATED_EVALUATION_PIPELINE.md`
- 测试：后端 **596 passed**（+31：流水线 31）；浏览器 **88 passed**（+6）；前端构建通过

## 第 53 步新增：Token & Cost Analytics（Token 与成本分析）

- 新增 `backend/app/services/token_analytics.py`：Token 采集 / 价格配置 / 成本与延迟计算 / 成本归因
  - 用量来源四分：`PROVIDER_REPORTED / ESTIMATED / SIMULATED / UNKNOWN`；Provider 未返回用量 → `null`（**不填 0**）
  - 成本状态五分：`ACTUAL_USAGE_PRICED / ESTIMATED / SIMULATED / PRICE_UNKNOWN / UNKNOWN`；`ACTUAL_USAGE_PRICED` 仍只是「Provider 用量 × 本地价格表」的估算，**不是实际账单**
  - 输入/输出分别计价（`prompt_tokens / 1e6 × input_price + completion_tokens / 1e6 × output_price`），Decimal 计算并量化到 8 位小数；缓存分桶未配置缓存价 → `PRICE_UNKNOWN`
  - 聚合状态优先级 `PRICE_UNKNOWN > UNKNOWN > ESTIMATED > SIMULATED > ACTUAL_USAGE_PRICED`；UNKNOWN/PRICE_UNKNOWN 不计为 0、不进平均成本
  - 延迟：`model_latency_ms / tool_latency_ms / total_run_latency_ms / evaluation_latency_ms / pipeline_latency_ms`，无法可靠计算为 `null` + `unknown_fields`
  - 归因：model_call（唯一计费单元）→ trace → agent_run → evaluation → pipeline；流水线无阶段级分摊时返回 `stage_allocation_available: false`（不伪造）
- 新增版本化价格配置 `config/model_pricing.yaml`（provider/model/currency/输入输出单价/缓存单价/生效日期/pricing_version/pricing_source）
  - **当前未配置可信官方价格**：DeepSeek 条目价格 null → 成本 `PRICE_UNKNOWN`（不编造）；模拟执行器 0 价标记 `SIMULATED_ZERO_COST`
- Trace 采集扩展（不修改 Trace Schema）：step `metadata.usage_meta`（provider/model/usage_source/raw_usage/recorded_at）
  - 真实 API：`basic_agent._chat` 从 `response.usage` 读取并保留原始字段；mock/评测模拟器写 `SIMULATED`
- 新增 `/api/cost/*` 10 端点：pricing / overview / models / versions / unknown / attribution（支持 `usage_source` 过滤）/ traces / runs / evaluations / pipelines
- 新增前端 `/costs` Cost Analytics 工作台：顶部 KPI（Token / 成本估算+状态 / 延迟 / 覆盖率 / UNKNOWN）、成本口径声明、成本趋势、模型成本分布、延迟分析、数据来源与价格版本、版本成本对比、Cost Attribution Explorer、单对象查询、UNKNOWN 与缺失数据清单；PC/平板/手机响应式
- 性能：Trace 目录签名 30s 缓存 + 文件级增量解析/分析缓存 + 单飞锁（并发请求只重算一次）；实测 9,766 Trace 冷启动约 33s、热态 0–4s
- 修复 3 个真实缺陷：P2-42（并发冷启动重复全量分析导致 overview 超时）、P2-43（归因用量来源硬编码/不可过滤）、P2-44（本地 CI 脚本输出编码依赖环境）；浏览器验收附带修复 P2-48（批量归因/失败模式分析前端 15s 超时过短，并发负载下误报「请求超时」→ 显式 120s）
- 额外修复：step 显式声明其他 provider/model 且无价格时曾回退套用 Trace 级 0 价 → 现在严格 `PRICE_UNKNOWN`（回归测试锁定）
- 文档 `docs/TOKEN_COST_ANALYTICS.md`
- 测试：后端 **623 passed**（+27：`test_token_analytics.py`）；浏览器 **93 passed**（+5：cost spec 3 + 页面/响应式 2）；前端构建通过；付费 API 0 次
- **真实 API 用量验证状态**：仅历史 1 次（Trace `trace-4d3d82dc7a67496dbc2842cd3b31aa47`，Provider 报告 1852 Token）；因无官方可信价格，该 Trace 成本为 `PRICE_UNKNOWN`，不展示任何「实际账单/扣费金额」

## 第 54 步新增：Cost–Latency–Quality Pareto Analysis（成本–延迟–质量帕累托分析）

- 新增 `backend/app/services/pareto_analysis.py`：三维帕累托分析引擎
  - 候选构建：从 `evaluation_store` 已完成评测运行读取；质量=`task_success_rate`、延迟=`average_duration_ms`、成本=CostAnalytics 平均每任务成本；`agent_version` 缺失为 null + 原因（不伪造）
  - 可比性 9 项检查：数据集一致（按 dataset_id+version 分组，自动选择最大可比组，组外候选排除并警告）、评分规则、指标定义、执行模式、币种、价格版本、样本量、数据来源兼容、缺失指标；真实 API 与模拟数据混用 → `NOT_COMPARABLE`，不生成前沿
  - 支配定义：质量更高/相等、延迟与成本更低/相等且至少一项严格更优；三项完全相同 → 互不支配、并列进入前沿（`tie_group`）；缺失指标不参与支配计算（UNKNOWN 不当 0）
  - 输出：非支配解（frontier）、被支配解、支配关系（严格更优维度 + 差值）、并列组、排除原因、结构化报告；报告落盘 `data/pareto/reports/`
  - 交互约束：`minimum_quality / maximum_latency_ms / maximum_cost / required_safety_gate / dataset_version`；不满足时明确返回 `NO_FEASIBLE_CANDIDATE`（另有 `NO_COMPARABLE_CANDIDATE` / `INSUFFICIENT_DATA` / `NO_CANDIDATE`）
- 新增 `/api/pareto/*` 10 端点：candidates / analyses（创建·列表·查询）/ frontier / dominated / dominance / filter / 关联 Evaluation Run / 关联 Trace
- 新增前端 `/pareto` Interactive Pareto Explorer：
  - 左侧约束面板；中部自绘 SVG 三维散点（拖拽旋转、旋转/俯仰滑杆、悬停指标、点击选中）+ 二维投影切换（质量-延迟/质量-成本/延迟-成本，移动端友好）；右侧候选详情（指标/来源/可比性/成本口径/Evaluation Run/Trace 跳转）；底部 Frontier、被支配解、支配关系、排除与警告
  - 顶部 KPI、可比性横幅、NO_FEASIBLE_CANDIDATE 等状态横幅；专业浅色 SaaS 风格，PC/平板/手机响应式
- 诚实性：全部候选来自本地真实评测运行；当前评测为受控模拟执行器，明确声明不代表真实模型性能；模拟成本 0 属于 `SIMULATED_ZERO_COST`（不是真实计费）；全流程 `paid_api_called=false`（0 次付费调用）
- 历史技术债：P0 = 0 / P1 = 0 剩余，本步无需修复；与成本/版本对比/评测指标相关的历史 P2 问题已在前序步骤修复（P2-25~P2-27、P2-30~P2-32、P2-42/P2-43 等）
- 本步修复 3 个真实缺陷：P2-49（跨数据集候选污染可比组判定）、P2-52（约束切换数据集时沿用过期的不可比状态）、P2-51（Trace 列表在全站并发下全量重复解析导致请求超时）
- 新增技术债：P2-49（候选池依赖内存评测运行，重启后候选为空）、P3-32（三维散点为自绘等距投影，无 WebGL/刻度缩放）、P3-33（安全门禁状态仅来自已落盘报告，多数候选无门禁记录）
- 文档 `docs/PARETO_ANALYSIS.md`
- 实测（全量验收后）：候选 20（eval 16 / fault 4）、可比 16、可行 16、非支配 1、被支配 15、支配关系 100、并列 0；fault+最低成功率 1 → NO_FEASIBLE_CANDIDATE；门禁 PASS 筛选 → 可行 1；全部为模拟评测数据（非真实模型性能）
- 测试：后端 **643 passed**（+20：`test_pareto_analysis.py`）；浏览器 **98 passed**（+5：pareto spec 3 + 页面冒烟 2）；前端构建通过；付费 API 0 次

## 第 55 步新增：需求回溯与缺口审计

- 原始需求 PDF 已用 `pypdf` 成功提取（12 页 / 16,914 字符），不是猜测或转述；审计文件 `docs/REQUIREMENTS_AUDIT.md`
- 建立 74 条需求/验收项追踪矩阵：PRD 功能 11 项、20 项 Agent 能力、7 个创新点、验收/交付 8 项、12 个阶段、CONV 追加 15 项
- 修复 P1 缺口：
  - **根因准确率指标**：新增 `backend/app/services/rootcause_metrics.py` + `/api/attribution/metrics` + Evaluation Center 指标卡；Top-1/Top-3（类型与步骤级）、误归因率、平均诊断耗时/成本、多数类与随机基线；金标准为受控故障集标注（合成数据）
  - **能力缺失**：新增 `backend/app/services/capability_suite.py` + `backend/app/tools/capability_tools.py`：RAG（本地知识库）、Memory（运行内）、Skill（复合查询）、Context Review、Context Engineering、Reflection（可选）、Explainability（证据化解释，不保存思维链）、HITL（审批工作流）、Execution Environment（工具超时）
  - **能力矩阵**：`GET /api/capabilities/matrix` + Architecture 页 20 能力/7 创新点矩阵（状态、实现、证据、限制；15 COMPLETE / 5 PARTIAL 不拔高）
  - **HITL API**：`/api/hitl/approvals` 创建/列表/查询/批准拒绝（PENDING→APPROVED/REJECTED，含审计字段，不自动批准）
  - **Trace 决策解释**：`GET /api/traces/{id}/explanation` + Trace 详情卡（证据化理由，显式 `is_chain_of_thought=false`）
  - **部署配置**：`Dockerfile` / `docker-compose.yml` / `frontend/Dockerfile` / `nginx.conf` / `docs/DEPLOYMENT.md`（本机无 Docker，运行时 NOT_VERIFIED）
- P0 重新确认：真实 DeepSeek API 端到端仅 1 次历史手工验证，无自动化真实 API 测试/CI；受「不授权不付费」边界限制，标记 **BLOCKED（未修复）**，未伪造真实调用证据
- 其他未完成：统计置信区间/剪枝对照实验（P2-55）、报告导出 UI/PDF（P2-54）、容器运行时验证（P2-53）、因果代数形式化（P3-34）、RAG/Skill L3 面板（P3-35）、作品集 PDF/演示视频（P3-36）
- 新增测试：后端 **+18**（`test_capability_suite.py` 13 + `test_rootcause_metrics.py` 5）；浏览器 **+4**（`e2e/capabilities.spec.ts`）
- 文档：`docs/REQUIREMENTS_AUDIT.md`、`docs/DEPLOYMENT.md`，更新 `docs/BROWSER_ACCEPTANCE.md` / `FEATURE_AUDIT.md` / `TECH_DEBT.md`

## 第 55-A 步新增：数据治理与可信度修复

- 备份：`backups/step55a_20261119_183831/`（16,414 文件 / 94.2 MB，两个 SQLite `integrity_check=ok`，SHA256 清单）；Git 检查点分支 `fix/step55a-data-governance`（起点 `84025e0`）
- 来源分类（全库 16,408 = 主目录 8,618 + 评测目录 7,790）：REAL_API **0** / MOCK **895** / SIMULATED **13,147** / REPLAY **2,365** / UNKNOWN **1**；重复 trace_id 0、坏文件 0
  - 历史唯一真实调用 `trace-4d3d82dc…` 缺少 `real_api_verified` 独立证据 → 归 UNKNOWN，不自动升级
- 统计口径统一：Overview / Trace Explorer 显式只统计主 Trace 目录；Cost Analytics 显式全库；差异 = 7,790 条评测 Trace（无重复计入）
- 修复 3 个真实 P1 问题：
  - 测试污染生产 Trace 目录：一次完整测试会话写入 565 条（SIMULATED 452 / MOCK 22 / REPLAY 91）→ 已隔离到 `data/governance/quarantine/`（可回溯 manifest）+ `tests/conftest.py` 会话级隔离 fixture 防复发
  - Trace 详情把 `data_source=real_api` 自动升级为 REAL API → 改为后端分类 + 保守规则（缺验证标记 → UNKNOWN）
  - 失败 Trace 不可筛选/口径混乱 → `/api/traces` 增加来源/状态/故障过滤与全量计数，前端增加筛选 + 分页 + 空态
- 验收期间另修复 2 个真实性能/健壮性缺陷：流水线成本归因对失效 Trace 引用逐条强制全库重载（>5 分钟 → 秒级，P2-59 FIXED）、来源分类接口目录变化时同步全量扫描阻塞页面（改为 stale-while-revalidate + 单飞，P2-60 FIXED）
- 真实本地执行 MOCK 失败 Trace 验证：`trace-f3b36afd4f034bbea8715fd64a3b1719`（7 步；`text_analyzer` 真实执行成功、`calculator` 注入 `tool_timeout` 失败、最终回答明确失败；来源 MOCK；四类受控实验资格全部可用；`paid_api_called=false`）
- 新增 API：`/api/governance/sources`、`/api/governance/scan`、`/api/governance/failed-summary`、`/api/governance/traces/{id}`、`/api/governance/traces/{id}/eligibility`；`/api/traces`、`/api/cost/overview`、`/api/cost/attribution` 支持 `source_class`
- 新增脚本：`scripts/audit_data_sources.py`、`scripts/quarantine_test_pollution.py`、`scripts/create_mock_failure_trace.py`
- 前端：Overview 数据口径 + 来源分类表 + 失败口径；Trace Explorer 筛选/分页；Trace 详情来源分类 + 四类资格；Cost Analytics 来源范围 + UNKNOWN/PRICE_UNKNOWN 不按 0 展示
- 测试：后端 **685 passed**（661 → 685，+24 `tests/test_data_governance.py`）；浏览器 **106 passed**（102 → 106，+4 `e2e/data-governance.spec.ts`）；前端构建通过；付费 API 0 次
- 文档：`docs/STEP55A_DATA_AUDIT.md`
- 未完成：Fault Lab / Counterfactual / Repair 尚未按资格过滤（P2-56）；报告类数据目录仍被测试写入（P2-57）；`/api/traces` 未做服务端分页（P2-58）

## 尚未完成

- **P0（BLOCKED）**：真实 Agent 执行链仅 1 次历史手工验证，无自动化真实 API 测试；需用户显式授权后补测（默认关闭、0 次付费调用）
- 帕累托候选池依赖内存评测运行，重启后为空；真实 API 评测运行缺失，当前不存在真实模型候选（P2-49 / 沿用 P2-46）
- 三维散点为自绘等距投影，无 WebGL/坐标轴刻度缩放（P3-32）；安全门禁筛选依赖已落盘门禁报告（P3-33）
- **无官方可信价格**：真实调用成本为 `PRICE_UNKNOWN`，需用户填入有来源/生效日期的官方价格后才能得到估算（P2-45）
- 成本分析冷启动仍为全量计算（约 33s），未持久化分析索引（P3-30）
- 已授权的 real_api 流水线执行路径未实际验证（P3-28，NOT_VERIFIED）
- 评测/对比/回归/门禁/流水线记录持久化（当前主要内存，重启后部分证据不可查；见 P2-28 / P2-38 / P2-39）
- 失败模式语义聚类（P2-24）、回归阈值按项目标定（P2-33）
- **GitHub 云端 CI 尚未验证**（P3-26；需配置远程仓库后实际运行一次工作流）
- Docker 容器运行时验证（配置已提供，本机无 Docker：P2-53）
- 报告导出 UI（JSON/CSV/PDF）、统计置信区间与剪枝对照实验（P2-54 / P2-55）
- RAG/Skill L3 专属可视化、因果代数形式化、作品集 PDF/演示视频（P3-34~P3-36）
- Fault Lab / Counterfactual Lab / Repair Studio 尚未按「受控实验资格」过滤候选（P2-56；资格 API 已就绪）
- 报告类数据目录（attribution / failure_patterns / pipelines / quality_gate / regression / version_comparison / pareto / rootcause_metrics）仍会被测试写入（P2-57）
- `/api/traces` 默认返回主目录全部 8,618 条摘要，分页在前端完成；服务端分页待做（P2-58）
- 大 Trace 前端性能优化、视觉回归/跨浏览器/a11y 扫描

## 技术债

详见 `docs/TECH_DEBT.md`：P0 = 1（BLOCKED，需真实 API 授权）｜P1 = 0 剩余｜P2 = 39｜P3 = 38。

## 浏览器交互验证状态

**已执行**：Playwright 1.65.1 + Microsoft Edge（`channel: 'msedge'`）覆盖 19 个真实页面、
主要用户流程、响应式（PC/平板/手机）与第 43–55-A 步各专题交互；当前 **106 passed / 0 failed**。
未覆盖项：视觉回归、跨浏览器（Chrome/Firefox/Safari）、完整 a11y 扫描。
