# TracePilot 技术债清单

- 生成时间：2026-11-18（第 37 步全项目审计）
- 关联文档：`docs/FEATURE_AUDIT.md`
- 优先级定义：
  - **P0** 阻断核心功能
  - **P1** 主要功能不完整
  - **P2** 次要功能或体验问题
  - **P3** 优化和创新

统计：P0 = 3（**已修复 2 / 待处理 1**）｜P1 = 5｜P2 = 7｜P3 = 5

---

## P0（阻断核心功能）

### P0-1 · 前端 `/evaluation` 路由重复注册 — ✅ 已修复

| 项 | 内容 |
|----|------|
| 所属模块 | React 网站 / 路由配置 |
| 缺失功能 | `App.tsx` 同时声明 `EvaluationCenter` 与残留的 `NotImplemented` 于同一路径 |
| 当前状态 | **已修复**（删除重复路由，`/evaluation` 现唯一） |
| 影响范围 | 路由解析歧义、死代码；用户可能落到占位页 |
| 修复建议 | 已删除重复行 |
| 验收标准 | `grep -c 'path="/evaluation"'` == 1；构建通过 ✅ |

### P0-2 · 侧边栏 DAG Analysis 错误标记「待实现」 — ✅ 已修复

| 项 | 内容 |
|----|------|
| 所属模块 | React 网站 / 导航 |
| 缺失功能 | DAG 页面已完整实现（423 行，含 Time Travel），但导航 `done` 仍为 `false` |
| 当前状态 | **已修复**（改为 `done: true`） |
| 影响范围 | 用户误判核心功能不可用 |
| 修复建议 | 已修正一行 |
| 验收标准 | 导航项 DAG Analysis 不再显示「待实现」徽标 ✅ |

### P0-3 · 真实 DeepSeek API 端到端路径从未验证 — ⏳ 待处理

| 项 | 内容 |
|----|------|
| 所属模块 | Agent 执行引擎（basic_agent.py） |
| 缺失功能 | 真实模型调用 + 真实 Function Calling 循环 + 真实 Token 计量的端到端验证 |
| 当前状态 | 代码完整，但**18/18 已保存 Trace 均为 `simulated: true`**；所有测试用 `monkeypatch` 替换 `_chat` |
| 影响范围 | 项目最核心展示价值（真实 Agent 轨迹分析）缺乏证据；若真实调用失败，整条链路（Trace→DAG→归因→修复→评测）在真实数据上未经检验 |
| 优先级理由 | 阻断「真实 Agent 可靠性评测平台」的产品定位 |
| 修复建议 | ① 由用户在 `.env` 配置有效 `DEEPSEEK_API_KEY`；② 核对 `DEEPSEEK_MODEL`（当前为 `deepseek-v4-pro`，非 api.deepseek.com 标准 ID，建议先改为 `deepseek-chat`）；③ 执行 `python backend/run_agent.py "请使用计算器工具计算 (12+8)*5" --save-trace`；④ 确认终端打印 `[Trace 已保存]` 且新 Trace 的 `metadata.simulated` 不存在/为 false |
| 验收标准 | 至少 1 条真实 API Trace 成功落盘，其中 `token_usage.total_tokens > 0`，且该 Trace 能被 DAG 模块解析 |
| **无法自动修复原因** | 需要真实付费 API 调用，超出本次授权范围（禁止擅自调用付费模型） |

---

## P1（主要功能不完整）

### P1-1 · Trace Comparison 页面缺失

| 项 | 内容 |
|----|------|
| 所属模块 | React 网站 |
| 缺失功能 | 已完成 `/api/traces/compare`（后端已测试），前端仍是 `NotImplemented` 占位 |
| 当前状态 | MISSING（前端）/ COMPLETE（后端） |
| 影响范围 | 用户无法在 UI 中做正常 vs 故障 Trace 对比；「产品核心流程」中的对比环节断裂 |
| 修复建议 | 新建 `pages/TraceComparison.tsx`，复用 `api.post('/api/traces/compare')`，双下拉选 Trace + 差异分区（observations / inferred / unverified）+ diff_nodes 表 |
| 验收标准 | 页面可选择两条 Trace、显示状态/步骤数/Token/耗时对比与差异节点表；错误与空数据处理完整 |

### P1-2 · Fault Attribution 页面缺失

| 项 | 内容 |
|----|------|
| 所属模块 | React 网站 |
| 缺失功能 | `/api/traces/{id}/propagation` 后端已完备，前端占位 |
| 当前状态 | MISSING（前端）/ COMPLETE（后端） |
| 影响范围 | 故障传播可视化（创新点 #7）在 UI 不可见 |
| 修复建议 | 新建 `pages/FaultAttribution.tsx`：展示 confirmed_fault / observed_difference / potentially_affected / unaffected / unknown 五类节点计数与路径列表，并跳转 DAG |
| 验收标准 | 页面能展示 propagation summary 与 paths，且明确声明「依赖 ≠ 因果」 |

### P1-3 · Agent Runs 页面缺失（且后端无对应 API）

| 项 | 内容 |
|----|------|
| 所属模块 | React 网站 + FastAPI |
| 缺失功能 | 无运行列表/触发/状态查询 API；前端为占位页 |
| 当前状态 | MISSING（前后端均缺） |
| 影响范围 | 无法在 Web 端发起 Agent 运行，「创建任务 → Agent 执行」首环节缺失 |
| 修复建议 | **需先做安全设计**：真实执行涉及付费 API，必须引入「显式开关 + 配额 + 异步队列 + 结果落盘」，不能简单暴露 POST 接口 |
| 验收标准 | 设计评审通过后：受控开关默认关闭、单次配额上限、异步状态机、运行结果写入 data/traces |
| 风险提示 | 直接开放会违反「不开放真实付费模型调用接口」安全约束 |

### P1-4 · Trace Time Travel 无自动化测试

| 项 | 内容 |
|----|------|
| 所属模块 | React 网站 / DAG 页面 |
| 缺失功能 | 时间轴播放、拖动、跳转故障、DAG 联动弱化均无测试 |
| 当前状态 | UNVERIFIED |
| 影响范围 | 回归风险高；交互缺陷无法被 CI 捕获 |
| 修复建议 | 引入 Vitest + React Testing Library，对纯函数（时间轴排序、下游计算、聚焦集合）做单元测试；交互用 Testing Library 断言 |
| 验收标准 | 时间轴排序（同时间戳按拓扑序）、faultIds 识别、下游集合计算、播放步进 4 类测试通过 |

### P1-5 · 前端零测试覆盖（无测试框架）

| 项 | 内容 |
|----|------|
| 所属模块 | React 网站 |
| 缺失功能 | `frontend` 无 vitest/jest/RTL 依赖，`src` 下 0 个测试文件 |
| 当前状态 | MISSING |
| 影响范围 | 7 个业务页面全部依赖人工验证；第 26～36 步所有前端改动均无回归保护 |
| 修复建议 | 安装 `vitest @testing-library/react @testing-library/jest-dom jsdom`，配置 `npm run test`，先覆盖纯函数与 api 封装错误处理 |
| 验收标准 | `npm run test` 可运行且至少 15 个断言用例通过；CI 可接入 |

---

## P2（次要功能或体验问题）

### P2-1 · 两套重复的修复入口

`/api/repair/report`（第 33 步同步版）与 `/api/repairs/*`（第 34 步异步版）功能重叠。建议保留 `/api/repairs/*`，将 `/api/repair/report` 标记 deprecated 后在下一大版本移除。

### P2-2 · 两个同名 `tracepilot.db` 造成混淆

- 根目录 `tracepilot.db`：仅 `sales` 表（3 行），供 `sqlite_query` 工具使用
- `backend/data/tracepilot.db`：`traces` / `steps` 表（0 行）
两库同名不同路径，易误操作。建议重命名为 `sales_demo.db` 与 `tracepilot_meta.db`。

### P2-3 · `traces` / `steps` 表建好但从未写入

`database.py` / `init_db.py` 创建了持久化表，但 API 与采集器均只写 JSON 文件，表行数恒为 0。建议二选一：接入 ORM 落库，或删除建表逻辑并在文档标注「纯文件存储」。

### P2-4 · 实验/报告/评测均为内存存储，重启即丢失

`experiment_store` / `repair_store` / `evaluation_store` 分别在内存保留 20～50 条。进程重启后历史实验不可查。建议增加 SQLite 或 JSON 落盘（注意不要污染 `data/traces`）。

### P2-5 · `data/evaluation/traces/` 无保留策略

当前已累积 **138** 个评测 Trace 文件（每次评测 × 案例数）。虽有 gitignore，但磁盘会持续增长。建议按 run_id 分目录 + 保留最近 N 次。

### P2-6 · `data/traces/` 累积 18 个测试产生的模拟 Trace

仅 2 条为示例（其余 16 条由测试/手工运行产生）。建议测试改用 `tmp_path`，避免污染真实数据目录。

### P2-7 · 测试依赖固定 fixture Trace，缺少时静默 `pytest.skip`

`test_failure_replay` / `test_minimal_repair` / `test_counterfactual_engine` 等在找不到故障 Trace 时 `pytest.skip`，会导致「数据被移动后测试仍显示绿色」。建议改为 `pytest.fail` 或在 `conftest.py` 用程序化合成 fixture。

---

## P3（优化和创新）

### P3-1 · 无 CI/CD

无 `.github/workflows`。建议增加：后端 `pytest` + 前端 `tsc && vite build` + gitignore 泄露扫描（禁止 `.env`）。

### P3-2 · 无 Docker 部署

无 `Dockerfile` / `docker-compose.yml`，部署能力（PRD 阶段 12）未落地。

### P3-3 · 无 LLM-as-a-Judge

评测全部为确定性验证器（这是优点），但开放式答案的语义正确性无法评估。扩展方案已在 `docs/EVALUATION_ENGINE.md` §9 列出，需真实模型 + 显式开关 + 成本统计。

### P3-4 · 大 Trace 前端性能未优化

DAG 画布与时间轴 chip 列表未做虚拟化；当前 Trace 均为 5 步，规模化后可能卡顿。

### P3-5 · RAG 检索能力（20 项能力 #4）未真实接入

`StepType.RETRIEVAL` 与 `EMPTY_RETRIEVAL` 故障已支持，但无真实向量检索后端（Chroma 未安装、未接入）。能力矩阵第 4 项只能算「结构就绪、实现缺失」。

---

## 附：本轮已修复项汇总

| 编号 | 修复内容 | 验证 |
|------|---------|------|
| P0-1 | 删除重复 `/evaluation` 路由 | `grep` 计数=1，构建通过 |
| P0-2 | DAG 导航 `done: true` | 导航代码核对，构建通过 |

修复未删除任何测试、未修改任何测试断言、未改动业务算法。

---

## 第 38 步后的技术债变化

### 已关闭

| 编号 | 内容 | 关闭证据 |
|------|------|---------|
| P3-3（部分） | LLM-as-a-Judge 缺失 | 已实现 Rubric 驱动 Judge + Mock/Real Provider + 4 Rubric + 34 测试；**Mock 路径已闭环** |

### 新增

#### P3-6 · Real Judge 未验证（原 P3-3 剩余部分）

| 项 | 内容 |
|----|------|
| 所属模块 | LLM Judge（DeepSeekJudgeProvider） |
| 缺失功能 | 真实模型评审的评分质量、一致性、成本与延迟实测数据 |
| 当前状态 | UNVERIFIED：Provider 代码完整（含 SQL 注入防护、超时、重试、JSON 抽取），但零真实调用 |
| 影响范围 | 无法宣称「支持真实 LLM 评审」；只能宣称「Mock 评审已验证」 |
| 优先级 | P3（Mock 已可支撑功能演示与测试） |
| 修复建议 | 设置 `TRACEPILOT_JUDGE_ALLOW_REAL=1` + 有效 Key + `DEEPSEEK_MODEL=deepseek-chat`，对 3~5 条 Trace 实跑并人工核对评分合理性 |
| 验收标准 | 至少 3 条真实评审落盘，`is_mock=false`、`token_usage.total_tokens > 0`，且有评分依据可审计 |

#### P3-7 · Judge 校准与分歧率未实现

| 项 | 内容 |
|----|------|
| 所属模块 | LLM Judge |
| 缺失功能 | Judge 评分与确定性验证器的分歧率统计、Judge 自身一致性（同输入多次评分方差） |
| 当前状态 | MISSING |
| 影响范围 | 无法量化 Judge 可信度，只能定性声明「主观判断」 |
| 优先级 | P3 |
| 修复建议 | 增加 `judge_calibration` 报告：同 Trace 重复评审 N 次计算方差；与确定性 FAIL 案例交叉计算分歧率 |
| 验收标准 | 报告含分歧率与方差指标，且明确标注「非置信概率」 |

#### P2-8 · Judge 输入截断可能丢弃尾部证据

| 项 | 内容 |
|----|------|
| 所属模块 | LLM Judge |
| 缺失功能 | 长 Trace 的智能分段/摘要策略 |
| 当前状态 | 简单截断到 12,000 字符并标记 `input_truncated` |
| 影响范围 | 超长 Trace 的关键尾部证据可能未被评审 |
| 优先级 | P2 |
| 修复建议 | 按步骤重要性采样（保留故障步骤 + 首尾步骤）后再送入 Judge |

### 未变化

P0-3（真实 DeepSeek API 端到端未验证）、P1-1~P1-5、P2-1~P2-7、P3-1/P3-2/P3-4/P3-5 状态不变。

### 统计

P0 = 3（已修 2 / 待处理 1）｜P1 = 5｜P2 = 8（+1）｜P3 = 7（-1 关闭 +2 新增）

---

## 第 39 步后的技术债变化

### 已关闭

| 编号 | 内容 | 关闭证据 |
|------|------|---------|
| **P3-7** | Judge 校准与分歧率未实现 | 已实现 Exact / MAE / Within-1 / 混淆矩阵 / 偏差分析 / 稳定性；校准一致率已完成，**分歧率（Judge vs 确定性验证器）仍未实现**，故本项部分关闭，剩余部分见 P3-8 |
| **P2-8** | Judge 输入截断可能丢弃尾部证据 | 已实现优先级采样：保留 TASK、FINAL_ANSWER（首尾保留+中段省略）、失败步骤、首末步骤；`input_truncated` 真实反映是否省略 |

### 新增

#### P3-8 · Judge 与确定性验证器的分歧率未统计

| 项 | 内容 |
|----|------|
| 所属模块 | Judge Calibration / Evaluation Engine |
| 缺失功能 | 同一批案例上「Judge 判低分但确定性 PASS」「Judge 判高分但确定性 FAIL」的交叉分歧率 |
| 当前状态 | MISSING |
| 影响范围 | 无法定位 Judge 与规则判定冲突的系统性来源 |
| 优先级 | P3 |
| 修复建议 | 在校准报告中增加 `cross_check`：对同一 Trace 同时跑确定性六维与 Judge，统计冲突矩阵 |
| 验收标准 | 报告输出分歧率与典型冲突案例列表（含双方证据） |

#### P2-9 · Mock Judge 校准表现差且未改进

| 项 | 内容 |
|----|------|
| 所属模块 | LLM Judge（MockJudgeProvider） |
| 缺失功能 | Mock 的加权总分与人工整体分存在系统性偏差（MAE 1.40、11 高估 vs 1 低估） |
| 当前状态 | 已知缺陷（**故意未调参**，避免拟合校准集造成虚假质量） |
| 影响范围 | Mock 分数不能作为质量代理；前端已标注「模拟评审」 |
| 优先级 | P2 |
| 修复建议 | 明确 Mock 定位为「链路验证器」并在 UI/文档声明（已完成）；若需质量代理必须改用 Real Judge 并重新校准 |
| 验收标准 | 文档与 UI 均声明 Mock 不可作质量代理；Real Judge 校准后指标可对比 |

#### P3-9 · 多标注者一致性未实现

| 项 | 内容 |
|----|------|
| 所属模块 | Judge Calibration |
| 缺失功能 | Cohen's κ / Krippendorff's α（需要至少 2 名独立标注者） |
| 当前状态 | MISSING（当前标注为单人 synthetic_reference） |
| 优先级 | P3 |
| 修复建议 | 引入第二标注者或真实标注集后实现 κ/α |

### 未变化

P0-3、P1-1~P1-5、P2-1~P2-7、P3-1/P3-2/P3-4/P3-5/P3-6 状态不变。

### 统计

P0 = 3（已修 2 / 待处理 1）｜P1 = 5｜P2 = 9（+1，关闭 1）｜P3 = 9（关闭 1 部分、新增 2）

---

## 第 40 步后的技术债变化

### 部分关闭

| 编号 | 内容 | 说明 |
|------|------|------|
| **P3-7**（剩余部分） | Judge 分歧率 | 已实现 **Judge-Judge 分歧率**（最大分差/方差/一致性/证据冲突）；**Judge vs 确定性验证器**交叉分歧仍开放（P3-8） |

### 新增

#### P3-10 · 真实模型间多 Judge 分歧未验证

| 项 | 内容 |
|----|------|
| 所属模块 | Multi-Judge Arbitration |
| 缺失功能 | 真实模型间（如 deepseek-chat vs deepseek-reasoner）的评分分歧实测数据 |
| 当前状态 | UNVERIFIED：框架完整，`independent_models` 等级仅在真实模式可达 |
| 影响范围 | 无法宣称"支持真实多模型仲裁"，只能宣称"框架已就绪并用 Mock 验证" |
| 优先级 | P3 |
| 修复建议 | 授权 `TRACEPILOT_JUDGE_ALLOW_REAL=1` + 两个真实模型配置，跑 3~5 案例观察分歧分布 |
| 验收标准 | 报告 `independence.level=independent_models`、`paid_api_called=true`、含真实分歧指标 |

#### P3-11 · 校准加权聚合缺少真实权重来源

| 项 | 内容 |
|----|------|
| 所属模块 | Multi-Judge Arbitration |
| 缺失功能 | 从 Judge Calibration 结果自动推导 `judge_id → 权重` 并注入仲裁 |
| 当前状态 | 策略已实现但无数据：校准集只有 Mock 结果，且 API 未开放自定义权重（防注入） |
| 影响范围 | `calibration_weighted` 只能通过服务层代码调用，Web 端不可用 |
| 优先级 | P3 |
| 修复建议 | 校准运行产出 `reliability_weights`（基于 Within-1 一致率或 MAE 反比），并在仲裁中显式引用其 `source` 与 `evidence` |
| 验收标准 | 加权聚合的 `calibration.source` 指向真实校准 run_id，权重可复算 |

#### P3-12 · 多轮仲裁与评审器主动选择未实现

| 项 | 内容 |
|----|------|
| 所属模块 | Multi-Judge Arbitration |
| 缺失功能 | 分歧时追加 Judge、基于历史一致性动态选择评审器等策略 |
| 当前状态 | MISSING |
| 优先级 | P3 |

### 未变化

P0-3、P1-1~P1-5、P2-1~P2-9、P3-1/P3-2/P3-3~P3-6/P3-8/P3-9 状态不变（P3-7 进一步收敛）。

### 统计

P0 = 3（已修 2 / 待处理 1）｜P1 = 5｜P2 = 9｜P3 = 11（新增 3、部分关闭 1）

---

## 第 41 步后的技术债变化

### 已关闭

| 编号 | 内容 | 关闭证据 |
|------|------|---------|
| **P0-3** | 真实 DeepSeek API 端到端路径从未验证 | 用户批准后完成 1 次真实调用：`trace-4d3d82dc…`，Token 1852 > 0，DAG 6 节点合法，`data_source=real_api`、`simulated=false` |
| **P1-3**（部分） | Agent Runs 页面缺失 | `/runs` 已实现真实运行控制台（含任务提交/状态/答案/工具详情/Trace+DAG 跳转/来源区分） |
| **P2-7**（部分） | 测试依赖目录扫描取 fixture | 两处扫描已改为固定 trace_id + 显式 skip |

### 新增

#### P1-6 · `.env` 中模型 ID 非官方值

| 项 | 内容 |
|----|------|
| 所属模块 | 配置 / Agent 执行 |
| 缺失功能 | `DEEPSEEK_MODEL=deepseek-v4-pro` 不是 api.deepseek.com 合法模型 ID |
| 当前状态 | 运行器已自动回退到 `deepseek-chat` 并记录 `model_selection.fallback`，但配置本身仍错误 |
| 影响范围 | 每次真实调用都会走回退分支；用户若期望特定模型将得不到 |
| 优先级 | **P1**（影响真实执行的模型选择） |
| 修复建议 | 用户将 `.env` 改为 `DEEPSEEK_MODEL=deepseek-chat`（或 `deepseek-reasoner`） |
| 验收标准 | `/api/agent/status` 返回 `configured_model_valid=true`，且无 fallback 记录 |

#### P3-13 · 真实 API 仅 1 次样本

| 项 | 内容 |
|----|------|
| 所属模块 | Agent 执行 |
| 缺失功能 | 真实调用的稳定性、成本分布、多轮工具链、失败重试策略实测数据 |
| 当前状态 | 1 次成功样本 |
| 优先级 | P3 |
| 修复建议 | 需用户逐次批准；建议后续在明确预算下跑 3~5 个任务（含一次工具失败场景） |
| 验收标准 | 至少 3 条真实 Trace，覆盖成功与失败各 1 例 |

#### P3-14 · 真实模式 Token/调用次数为事后审计

| 项 | 内容 |
|----|------|
| 所属模块 | agent_runner |
| 缺失功能 | Token 预算的硬中断（当前只能事后判定 `limit_exceeded`） |
| 当前状态 | 硬中断仅 `max_wall_seconds`、`max_tool_rounds`；Token 需响应返回后可知 |
| 优先级 | P3 |
| 修复建议 | 在每轮工具循环后累计 usage 并提前终止 |
| 验收标准 | 超 Token 预算时立即停止后续模型调用 |

#### P2-10 · 运行记录仅存内存

| 项 | 内容 |
|----|------|
| 所属模块 | agent_runner |
| 缺失功能 | Run 记录持久化（Trace 已落盘，但 Run 元数据重启丢失） |
| 优先级 | P2 |
| 修复建议 | 复用 SQLite `runs` 表（`database.py` 已建表但未写入，见 P2-3） |

### 未变化

P1-1、P1-2、P1-4、P1-5｜P2-1~P2-6、P2-8、P2-9｜P3-1/P3-2/P3-4~P3-12 状态不变（P3-6 已由 P0-3 关闭部分覆盖，余下为 P3-13）。

### 统计

P0 = 3（**全部已修复/关闭**）｜P1 = 6（新增 1，部分关闭 1）｜P2 = 10｜P3 = 14

---

## 第 42 步后的技术债变化

### 已关闭

| 编号 | 内容 | 关闭证据 |
|------|------|---------|
| **P1-5** | 前端零自动化测试（浏览器交互从未验证） | 新增 41 项 Playwright 用例，覆盖 12 真实页 + 6 大流程 + 3 视口响应式，全绿 |
| **P2-11**（本轮新增即关闭） | Judge 校准页面 404（`calibration_id` / 文件名 stem 契约不一致） | 后端同时支持两种标识 + 回归测试；浏览器流程 F2 PASS |
| **P2-12**（本轮新增即关闭） | 窄屏横向溢出（表格/select/网格） | `responsive-fixes.css`，22 个视口×页面组合溢出归零 |

### 新增

#### P2-13 · 视觉回归与跨浏览器未覆盖

| 项 | 内容 |
|----|------|
| 所属模块 | 前端质量保障 |
| 缺失功能 | 截图基线对比（toHaveScreenshot）、Chrome/Firefox/Safari 矩阵 |
| 当前状态 | 仅 Edge + 功能性断言 |
| 优先级 | P2 |
| 修复建议 | 增加 `projects` 多浏览器 + 关键页面视觉快照基线 |
| 验收标准 | 3 浏览器 × 核心页截图对比通过 |

#### P3-15 · 全站可访问性（a11y）未系统验证

| 项 | 内容 |
|----|------|
| 所属模块 | 前端 |
| 缺失功能 | axe-core 扫描、键盘可达性、对比度检查 |
| 当前状态 | 仅修复侧栏折叠按钮 aria（单点） |
| 优先级 | P3 |
| 修复建议 | 引入 `@axe-core/playwright` 对 12 页扫描 |
| 验收标准 | 关键页面无 critical/serious a11y 违规 |

#### P3-16 · 部分交互未自动化

| 项 | 内容 |
|----|------|
| 所属模块 | 前端 |
| 缺失功能 | Trace Time Travel 播放时序、DAG 拖拽/平移、节点聚焦视觉弱化、并发场景 |
| 当前状态 | NOT_TESTED（已登记，未冒充 PASS） |
| 优先级 | P3 |

### 未变化

P0 全部关闭｜P1-1、P1-2、P1-3（部分）、P1-4、P1-6｜P2-1~P2-10｜P3-1~P3-14 状态不变。

### 统计

P0 = 3（全部关闭）｜P1 = 6（关闭 1）｜P2 = 13（关闭 2、新增 1）｜P3 = 16（新增 2）

---

## 第 43 步：历史 P1 技术债集中修复

### 修复总览

| 状态 | 数量 |
|------|------|
| FIXED | **6** |
| PARTIAL | 0 |
| BLOCKED | 0 |
| NOT_FIXED | 0 |

### 逐项验收

#### P1-1 · Trace Comparison 页面缺失 → **FIXED**

| 项 | 内容 |
|----|------|
| 原始问题 | 后端 `POST /api/traces/compare` 已实现，前端 `/comparison` 仅为占位页（「该模块尚未实现」） |
| 根因 | 前端从未实现该页面；`CompareResponse` 类型定义为 `Record<string, unknown>`，缺乏字段契约 |
| 修改文件 | `frontend/src/pages/TraceComparison.tsx`（新增）、`frontend/src/App.tsx`、`frontend/src/components/Layout.tsx`、`frontend/src/responsive-fixes.css` |
| 修复方式 | 新建完整对比页：双 Trace 选择（默认参考=无故障、目标=含故障）→ 概览双栏对照 → 顶层差异表 → 最早差异点 → 差异节点（含上下游）→ 步骤匹配/未匹配 → **观察/推断/未验证三段严格分区** → 跳转 DAG/反事实；相同 Trace 明确拒绝 |
| 测试证据 | `frontend/e2e/p1-pages.spec.ts` 2 项（正常对比 + 相同拒绝）PASS；`pages.spec.ts` 打开/刷新 2 项 PASS；响应式 3 视口 PASS |
| 最终状态 | **FIXED** |

#### P1-2 · Fault Attribution 页面缺失 → **FIXED**

| 项 | 内容 |
|----|------|
| 原始问题 | 后端 `GET /api/traces/{id}/propagation` 已实现，前端 `/attribution` 仅为占位页 |
| 根因 | 前端从未实现；`PropagationResponse` 内部字段为 `unknown[]`，无法直接渲染 |
| 修改文件 | `frontend/src/pages/FaultAttribution.tsx`（新增）、`App.tsx`、`Layout.tsx` |
| 修复方式 | 新建归因页：故障 Trace + 可选基线 → 5 类分类指标卡（注入故障/观测差异/潜在受影响/无法判定/未受影响）→ 故障节点卡 → 传播路径 → 节点分类表（含分类含义图例与筛选）→ **结论口径声明（可达≠因果）** → 跳转 DAG/反事实 |
| 测试证据 | `p1-pages.spec.ts` 1 项 PASS（含基线选择、分类表、筛选、口径断言） |
| 最终状态 | **FIXED** |

#### P1-4 · Trace Time Travel 无自动化测试 → **FIXED**

| 项 | 内容 |
|----|------|
| 原始问题 | DAG 时间轴回放（播放/暂停/上一事件/下一事件/跳转故障/速度）从未有自动化验证 |
| 根因 | 第 42 步浏览器验收未覆盖该交互 |
| 修改文件 | `frontend/e2e/time-travel.spec.ts`（新增）、`frontend/src/pages/DagAnalysis.tsx`（修复 a11y） |
| 修复方式 | 新增 2 项用例覆盖展开/收起、进度条数值推进、播放→暂停、回到起点、跳转故障、速度切换、缺时间戳排除提示；同时修复发现的可访问性缺陷（见下） |
| 测试证据 | `time-travel.spec.ts` 2 项 PASS |
| 最终状态 | **FIXED** |

#### P1-6 · `.env` 中模型 ID 非官方值 → **FIXED**

| 项 | 内容 |
|----|------|
| 原始问题 | `DEEPSEEK_MODEL=deepseek-v4-pro` 不是 api.deepseek.com 合法模型 ID，每次真实调用都会走回退分支 |
| 根因 | 用户侧配置值错误（非代码缺陷） |
| 修改文件 | `.env`（gitignored，不提交） |
| 修复方式 | 改为官方值 `DEEPSEEK_MODEL=deepseek-chat` |
| 测试证据 | `GET /api/agent/status` 返回 `configured_model=deepseek-chat`、`configured_model_valid=true`（重启后实测） |
| 最终状态 | **FIXED** |

### 额外修复（本轮发现）

| # | 问题 | 严重度 | 修复 | 证据 |
|---|------|--------|------|------|
| A | DAG「起点」按钮 `aria-label="回到开始"` 与可见文字不符，违反 WCAG 2.5.3 Label-in-Name | P2（a11y） | 改为 `aria-label="回到起点"` | `time-travel.spec.ts` 用例通过 |
| B | 两个 P1 页面缺失导致导航显示「待实现」 | — | 导航 `done: true`，占位页清零 | `p1-pages.spec.ts` 断言 `待实现` 链接数为 0 |

### 已验证无问题的项（不虚构修复）

| 检查项 | 方法 | 结论 |
|--------|------|------|
| 全接口异常处理 | 40 项异常冒烟（未知资源/非法参数/路径穿越/空值） | 无 500；全部返回 4xx 且带中文 detail |
| 非法干预类型与参数 | 真实故障 Trace 上测 `evil`/空/缺字段/负 retries | 均 400 并列出允许值 |
| 后端已有但前端未用接口 | 42 条路由 vs 前端调用比对 | 4 条为冗余/内部用途（`bad-cases`、`cases/{id}`、`repair/types`、`intervention-types`），前端已通过运行详情内联数据实现同等能力，**不构成用户可见缺口** |

### 未变化

P0 全部关闭｜P1 全部关闭（6/6）｜P2-1~P2-13｜P3-1~P3-16 状态不变。

### 统计

P0 = **0 剩余**｜P1 = **0 剩余**｜P2 = 13｜P3 = 16

---

## 第 44 步：全链路数据真实性与一致性验证

### 修复（均为 FIXED，非 PARTIAL）

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P1-7** | 反事实实验存在两套 `experiment_id`（登记表 vs 引擎结果） | 引擎自行生成 ID，未接收登记表 ID | 引擎新增 `experiment_id` 参数 + 存储层透传 | `test_experiment_lineage_and_id_consistency`（断言无 consistency_warning） |
| **P1-8** | 派生 Trace `trace-cf-*` 无法查询（404），证据链断裂 | 派生 Trace 从未落盘 | 新增 `derived_trace.py`，引擎 replays 后落盘 | `test_experiment_variant_trace_recorded_and_marked`（HTTP 200 + 来源标记） |
| **P1-9** | 修复候选 `repair_trace_id` 404、派生类型不可区分 | 同 P1-8；复用反事实引擎未区分用途 | 同 P1-8 + `derivation=repair_validation` | 血缘修复报告节点 `validated_by_trace` 边 |
| **P1-10** | 评测 Trace 缺少 `data_source`，可能被误认为真实数据 | `_persist_trace` 直接写原 Trace | 落盘时补 `data_source=simulated` / `simulated=true` / `origin_note` | `test_no_paid_api_used` + 血缘 origin 断言 |

### 新增能力

- `backend/app/services/evidence_lineage.py`：血缘构建（4 类根）+ 只读一致性检查
- `backend/app/services/derived_trace.py`：派生 Trace 落盘与诚实来源标记
- `/api/lineage/*` 5 个只读端点
- 前端 `LineagePanel` / `LineageDrawer`，接入 Trace 详情、Evaluation Center、Counterfactual Lab、Repair Studio
- `docs/DATA_LINEAGE.md`

### 新增技术债

#### P2-14 · 运行登记表仅内存，血缘节点依赖进程状态

| 项 | 内容 |
|----|------|
| 所属模块 | 血缘 / 各 store |
| 缺失功能 | Run / Experiment / Repair / Evaluation / Multi-Judge 登记表持久化 |
| 当前状态 | 血缘已标记 `storage=MEMORY` 并在丢失时给出说明，但登记内容本身重启即丢 |
| 优先级 | P2（与 P2-10 合并跟踪） |
| 修复建议 | 复用 SQLite 落表或 JSON 快照 |
| 验收标准 | 重启后 `/api/counterfactual/experiments`、`/api/repairs/reports`、`/api/evaluation/runs` 仍可查询历史记录 |

#### P3-17 · 历史 `trace-cf-*` 永久不可追溯

| 项 | 内容 |
|----|------|
| 说明 | 第 44 步之前生成的派生 Trace 从未落盘，血缘显示 NOT_AVAILABLE（不补造） |
| 优先级 | P3（不可逆历史，仅记录） |

#### P3-18 · 血缘面板无虚拟滚动

| 项 | 内容 |
|----|------|
| 说明 | 超大 Trace（>200 步）展开血缘时渲染全部节点，可能卡顿 |
| 优先级 | P3 |

### 统计

P0 = 0｜P1 = 0 剩余（新增 P1-7~P1-10 均已 FIXED）｜P2 = 14（+1）｜P3 = 18（+2）

---

## 第 45 步：Red Team 生成器

### 本轮修复的真实缺陷（FIXED）

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P1-11** | 红队故障恢复案例的故障永不触发，恢复率恒为 0 | `tool_timeout`/`tool_exception` 属**前置故障**，需 `before_tool` 抛出；执行器只调用了 `after_tool` | 执行器补 `injector.before_tool()`，并区分 `FaultInjectionError` | `test_fault_recovery_uses_before_tool`（断言首次失败 + fault_type + recovery_observed） |
| **P2-15** | 金丝雀负控 Trace 未落盘 → Bad Case 无法回查 | 金丝雀分支提前 `return`，跳过持久化 | 抽出 `_persist_trace()`，两处复用 | `test_bad_case_linked_to_trace` |
| **P2-16** | 安全判定系统性误报（3 类） | ① 把「分析含 DROP TABLE 的文本」当作危险执行 ② 把工具回显的被分析文本当作泄漏 ③ 把被拦截的误用尝试当作违规 | ① 危险参数只查执行型工具（`sqlite_query`）② `forbidden_markers` 只查最终回答 ③ 禁止工具只统计已执行调用 | 安全通过率从 46% → 100%（26/26），且金丝雀 2/2 可检出 |

### 新增技术债

#### P2-17 · 红队模板覆盖面有限

| 项 | 内容 |
|----|------|
| 说明 | 10 个模板 + 4 个变异维度；载荷为人工设计固定文本，非自适应生成 |
| 影响 | 未发现真实绕过；覆盖面受模板限制 |
| 优先级 | P2 |
| 建议 | 增加模板库 + 对抗式变异（同义改写、编码变体、多语言） |

#### P3-19 · 红队 Trace 会持续累积文件

| 项 | 内容 |
|----|------|
| 说明 | 每次红队运行都写 `data/traces/trace-rt-*`，长期运行需人工清理 |
| 优先级 | P3 |

#### P3-20 · 金丝雀为合成违规

| 项 | 内容 |
|----|------|
| 说明 | 金丝雀通过伪造执行记录验证检测器，不验证真实攻击链是否可达 |
| 优先级 | P3 |

### 统计

P0 = 0｜P1 = 0 剩余（新增 P1-11 已 FIXED）｜P2 = 17（+3）｜P3 = 20（+2）

---

## 第 46 步：Adversarial Evaluation Lab

### 修复（FIXED）

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P2-18** | 运行 `case_count` 与案例列表长度不一致（13 vs 15） | `case_count` 取了「排除金丝雀」的指标计数，而 cases 列表含金丝雀 | 统一口径为「已执行总数（含金丝雀）」，并新增 `attack_case_count` / `canary_case_count` | `test_cases_endpoint_filters`、`test_coverage_uses_real_data_only` |

### 新增能力

- `/api/red-team/*` 9 个交互接口（生成/运行/案例/详情/Bad Case/覆盖率）
- 难度筛选（generator 层支持 `difficulties`）
- 按 case_ids 选择执行
- `compute_coverage()` 覆盖率地图（严格口径 + 自洽断言）
- 前端 `/adversarial` Adversarial Evaluation Lab（覆盖热力卡片 + Bad Case Explorer 四维筛选）

### 新增技术债

#### P2-19 · 对抗评测运行记录仅内存

| 项 | 内容 |
|----|------|
| 说明 | `_REDTEAM_RUNS` 为进程内字典（上限 20），重启后无法查询历史运行 |
| 影响 | Trace 已落盘可查，但"运行 → 案例 → 结论"的登记信息丢失 |
| 优先级 | P2（与 P2-10/P2-14 同源） |

#### P3-21 · 覆盖率不含"攻击强度"维度

| 项 | 内容 |
|----|------|
| 说明 | 覆盖率只回答"是否执行并判定"，不量化攻击变异强度/绕过难度 |

### 统计

P0 = 0｜P1 = 0 剩余｜P2 = 19（+2，关闭 1）｜P3 = 21（+1）

---

## 第 47 步：自动化 Bad Case 归因

### 修复（FIXED）

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P1-12** | `/api/evaluation/datasets` 硬编码只返回 `dataset_v1`，新增数据集无法在 UI 选择 | 端点直接 `load_dataset()`（默认文件）而非遍历白名单 | 遍历 `ALLOWED_DATASETS` 并逐个加载；详情端点按 `dataset_id` 解析文件 | 新增 2 个数据集均可选；旧测试同步更新断言 |
| **P1-13** | 批量归因极慢（单次 >25s，浏览器测试超时） | 每个案例都 `list_traces()` 全量解析 2000+ Trace 文件 | 增加 30s TTL 的按 `task_id` 索引缓存 | 冷启动 1.36s / 热态 0.05s |
| **P2-20** | 单案例归因接口对「非 Bad Case」也生成报告 | 引擎按 `case_ids` 过滤时从全部案例取，未限制在 Bad Case 内 | 过滤限制在 bad 列表内；API 无报告时返回 409 | `test_api_single_case` |

### 新增能力

- `bad_case_attribution.py`：10 类归因 × 5 级证据、批量/单案例/单 Trace、报告持久化、索引缓存
- `/api/attribution/*` 9 个端点（含证据详情 / Trace / 实验查询）
- 受控失败数据集 `tracepilot-fault-v1`（8 案例，synthetic），用于产生可归因 Bad Case
- 前端归因证据图（5 类关系分层）+ 批量面板 + 单案例抽屉

### 新增技术债

#### P2-21 · 归因为确定性启发式，非语义推理

| 项 | 内容 |
|----|------|
| 说明 | 归因依赖故障类型映射、异常类名映射、指标状态与 DAG 可达性；不做 LLM 语义根因分析 |
| 影响 | 未覆盖的失败模式只能落到 UNKNOWN |
| 优先级 | P2 |

#### P3-22 · 归因报告无清理策略

| 项 | 内容 |
|----|------|
| 说明 | 每次归因写 `data/attribution/reports/attr-*.json`，长期累积需人工清理 |
| 优先级 | P3 |

### 统计

P0 = 0｜P1 = 0 剩余（新增 P1-12、P1-13 均已 FIXED）｜P2 = 21（+3，关闭 1）｜P3 = 22（+1）

---

## 第 48 步：失败模式挖掘

### 修复（FIXED）

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P1-14** | 归因/模式分析的证据下钻 404 | 失败模式挖掘调用 `attribute_case` 只计算不持久化，前端按 `attribution_id` 查询时报告不存在 | 新增 `bad_case_attribution.persist_report()`；挖掘时为新建归因落盘 | 浏览器「证据」下钻可展示归因证据图 |
| **P2-22** | 模式分析在评测完成前触发，后端 400 且不再重试 | `runId` 一出现即触发分析，此时运行仍为 `running` | 前端改为「评测报告就绪后」才挂载；并加 3 次有限重试 | e2e 4 项全通过 |
| **P2-23** | 案例表「证据」按钮不打开抽屉，证据不可见 | 只设置归因状态，未设置所属模式（抽屉以 `active` 为条件） | 点击证据时同时选中所属模式；关闭抽屉时清除归因 | 证据下钻 e2e 通过 |

### 新增能力

- `failure_pattern_mining.py`：5 维指纹（内容哈希可复现）+ 分组统计 + 分母透明 + UNKNOWN 独立计数 + 运行对比 + 语义聚类扩展接口
- `/api/failure-patterns/*` 8 端点
- 前端 Failure Pattern Explorer（分布图 / 排行榜 / 双维筛选 / 证据下钻 / 运行对比 / 分母声明）

### 新增技术债

#### P2-24 · 语义聚类未实现

| 项 | 内容 |
|----|------|
| 说明 | 已预留 `register_cluster_strategy` 接口，但未实现 embedding/语义聚类 |
| 影响 | 只能按结构化特征分组，无法发现"特征不同但语义相同"的失败 |
| 优先级 | P2 |

#### P3-23 · 模式报告无清理策略

| 项 | 内容 |
|----|------|
| 说明 | `data/failure_patterns/reports/` 与 `data/attribution/reports/` 持续累积 |
| 优先级 | P3 |

### 统计

P0 = 0｜P1 = 0 剩余（新增 P1-14 已 FIXED）｜P2 = 24（+3，关闭 1）｜P3 = 23（+1）

---

## 第 49 步：评测版本对比

### 修复（FIXED）

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P2-25** | 标量指标（耗时/Token）参与对比时崩溃 `AttributeError: 'float' object has no attribute 'get'` | 指标差异逻辑默认所有指标都是 `{value,numerator,denominator}` 结构 | 新增 `_norm_metric()`：标量指标包装为 `{value, numerator:null, denominator:null, shape:"scalar"}`，不编造分子分母 | `test_metric_delta_pp_relative_and_scalar` |
| **P2-26** | 同版本「两次独立运行」被 60s 去重吞掉，导致无法做版本对比 | `evaluation_store.submit()` 默认按指纹去重 | 新增 `dedup` 参数 + API `force_new` 字段，对比场景强制创建独立运行 | `test_build_version_descriptor_from_real_run`（同版本不同 run_id） |
| **P2-27** | 双方都未启用 Judge 时，Rubric 检查把「不适用」误报为「元数据缺失」→ 确定性评测永远 `COMPARABLE_WITH_WARNINGS` | `check_comparability` 对所有字段统一「双方缺失即警告」 | 双 Judge 未启用时跳过 Rubric 检查并记录 `RUBRIC_CHECK_SKIPPED`；一侧启用时仍按 CRITICAL 处理 | `test_check_comparability_levels` |

### 新增能力

- `evaluation_version_comparison.py`：版本描述（内容哈希版本 ID）、三档可比性（13 类问题码）、指标差异（pp/相对变化/分子分母/UNKNOWN）、维度差异、Case-level Diff（5 类迁移 + 缺失案例）、失败模式 diff、Trace 链接
- `/api/version-comparison/*` 7 端点 + 报告落盘 `data/version_comparison/reports/`
- 前端 `/version-compare` 工作台（版本选择、可比性横幅、双版本元数据、指标卡、Case Diff、失败模式变化）

### 新增技术债

#### P2-28 · 版本对比依赖内存运行记录

| 项 | 内容 |
|----|------|
| 说明 | `evaluation_store` 仅保留最近 20 条运行（进程内）；重启后历史运行不可对比 |
| 影响 | 对比/回归只对当前进程内运行可用；与 P2-10/P2-14/P2-19 同源 |
| 优先级 | P2 |

#### P2-29 · 每次对比重算失败模式分析

| 项 | 内容 |
|----|------|
| 说明 | `compare_runs()` 每次都调用 `failure_pattern_mining.analyze_run()` 两次；bad case 多时耗时线性增长（实测首次约 3s） |
| 影响 | 对比接口延迟随数据量上升；未做失败模式报告复用/缓存 |
| 优先级 | P2 |

#### P3-24 · 对比/回归报告无清理策略

| 项 | 内容 |
|----|------|
| 说明 | `data/version_comparison/reports/`、`data/regression/reports/` 持续累积（均 gitignored） |
| 优先级 | P3 |

### 统计

P0 = 0｜P1 = 0 剩余｜P2 = 26（+2 新登记 P2-28/P2-29；P2-25~P2-27 已 FIXED）｜P3 = 24（+1 P3-24）

---

## 第 50 步：回归检测

### 修复（FIXED）

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P2-30** | 显式禁用的规则被记为 `NOT_EVALUABLE`，导致门禁永远 `WARN`（或用户关掉规则后反而拿不到 PASS） | 规则求值把 `enabled=false` 与「证据缺失」混为一谈 | 新增 `DISABLED` 状态；不计入 `NOT_EVALUABLE`；报告中单列 `disabled_rules`；**全部规则禁用** → `UNKNOWN/INCONCLUSIVE` | `test_disabled_rules_are_reported_not_hidden` |
| **P2-31** | `NEW_SAFETY_FAILURE` 阈值 0 时「无变化」也会触发 | 触发条件只比较阈值，未要求方向真实发生 | 判据改为 `magnitude < 0`（drop）/`> 0`（rise）严格方向 | `test_all_rules_evaluable_pass` |
| **P2-32** | 基线 Token 为 0 时 `COST_INCREASE` 相对变化未定义被静默跳过 | 相对变化 `None` 时未定义判定口径 | 相对变化无定义时按绝对变化判定，并在 `observed/reason` 明示 | `test_latency_and_cost_increase` |
| **P2-34** | 浏览器全量验收中 `/api/traces` 请求超时（前端 15s），Trace Explorer 无法渲染 | 4300+ Trace 文件每次请求全量解析（实测 4.1s，负载下 >15s） | `trace_store.list_traces()` 增加目录签名缓存（文件数+总大小+最大 mtime）；增删改自动失效，并提供 `clear_trace_list_cache()` | 冷启 5.2s / 热态 1.5s；`test_trace_store_cache.py` 3 项；移动端表格用例恢复通过 |

### 新增能力

- `regression_detector.py`：8 条默认规则（阈值来源显式标注）、5 类规则状态、噪声抑制、风险等级（取最高严重度）、质量门禁（PASS/WARN/BLOCK/INCONCLUSIVE）、证据引用与限制声明
- `/api/regression/*` 9 端点 + 报告落盘 `data/regression/reports/`
- 前端风险矩阵（6 风险维度 × 4 严重度）+ 规则明细 + 门禁横幅

### 新增技术债

#### P2-33 · 阈值未按项目标定

| 项 | 内容 |
|----|------|
| 说明 | 8 条规则阈值为项目默认演示值（`PROJECT_DEFAULT_NOT_INDUSTRY_STANDARD`），未基于历史分布/业务损失标定 |
| 影响 | 阈值可能过松或过严；误报/漏报率未度量 |
| 优先级 | P2 |

#### P3-25 · 回归检测无趋势/多版本对比

| 项 | 内容 |
|----|------|
| 说明 | 只支持两版本成对比较；不支持跨多版本趋势、连续回归或滚动基线 |
| 优先级 | P3 |

### 统计

P0 = 0｜P1 = 0 剩余｜P2 = 27（+1 新登记 P2-33；P2-30~P2-32、P2-34、P2-35 已 FIXED）｜P3 = 25（+1 P3-25）

---

## 第 50 步补充：浏览器验收中发现的性能缺陷

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P2-34** | `/api/traces` 在 4300+ Trace 时前端请求超时（15s），Trace Explorer 空白 | 每次请求全量读取并解析所有 Trace JSON | 目录签名缓存（文件数 + 总大小 + 最新 mtime），增删改自动失效；`clear_trace_list_cache()` 供测试/强制刷新 | `traces cold 5.15s → warm 1.46s`；`test_trace_store_cache.py` 3 项；移动端表格用例通过 |
| **P2-35** | 评测数据集异步加载完成后覆盖用户已选数据集，导致「选择 fault 数据集却跑了 eval 数据集」 | `loadDatasets()` 无条件 `setDataset(datasets[0])`；StrictMode 双请求 / 慢响应下后到的响应覆盖用户选择 | 评估中心与 Trace 对比/归因/修复页的默认选中改为 `setX(prev => prev || default)`，异步响应不得覆盖用户选择 | 浏览器全量验收中归因用例由偶发失败转为稳定通过 |

> 说明：这两类缺陷在浏览器全量验收（80 项）中稳定复现，属于真实产品问题而非测试问题：
> P2-34 修复后未改变 Trace 读写语义（新增/删除/覆盖仍立即可见）；
> P2-35 修复后用户先做出的选择不会被后到的异步响应重置。

---

## 第 51 步：CI/CD 质量门禁

### 修复（FIXED）

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P2-36** | 本地 CI 命令在 baseline 评测完成后立即误报「执行超时」，退出码 3 | 整体超时用 `time.monotonic()` 计算 deadline，却用 `time.time()` 比较（不同时间基准） | 统一使用 `time.monotonic() > deadline` | `test_local_ci_command_passes_exit_zero` |
| **P2-37** | 版本不可比（不同数据集）时门禁返回 `BLOCK` 而不是 `INCONCLUSIVE`，与可比性语义冲突 | 数据完整性检查把「案例集合不同」重复计为缺失证据，BLOCK 优先级高于 INCONCLUSIVE | 不可比场景不再重复把案例集差异计为数据完整性问题，由可比性检查给出 INCONCLUSIVE | `test_gate_inconclusive_nonzero_exit` + 浏览器 INCONCLUSIVE 用例 |

### 新增能力

- `quality_gate.py`：11 项确定性检查、四态门禁、退出码语义、证据缺失不放行、配置校验与指纹（`qg-cfg-*`）
- `config/quality_gate.yaml`：可版本管理的 9 项需求阈值 + 判定下限 + WARN 策略，默认标注为项目演示配置
- `/api/quality-gate/*` 6 端点 + 报告落盘 `data/quality_gate/reports/`（JSON + Markdown）
- `scripts/ci_quality_gate.py`：本地/CI 运行器（确定性 Mock 评测 → 对比 → 回归 → 门禁 → 报告 → 退出码）
- `.github/workflows/agent-evaluation.yml`：GitHub Actions 工作流（YAML 已校验，云端未运行）
- 前端复用 `/version-compare`：CI Quality Gate 卡片（状态/触发规则/阈值与实际值/关联评测/证据入口/CI Run ID）

### 新增技术债

#### P2-38 · 质量门禁依赖内存中的评测运行记录

| 项 | 内容 |
|----|------|
| 说明 | 门禁通过 `evaluation_store` 读取候选评测报告（内存保留最近 20 条）；进程重启后报告中的 run ID 不再可查询，只能使用已落盘的 JSON/Markdown 结论 |
| 影响 | API 侧重新执行门禁需要在同一进程内；CI 中每次运行自建运行故不受影响 |
| 优先级 | P2（与 P2-10/P2-14/P2-19/P2-28 同源） |

#### P3-26 · GitHub 云端 CI 未实际运行

| 项 | 内容 |
|----|------|
| 说明 | 仓库当前没有 GitHub 远程仓库（`git remote -v` 为空），工作流只做了 YAML 语法校验，未在 GitHub 上执行 |
| 影响 | 本地工作流通过不等于云端通过；远端分支保护/缓存/action 行为未验证 |
| 后续 | 配置远程仓库后实际运行一次 `agent-evaluation` 并人工确认结果 |
| 优先级 | P3 |

#### P3-27 · 质量门禁报告无保留策略

| 项 | 内容 |
|----|------|
| 说明 | `data/quality_gate/reports/` 与 `ci-reports/` 持续累积（均 gitignored）；仅 GitHub artifact 有 14 天保留 |
| 优先级 | P3 |

### 统计

P0 = 0｜P1 = 0 剩余｜P2 = 28（+1 新登记 P2-38；P2-36、P2-37 已 FIXED）｜P3 = 27（+2：P3-26 云端 CI 未验证、P3-27 报告无保留策略）

---

## 第 52 步：自动化评测流水线

### 修复（FIXED）

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P2-40** | 流水线终态在 `_finalize_pipeline` 尚未完成时即发布，查询方可能看到 `SUCCEEDED` 但 `final_report_paths=null` 的中间态 | 先写 `record["status"]=终态` 再生成报告路径/落盘 | 终态延后到报告路径与 Markdown 全部写入后再发布；报告内 status 同步更新 | `test_full_mock_pipeline_fault_dataset` |
| **P2-41** | 未选中流水线（例如页面刷新后）时，历史流水线列表被报告条件隐藏，无法查看历史 | 历史卡片写在 `report` 条件分支内部 | 将历史卡片移出报告条件，任何状态都可见；刷新后可点「查看」恢复运行状态 | `e2e/pipeline.spec.ts` 刷新恢复用例 |

### 新增能力

- `evaluation_pipeline.py`：11 阶段编排、7 类状态、可复现性记录、真实工件引用、执行控制（创建/启动/取消/历史/报告）、限额与有限重试
- 抽取 `evaluation_engine.build_evaluation_report_from_results()` / `persist_trace()`（无行为变更）
- `evaluation_store.register_report()` / `update_report()`：登记流水线真实产生的报告（不重新执行）
- `/api/pipelines/*` 8 端点
- 前端 `/pipelines` Pipeline Run Inspector（配置 / 阶段流程图 / 阶段详情与日志 / 最终报告与证据入口 / 历史）

### 新增技术债

#### P2-39 · 流水线执行器为单线程内存队列

| 项 | 内容 |
|----|------|
| 说明 | 执行器为进程内 `ThreadPoolExecutor(max_workers=1)`；无任务持久化、无跨进程恢复、无分布式调度 |
| 影响 | 进程重启后运行中流水线不会自动恢复；并发上限为 1（设计限制） |
| 优先级 | P2 |

#### P3-28 · 已授权 real_api 流水线路径未验证

| 项 | 内容 |
|----|------|
| 说明 | `execution_mode=real_api` 的双重授权门已测试（未授权 403、零调用）；**已授权后的真实执行路径未实际验证**（会产生付费调用，未获授权） |
| 影响 | 真实 API 流水线的执行/评测/报告链路状态为 NOT_VERIFIED |
| 优先级 | P3 |

#### P3-29 · 流水线报告无自动保留/清理策略

| 项 | 内容 |
|----|------|
| 说明 | `data/pipelines/reports/` 持续累积（已 gitignored）；实测已产生 179 份快照/132 份 Markdown |
| 优先级 | P3 |

### 统计

P0 = 0｜P1 = 0 剩余｜P2 = 29（+1 新登记 P2-39；P2-40、P2-41 已 FIXED）｜P3 = 29（+2：P3-28 真实 API 流水线未验证、P3-29 报告无清理策略）

---

## 第 53 步：Token & Cost Analytics（Token 与成本分析）

### 修复（FIXED）

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P2-42** | 成本分析页并发加载时 `/api/cost/overview` 超过前端 60s 超时（浏览器用例报「请求超时」，页面无 KPI） | 多个请求同时发现分析缓存失效，各自对 9.7k Trace 做全量 CPU 密集分析（GIL 下互相拖慢，实测并发冷启动 >60s） | 增加单飞锁（`_ANALYSIS_LOCK`）：只允许一个线程重算，其余等待后复用结果；Trace 解析与分析结果按文件 mtime/size + 价格指纹增量缓存 | 冷启动 33s（单次）、热态 0–4s；`e2e/cost-analytics.spec.ts` 3 项由失败转为全通过 |
| **P2-43** | 成本归因的聚合维度用量来源被硬编码（evaluation 恒为 `SIMULATED`、pipeline 恒为 `MIXED`），且不支持按用量来源过滤，无法区分真实 API 与模拟调用 | `cost_attribution()` 在聚合行直接写死来源，未使用实际调用计数；API 无过滤参数 | 新增 `_merged_usage_source()`（按实际 counts 合并：单来源→该来源、多来源→`MIXED`、空→`UNKNOWN`）；`/api/cost/attribution` 支持 `usage_source` 过滤；前端归因表格新增「用量来源」筛选 | `test_attribution_usage_source_filter` + API 过滤/非法值断言；真实 Trace 可筛出 4 条 `PROVIDER_REPORTED` 调用 |
| **P2-44** | 本地 CI 脚本（`scripts/ci_quality_gate.py`）输出编码依赖环境：Windows 默认 cp936 时中文日志在 UTF-8 解析器（测试子进程 / GitHub Actions）中变成乱码，导致 3 个既有测试在默认 PowerShell 环境失败 | 脚本未固定 stdout/stderr 编码，而测试按 UTF-8 解码 | 脚本启动时显式 `reconfigure(encoding="utf-8")`（失败时兜底），不改变退出码语义 | `test_ci_quality_gate_script.py` 4 项在默认环境通过 |
| **P2-48** | 浏览器全量验收中「批量归因」与「失败模式分析」在并发负载下报「请求超时」，归因面板无结果 | 两个接口为 CPU 密集任务，前端沿用 15s 默认超时；成本分析冷启动等并发负载下实际耗时超过 15s | 为 `/api/attribution/runs` 与 `/api/failure-patterns/runs` 显式设置 120s 超时（仍受服务端案例数/时长上限约束） | 全量浏览器验收 93 项全通过（该用例由偶发失败转稳定） |

### 新增能力

- `backend/app/services/token_analytics.py`：用量来源四分、价格表加载/校验、Decimal 成本计算、延迟计算、成本聚合、归因、版本/模型对比、UNKNOWN 报告
- `config/model_pricing.yaml`：版本化价格配置（provider/model/currency/输入/输出/缓存单价/生效日期/版本/来源）；**当前无官方可信价格 → `PRICE_UNKNOWN`，不编造**
- Trace 采集扩展（不修改 Schema）：step `metadata.usage_meta`（provider / model / usage_source / raw_usage / recorded_at）
- `/api/cost/*` 10 端点（pricing / overview / models / versions / unknown / attribution / traces / runs / evaluations / pipelines）
- 前端 `/costs` Cost Analytics 工作台（KPI、成本口径声明、趋势、模型分布、延迟、价格版本与数据来源、版本对比、Cost Attribution Explorer、单对象查询、UNKNOWN 清单；PC/平板/手机）
- `/api/cost/attribution` 支持 `usage_source` 过滤（`PROVIDER_REPORTED / ESTIMATED / SIMULATED / UNKNOWN / MIXED`）

### 新增技术债

#### P2-45 · 无可信官方价格，真实调用成本不可给出金额

| 项 | 内容 |
|----|------|
| 说明 | 仓库 `config/model_pricing.yaml` 中 DeepSeek 条目价格均为 `null`（不编造官方价格），真实 API 调用成本一律 `PRICE_UNKNOWN`；只有模拟执行器有 0 价（`SIMULATED`） |
| 影响 | 无法展示真实账单/成本金额；用户需自行填入有来源、有生效日期的官方价格后才能得到估算值（仍非账单） |
| 优先级 | P2（诚实性优先，不编造） |

#### P2-46 · 真实 API 用量样本仅 1 条，分布/版本对比代表性不足

| 项 | 内容 |
|----|------|
| 说明 | 全库仅 1 条真实 API Trace（`trace-4d3d82dc…`，Provider 报告 1852 Token）；其余约 9.7k 条为模拟/评测 Trace |
| 影响 | 模型成本分布、版本成本对比、缓存 Token 语义等无法用真实流量验证；当前结论仅覆盖确定性模拟数据 |
| 优先级 | P2 |

#### P2-47 · 评测/流水线运行记录为内存态，成本归因重启后不完整

| 项 | 内容 |
|----|------|
| 说明 | 评测运行与流水线运行主要保存在进程内存（流水线有磁盘快照）；服务重启后对应运行不可查询 |
| 影响 | Trace 级成本仍可从文件计算，但评测成本、流水线成本、部分归因行的历史记录在重启后不可查（与 P2-28/P2-38/P2-39 同源） |
| 优先级 | P2 |

#### P3-30 · 成本分析冷启动为全量计算，未持久化分析索引

| 项 | 内容 |
|----|------|
| 说明 | 首次请求需解析并分析全部 Trace（当前 9,766 条约 33s）；虽已做增量缓存与单飞，但进程重启后仍需一次全量分析 |
| 影响 | 冷启动首个成本页面请求较慢；Trace 数量继续增长时线性变慢 |
| 优先级 | P3 |

#### P3-31 · `agent_version` 缺失导致版本成本对比为代理分组

| 项 | 内容 |
|----|------|
| 说明 | Trace 未写入 `agent_version`；版本成本对比退化为按模型/执行模式分组，并在响应中显式标注 `agent_version_note` |
| 影响 | 无法做真实 Agent 版本间的成本变化对比，只能作为数据诊断 |
| 优先级 | P3 |

### 统计

P0 = 0｜P1 = 0 剩余｜P2 = 32（+3 新登记 P2-45 / P2-46 / P2-47；P2-42、P2-43、P2-44、P2-48 已 FIXED）｜P3 = 31（+2：P3-30 冷启动全量分析、P3-31 agent_version 缺失代理对比）

---

## 第 54 步：成本–延迟–质量帕累托分析

### 修复（FIXED）

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P2-49** | 存在跨数据集候选时，整个分析被判 `NOT_COMPARABLE`，连候选数最多的数据集组也无法计算前沿（即使指定 dataset_version 也不行） | 数据集不一致问题被记为组级 CRITICAL，污染了可比组自身的判定 | 按 `dataset_id + dataset_version` 分组，自动选择候选数最多的可比组；跨组候选逐条标 `NOT_COMPARABLE` 并排除；组级仅给 WARNING（明确提示跨数据集不可比），混合真实/模拟仍为 CRITICAL | `test_dataset_mismatch_marks_not_comparable` + API 实测：3 候选跨 2 数据集 → 组内 2 候选、可行 2、前沿 1 |
| **P2-51** | 全量浏览器验收中 Trace Explorer / 流程 B/C 偶发 `/api/traces` 超时（前端 15s），页面显示「请求超时」 | Trace 文件增长到 7k+ 后，每次目录签名变化（新增 mock/eval Trace）都会全量重新反序列化；全站并发多个请求同时触发重复全量解析（GIL 争用）；每次请求还要为数千条 Trace 重建摘要字典 | `trace_store.list_traces()` 增加文件级增量解析缓存（mtime/size 未变则复用记录）+ 单飞锁（并发请求只重解析一次）；新增 `list_trace_summaries()` 与记录共享缓存（摘要只构建一次）；`fetchTraces()` 前端超时显式放宽到 60s | `test_trace_store_cache.py` 3 项（含摘要共享缓存）；端点冷启动 16s（首次全量解析）/ 热态约 3.3s（仅 JSON 序列化）；全量浏览器验收通过 |
| **P2-52** | 帕累托交互约束切换到另一个数据集时，筛选结果误判 `NO_COMPARABLE_CANDIDATE`（沿用了原分析中「组外不可比」状态） | `_filtered_view()` 只改 `in_comparison_group` 标记，未基于新数据集组重新做可比性检查 | 切换 dataset_version 时重新调用 `_assess_comparability()` 计算新组状态，再应用约束 | `test_filter_view_switches_dataset_group_recomputes_comparability`；浏览器约束筛选用例通过 |

### 新增能力

- `backend/app/services/pareto_analysis.py`：候选构建（真实评测运行）、9 项可比性检查、三维支配/非支配计算、并列处理、约束筛选、安全门禁筛选、报告持久化
- `/api/pareto/*` 10 端点（candidates / analyses 创建·列表·查询 / frontier / dominated / dominance / filter / 关联 Evaluation Run / 关联 Trace）
- 前端 `/pareto` Interactive Pareto Explorer：约束面板、可旋转三维散点 + 二维投影、候选详情、Frontier/被支配/支配关系/排除原因、响应式
- 测试：`tests/test_pareto_analysis.py` 20 项；`frontend/e2e/pareto.spec.ts` 3 项

### 新增技术债

#### P2-50 · 帕累托候选池依赖内存评测运行

| 项 | 内容 |
|----|------|
| 说明 | 候选通过 `evaluation_store`（进程内保留最近 20 条运行）构建；服务重启后候选为空。已落盘的分析报告 JSON 仍可查询，但关联 Evaluation Run / Trace 可能 404 |
| 影响 | 重启后无法重新计算同一候选集合；历史分析只能看快照 |
| 优先级 | P2（与 P2-28/P2-38/P2-47 同源） |

#### P3-32 · 三维散点为自绘等距投影

| 项 | 内容 |
|----|------|
| 说明 | 使用 SVG 自绘等距投影 + 旋转/俯仰滑杆，未使用 WebGL；无坐标轴刻度、缩放、旋转惯性 |
| 影响 | 大数据集下散点可能重叠；移动端依赖二维投影切换 |
| 优先级 | P3 |

#### P3-33 · 安全门禁状态依赖已落盘门禁报告

| 项 | 内容 |
|----|------|
| 说明 | `safety_gate_status` 只从 `data/quality_gate/reports/` 读取（需先运行质量门禁）；大多数评测候选没有门禁记录，`required_safety_gate` 筛选会严格排除 |
| 影响 | 门禁筛选常返回 `NO_FEASIBLE_CANDIDATE`（诚实但可用性受数据限制） |
| 优先级 | P3 |

### 统计

P0 = 0｜P1 = 0 剩余｜P2 = 33（+1 新登记 P2-50；P2-49、P2-51、P2-52 已 FIXED）｜P3 = 33（+2：P3-32 / P3-33）

---

## 第 55 步：需求回溯与缺口审计

### P0（重新确认，BLOCKED）

#### P0-4 · 真实 Agent 执行链仅历史 1 次手工验证，缺少自动化真实 API 验证

| 项 | 内容 |
|----|------|
| 事实 | 全库仅 1 条真实 API Trace（任务「计算125乘以48」，1852 Token）；真实 API 调用路径无自动化测试、无 CI 覆盖 |
| 原始需求 | PDF 要求「在真实 Agent 和工具调用环境中完整实现并自动化验证所有功能」 |
| 状态 | **BLOCKED（未修复）**：真实调用产生费用，需用户逐次授权；既有安全边界禁止未授权付费调用，本步 0 次调用 |
| 影响 | 真实模型规划质量、缓存成本、真实故障分布与归因准确率仍无证据；不能宣称「真实生产验证」 |
| 解除条件 | 用户显式授权 + `TRACEPILOT_ALLOW_REAL_API=1` + 有效 Key；补跑 3–5 个任务并记录 Trace/Token/成本（成本仍 PRICE_UNKNOWN 直到配置可信价格） |

### 修复（FIXED / PARTIAL）

| 编号 | 问题 | 修复 | 证据 |
|------|------|------|------|
| **P1-55-1** | 根因准确率指标（Top-1/Top-3/误归因/修复率/成本/基线）缺失，创新点 6 无实验 | `rootcause_metrics.py` + `/api/attribution/metrics` + Evaluation Center 指标卡 | `test_rootcause_metrics.py` 5 项 + `e2e/capabilities.spec.ts` |
| **P1-55-2** | RAG/Memory/Skill/Context Review/Context Engineering/Reflection/HITL/Explainability/Execution Environment 缺失或仅文档 | `capability_suite.py` + `capability_tools.py` + `/api/capabilities/matrix` + `/api/hitl/*` + 能力矩阵 UI | `test_capability_suite.py` 13 项 + e2e 4 项；部分能力仍 PARTIAL（HITL/Explainability/ExecutionEnv） |
| **P1-55-3** | 无 Docker 部署配置 | Dockerfile / docker-compose / frontend Dockerfile / nginx.conf / docs/DEPLOYMENT.md | 配置审查；本机无 Docker → **运行时 NOT_VERIFIED** |

### 新增技术债

#### P2-53 · 容器部署未实际构建运行

| 项 | 内容 |
|----|------|
| 说明 | 配置已提供，但开发机未安装 Docker；`docker compose build/up` 未执行，nginx 反代/volume 路径未验证 |
| 影响 | 不能宣称「可部署 Demo」已闭环；仅 PARTIAL/L1 |
| 优先级 | P2 |

#### P2-54 · 报告导出仅有后端 JSON/Markdown 落盘

| 项 | 内容 |
|----|------|
| 说明 | 原始需求要求导出 JSON/CSV/PDF；当前报告落盘 JSON/Markdown，无 UI 导出与 PDF 生成 |
| 影响 | 面试/交付时需手工读取文件；PDF 交付未实现 |
| 优先级 | P2 |

#### P2-55 · 不确定性统计与因果剪枝缺少对照实验

| 项 | 内容 |
|----|------|
| 说明 | 原始创新点 3/4 要求置信区间/重采样与剪枝效率对照；当前用证据等级+门禁四态，刻意不输出无来源概率/CI |
| 影响 | 无法量化漏诊/误诊率与剪枝成本收益；创新点仍 PARTIAL |
| 优先级 | P2（需独立实验设计，不能伪造统计数字） |

#### P3-34 · 因果轨迹代数未形式化

| 项 | 内容 |
|----|------|
| 说明 | 干预以受控操作符实现，未给出完整代数系统与形式化证明 |
| 优先级 | P3 |

#### P3-35 · RAG/Skill 缺 L3 专属可视化

| 项 | 内容 |
|----|------|
| 说明 | 能力步骤在 Trace/DAG 中可见，但无 RAG 命中详情/Skill 组合编排的专属面板；核心能力 L3 验收仅部分满足 |
| 优先级 | P3 |

#### P3-36 · 作品集 PDF/演示视频/在线 Demo 未产出

| 项 | 内容 |
|----|------|
| 说明 | 原始需求交付要求可下载 PDF、演示链接/视频；当前只有代码/文档/本地 Demo |
| 优先级 | P3 |

### 统计

P0 = 1（P0-4 BLOCKED，需授权）｜P1 = 0 剩余（P1-55-1/2/3 已修复或部分修复，P1-55-4 转 P2-55）｜P2 = 36（+3：P2-53/54/55）｜P3 = 36（+3：P3-34/35/36）

---

## 第 55-A 步：数据治理与可信度修复

### 修复（FIXED）

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P1-55A-1** | 完整后端测试会话会通过生产默认路径写入 Trace，污染统计口径（实测一次会话 +565 条：SIMULATED 452 / MOCK 22 / REPLAY 91） | AgentRunner / 评测执行器使用生产默认目录；测试未隔离 | 新增 `scripts/quarantine_test_pollution.py`（以第 55-A 备份为基线，移动而非删除，写 manifest）；`tests/conftest.py` 会话级 autouse fixture：测试结束把本会话新增 Trace 移到系统临时隔离目录，并还原被测试重写的 redteam 数据集 | 文件数从 8869/8103 恢复为 8618/7790；隔离区 `data/governance/quarantine/step55a_20261119_105430/` 含 565 文件 + manifest；683 项后端测试后目录仍为 8618/7790 |
| **P1-55A-2** | Trace 详情把 `data_source=real_api` 且 `simulated=false` 直接展示为「REAL API」，缺少独立验证证据也会被升级 | 前端 `DataSourceTag` 只判断 `data_source`，未使用后端分类 | 新增 `/api/governance/traces/{id}` 分类端点；前端以后端 `source_class` 为准，缺 `real_api_verified` 时显示 UNKNOWN（`real_api` 未验证） | `tests/test_data_governance.py` 分类规则 9 项；`e2e/data-governance.spec.ts` Trace 详情用例 |
| **P1-55A-3** | 统计口径不一致：Overview 8,617 / Cost Analytics 16,407 / Trace Explorer 全量无筛选，用户无法判断数据从哪来、为什么不同 | 两个页面读取目录不同且未标注；列表接口无过滤；失败 Trace 无法筛选 | `/api/traces` 增加 `source_class/status/fault_only` 过滤与 `total_count/by_source/by_status/scope_note`；Overview 增加数据口径 + 来源分类 + 失败口径；Trace Explorer 增加筛选/分页/空态；Cost Analytics 增加来源范围与 UNKNOWN 不按 0 展示 | `test_traces_filter_by_source_and_status` 等 8 项 API 测试；`e2e/data-governance.spec.ts` 4 项；`tests/test_token_analytics.py` 回归通过 |
| **P2-59** | 成本归因「按流水线」维度从秒级恶化到 >5 分钟（浏览器用例超时） | `analyze_pipeline_run()` 对每条历史流水线的失效 Trace 引用逐个触发 `list_all_traces(force=True)` 全库重解析（1.7 万份 JSON × 200 条流水线） | 流水线归因改用 `_get_trace(allow_full_reload=False)`，失效引用计入 `missing_trace_ids` 并跳过；单条流水线异常不阻断整体归因 | `test_get_trace_can_skip_full_reload` + `e2e/cost-analytics.spec.ts` 通过 |
| **P2-60** | `/api/governance/sources` 在目录变化后每个请求同步全量扫描 35s，Overview 与浏览器用例超时 | sidecar 签名失效时同步全量扫描；并发请求无单飞锁 | 增加单飞锁 + stale-while-revalidate：页面读取返回旧快照（`stale=true`）并后台重扫回写 sidecar；审计/脚本仍同步扫描 | `test_sources_stale_while_revalidate` + `e2e/data-governance.spec.ts` 通过 |

### 新增能力

- `backend/app/services/data_governance.py`：来源分类（REAL_API / MOCK / SIMULATED / REPLAY / UNKNOWN，保守规则）、全库扫描（sidecar + 30s 缓存 + 目录签名）、非破坏迁移、单条分类、四类受控实验资格（含逐项原因/要求）
- `/api/governance/*` 5 端点 + `/api/traces` / `/api/cost/*` 来源过滤
- 前端：Overview（口径/来源表/失败口径）、Trace Explorer（筛选/分页）、Trace 详情（来源分类/资格）、Cost Analytics（来源范围/UNKNOWN 不按 0）
- 脚本：`audit_data_sources.py`、`quarantine_test_pollution.py`、`create_mock_failure_trace.py`
- 文档 `docs/STEP55A_DATA_AUDIT.md`

### 新增技术债

#### P2-56 · 受控实验入口未接入资格过滤

| 项 | 内容 |
|----|------|
| 说明 | `/api/governance/traces/{id}/eligibility` 已提供四类资格判断，但 Fault Lab / Counterfactual Lab / Repair Studio 仍展示主目录全部 8,618 条 Trace，不区分是否可用 |
| 影响 | 用户仍可能选中不可用 Trace 后才看到错误；体验与数据可信度受损 |
| 后续 | 55-B 在三个页面接入资格过滤与不可用原因展示（数据与 API 已就绪） |
| 优先级 | P2 |

#### P2-57 · 报告类数据目录仍会被测试写入

| 项 | 内容 |
|----|------|
| 说明 | `data/attribution|failure_patterns|pipelines|quality_gate|regression|version_comparison|pareto|rootcause_metrics/reports` 等报告目录在测试后新增文件（多被 gitignore）；本步只隔离了 Trace 目录与 redteam 数据集 |
| 影响 | 报告列表可能混入测试产生的记录；不影响来源分类与统计 KPI（这些是派生报告，不是 Trace） |
| 优先级 | P2 |

#### P2-58 · `/api/traces` 未做服务端分页

| 项 | 内容 |
|----|------|
| 说明 | 默认返回主目录全部 8,618 条摘要（约 2–3 MB），分页/每页 50 在前端完成；筛选已在服务端 |
| 影响 | Trace 数量继续增长时首屏传输与内存占用线性上升 |
| 优先级 | P2 |

#### P3-37 · 来源分类 sidecar 体积随 Trace 增长

| 项 | 内容 |
|----|------|
| 说明 | `data/governance/source_index.json` 当前约 7.2 MB（16,408 条逐条分类记录），随 Trace 数线性增长；已用目录签名避免每次重扫 |
| 影响 | 大库下 sidecar 读取/写入变慢；可改 SQLite 或分片 |
| 优先级 | P3 |

#### P3-38 · 模拟 Trace 的 0 Token / 0 成本语义易被误读

| 项 | 内容 |
|----|------|
| 说明 | 模拟执行器不消耗 Token，`token_usage` 为 0（已知用量），成本显示 SIMULATED 0；页面已注明「0 表示未调用付费 API，不是真实计费」，但仍可能被误读为「真实成本为 0」 |
| 影响 | 面试/演示时需口头解释；前端文案已尽力区分 |
| 优先级 | P3 |

### 统计

P0 = 1（P0-4 BLOCKED，需授权）｜P1 = 0 剩余（P1-55A-1/2/3 已修复）｜P2 = 39 未解决（+3：P2-56/57/58；P2-59/P2-60 已 FIXED）｜P3 = 38（+2：P3-37/38）
