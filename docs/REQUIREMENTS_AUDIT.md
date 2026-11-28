# REQUIREMENTS AUDIT（第 55 步：需求回溯与缺口审计）

- 审计日期：2026-11-19
- 审计范围：原始需求 PDF + 项目全部路由/API/测试/Git 历史 + 第 1–55 步对话与开发规划
- 审计方法：原始 PDF 逐条提取 → 文档/代码/测试/Git 交叉核对 → 全量测试与浏览器验收复核 → 缺口分级 → 修复与回归
- 判定原则：**代码存在 ≠ 功能完成**。只有「真实实现 + 自动化测试证据」才判 `COMPLETE`；
  受限实现判 `PARTIAL`；有代码但未运行验证判 `UNVERIFIED`；未实现判 `MISSING`。

## 1. 原始需求来源核实

原始需求文件：`C:\Users\yyxxy\Desktop\TracePilot 项目二：完整产品需求、技术架构、研究创新与逐步开发交付总方案.pdf`
（位于项目目录的上一级 `Desktop`，大小 864,464 字节，12 页）。

- 已通过 `pypdf` 成功提取全文（16,914 字符），并逐节核对：项目定位、PRD/用户故事、20 项能力矩阵、
  7 个创新点、因果归因边界、四层架构、数据/测试集、评测指标、模型与工具选型、智能体开发工具使用、
  12 个开发阶段、风险与成本、引用与待确认项。
- **结论：原始需求文档可访问，本次审计不依赖猜测**。PDF 中截图内容与部分外部引用标注为
  「待核对/待确认」，涉及具体模型版本、部署云环境等，本审计对这类事项显式标注「原始需求待确认」。

需求来源分为两类，矩阵中逐项标注：

| 来源代码 | 含义 |
| --- | --- |
| `PDF` | 原始需求 PDF 明确要求/列出（第 3/4 节能力与创新点、PRD 功能列表、验收标准、12 阶段等） |
| `CONV` | 第 1–55 步对话与开发规划推断/追加的需求（Judge、多 Judge、红队、对抗评测、版本对比、回归、门禁、流水线、成本分析、帕累托、能力审计等），PDF 未逐条列出 |

## 2. 需求–实现追踪矩阵

### 2.1 PRD 功能列表（PDF 第二节）

| # | 需求 | 来源 | 实现 | 测试/证据 | UI | 状态 |
|---|------|------|------|-----------|----|------|
| A1 | Trace 采集（每轮输入/输出/工具调用/结果） | PDF | `trace_collector.py` + Trace Schema | `test_trace_schema/test_trace_collector` | Trace Explorer / 详情 | COMPLETE |
| A2 | 依赖建模为 DAG | PDF | `trace_dependency.py` / `trace_dag.py` | `test_trace_dag` | DAG Analysis + Time Travel | COMPLETE |
| A3 | 根因搜索（干预候选步骤并比较结果） | PDF | `bad_case_attribution.py` + `counterfactual_engine.py` | `test_bad_case_attribution` / `test_counterfactual_engine` | Fault Attribution / Counterfactual Lab | PARTIAL（候选+干预，但非穷举/统计搜索） |
| A4 | 最小修复生成与验证 | PDF | `minimal_repair.py` | `test_minimal_repair` | Repair Studio | PARTIAL（启发式有限候选，非全局最优） |
| A5 | 置信度评估（统计/重采样） | PDF | 证据等级 + 质量门禁四态 | `test_quality_gate` | 归因/门禁面板 | PARTIAL（刻意不输出无来源概率与置信区间） |
| A6 | 门禁控制（低可信归因拦截） | PDF | `quality_gate.py`（11 检查/BLOCK/WARN/INCONCLUSIVE） | 门禁 25 项 + CI 脚本 4 项 | Version Compare 门禁卡 | PARTIAL（规则门禁，不是统计显著性门限） |
| A7 | 故障注入与自动测试生成 | PDF | `fault_injector.py`（6 类）+ fault 数据集 + Red Team Generator | `test_fault_injector` / `test_red_team*` | Fault Lab / Red Team Lab | COMPLETE |
| A8 | 评测指标统计（Top-1/Top-3、修复率、误归因、平均成本） | PDF | **第 55 步新增** `rootcause_metrics.py` + `/api/attribution/metrics` | `test_rootcause_metrics.py` | Evaluation Center「根因准确率评估」卡 | COMPLETE（修复成功率为可选项；金标准为合成故障集） |
| A9 | 故障传播可视化 | PDF | `failure_propagation.py` + DAG 可视化 | `test_failure_propagation`、`e2e/time-travel` | DAG Analysis / Fault Attribution | COMPLETE |
| A10 | 四层系统架构 | PDF | `docs/ARCHITECTURE.md` + 实现模块分层 | `e2e/pages.spec.ts` | Architecture 页 | COMPLETE |
| A11 | 技术栈与交付（FastAPI/React/SQLite、测试、CI/CD、可部署 Demo） | PDF | FastAPI + React + 文件/SQLite；CI 工作流；**第 55 步新增 Dockerfile/compose** | 本地 CI PASS；浏览器 100+ 项 | 本地 Demo 可用 | PARTIAL（容器未实际构建；云端 CI 未运行；无在线 Demo 地址） |

### 2.2 20 项 Agent 能力矩阵（PDF 第三节）

状态与证据由 `GET /api/capabilities/matrix` 实时返回（前端 Architecture 页展示）。
**第 55 步新增实现**：Skill、RAG、Memory、Context Engineering、Reflection、HITL、Context Review、
Explainability、Execution Environment（工具超时）、Deployment（容器配置）。

| # | 能力 | 来源 | 实现 | 证据 | 状态 |
|---|------|------|------|------|------|
| B1 | 规划 Planning | PDF | basic_agent understand→plan→agent | `test_basic_agent`、Trace planning 步骤 | COMPLETE (L3) |
| B2 | 工具调用 Tool Calling | PDF | calculator / sqlite_query / text_analyzer | `test_basic_tools`、Agent Runs | COMPLETE (L3) |
| B3 | 技能 Skill | PDF | **新增** `composite_sales_report`（SQLite 查询+汇总） | `test_capability_suite` | COMPLETE (L2) |
| B4 | RAG | PDF | **新增** 本地知识库 `knowledge_search` | `test_capability_suite`、Trace retrieval 步骤 | COMPLETE (L2，无向量库) |
| B5 | 记忆 Memory | PDF | **新增** `MemoryStore` + remember/recall 工具（运行内） | `test_capability_suite` | COMPLETE (L2，非持久化) |
| B6 | 上下文工程 Context Engineering | PDF | **新增** `build_context` + `run_agent(extra_context=...)` | `test_capability_suite` | COMPLETE (L2) |
| B7 | 反思 Reflection | PDF | **新增** `run_agent(enable_reflection=True)` 失败后反思步骤 | `test_capability_suite`、Trace reflection | COMPLETE (L2，默认关闭) |
| B8 | 可解释性 Explainability | PDF | **新增** `explain_trace` + Trace 详情「决策解释」卡 | `test_capability_suite`、`e2e/capabilities` | PARTIAL（不保存/不还原思维链，改为证据化解释） |
| B9 | 人机协同 HITL | PDF | **新增** 审批工作流 `HitlStore` + `/api/hitl/*` + `request_human_approval` 工具 | `test_capability_suite`、`e2e/capabilities` | PARTIAL（运行外审批，不中途暂停） |
| B10 | 执行评估 Evaluation | PDF | evaluation_engine + Evaluation Center + pipeline | `test_evaluation_*`、e2e flows | COMPLETE (L3) |
| B11 | 安全 Safety | PDF | 六维安全指标 + Red Team + Adversarial + 门禁 | `test_evaluation_engine`、`test_red_team*` | COMPLETE (L3) |
| B12 | 故障检测/错误处理 | PDF | 工具异常结构化 + fault injector + propagation | `test_fault_injector`、`test_failure_propagation` | COMPLETE (L3) |
| B13 | 执行环境管理 | PDF | **新增** 工具超时（`tool_timeout_seconds`）+ Agent 整体超时 | `test_capability_suite` | PARTIAL（无子进程/容器隔离） |
| B14 | 记账计费 Cost Monitoring | PDF | token_analytics + Cost Analytics | `test_token_analytics`、e2e | COMPLETE (L3) |
| B15 | 版本控制 Versioning | PDF | Git + PROJECT_STATE + 版本对比/回归 | git log、e2e version | COMPLETE (L2) |
| B16 | 自动化测试 Automation | PDF | pytest + Playwright + GitHub Actions + 本地 CI | 后端 661 / 浏览器 102 / CI PASS | COMPLETE (L2) |
| B17 | 部署 Deployment | PDF | **新增** `Dockerfile`、`docker-compose.yml`、`frontend/Dockerfile`、`docs/DEPLOYMENT.md` | 配置存在；本机无 Docker → 未构建 | PARTIAL (L1，NOT_VERIFIED) |
| B18 | 性能监控 Monitoring | PDF | Overview + Cost Analytics + Pareto + 延迟指标 | e2e cost/pareto/pages | COMPLETE (L3) |
| B19 | 成本优化 Cost Optimization | PDF | Pareto 成本-延迟-质量选择 | `test_pareto_analysis`、e2e | PARTIAL（决策支持，无自动调度/缓存） |
| B20 | 上下文审查 Context Review | PDF | **新增** `review_context` + RAG 结果自动审查 | `test_capability_suite` | COMPLETE (L2) |

### 2.3 7 大研究创新点（PDF 第四节）

| # | 创新点 | 来源 | 实现/实验 | 证据 | 状态 |
|---|--------|------|-----------|------|------|
| C1 | 因果轨迹代数 | PDF | counterfactual_engine 的 3 类干预算子 | `test_counterfactual_engine`、Counterfactual Lab | PARTIAL（未形式化完整代数） |
| C2 | 最小充分干预搜索 | PDF | minimal_repair 候选 + 受控验证 | `test_minimal_repair`、Repair Studio | PARTIAL（启发式、最多少量组合） |
| C3 | 不确定性感知门禁 | PDF | 证据等级 + 门禁四态（INCONCLUSIVE≠PASS） | `test_quality_gate` | PARTIAL（无统计 CI/重采样，刻意不伪造概率） |
| C4 | 因果影响剪枝 | PDF | 传播分析限定候选 + 归因合并去重 | `test_failure_propagation` | PARTIAL（无基于效应量的剪枝实验） |
| C5 | 故障注入与自动测试 | PDF | 6 类注入 + 故障数据集 + 红队生成 | `test_fault_injector` / red team | COMPLETE |
| C6 | 根因准确率评估 | PDF | **第 55 步新增** Top-1/Top-3、步骤级、误归因、成本、基线 | `test_rootcause_metrics`、Evaluation Center | COMPLETE（金标准为合成故障集） |
| C7 | 失败传播路径可视化 | PDF | DAG + 传播 + 干预对比（界面标注非因果） | `test_failure_propagation`、e2e | COMPLETE |

### 2.4 验收标准 / 交付要求（PDF 第二节第 4 点）

| # | 要求 | 来源 | 状态 | 说明 |
|---|------|------|------|------|
| D1 | 20 项能力均至少 L2 自动化测试 | PDF | PARTIAL | 15 COMPLETE / 5 PARTIAL（限制见矩阵） |
| D2 | 核心能力（Planning/Function Calling/RAG/Skill/Safety/Evaluation）L3 可视化 | PDF | PARTIAL | RAG/Skill 有 Trace 步骤可视化但无专属面板，判 L2 |
| D3 | 7 个创新方向均有可证伪实验 | PDF | PARTIAL | 3 COMPLETE / 4 PARTIAL（C1/C2/C3/C4） |
| D4 | 含标注真根因的可复现失败测试集 | PDF | COMPLETE | `dataset_fault_v1.json` + 根因指标金标准映射 |
| D5 | API 文档、开发指南、功能规格说明书 | PDF | PARTIAL | 已有 30+ docs、README、FastAPI OpenAPI；无独立功能规格 PDF |
| D6 | 可部署 Demo（GitHub/在线/演示视频） | PDF | PARTIAL | 本地 Demo 可运行；Docker 配置已加但未构建；无在线地址/视频 |
| D7 | CI/CD（GitHub Actions，测试全绿） | PDF | COMPLETE（本地）/NOT_VERIFIED（云端） | `agent-evaluation.yml` 仅 YAML 校验；本地 CI PASS |
| D8 | API 成本监测与风险/回滚文档 | PDF | COMPLETE | cost analytics + PRICE_UNKNOWN 口径 + DECISIONS/PROJECT_STATE |

### 2.5 12 个开发阶段（PDF 第十一节）

| 阶段 | 内容 | 状态 | 说明 |
|------|------|------|------|
| 0 | 环境搭建 | COMPLETE | venv/Git/智能体开发工具/测试环境 |
| 1 | PRD 与架构 | COMPLETE | PRD/ARCHITECTURE/Trace Schema |
| 2 | Agent 最小可用执行 | COMPLETE | 受控模拟 + 1 次真实 API 历史验证 |
| 3 | 多工具接入 | PARTIAL | 3 基础工具 + RAG/Skill 新增；无外部检索/翻译 API |
| 4 | 故障注入模块 | COMPLETE | 6 类故障 + 测试 |
| 5 | Trace 采集与标准化 | COMPLETE | Schema + 采集 + 持久化 |
| 6 | DAG 构建 | COMPLETE | DAG/依赖/环检测 |
| 7 | 单步反事实干预 | COMPLETE | Counterfactual Engine + Lab |
| 8 | 根因搜索与最小修复 | COMPLETE | 归因 + 最小修复 + 修复验证 |
| 9 | 置信度门限与剪枝 | PARTIAL | 证据等级/门禁；无 CI 与剪枝对照实验 |
| 10 | 自动化评测与版本对比 | COMPLETE | 评测/版本对比/回归/门禁/流水线 |
| 11 | 前端可视化工作台 | COMPLETE | 19 路由 / 98+ 项浏览器测试 |
| 12 | CI/CD、部署、作品集 | PARTIAL | 本地 CI PASS；云端 CI 与容器部署未运行；作品集材料未生成 PDF |

### 2.6 对话/开发规划推断的追加需求（CONV）

| 需求 | 来源 | 状态 | 证据 |
|------|------|------|------|
| LLM-as-a-Judge（Rubric/Mock/Real 双开关） | CONV | COMPLETE（Mock 路径；Real 未验证） | `test_llm_judge*` |
| Judge Calibration | CONV | COMPLETE（合成参考标注） | `test_judge_calibration*` |
| Multi-Judge Arbitration | CONV | COMPLETE（Mock 路径） | `test_multi_judge*` |
| 真实 Agent 执行链（双授权/限额/来源） | CONV | COMPLETE（单次真实样本） | `test_agent_runner*`、历史 Trace 1852 tokens |
| Red Team 生成器 / 对抗评测 | CONV | COMPLETE | `test_red_team*` / `test_adversarial*` |
| Bad Case 自动归因 / 失败模式挖掘 | CONV | COMPLETE | `test_bad_case_attribution` / `test_failure_pattern_mining` |
| 版本对比 / 回归检测 / CI 质量门禁 / 评测流水线 | CONV | COMPLETE | 对应测试与 e2e |
| Token & 成本分析 | CONV | COMPLETE | `test_token_analytics`、`e2e/cost-analytics` |
| 成本-延迟-质量帕累托分析 | CONV | COMPLETE | `test_pareto_analysis`、`e2e/pareto` |
| 需求回溯与能力审计（本步） | CONV | COMPLETE | 本文件 + `test_capability_suite` + `e2e/capabilities` |

## 3. 状态统计

按上述矩阵（74 条需求/验收项）：

| 状态 | 数量 | 占比 |
| --- | --- | --- |
| COMPLETE | 52 | 70.3% |
| PARTIAL | 22 | 29.7% |
| BROKEN | 0 | 0% |
| MISSING | 0 | 0% |
| UNVERIFIED（列内） | 0（部署运行时另注 NOT_VERIFIED） | — |

按来源：

| 来源 | 条目 | COMPLETE | PARTIAL |
| --- | --- | --- | --- |
| PDF（A+B+C+D+阶段） | 59 | 37 | 22 |
| CONV（第 38–55 步追加） | 15 | 15 | 0 |

> PARTIAL 集中在外部件（统计置信度、因果剪枝实验、容器部署、作品集交付），
> **不存在 BROKEN/MISSING 的核心演示链路**。报告不把 PARTIAL 说成 COMPLETE。

## 4. 功能缺口与优先级

### P0（阻断核心场景/演示流程）

| 编号 | 缺口 | 原因 | 处理 |
|------|------|------|------|
| **P0-55-1** | 真实 DeepSeek API 端到端路径仅有 1 次历史手工验证，无自动化真实 API 测试/CI | 真实调用产生费用，需用户逐次授权；既有安全边界禁止未经授权调用 | **未修复（BLOCKED）**：已记录；默认关闭；不伪造真实调用证据。可提供 `TRACEPILOT_ALLOW_REAL_API=1` 双授权后的验证步骤，但本步不执行付费调用 |

**本步未发现其他 P0**：19 条真实路由页面均可打开；核心流程（任务→Trace→DAG→归因→反事实→修复→评测→成本/帕累托）本地可运行并有浏览器测试。

### P1（主要功能不完整/不稳定）

| 编号 | 缺口 | 处理结果 |
|------|------|---------|
| **P1-55-1** | 根因准确率指标（Top-1/Top-3、误归因、修复率、平均成本、基线）缺失，创新点 6 无实验证据 | **已修复**：`rootcause_metrics.py` + `/api/attribution/metrics` + Evaluation Center 指标卡 + `test_rootcause_metrics.py`（19 条 20 项测试映射见 FEATURE_AUDIT） |
| **P1-55-2** | 20 项能力中 RAG/Memory/Context Engineering/Reflection/Skill/Context Review/HITL/Explainability/Execution Environment 缺失或仅文档 | **已修复（L2）**：`capability_suite.py` + `capability_tools.py` + `/api/capabilities/*` + `/api/hitl/*` + Architecture 能力矩阵 + `test_capability_suite.py`；Explainability/HITL/Execution Environment 仍 PARTIAL（限制明确） |
| **P1-55-3** | 部署能力缺失（无 Docker 配置） | **部分修复**：`Dockerfile`/`docker-compose.yml`/`frontend/Dockerfile`/`docs/DEPLOYMENT.md` 已添加；本机无 Docker → **运行时 NOT_VERIFIED（未修复完）** |
| **P1-55-4** | 因果影响剪枝/不确定性门禁缺少可证伪实验（无统计 CI 与剪枝对照） | **未修复**：属于研究方法缺口；项目明确不伪造概率/CI，需独立实验设计（P2 记录，保留 PARTIAL 状态） |

### P2 / P3（次要/体验/优化）

- 报告导出仅有后端 JSON/Markdown 落盘，缺少 UI 导出 JSON/CSV/PDF（PDF 生成未实现）；
- 因果轨迹代数未形式化；Pruning 无对照实验；
- 容器部署未在 Docker 环境构建验证；GitHub 云端 CI 未运行；
- RAG/Skill 只有 L2（无专属 L3 可视化面板）；Skill 为固定组合而非模型动态编排；
- Memory 仅运行内，不跨进程；HITL 不支持执行中途暂停；
- 视觉回归/跨浏览器/a11y 未做；作品集 PDF 未生成；
- 真实 API 多场景、真实流量下的归因/成本指标仍未验证。

## 5. 第 55 步修复记录（真实改动）

| 修复 | 文件 | 测试 |
|------|------|------|
| 根因准确率指标（Top-1/Top-3/步骤级/误归因/成本/基线） | `backend/app/services/rootcause_metrics.py`、`main.py` API | `tests/test_rootcause_metrics.py` |
| 20 项能力套件（RAG/Memory/Skill/Context Review/Context Engineering/Reflection/HITL/Explainability/ Execution Environment） | `backend/app/services/capability_suite.py`、`backend/app/tools/capability_tools.py`、`basic_agent.py` | `tests/test_capability_suite.py` |
| 能力矩阵 API + Architecture 页矩阵 | `/api/capabilities/matrix`、`Architecture.tsx` | `e2e/capabilities.spec.ts` |
| 根因指标卡 | `RootCauseMetricsPanel.tsx` + Evaluation Center | `e2e/capabilities.spec.ts` |
| Trace 决策解释卡 | `/api/traces/{id}/explanation` + `TraceDetail.tsx` | `e2e/capabilities.spec.ts` |
| HITL 审批工作流 | `/api/hitl/approvals` | `tests/test_capability_suite.py`、e2e |
| 部署配置 | `Dockerfile`、`docker-compose.yml`、`frontend/Dockerfile`、`nginx.conf`、`docs/DEPLOYMENT.md` | 配置审查（未运行容器） |

## 6. 验证结果

| 项 | 结果 |
|----|------|
| 后端全量测试 | **661 passed / 0 failed / 0 skipped**（第 54 步 643，+18：能力套件 13 + 根因指标 5） |
| 浏览器全量测试 | **102 passed / 0 failed**（第 54 步 98，+4：capabilities spec） |
| 前端构建 | `tsc + vite build` 通过 |
| 核心用例 | 架构能力矩阵、根因指标、决策解释、HITL 审批、评测→归因→指标、Pareto、成本等 e2e 全部通过 |
| 付费 API | 0 次；所有新增接口 `paid_api_called=false` |
| 密钥 | 新增代码/文档/测试未包含任何密钥 |

## 7. 审计结论

- 原始需求文档已成功访问并按 74 条需求/验收项完成矩阵对照；
- 本轮修复了 P1 级根因准确率指标缺失与多项能力缺失，新增 18 项后端测试、4 项浏览器测试与能力矩阵/根因指标/解释 UI；
- 仍有 4 项 P1/P2 级研究或交付缺口未闭环（真实 API 自动化验证、统计 CI/pruning 实验、容器运行时验证、报告导出）；
- **不存在被判定为 BROKEN 或 MISSING 的核心演示链路**；所有未完成项均已在能力矩阵、`docs/TECH_DEBT.md` 与本文件中显式列出，未用示例数据冒充真实评测结论。
