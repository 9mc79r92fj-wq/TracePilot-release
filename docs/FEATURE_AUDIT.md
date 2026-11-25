# TracePilot 功能审计报告（第 37 步）

- 审计时间：2026-11-18
- 审计范围：第 1～36 步已开发全部模块
- 审计方式：静态代码审查 + 全量测试执行 + 全端点冒烟调用 + 前后端契约交叉核对 + 数据真实性核查
- 判定原则：**代码存在 ≠ 功能完成**。只有「实现 + 测试证据」双满足才判 `COMPLETE`

---

## 一、审计方法与证据来源

| 证据类型 | 具体动作 | 结果 |
|---------|---------|------|
| 后端测试 | `pytest tests/ -q` | **196 passed / 0 failed / 0 skipped** |
| 前端构建 | `npm run build`（tsc 严格 + Vite） | **通过**（1.52s） |
| API 冒烟 | 逐个调用全部 26 个路由 | 26/26 符合预期，**0 个 5xx** |
| 前后端契约 | 交叉核对前端调用路径 vs 后端注册路由 | 全部匹配，无悬空调用 |
| 数据真实性 | 检查已保存 Trace 的 `metadata.simulated` | 18/18 = `true`（**无真实 API Trace**） |
| 独立脚本 | `verify_trace_dag.py` / `init_db.py` / `run_agent.py --help` | 均可运行 |
| 浏览器交互 | 无浏览器自动化环境 | **未执行**，相关 UI 判定为 UNVERIFIED |

---

## 二、模块状态总览

统计口径：16 个审计模块。

| 状态 | 数量 | 模块 |
|------|------|------|
| **COMPLETE** | **13** | Trace Schema、TraceCollector、DAG 分析、FaultInjector、TraceComparator、FailurePropagation、Failure Replay、CounterfactualEngine、MinimalRepairEngine、EvaluationEngine、FastAPI 接口、前后端联调、Git 与项目配置 |
| **PARTIAL** | **2** | Agent 执行引擎、React 网站 |
| **BROKEN** | **0** | —（本次审计发现 2 个 P0 缺陷，已修复，见第三节） |
| **MISSING** | **0** | —（16 个模块均有实现；缺失项集中在 React 页面子系统内部，见第四节） |
| **UNVERIFIED** | **1** | Trace Time Travel |

---

## 三、逐模块审计明细

### 1. Agent 执行引擎 — PARTIAL

| 项 | 内容 |
|----|------|
| 代码 | `backend/app/agents/basic_agent.py`（619 行） |
| 已实现 | LangGraph 图（understand → plan → agent ↔ tools）、DeepSeek Function Calling 循环、工具注册与执行、超时/重试、Trace 步骤采集、错误分类、同步/异步入口 |
| 测试证据 | `test_basic_tools.py`(15)、`test_trace_collector.py`(8)、`test_fault_injector.py`(14)、`test_fault_e2e.py`(1) 覆盖引擎图结构、工具循环、Trace 产出 |
| **缺口** | **全部测试均以 `monkeypatch` 替换 `_chat`（模拟模型）。真实 DeepSeek API 路径从未成功执行**——已保存 18 条 Trace 全部 `simulated: true` |
| 判定理由 | 引擎逻辑可运行，但「真实模型调用」这一核心能力缺乏任何成功证据 → PARTIAL |

### 2. TraceCollector — COMPLETE

代码 `trace_collector.py`（123 行）；`test_trace_collector.py` 8 用例覆盖 TraceRecord 生成、JSON 序列化、Schema 校验、脱敏、DAG 可解析性。

### 3. Trace Schema — COMPLETE

代码 `schemas/trace.py`（116 行，Pydantic v2）；`test_trace_schema.py` **21 用例**覆盖全字段校验、枚举约束、多父节点、TokenUsage 自动补全、非法类型拒绝。被其余全部模块复用。

### 4. DAG 分析 — COMPLETE

代码 `trace_dependency.py`(206) + `trace_dag.py`(145)；`test_trace_dag.py` 10 用例覆盖串行/并行/多父汇合/环检测/拓扑排序/祖先后代查询。`backend/verify_trace_dag.py` 实测可对真实 Trace 输出 5 节点 4 边、拓扑完整、无孤立节点、**DAG 合法=True**。

### 5. FaultInjector — COMPLETE

代码 `fault_injector.py`（162 行）；**6 类故障**（TOOL_TIMEOUT / TOOL_EXCEPTION / INVALID_TOOL_RESULT / EMPTY_RETRIEVAL / INVALID_ARGUMENTS / PERMISSION_DENIED）；`test_fault_injector.py` 14 用例覆盖六类触发、目标匹配、关闭恢复、确定性、配置记录。

### 6. TraceComparator — COMPLETE

代码 `trace_comparator.py`（198 行）；`test_trace_comparator.py` 5 用例覆盖语义匹配（非 step_id）、unmatched 标记、unknown 不填 0、最早差异不宣称根因。

### 7. FailurePropagation — COMPLETE

代码 `failure_propagation.py`（135 行）；`test_failure_propagation.py` 6 用例覆盖串行传播、并行分支、多故障、无后代、无法匹配、真实 Trace 集成。

### 8. Trace Time Travel — **UNVERIFIED**

| 项 | 内容 |
|----|------|
| 代码 | `frontend/src/pages/DagAnalysis.tsx` 内时间轴（播放/暂停/1×2×4×/拖动/跳转故障/事件 chip/当前事件联动） |
| 测试证据 | **无任何自动化测试**；仅 `tsc` 类型检查与构建通过 |
| 缺口 | 播放时序、拖动进度、DAG 联动弱化效果均未在浏览器中验证 |
| 判定理由 | 有代码、可构建，但零行为验证 → UNVERIFIED |

### 9. Failure Replay — COMPLETE

代码 `failure_replay.py`（307 行）；`test_failure_replay.py` 9 用例覆盖 Recorded 只读重放、Controlled 重跑生成独立 Trace、故障类型复现、原 Trace 不变、不执行危险工具、场景白名单拒绝。

### 10. CounterfactualEngine — COMPLETE

代码 `counterfactual_engine.py`（417 行）；3 种干预类型；`test_counterfactual_engine.py` 14 用例 + `test_counterfactual_api.py` 13 用例 + `test_counterfactual_lab`（前端页面，无测试）。因果等级保守分级（none/weak/moderate，永不输出 strong）经验证。

### 11. MinimalRepairEngine — COMPLETE

代码 `minimal_repair.py`（508 行）；6 种修复类型；`test_minimal_repair.py` 14 用例 + `test_repair_api.py` 11 用例 + `test_repair_studio_api.py` 11 用例。实测：`LIMITED_RETRY` 验证有效（score 0.775）、`INCREASE_TIMEOUT` 如实标为 failed。

### 12. EvaluationEngine — COMPLETE

代码 `evaluation_engine.py`（727 行）；六维度 + 10 确定性验证器；数据集 13 案例（全部 `synthetic`）；`test_evaluation_engine.py` 16 + `test_evaluation_api.py` 8 + `test_evaluation_center_api.py` 11 用例。实测 13/13 执行、任务成功率 1.0（13/13，分母=已执行案例）。

### 13. FastAPI 接口 — COMPLETE

`backend/app/main.py`（622 行），**26 个 API 路由**。逐个冒烟调用：全部返回符合预期状态码，**0 个 5xx**。统一错误响应（404/400/422/429/413）、路径穿越防护（`bad..name` → 400）、trace_id 白名单、请求体限制、受限 CORS 均实测有效。

### 14. React 网站 — PARTIAL

| 页面 | 状态 | 证据 |
|------|------|------|
| Overview | COMPLETE(代码) / UNVERIFIED(交互) | 消费健康检查 + Trace 列表 |
| Trace Explorer | COMPLETE(代码) | 消费 `/api/traces` |
| Trace 详情 | COMPLETE(代码) | 消费 `/api/traces/{id}` |
| DAG Analysis + Time Travel | PARTIAL | DAG 可构建；Time Travel 无测试 |
| Fault Lab（Failure Replay） | COMPLETE(代码) | 消费 `/api/replay/*` |
| Counterfactual Lab | COMPLETE(代码) | 消费 `/api/counterfactual/*` |
| Repair Studio | COMPLETE(代码) | 消费 `/api/repairs/*` |
| Evaluation Center | COMPLETE(代码) | 消费 `/api/evaluation/*`，证据下钻链路实测 200 |
| Architecture | COMPLETE(代码，静态展示) | — |
| **Agent Runs** | **MISSING** | 仍是 `NotImplemented` 占位页 |
| **Trace Comparison** | **MISSING** | 占位页（后端 `/api/traces/compare` 已完备） |
| **Fault Attribution** | **MISSING** | 占位页（后端 propagation 已完备） |

判定为 PARTIAL 的依据：11 个导航入口中 8 个有真实实现，3 个为占位；且**所有页面均无浏览器交互验证**。

### 15. 前后端联调 — COMPLETE

前端全部 `api.get/post` 路径经交叉核对**均能匹配后端已注册路由**（`/api/traces`、`/api/traces/compare`、`/api/health`、`/api/replay/*`、`/api/counterfactual/*`、`/api/repairs/*`、`/api/evaluation/*`），无悬空调用。Evaluation Center 的「案例 → Trace → DAG」链路实测：`/api/traces/{eval_trace}` 200、`/dag` 200（4 节点 3 边）。

### 16. Git 与项目配置 — COMPLETE

- 工作区干净（`git status --porcelain` 空）
- 敏感文件零泄露：`.env`、`*.db`、`node_modules`、`dist`、`__pycache__`、`.venv`、`data/evaluation/traces/` 经 `git check-ignore` 逐项确认为 IGNORED
- 已跟踪文件中不含任何 API Key
- 历史提交 12 个，无强制重置
- 缺口：**无 CI 配置**（见技术债 P3-1）

---

## 四、子系统级缺失（计入技术债）

虽 16 个审计模块无 MISSING，但存在 3 个前端页面缺失，已登记至 `docs/TECH_DEBT.md`：

1. **Agent Runs**（P1）— 无页面；后端亦无运行管理 API
2. **Trace Comparison**（P1）— 后端已完备，仅缺页面
3. **Fault Attribution**（P1）— 后端已完备，仅缺页面

---

## 五、审计发现的 P0 缺陷（已修复）

| # | 缺陷 | 影响 | 修复 |
|---|------|------|------|
| P0-1 | `App.tsx` 中 `/evaluation` 路由**重复注册**（EvaluationCenter + 残留 NotImplemented） | 路由冲突风险、死代码 | 删除重复的 NotImplemented 路由，`/evaluation` 唯一 |
| P0-2 | 侧边栏 **DAG Analysis 仍标记「待实现」**（`done: false`），但页面已完整实现 | 用户误判功能不可用 | 修正为 `done: true` |

两项修复后前端构建通过（1.52s），路由唯一性经 grep 验证为 1 处。

---

## 六、关键数据真实性核查

| 检查项 | 结论 |
|--------|------|
| 已保存 Trace 是否为真实 API 产物 | ❌ **否**。18/18 条 `metadata.simulated = true` |
| 评测数据集是否为真实用户数据 | ❌ **否**。13/13 案例 `data_source = synthetic`，文档已声明 |
| 评测指标是否为硬编码 | ✅ 否。由 `case_results` 实时聚合，可复算（测试断言维度计数与案例状态严格一致） |
| 修复/反事实结论是否伪造 | ✅ 否。无效候选如实标记 `failed`（如 INCREASE_TIMEOUT） |
| Token 统计是否真实 | ⚠️ 模拟模型返回 0 token，不代表真实成本（报告已标注） |

---

## 七、结论

- 16 个审计模块：**13 COMPLETE / 2 PARTIAL / 1 UNVERIFIED / 0 BROKEN / 0 MISSING**
- 后端：26 API 全部可用，196 测试全绿
- 前端：11 个导航入口中 8 个有真实实现，3 个占位；构建通过；**0 浏览器交互验证**
- 核心风险：**真实 DeepSeek API 路径从未成功验证**，全部能力建立在模拟模型之上

---

## 八、第 38 步增量审计（LLM-as-a-Judge）

### 新增模块

| 模块 | 状态 | 证据 |
|------|------|------|
| LLM Judge（`llm_judge.py`，732 行） | **COMPLETE（Mock 路径）** | 23 项模块测试 + 11 项 API 测试全通过 |
| Rubric 数据集（`data/evaluation/rubrics/`） | **COMPLETE** | 4 个 Rubric，结构经 Pydantic 校验，6 维度白名单 |
| Real Judge（DeepSeek Provider） | **UNVERIFIED** | 代码完整（含 Prompt Injection 防护、超时、重试），但**从未产生真实模型评分** |

### 模块状态更新

| 模块 | 第 37 步 | 第 38 步 |
|------|---------|---------|
| EvaluationEngine | COMPLETE | COMPLETE（新增可选 Judge 维度，确定性行为零变更） |
| React 网站 | PARTIAL | PARTIAL（Evaluation Center 新增 Judge 配置与展示） |

### 审计结论

- 后端测试总数：185 → **230**（+34 Judge 相关），全绿
- 前端构建：通过（1.65s）
- 新增能力：Rubric 驱动结构化评分、Mock/Real 双 Provider、证据引用、UNKNOWN 语义、版本与成本记录
- 仍未验证：真实模型评审质量与一致性


---

## 九、第 39 步增量审计（Judge Calibration）

### 新增模块

| 模块 | 状态 | 证据 |
|------|------|------|
| Judge Calibration（`judge_calibration.py`） | **COMPLETE（Mock 路径）** | 23 项模块测试 + 12 项 API 测试全通过 |
| 校准数据集（`data/evaluation/calibration/`） | **COMPLETE** | 22 案例覆盖 8 类场景，19 SCORED + 3 UNKNOWN，全部标记 synthetic_reference |
| 前端 Judge Calibration 页面（`/calibration`） | **COMPLETE（代码）/ UNVERIFIED（浏览器交互）** | 构建通过，路由 200；无浏览器交互验证 |
| Real Judge 校准 | **UNVERIFIED** | Provider 未授权，无法产生真实评分 |

### 模块状态更新

| 模块 | 第 38 步 | 第 39 步 |
|------|---------|---------|
| LLM Judge | COMPLETE（Mock） | COMPLETE（Mock）＋ 新增可配置调用预算（`set_call_limit`）与智能采样截断 |
| EvaluationEngine / Center | COMPLETE | COMPLETE（Evaluation Center 新增 Judge Calibration 入口） |
| React 网站 | PARTIAL | PARTIAL（新增第 9 个可用页面 `/calibration`） |

### 审计结论

- 后端测试总数：230 → **265**（+35 校准相关），全绿
- 前端构建：通过（1.50s）
- 新增能力：参考标注比对、Exact/MAE/Within-1、混淆矩阵、偏差分析、稳定性分析、版本记录、UNKNOWN 与 Mock/Real 隔离
- 重要负面发现：**Mock Judge 校准表现较差**（MAE 1.40、Exact 15.8%、系统性高估 11:1）→ Mock 不可作为质量代理，仅验证功能链路
- 仍未验证：真实模型评审与校准


---

## 十、第 40 步增量审计（Multi-Judge Arbitration）

### 新增模块

| 模块 | 状态 | 证据 |
|------|------|------|
| Multi-Judge Arbitration（`multi_judge.py`） | **COMPLETE（Mock 路径）** | 29 项模块测试 + 20 项 API 测试全通过 |
| Disagreement Explorer 前端（`/multi-judge`） | **COMPLETE（代码）/ UNVERIFIED（浏览器交互）** | 构建通过，路由已注册；无浏览器交互验证 |
| 真实模型间多 Judge 分歧 | **UNVERIFIED** | 需要真实模型，未授权 |

### 模块状态更新

| 模块 | 第 39 步 | 第 40 步 |
|------|---------|---------|
| LLM Judge | COMPLETE（Mock） | COMPLETE（Mock）＋ 新增 strict/lenient Prompt 变体与 `prompt_version` 区分 |
| Judge Calibration | COMPLETE（Mock） | COMPLETE（Mock） |
| Evaluation Center | COMPLETE | COMPLETE（新增 Multi-Judge 入口） |
| React 网站 | PARTIAL | PARTIAL（新增第 10 个可用页面 `/multi-judge`） |

### 关键诚实性约束（已实现并被测试锁定）

1. Mock 变体的 model_name 后缀差异**不构成独立模型**，独立性等级被钳制在 `mock_prompt_variants`；
2. 三个 Mock 变体在规则上确有差异（strict < base < lenient），非"同一规则复制三份"；
3. 加权聚合缺校准依据时抛 `MissingCalibrationError`，不编造权重；
4. 严重分歧返回 `INCONCLUSIVE` 且 `aggregated_score = null`；
5. UNKNOWN 评分不参与统计、不按 0 处理。

### 审计结论

- 后端测试总数：265 → **314**（+49），全绿
- 前端构建：通过（1.61s）
- 新增能力：多 Judge 独立评分、分歧检测（分差/方差/分布/一致性/证据冲突）、Median/Mean/校准加权仲裁、INCONCLUSIVE 与人工复核建议、独立性诚实评级
- 仍未验证：真实模型间分歧、真实校准权重下的加权聚合效果


---

## 十一、第 41 步增量审计（真实 Agent 执行链路）

### 模块状态变更（关键）

| 模块 | 第 40 步 | 第 41 步 | 证据 |
|------|---------|---------|------|
| **Agent 执行引擎（真实 API 路径）** | PARTIAL（真实调用 0 次） | **COMPLETE（最小验证）** | 1 次经用户批准的真实调用成功：`trace-4d3d82dc…`，6 步，Token 1852，工具调用 2 次，答案 6000 |
| Agent 执行引擎（Mock 路径） | 已验证 | 已验证 | 25 项 `test_agent_runner.py` |
| 前端 Agent Runs 页面 | **MISSING**（占位页） | **COMPLETE（代码）/ UNVERIFIED（浏览器交互）** | `/runs` 真实实现，构建通过 |
| 数据来源标记体系 | 缺失 | **COMPLETE** | `data_source` ∈ {real_api, mock, simulated}，三路径测试覆盖 |

### 新增模块

| 模块 | 状态 | 证据 |
|------|------|------|
| `agent_runner.py`（执行运行器 + 硬限制 + 双重授权） | COMPLETE | 25 模块测试 + 17 API 测试 |
| `/api/agent/*`（4 端点） | COMPLETE | 端到端实测 200/403/400/422/404 |

### 统计更新

- 后端测试：314 → **356**（+42），全绿
- 前端构建：通过（1.55s）
- 可用页面：10 → **11**（Agent Runs 补齐），剩余占位页 **2**（Trace Comparison、Fault Attribution）
- 真实 API 调用次数：0 → **1**（用户明确批准，仅此一次）

### 审计结论

- P0-3（真实 DeepSeek API 端到端未验证）**已关闭**：有 1 条真实 Trace 证据链（执行 → Trace → DAG）。
- 但"真实调用"仅 1 次样本，稳定性/成本/多轮行为仍属 UNVERIFIED，转为 P3 级技术债。
- 修复过程中发现并修正 3 个真实缺陷：async 调用误用、真实路径 run_id 未归一化、后端未加载 `.env`。
- 诚实性处置：桩测试曾产生一条被标记 `real_api` 的假 Trace，已删除并让测试自清理（不得把模拟标记为真实）。


---

## 十二、第 42 步增量审计（全站浏览器交互验收）

### 验收结果

| 状态 | 数量 |
|------|------|
| PASS | **41** |
| FAIL | 0 |
| BLOCKED | 0 |
| NOT_TESTED（本轮范围外，已单独登记） | 7 项 |

- 浏览器：Playwright 1.65.1 + Microsoft Edge（系统浏览器复用，无 Chromium 下载）
- 页面：**12 个真实页面 + 2 个占位页**
- 详见 `docs/BROWSER_ACCEPTANCE.md`

### 模块状态变更

| 模块 | 第 41 步 | 第 42 步 |
|------|---------|---------|
| 前端浏览器交互验证 | **NOT_TESTED** | **PASS（41 用例）** |
| Judge Calibration 页面 | 构建通过/未验证 | **PASS**（且修复 P0 契约缺陷） |
| 响应式布局 | 未验证 | **PASS**（3 视口 × 11 页无溢出） |
| 可访问性（aria） | 未验证 | PARTIAL（仅修折叠按钮，未做全站扫描） |

### 本轮修复（6 项）

| # | 问题 | 严重度 |
|---|------|--------|
| 1 | Judge 校准页面运行必然 404（前端 `calibration_id` vs 后端文件名 stem） | **P0** |
| 2 | 全站窄屏横向溢出（最大 1057px） | **P1** |
| 3 | 侧栏折叠按钮无可访问名称 | **P1** |
| 4 | Trace 详情页不显示数据来源标记 | **P1** |
| 5 | Repair Studio 默认选中无候选 Trace | P2 |
| 6 | 验证修复后详情面板消失（repair_id 变化导致选中失效） | P1 |

### 审计结论

- 后端测试：356 → **357**（新增校准双标识回归测试）
- 前端构建：通过（1.56s）
- 浏览器测试：**41 passed / 0 failed**（54s）
- 6 大核心业务流程全部有可重复运行的自动化用例
- 未验证项已如实登记，未冒充 PASS

---

## 十三、第 43 步增量审计（历史 P1 集中修复）

### 页面清单变化（关键）

| 项 | 第 42 步 | 第 43 步 |
|----|---------|---------|
| 真实实现页面 | 12 | **13**（+ Trace Comparison、Fault Attribution 中的两个，合计 13 个路由页面） |
| 占位页 | 2（`/comparison`、`/attribution`） | **0** |
| 导航「待实现」标记 | 2 处 | **0 处** |

> 精确口径：真实业务页面 **13 个**（Overview / Trace Explorer / Trace 详情 / DAG / Agent Runs / Fault Lab / Counterfactual / Repair Studio / Evaluation Center / Judge Calibration / Disagreement Explorer / Architecture / Trace Comparison / Fault Attribution —— 其中 Trace 详情为子路由，页面组件共 **13 个**）。

### P1 清零

| 编号 | 内容 | 状态 |
|------|------|------|
| P1-1 | Trace Comparison 页面缺失 | **FIXED** |
| P1-2 | Fault Attribution 页面缺失 | **FIXED** |
| P1-3 | Agent Runs 页面缺失 | FIXED（第 41 步） |
| P1-4 | Trace Time Travel 无自动化测试 | **FIXED** |
| P1-5 | 前端零测试覆盖 | FIXED（第 42 步） |
| P1-6 | `.env` 模型 ID 非官方值 | **FIXED** |

### 后端复用情况（未新增后端接口）

- 复用 `POST /api/traces/compare`（`trace_comparator.compare_traces`）
- 复用 `GET /api/traces/{id}/propagation`（`failure_propagation.analyze_failure_propagation`）
- 复用 `GET /api/traces`（选择器数据源）
- **未新增任何后端 API**，符合「尽量复用已有模块、不做无关重构」

### 测试证据

- 后端：**357 passed / 0 failed**（本轮无后端逻辑改动）
- 前端构建：通过（1.50s）
- 浏览器：**49 passed / 0 failed**（第 42 步 41 → 本轮 49，新增 P1 页面 4 项 + 时间旅行 2 项 + 页面清单扩充 2 项）
- 接口异常冒烟：40 项，无 500

### 诚实性说明

- 两个新页面的数据来自后端真实 mock Trace（`data_source=mock`），未伪造接口返回
- 页面对「观察 / 推断 / 未验证」三段严格分区，明确声明「可达 ≠ 因果」「差异为相关性观察」
- 未把 PARTIAL 标为 FIXED：本轮 6 项均为完整实现 + 测试证据

---

## 十四、第 44 步增量审计（全链路数据真实性与一致性）

### 数据对象审计结论

| 项 | 结论 |
|----|------|
| ID 命名空间 | **不统一**：`trace-` / `trace-eval-` / `trace-cf-` / `run-` / `run-cf-` / `exp-` / `report-` / `repair-` / `eval-` / `mj-`，已验证并文档化 |
| 证据锚点 | **Trace 是唯一跨模块锚点**；评测/实验/修复/对比/DAG 均通过 `trace_id` 关联 |
| 持久化 | 仅 Trace（含派生与评测 Trace）落盘；其余登记表**仅内存** |
| 来源标记 | 双维度：`origin`（REAL_API/MOCK/SIMULATED/UNKNOWN）× `storage`（RECORDED/MEMORY/NOT_AVAILABLE） |

### 修复前后对比（实测）

| 检查 | 修复前 | 修复后 |
|------|--------|--------|
| 实验 experiment_id 一致 | ❌ 两套 ID | ✅ 一致 |
| 变体 Trace 可查询 | ❌ 404 | ✅ 200（`data_source=simulated`、`derived_from` 保留） |
| 修复 Trace 可查询 | ❌ 404 | ✅ 200（`derivation=repair_validation`） |
| 评测 Trace 有来源标记 | ❌ 缺失 | ✅ `data_source=simulated` |
| 一致性检查 | 无 | ✅ 488 Trace 全量 0 issue |

### 新增模块

| 模块 | 状态 | 证据 |
|------|------|------|
| Evidence Lineage（后端） | COMPLETE | `test_evidence_lineage.py` 28 项 |
| 派生 Trace 持久化 | COMPLETE | 同上 + 浏览器用例 |
| 血缘前端面板 | COMPLETE / 浏览器已验证 | `e2e/lineage.spec.ts` 4 项 |
| 一致性检查 API | COMPLETE | `/api/lineage/consistency` 实测 0 issue |

### 测试

- 后端：**385 passed**（357 → 385，+28）
- 浏览器：**53 passed**（49 → 53，+4）
- 前端构建：通过

---

## 十五、第 45 步增量审计（Red Team Generator）

### 新增模块

| 模块 | 状态 | 证据 |
|------|------|------|
| Red Team Generator（`red_team_generator.py`） | **COMPLETE** | `test_red_team_generator.py` 38 项全通过 |
| Red Team API（6 端点） | **COMPLETE** | 端到端 200/400/404 实测 |
| Red Team Lab 前端（`/redteam`） | **COMPLETE / 浏览器已验证** | `e2e/redteam.spec.ts` 4 项 |

### 关键指标（实测，seed=42、variations=2）

| 指标 | 值 |
|------|-----|
| 生成案例总数 | **28**（26 攻击 + 2 金丝雀） |
| 覆盖类型 | 6/6（prompt_injection 6、tool_misuse 4、permission_boundary 4、invalid_arguments 4、conflicting_instructions 4、fault_recovery 4） |
| 变体 | 每模板 2 个（位置/格式/冲突形式/故障形态） |
| 安全通过率 | **100%（26/26）** |
| 任务成功率 | **61.5%（16/26）**（独立指标） |
| 金丝雀检测 | **2/2 检出**，`detector_verified=true` |
| 执行错误 | 0 |
| 付费 API 调用 | **0** |

### 安全边界验证

| 检查 | 结果 |
|------|------|
| 越权操作是否真实执行 | 全部被模拟权限系统拒绝，`executed=False` |
| sqlite 写操作是否触达工具 | 否（仅执行型工具检查通过） |
| Token 消耗 | 全部为 0（未调用模型） |
| 生成载荷安全校验 | 凭据/项目外路径/Shell 结构均被拒绝 |
| 敏感信息泄漏 | 无（测试断言） |

### 测试

- 后端：**423 passed**（385 → 423，+38）
- 浏览器：**57 passed**（53 → 57，+4）
- 前端构建：通过

---

## 十六、第 46 步增量审计（Adversarial Evaluation Lab）

| 模块 | 状态 | 证据 |
|------|------|------|
| `/api/red-team/*`（9 端点） | COMPLETE | 25 项 `test_adversarial_lab.py` |
| Attack Coverage Map | COMPLETE | 口径自洽断言 + 覆盖率接口 |
| Bad Case Explorer | COMPLETE / 浏览器已验证 | `e2e/adversarial.spec.ts` 4 项 |
| `/adversarial` 页面 | COMPLETE | 构建通过 + 4 项浏览器用例 |

### 实测（seed=42, variations=1）

| 指标 | 值 |
|------|-----|
| 生成案例 | 15（13 攻击 + 2 金丝雀） |
| 执行案例 | 按所选执行（全选时 15） |
| 安全通过率 | 13/13（攻击案例） |
| 任务成功率 | 独立统计 |
| 金丝雀检测 | 2/2 |
| 覆盖状态 | 全选时 FULL；部分选择时 PARTIAL |
| 付费 API | 0 次 |

### 测试

- 后端：**448 passed**（423 → 448，+25）
- 浏览器：**61 passed**（57 → 61，+4）

---

## 十七、第 47 步增量审计（Automated Bad Case Attribution）

| 模块 | 状态 | 证据 |
|------|------|------|
| 归因引擎（`bad_case_attribution.py`） | COMPLETE | `test_bad_case_attribution.py` 31 项 |
| `/api/attribution/*`（9 端点） | COMPLETE | 端到端 200/400/404/409 实测 |
| 受控失败数据集 `tracepilot-fault-v1` | COMPLETE | 8 案例，6 Bad + 2 UNKNOWN |
| 归因证据图 / 抽屉 / 批量面板 | COMPLETE / 浏览器已验证 | `e2e/attribution.spec.ts` 4 项 |

### 实测（受控失败数据集）

| 指标 | 值 |
|------|-----|
| Bad Case 数 | 6 |
| 已归因 | 6 |
| 证据等级 | CONFIRMED_EVENT 9 / POTENTIAL_CAUSE 4 / OBSERVED_DIFFERENCE 1 |
| 根因状态 | MULTI_ANOMALY 4 / SINGLE_CONFIRMED 1 / UNKNOWN 1 |
| 识别类型 | TOOL_TIMEOUT / TOOL_EXCEPTION / INVALID_ARGUMENTS / PERMISSION_DENIED / TASK_INCOMPLETE / PROCESS_VIOLATION / OUTPUT_MISMATCH / UNKNOWN |
| 批量耗时 | 1.36s（冷）/ 0.05s（热） |
| 付费 API | 0 次 |

### 诚实性验证（测试锁定）

- `CONFIRMED_EVENT` 必须引用 `trace_step` / `metric` 等可核对证据
- DAG 依赖候选强制 `POTENTIAL_CAUSE` 且 `is_conclusive=false`、注释"不构成因果"
- 报告无 `root_cause` 单因字段、无 `confidence/probability/score` 数字
- 实验引用必须真实 `exp-` ID 且含控制条件；缺失返回 NOT_AVAILABLE 而非捏造

### 测试

- 后端：**479 passed**（448 → 479，+31）
- 浏览器：**65 passed**（61 → 65，+4）
- 前端构建：通过

---

## 十八、第 48 步增量审计（Failure Pattern Mining）

| 模块 | 状态 | 证据 |
|------|------|------|
| 失败模式引擎（`failure_pattern_mining.py`） | COMPLETE | `test_failure_pattern_mining.py` 26 项 |
| `/api/failure-patterns/*`（8 端点） | COMPLETE | 端到端 200/400/404 实测 |
| Failure Pattern Explorer 前端 | COMPLETE / 浏览器已验证 | `e2e/failure-patterns.spec.ts` 4 项 |

### 实测（`tracepilot-fault-v1`）

| 指标 | 值 |
|------|-----|
| 模式数 | 6 |
| 分析 Bad Case | 6 / bad 6 / executed 8 / total 8 |
| 各模式占比 | 16.7%（1/6），占比之和 = 100% |
| UNKNOWN | 0（2 个 UNKNOWN 案例本身非 Bad Case，未进入分析） |
| 同数据集两次运行对比 | 新增 0 / 消失 0 / 不变 6 |
| 故障集 vs 正常集对比 | 6 模式全部「消失」（正常集无 Bad Case） |
| 付费 API | 0 次 |

### 诚实性验证（测试锁定）

- 指纹同输入同 ID（可复现）；任一字段变化都会改变 ID
- 缺失字段为 `null` 且计入 `missing_fields`，指纹 label 用 `-`
- 各模式案例数之和 == `analyzed_cases`；占比之和 == 1.0
- 未执行案例不进入任何模式
- UNKNOWN 单独计数，不并入失败类型统计
- 报告明示「相同指纹不代表已证明的相同根因」

### 测试

- 后端：**505 passed**（479 → 505，+26）
- 浏览器：**69 passed**（65 → 69，+4）
- 前端构建：通过

---

## 十九、第 49 步增量审计（Evaluation Version Comparison）

| 模块 | 状态 | 证据 |
|------|------|------|
| 版本对比引擎（`evaluation_version_comparison.py`） | COMPLETE | `test_evaluation_version_comparison.py` 11 项 |
| `/api/version-comparison/*`（7 端点） | COMPLETE | 端到端 200/400/404/422 实测 |
| `/version-compare` 版本对比工作台 | COMPLETE / 浏览器已验证 | `e2e/version-comparison.spec.ts` 5 项 |

### 实测（真实受控评测，`tracepilot-fault-v1`，8 案例）

| 指标 | 值 |
|------|-----|
| 版本 ID | `ev-ver-` + 内容哈希（同版本两次运行相同、run_id 不同） |
| 同版本两次运行可比性 | `COMPARABLE`，0 issue，`skipped_checks=[RUBRIC_CHECK_SKIPPED]` |
| Case-level Diff | common 8 / stable_fail 6 / unknown_involved 2 / improved 0 / regressed 0 |
| 指标差异 | task 0.0→0.0、tool 0.375→0.375、safety 1.0→1.0、recovery null（分母 0）、tokens 0.0 |
| 失败模式 diff | 两侧各 6 模式（bad_cases=6），新增/消失 0 |
| 跨数据集对比 | `NOT_COMPARABLE`（DATASET_ID / DATASET_FINGERPRINT / CASE_COUNT / CASE_SET） |
| 对比耗时 | 首次约 3.0s，热态约 0.5s |
| 付费 API | 0 次 |

### 诚实性验证（测试锁定）

- 版本元数据全部来自运行记录与数据集文件哈希；`agent_version` 缺失时输出 `null` + 说明，不伪造
- 指标差异保留分子/分母；标量指标分子分母为 `null`（`shape="scalar"`）
- `UNKNOWN` 不当 0：任一侧 null → `delta=null`、`status=UNKNOWN`
- 不可比结果明示「不得解释为性能提升」；共同案例子集对比仅供诊断
- 缺失案例单独列出，不参与状态变化统计
- 双方未启用 Judge 时 Rubric 记为「不适用」而非「缺失」

### 测试

- 后端：**536 passed**（505 → 536，+31：版本对比 11 + 回归检测 17 + Trace 列表缓存 3）
- 浏览器：**80 passed**（69 → 80，+11：新 spec 5 项 + 页面冒烟/响应式扩充 6 项）
- 前端构建：通过

---

## 二十、第 50 步增量审计（Regression Detection）

| 模块 | 状态 | 证据 |
|------|------|------|
| 回归检测引擎（`regression_detector.py`） | COMPLETE | `test_regression_detection.py` 17 项 |
| `/api/regression/*`（9 端点） | COMPLETE | 端到端 200/400/404/422 实测 |
| 回归风险矩阵 UI | COMPLETE / 浏览器已验证 | 矩阵 6×4、规则 8 行、门禁横幅 e2e 断言 |

### 实测（真实运行）

| 场景 | 结果 |
|------|------|
| 同版本两次运行（禁用耗时/成本规则隔离计时噪声） | `LOW / PASS`，触发 0，`RECOVERY_RATE_DROP` 不适用 |
| 跨数据集对比 | `UNKNOWN / INCONCLUSIVE`，限制中列出 CRITICAL 可比性问题 |

### 受控场景（合成报告注入内存 Store，覆盖阈值语义）

| 场景 | 结果 |
|------|------|
| 成功率 -37.5pp | `HIGH / BLOCK` |
| 安全通过率 -12.5pp | `CRITICAL / BLOCK` |
| 工具正确率 -25pp | `MEDIUM / WARN` |
| 耗时 +100% / Token +40% | `MEDIUM / WARN` |
| 耗时 +30% 但仅 +3ms | 噪声抑制 → `PASS` |
| 恢复率 UNKNOWN | `NOT_EVALUABLE` → `WARN` |
| 聚合改善但 1 例 PASS→FAIL | `HIGH / BLOCK`（聚合不能掩盖案例回归） |
| 全部规则禁用 | `UNKNOWN / INCONCLUSIVE` |

### 浏览器验收附带修复

| 编号 | 问题 | 修复 |
|------|------|------|
| **P2-34** | `/api/traces` 在 4300+ Trace 时请求超时（15s），Trace Explorer 无法渲染 | `trace_store.list_traces()` 增加目录签名缓存（增删改自动失效），冷启 5.2s / 热态 1.5s；新增 `test_trace_store_cache.py` 3 项 |
| **P2-35** | 评测数据集异步加载覆盖用户已选数据集（选择 fault 却运行 eval） | 默认选中改为 `setX(prev => prev || default)`；同步修复 Trace 对比/归因/修复页同类问题 |

### 诚实性验证（测试锁定）

- 阈值来源固定为 `PROJECT_DEFAULT_NOT_INDUSTRY_STANDARD`，声明「非行业标准」
- 风险等级取最高严重度，不含概率；`UNKNOWN` 不当作 LOW
- `INCONCLUSIVE` 绝不等于 PASS；证据缺失 → `WARN` 而非 PASS
- 显式禁用规则单列 `disabled_rules`，全部禁用 → 无法判定
- 回归案例证据引用指向真实 `case_id` / `trace_id`；Trace 不可读时返回 NOT_AVAILABLE
- 付费 API 0 次

### 测试

- 后端：**536 passed**（505 → 536，+31）
- 浏览器：**80 passed**（69 → 80）
- 前端构建：通过

---

## 二十一、第 51 步增量审计（CI/CD Quality Gate）

| 模块 | 状态 | 证据 |
|------|------|------|
| 质量门禁引擎（`quality_gate.py`） | COMPLETE | `test_quality_gate.py` 25 项 |
| 可版本管理配置（`config/quality_gate.yaml`） | COMPLETE | 配置加载/指纹/错误识别测试 |
| `/api/quality-gate/*`（6 端点） | COMPLETE | 端到端 200/400/404 实测 |
| 本地/CI 运行器（`scripts/ci_quality_gate.py`） | COMPLETE / 本地已实测 | `test_ci_quality_gate_script.py` 4 项 |
| GitHub Actions 工作流 | **LOCAL_VERIFIED / CLOUD_NOT_VERIFIED** | YAML 语法校验通过（10 step）；无远程仓库，未在 GitHub 运行 |
| 前端 Quality Gate 卡片（复用 `/version-compare`） | COMPLETE / 浏览器已验证 | `e2e/version-comparison.spec.ts` 新增 2 项 |

### 实测（本地）

| 场景 | 结果 |
|------|------|
| 本地 CI 默认命令（`tracepilot-eval-v1`） | `gate_status=PASS`，退出码 0，JSON+Markdown 落盘 |
| 本地 CI（`tracepilot-fault-v1` + 阈值 0.90） | `gate_status=BLOCK`，退出码 1，触发 `GATE_TASK_SUCCESS_BELOW_THRESHOLD` |
| 配置错误（未知字段） | 退出码 3，错误信息「未知配置项」，不产出通过报告 |
| WARN 策略 | 默认退出码 0；`warn_exit_code: 2` 时退出码 2 |
| INCONCLUSIVE | 版本不可比 → 退出码 2（绝不等于 PASS） |
| 新增安全失败 | 基线 8/8 → 候选 7/8 → `GATE_NEW_SAFETY_FAILURE` BLOCK |
| 数据缺失 | 未执行案例 2 例 → 默认 BLOCK；宽松配置 → WARN（仍不放行） |
| 付费 API | 0 次 |
| 密钥泄露 | 伪造 `DEEPSEEK_API_KEY` 不出现在 JSON/Markdown/输出 |

### 前端入口

复用 `/version-compare` 页面（不新建重复页面）：「CI Quality Gate」卡片展示
门禁状态、退出码、CI Run ID、Git commit、配置指纹/来源、关联评测与对比/回归 ID、
触发规则表、全部 11 项检查（阈值+实际值）、证据入口、限制声明。

### 诚实性验证（测试锁定）

- 阈值来源固定为 `PROJECT_DEMO_DEFAULT_NOT_INDUSTRY_STANDARD`，报告与 UI 均声明非行业标准
- 门禁结果全部由确定性规则得出，不含模型评分/概率
- 数据缺失/版本不可比不会自动放行；`INCONCLUSIVE` 绝不等于 PASS
- 非阻断回归规则只作 advisory，不改变门禁状态
- 配置错误会被识别（退出码 3），异常不默认放行
- 报告只含 ID/指标/规则，不含任何环境变量或密钥
- **云端 CI 明确标记 NOT_VERIFIED**（本地通过 ≠ 云端通过）

### 测试

- 后端：**565 passed**（536 → 565，+29：门禁 25 + 本地 CI 脚本 4）
- 浏览器：**82 passed**（80 → 82，+2）
- 前端构建：通过

---

## 二十二、第 52 步增量审计（Automated Evaluation Pipeline）

| 模块 | 状态 | 证据 |
|------|------|------|
| 流水线编排引擎（`evaluation_pipeline.py`） | COMPLETE | `test_evaluation_pipeline.py` 31 项 |
| 阶段/状态/可复现性/数据一致性 | COMPLETE | 阶段顺序、状态机、refs、证据引用断言 |
| `/api/pipelines/*`（8 端点） | COMPLETE | 端到端 200/400/403/404/409/429 实测 |
| 前端 `/pipelines`（Pipeline Run Inspector） | COMPLETE / 浏览器已验证 | `e2e/pipeline.spec.ts` 4 项 + 页面冒烟 |
| 质量门禁集成 | COMPLETE | 同数据集 → PASS（退出码 0）；不可比 → INCONCLUSIVE（退出码 2） |
| 已有引擎复用 | COMPLETE | 抽取 `build_evaluation_report_from_results` / `persist_trace`，既有评测测试全部回归通过 |

### 实测（本地 Mock 模式）

| 场景 | 结果 |
|------|------|
| 完整 Mock 流水线（`tracepilot-fault-v1`） | SUCCEEDED；归因 + 失败模式执行，Trace/Bad Case/归因/模式引用均可追溯 |
| 无基线流水线（`tracepilot-eval-v1`） | SUCCEEDED；对比/回归/门禁 SKIPPED（原因明确） |
| 带基线流水线（同数据集） | SUCCEEDED；对比/回归/门禁执行，门禁 `PASS` 退出码 0 |
| 基线/候选不可比 | 对比 NOT_COMPARABLE；门禁 `INCONCLUSIVE` 退出码 2 |
| Judge 启用（mock） | judge_evaluation SUCCEEDED，报告含 judge_breakdown（is_mock=true） |
| Agent 失败 / Trace 保存失败 / Judge 不可用 / 归因失败（注入） | 对应阶段 FAILED，下游 BLOCKED，报告仍生成且含错误详情 |
| 超时（1s 上限） | agent_execution FAILED（TIMEOUT），下游 BLOCKED |
| 取消（PENDING / RUNNING） | CANCELLED；其余阶段 CANCELLED；报告仍生成 |
| 重试上限（max_retries=1） | 尝试 2 次后 FAILED（不无限重试） |
| 历史/刷新恢复 | 磁盘快照恢复，GET 历史/详情正常 |
| 付费 API / 密钥 | 0 次；伪造 Key 不出现在记录/报告/输出 |
| 本地实际运行总量 | **179 条**流水线（含测试/失败/取消/超时场景）；最终 HTTP 验证 3 条全部 SUCCEEDED |

### 诚实性验证（测试锁定）

- SKIPPED ≠ SUCCEEDED；依赖失败 → BLOCKED；失败/取消/超时仍生成报告，绝不显示空白
- 可复现性缺失字段为 `null` 并写明原因（agent_version / prompt_version / rubric_version）
- 不修改原始 Trace；所有引用为真实产生工件 ID（evaluation/trace/bad case/attribution/pattern/comparison/regression/gate）
- 默认 Mock；real_api 双授权，未授权 403 且零调用；授权路径标记 NOT_VERIFIED
- 有限重试、并发/队列/时长/案例数/输入长度限制由服务端强制

### 测试

- 后端：**596 passed**（565 → 596，+31：流水线 31）
- 浏览器：**88 passed**（82 → 88，+6：pipeline spec 4 + `/pipelines` 冒烟 2）
- 前端构建：通过



---

## 二十三、第 53 步增量审计（Token & Cost Analytics）

| 模块 | 状态 | 证据 |
|------|------|------|
| Token 采集扩展（step `metadata.usage_meta`） | COMPLETE | 真实 API / mock / 评测模拟器三条路径写入；Schema 未变更，旧 Trace 兼容 |
| 价格配置（`config/model_pricing.yaml`） | COMPLETE | 版本/字段/日期/货币/非负校验；非法配置 → 400；`test_token_analytics.py` 配置错误用例 |
| 成本计算引擎（`token_analytics.py`） | COMPLETE | 27 项测试：用量来源、UNKNOWN 不为 0、输入输出分开计价、Decimal、聚合、防重复计费、失败任务成本、缺失价格 |
| 延迟计算 | COMPLETE | model/tool/total/evaluation/pipeline 五类；无法计算为 null + `unknown_fields` |
| 成本归因（5 层级 + 来源过滤） | COMPLETE | model_call / trace / agent_run / evaluation / pipeline；`usage_source` 过滤测试 |
| `/api/cost/*`（10 端点） | COMPLETE | 端到端 200/400/404 实测，全部 `paid_api_called=false` |
| 前端 `/costs` Cost Analytics | COMPLETE / 浏览器已验证 | `e2e/cost-analytics.spec.ts` 3 项 + 页面/响应式冒烟 |
| 性能缓存（目录签名 + 增量 + 单飞） | COMPLETE | 9,766 Trace：冷启动约 33s、热态 0–4s；并发请求不再重复全量分析 |

### 需求 19 项测试映射

| # | 要求 | 证据 |
|---|------|------|
| 1 | 单次模型调用 Token 采集 | `test_provider_reported_usage_from_real_api_trace` |
| 2 | Provider 未返回用量不填 0 | `test_provider_missing_usage_is_unknown_not_zero` |
| 3 | 模拟用量标记 SIMULATED | `test_simulated_usage_is_simulated_zero` |
| 4 | 显式估算标记 ESTIMATED | `test_estimated_usage_when_enabled` |
| 5 | UNKNOWN 处理 | `test_unknown_data_report` + `/api/cost/unknown` |
| 6 | 价格版本 | `test_pricing_versions` + `/api/cost/pricing` |
| 7 | 输入/输出价格不同 | `test_input_output_price_separate` |
| 8 | 计算精度（Decimal） | `test_decimal_precision` |
| 9 | 多次调用聚合 | `test_aggregate_calls` |
| 10 | 避免嵌套/重复计费 | `test_no_double_counting_run_level_usage_ignored` |
| 11 | 失败任务成本 | `test_failed_task_cost` |
| 12 | 延迟计算 | `test_latency_*` |
| 13 | 价格缺失 → PRICE_UNKNOWN | `test_price_missing_is_price_unknown` |
| 14 | Trace 关联/归因 | `test_trace_linkage_fields` + `test_attribution_usage_source_filter` |
| 15 | API 端点 | `test_api_cost_endpoints` |
| 16 | 前端构建 / 页面 | `tsc + vite build`；`e2e/cost-analytics.spec.ts` |
| 17 | 后端测试 | `pytest tests -q` |
| 18 | 不调用付费 API | 所有响应 `paid_api_called=false`；测试断言 |
| 19 | 不泄露密钥 | `test_no_secret_leak` + 全量测试无 Key 输出 |

### 实测（本地确定性数据）

| 项 | 值 |
|----|-----|
| Trace 总数 | 9,766（`data/traces` + `data/evaluation/traces`） |
| 真实 API Trace | 1 条（`trace-4d3d82dc…`，PROVIDER_REPORTED 1852 Token） |
| 真实 API 成本状态 | `PRICE_UNKNOWN`（未配置可信官方价格，不编造） |
| 模拟执行器成本 | `SIMULATED` / 0（`SIMULATED_ZERO_COST`，不代表真实计费） |
| 冷启动分析 | 约 33s（单次请求；并发请求由单飞锁合并） |
| 热态分析 | 0–4s（含 TTL 边界重算） |
| 付费 API | 0 次 |

### 诚实性验证（测试锁定）

- 金额全部带「非实际账单/扣费」声明；`ACTUAL_USAGE_PRICED` 仍为价格表估算
- Provider 未返回用量 → `null`；UNKNOWN/PRICE_UNKNOWN 不计为 0、不进平均成本
- 缺少可信价格 → `PRICE_UNKNOWN`，不编造官方价格、不按 0 计费
- step 级 provider/model 与 Trace 级不一致且无价格时禁止回退套用 Trace 级价格（新增回归测试）
- 流水线无阶段级成本分摊时显式 `stage_allocation_available: false`，不伪造
- `agent_version` 缺失时版本对比降级为模型/执行模式分组并显式说明

### 浏览器验收附带修复

| 编号 | 问题 | 修复 |
|------|------|------|
| **P2-48** | 并发负载下「批量归因」「失败模式分析」前端报「请求超时」（默认 15s），归因面板无结果 | `/api/attribution/runs`、`/api/failure-patterns/runs` 前端超时显式设为 120s，服务端上限不变；全量浏览器验收 93 项稳定通过 |

### 限制

- 未配置可信官方价格：真实调用成本不可给出金额（P2-45）
- 真实 API 用量样本仅 1 条，分布/版本对比代表性不足（P2-46）
- 评测/流水线运行记录内存态，重启后部分归因历史不可查（P2-47）
- 冷启动全量分析约 33s，未持久化分析索引（P3-30）

### 测试

- 后端：**623 passed**（596 → 623，+27：`test_token_analytics.py`）
- 浏览器：**93 passed**（88 → 93，+5：cost spec 3 + `/costs` 页面/响应式 2）
- 前端构建：通过

---

## 二十四、第 54 步增量审计（Cost–Latency–Quality Pareto Analysis）

| 模块 | 状态 | 证据 |
|------|------|------|
| 帕累托分析引擎（`pareto_analysis.py`） | COMPLETE | `tests/test_pareto_analysis.py` 20 项（支配/非支配/多候选/并列/单一/缺失三维/可比性/约束/门禁/前沿正确性/约束切换数据集） |
| 候选构建（真实评测运行） | COMPLETE | 质量/延迟来自评测报告，成本来自 CostAnalytics；`agent_version` 缺失为 null + 原因 |
| 可比性检查（9 项） | COMPLETE | 数据集/评分规则/指标定义/执行模式/币种/价格版本/样本量/数据来源/缺失指标；真实+模拟混用 → NOT_COMPARABLE |
| `/api/pareto/*`（10 端点） | COMPLETE | 端到端 200/400/404 实测；`paid_api_called=false` |
| 前端 `/pareto` Interactive Pareto Explorer | COMPLETE / 浏览器已验证 | `e2e/pareto.spec.ts` 3 项 + 页面冒烟/响应式 |
| 交互约束 | COMPLETE | `minimum_quality / maximum_latency_ms / maximum_cost / required_safety_gate / dataset_version`；无可行 → NO_FEASIBLE_CANDIDATE |
| 三维散点 + 二维投影 | COMPLETE / 浏览器已验证 | 自绘 SVG、拖拽旋转、悬停指标、点击选中、投影切换、移动端布局 |

### 需求 20 项测试映射

| # | 要求 | 证据 |
|---|------|------|
| 1 | 明确支配关系 | `test_clear_dominance_relation` |
| 2 | 非支配关系 | `test_non_dominated_tradeoff` |
| 3 | 多候选版本 | `test_multiple_candidates_with_mixed_frontier` |
| 4 | 完全相同指标 | `test_identical_metrics_are_tied_not_dominating` |
| 5 | 单一候选 | `test_single_candidate_frontier` |
| 6 | 缺失成本 | `test_missing_metric_excluded_not_zero[average_cost]` |
| 7 | 缺失延迟 | `test_missing_metric_excluded_not_zero[average_latency_ms]` |
| 8 | 缺失质量 | `test_missing_metric_excluded_not_zero[task_success_rate]` |
| 9 | 数据集不同 | `test_dataset_mismatch_marks_not_comparable` |
| 10 | 币种不同 | `test_currency_mismatch_blocks_comparison` |
| 11 | 价格版本不同 | `test_pricing_version_mismatch_is_warning` |
| 12 | 模拟与真实数据混用 | `test_mixed_real_and_simulated_is_not_comparable`（前沿为空 + 明示不得解释为真实性能） |
| 13 | 无可行候选 | `test_no_feasible_candidate_when_constraints_too_strict` |
| 14 | 约束筛选 | `test_constraints_filter_candidates` |
| 15 | 安全门禁筛选 | `test_safety_gate_filter` |
| 16 | Pareto Frontier 正确 | `test_pareto_frontier_correctness`（含并列组与排名） |
| 17 | API 正常 | `test_api_pareto_end_to_end` |
| 18 | 前端构建通过 | `npm run build`（tsc + Vite） |
| 19 | 后端测试通过 | `pytest tests -q` |
| 20 | 不调用付费 API | 所有响应 `paid_api_called=false`；测试断言 |

### 实测（本地真实评测运行，受控模拟执行器）

| 项 | 值 |
|----|-----|
| 候选总数 | 20（`tracepilot-eval-v1@1.0.0` 组 16 + `tracepilot-fault-v1@1.0.0` 组 4） |
| 可比组 | `tracepilot-eval-v1@1.0.0`（16 个候选；fault 组被排除） |
| 满足约束候选 | 16 |
| 非支配解 | 1 |
| 被支配解 | 15 |
| 支配关系 | 100（指标相同的候选互不支配） |
| 约束筛选 | fault+最小值 1 → NO_FEASIBLE_CANDIDATE；门禁 PASS → 可行 1 |
| 可比性 | `COMPARABLE_WITH_WARNINGS`（跨数据集候选已排除并警告） |
| 数据来源 | 全部为受控模拟评测（非真实模型性能） |
| 成本 | 模拟执行器 0 成本（`SIMULATED_ZERO_COST`，不是真实计费） |
| 付费 API | 0 次 |

### 诚实性验证（测试锁定）

- 候选全部来自真实评测运行；`agent_version` 缺失输出 null + 原因，不伪造版本
- 缺失指标为 null，不参与前沿/平均，不按 0 计
- 真实 API 与模拟数据混用 → NOT_COMPARABLE + 前沿为空 + 明确禁止解释为真实模型性能
- 币种/评分规则不一致 → 不可比；价格版本/样本量 → 明确警告
- 成本继承第 53 步口径：价格表估算，不是 Provider 实际账单；PRICE_UNKNOWN 时为 null
- 同指标并列 → 互不支配并标记 tie_group，不人为制造唯一胜者
- 全流程无付费 API 调用

### 限制

- 候选池依赖内存评测运行，重启后为空（P2-50）
- 当前无真实 API 评测运行，全部候选为模拟执行器数据，前沿不具备真实性能含义
- 三维散点为自绘等距投影（P3-32）；安全门禁筛选依赖已落盘门禁报告（P3-33）

### 浏览器验收附带修复

| 编号 | 问题 | 修复 |
|------|------|------|
| **P2-49** | 跨数据集候选使整个分析不可比，无法选择数据集组做前沿 | 按 dataset_id+version 分组，自动选最大可比组；组外候选排除并警告 |
| **P2-51** | 全站并发验收时 `/api/traces` 偶发超时（7k+ Trace 全量重复解析） | 文件级增量解析 + 单飞锁 + 摘要共享缓存；前端 60s 超时；冷启 16s / 热态 3.3s |
| **P2-52** | 交互约束切换到另一个数据集时误判 NO_COMPARABLE_CANDIDATE | 切换数据集时重新做可比性检查，再应用约束 |

### 测试

- 后端：**643 passed**（623 → 643，+20：`test_pareto_analysis.py`）
- 浏览器：**98 passed**（93 → 98，+5：pareto spec 3 + 页面冒烟 2）
- 前端构建：通过
---

## 二十五、第 55 步增量审计（需求回溯与缺口审计）

| 模块 | 状态 | 证据 |
|------|------|------|
| 原始需求核实 | COMPLETE | PDF 12 页经 pypdf 提取（16,914 字符）；需求来源分为 PDF / CONV 两类 |
| 需求–实现追踪矩阵 | COMPLETE | `docs/REQUIREMENTS_AUDIT.md`：74 条（52 COMPLETE / 22 PARTIAL / 0 BROKEN / 0 MISSING） |
| 根因准确率指标（Top-1/Top-3） | COMPLETE | `rootcause_metrics.py` + `/api/attribution/metrics` + Evaluation Center 指标卡；`test_rootcause_metrics.py` 5 项 |
| 20 项能力矩阵 | COMPLETE（15 C / 5 P） | `capability_suite.py` + `/api/capabilities/matrix` + Architecture 页；`test_capability_suite.py` 13 项 |
| RAG / Memory / Skill / Context Review / Context Engineering | COMPLETE（L2） | `capability_tools.py` 工具 + Trace 步骤类型（retrieval/memory_*/skill/context_review）；测试覆盖 |
| Reflection / Execution Environment | COMPLETE（L2） | 可选反思步骤 + 工具超时；`test_reflection_on_tool_failure` / `test_tool_timeout_enforced` |
| HITL 审批工作流 | PARTIAL（运行外） | `HitlStore` + `/api/hitl/approvals` + 工具；不中途暂停（限制明确） |
| Explainability | PARTIAL（非思维链） | `/api/traces/{id}/explanation` + Trace 详情卡；显式 `is_chain_of_thought=false` |
| Deployment | PARTIAL（NOT_VERIFIED） | Dockerfile/compose/nginx/docs 已加；本机无 Docker，未构建运行 |
| P0 真实 API 自动化验证 | BLOCKED（未修复） | 仅 1 次历史手工验证；需用户授权，禁止未授权付费调用 |

### 需求 74 项状态统计

| 状态 | 数量 | 说明 |
|------|------|------|
| COMPLETE | 52 | 有实现 + 自动化测试证据 |
| PARTIAL | 22 | 受限实现或缺少运行时/统计实验（限制逐项列出） |
| BROKEN | 0 | 无核心链路故障 |
| MISSING | 0 | 无完全未实现的核心需求 |
| UNVERIFIED | 0（矩阵内） | 部署运行时另行标注 NOT_VERIFIED |

### 第 55 步修复

| 编号 | 问题 | 修复 |
|------|------|------|
| P1-55-1 | 根因准确率指标缺失 | 新增指标引擎/API/UI/测试 |
| P1-55-2 | 9 项能力缺失或仅文档 | 新增能力套件与工具、能力矩阵、HITL/解释 API/UI |
| P1-55-3 | 部署能力缺失 | Docker 配置与部署文档；运行时 NOT_VERIFIED |
| P1-55-4 | 统计置信区间/剪枝实验缺失 | 未修复：作为研究缺口记录 P2-55（拒绝伪造概率/CI） |

### 测试

- 后端：**661 passed**（643 → 661，+18：`test_capability_suite.py` 13 + `test_rootcause_metrics.py` 5）
- 浏览器：**102 passed**（98 → 102，+4：`e2e/capabilities.spec.ts`）
- 前端构建：通过
- 付费 API：0 次；新增接口全部 `paid_api_called=false`

## 二十六、第 55-A 步增量审计（数据治理与可信度修复）

| 模块 | 状态 | 证据 |
|------|------|------|
| 备份与可恢复快照 | COMPLETE | `backups/step55a_20261119_183831/`（16,414 文件 / 94.2 MB，DB `integrity_check=ok`，SHA256 清单）；Git 检查点 `fix/step55a-data-governance` |
| 数据来源分类 | COMPLETE | `data_governance.py` + `scripts/audit_data_sources.py`；全库 16,408 = REAL_API 0 / MOCK 895 / SIMULATED 13,147 / REPLAY 2,365 / UNKNOWN 1；重复 0、坏文件 0 |
| 真实/模拟数据分离 | COMPLETE | 历史 `real_api` 声明缺验证 → UNKNOWN（不自动升级）；测试污染 565 条已隔离 + conftest 防复发；`data_source=mock` 的 55-A 失败 Trace 分类为 MOCK |
| 统计口径统一 | COMPLETE | Overview/Trace Explorer 主目录（8,618）；Cost Analytics 全库（16,408）；差异 7,790 条评测 Trace；页面显式标注范围 |
| 失败 Trace 存储/查询/过滤/关联 | COMPLETE | 主目录失败 3,273（含故障 925）；`/api/traces` 支持来源/状态/故障过滤；Trace Explorer 筛选+分页+空态；失败 Trace 可打开详情/DAG |
| 真实本地执行失败 Trace 验证 | COMPLETE | `trace-f3b36afd…`：7 步，`text_analyzer` 真实成功 + `calculator` 注入 `tool_timeout` 失败，状态 failed、来源 MOCK、四类资格全部可用、`paid_api_called=false` |
| 受控实验资格判断 | COMPLETE（API） | `/api/governance/traces/{id}/eligibility` 四类操作 + 原因/要求；**UI 执行入口尚未接入**（P2-56） |
| 成本统计可信度 | COMPLETE | Cost Analytics 来源范围筛选；UNKNOWN/PRICE_UNKNOWN 不再显示 0（KPI/表格显示状态文本 + 已定价部分说明）；REAL_API 空态明确 |
| 测试数据隔离 | COMPLETE | `tests/conftest.py` 会话级 fixture；隔离区 manifest 可回溯 |
| 验收性能修复 | COMPLETE | P2-59 流水线归因全库重载（>5 分钟 → 秒级）、P2-60 来源分类阻塞扫描（stale-while-revalidate + 单飞）；均有回归测试 |
| 真实 API 数据 | BLOCKED（P0-4） | REAL_API = 0；历史唯一真实调用缺 `real_api_verified` → UNKNOWN；本步 0 次付费调用 |

### 诚实性验证（测试锁定）

- `real_api` 缺 `real_api_verified` 一律 UNKNOWN，矛盾标记（verified + simulated）也归 UNKNOWN；
- REAL_API 范围筛选返回空时页面显示「暂无已验证的真实模型运行记录。」，不伪造数据；
- UNKNOWN / PRICE_UNKNOWN 的金额不展示为 0，`/api/cost/overview?source_class=REAL_API` 返回 `tokens=null`、`cost.amount=null`、`cost.status=UNKNOWN`；
- 失败 Trace 的 `tool_timeout` 为**受控注入**（步骤 metadata `fault_injection.injected=true`），不是自然故障；来源 MOCK 且 `paid_api_called=false`；
- 分类为只读派生，不修改任何原始 Trace。

### 测试

- 后端：**685 passed**（661 → 685，+24：`tests/test_data_governance.py`，含 2 项性能回归）
- 浏览器：**106 passed**（102 → 106，+4：`e2e/data-governance.spec.ts`）
- 前端构建：通过
- 付费 API：0 次；新增接口全部 `paid_api_called=false`
- 文档：`docs/STEP55A_DATA_AUDIT.md`
