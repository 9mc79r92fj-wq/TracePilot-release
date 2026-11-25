# 全站浏览器交互验收报告（第 42 步）

- 验收时间：2026-11-18
- 工具：**Playwright 1.65.1** + **Microsoft Edge**（`channel: 'msedge'`，复用系统浏览器，无需下载 Chromium）
- 测试目录：`frontend/e2e/`
- 配置：`frontend/playwright.config.ts`（viewport 1440×900，workers=1，retries=0）
- 运行命令：`cd frontend && npx playwright test --project=desktop-edge`（或 `npm run test:e2e`）

---

## 1. 测试环境

| 项 | 值 |
|----|-----|
| 后端 | `http://127.0.0.1:8000`（uvicorn，`TRACEPILOT_ALLOW_REAL_API` 未开启） |
| 前端 | `http://localhost:5173`（Vite dev server） |
| 浏览器 | Microsoft Edge（系统安装，Playwright 通过 channel 复用） |
| 数据 | 后端 `data/traces` 真实存在的 **mock 运行** Trace（`data_source=mock`）；无付费 API 调用 |
| 依赖 | `@playwright/test@1.65.1`（新增 devDependency） |

---

## 2. 真实页面清单（依据 `src/App.tsx`，非假设）

| # | 路由 | 页面 | 实现状态 |
|---|------|------|---------|
| 1 | `/` | Overview | ✅ 真实实现 |
| 2 | `/traces` | Trace Explorer | ✅ 真实实现 |
| 3 | `/traces/:traceId` | Trace 详情 | ✅ 真实实现 |
| 4 | `/dag`、`/dag/:traceId` | DAG Analysis（含 Trace Time Travel） | ✅ 真实实现 |
| 5 | `/runs` | Agent Runs（运行控制台） | ✅ 真实实现 |
| 6 | `/fault-lab` | Fault Lab（Failure Replay） | ✅ 真实实现 |
| 7 | `/counterfactual` | Counterfactual Lab | ✅ 真实实现 |
| 8 | `/repair` | Repair Studio | ✅ 真实实现 |
| 9 | `/evaluation` | Evaluation Center | ✅ 真实实现 |
| 10 | `/calibration` | Judge Calibration | ✅ 真实实现 |
| 11 | `/multi-judge` | Disagreement Explorer | ✅ 真实实现 |
| 12 | `/architecture` | Architecture | ✅ 真实实现 |
| 13 | `/comparison` | Trace Comparison | ⚠️ **占位页**（明确标注"尚未实现"） |
| 14 | `/attribution` | Fault Attribution | ⚠️ **占位页**（明确标注"尚未实现"） |

**结论：12 个真实页面 + 2 个占位页 = 14 个导航入口。**

---

## 3. 测试结果总览

| 状态 | 数量 |
|------|------|
| **PASS** | **41** |
| FAIL | 0 |
| BLOCKED | 0 |
| NOT_TESTED | 0（本轮范围内） |

套件构成：

| 文件 | 用例数 | 覆盖 |
|------|--------|------|
| `e2e/pages.spec.ts` | 34 | 11 个真实页面「可打开 + 无白屏 + 刷新不 404」、2 个占位页标注、导航跳转与高亮、侧栏折叠 |
| `e2e/flows.spec.ts` | 7 | 6 大核心业务流程（A/B/C/D/E/F1/F2/F3） |
| `e2e/responsive.spec.ts` | 8 | PC/平板/手机三档 × 11 页横向溢出与可读性、手机侧栏、表格滚动、DAG 缩放、抽屉不超屏 |

---

## 4. 逐项验收对照（第三部分 12 项）

| # | 检查项 | 结果 | 证据 |
|---|--------|------|------|
| 1 | 页面能够打开 | PASS | 11/11 页面 HTTP < 400 且主内容非空 |
| 2 | 页面刷新不 404 | PASS | 11/11 页面 reload 后仍 200 |
| 3 | 导航能够正常跳转 | PASS | 逐个点击 11 个导航项，URL 与标题正确、当前项高亮 |
| 4 | 按钮能够正常响应 | PASS | 运行任务/开始重放/运行实验/验证修复/运行评测/运行校准/运行多 Judge/折叠侧栏 均实测生效 |
| 5 | 表单能够提交 | PASS | 任务输入、案例上限、重复评分次数均提交成功并触发后端执行 |
| 6 | 下拉框能够选择数据 | PASS | 数据集/配置组/Trace/Rubric 下拉均有真实 option 并可选中 |
| 7 | API 请求正常 | PASS | 所有页面操作触发的 `/api/*` 请求均为 2xx（无 4xx/5xx，除有意测试的 404 页） |
| 8 | 加载状态正常 | PASS | 页面显示 Spinner/「执行中…」文案，加载完成后消失 |
| 9 | 错误提示正常 | PASS | 未授权真实 API → 页面显示 403 错误框；后端错误以 ErrorBox 呈现 |
| 10 | 空数据状态正常 | PASS | Repair Studio 无候选时显示明确空状态；抽屉/列表空态有提示 |
| 11 | 页面不会出现白屏 | PASS | 每页 `main` 文本长度 > 10 |
| 12 | 不出现严重 JS 异常 | PASS | 所有页面 `pageerror` 与 `console.error` 为空（过滤 favicon/网络噪音后） |

---

## 5. 六大核心业务流程结果

| 流程 | 覆盖内容 | 结果 |
|------|---------|------|
| **A. Agent 运行** | 输入任务 → 提交执行 → 查看结果（来源徽标/答案/工具调用）→ 打开 Trace → 打开 DAG | **PASS** |
| **B. 故障分析** | Trace Explorer 选 Trace → 详情 → Fault Lab 只读重放显示故障位置 → DAG 可切换 Trace 结构对比 | **PASS** |
| **C. 反事实实验** | 选故障 Trace → 配置 remove_fault 干预 → 运行受控实验 → Evidence Strength 卡片 | **PASS** |
| **D. 修复验证** | 选含可修复故障的 Trace → 候选卡片 → 选中查看详情 → 验证修复 → Repair Confidence Breakdown | **PASS** |
| **E. Agent 评测** | 选数据集 → 运行 → 六维指标 → 案例表 → 证据抽屉 → 跳转 Trace | **PASS** |
| **F1. Mock Judge** | 选 Mock Judge + Rubric → 运行评测 → LLM Judge 评审卡片（MOCK 标记/均分） | **PASS** |
| **F2. Judge 校准** | 运行校准 → Exact Agreement/MAE/Within-1 → Confusion Matrix → synthetic_reference 声明 | **PASS** |
| **F3. 多 Judge 仲裁** | 选配置组 → 运行 → 独立性等级 → 分歧指标 → 仲裁结果 | **PASS** |

所有流程默认 **Mock 模式**，未调用任何付费 API。

---

## 6. 响应式与 UI 验收

| 检查项 | PC 1440 | Tablet 834 | Mobile 390 |
|--------|---------|-----------|-----------|
| 导航可用 | PASS | PASS | PASS（侧栏隐藏 + 汉堡展开） |
| 表格溢出 | PASS | PASS（修复后） | PASS（修复后，容器内滚动） |
| 图表遮挡 | PASS | PASS | PASS |
| 按钮可点击 | PASS | PASS | PASS |
| 文字可读（≥11px） | PASS | PASS | PASS |
| 弹窗不超屏 | PASS | PASS | PASS（抽屉 ≤ 屏宽） |
| DAG 可缩放 | PASS（缩放控件实测改变节点尺寸） | — | PASS |
| 横向异常滚动 | PASS | PASS（修复前 183–1057px 溢出） | PASS（修复前 447–817px 溢出） |

---

## 7. 发现并修复的问题

| # | 问题 | 严重度 | 根因 | 修复 | 验证 |
|---|------|--------|------|------|------|
| 1 | **Judge Calibration 页面运行校准必然 404** | **P0** | 前端传 JSON 内部 `calibration_id`（`judge-calibration-v1`），后端仅按文件名 stem（`calibration_set_v1`）查找 | `load_calibration_dataset` 同时支持两种标识（回退按 `calibration_id` 匹配），并新增回归测试 | F2 流程 PASS；新增 `test_dataset_id_both_identifiers` |
| 2 | **全站窄屏横向溢出**（表格/下拉/网格） | **P1** | 宽表格无滚动容器、`select` 被长 option 撑宽、grid 子项缺 `min-width:0` | 新增 `src/responsive-fixes.css`：卡片内容区 `overflow-x:auto`、控件 `max-width:100%`、网格子项 `min-width:0`、移动端字号/布局微调 | 22 个「视口×页面」组合溢出全部归零 |
| 3 | **侧栏折叠按钮无可访问名称** | **P1（可访问性）** | 折叠后文字隐藏，按钮仅剩图标，读屏/键盘无法识别 | 增加动态 `aria-label`（收起侧栏/展开侧栏）、`aria-expanded`、`title` | `侧边栏可折叠` 用例 PASS |
| 4 | **Trace 详情页不显示数据来源** | **P1（可信度）** | 页面未渲染 `metadata.data_source` | 新增 `DataSourceTag` 组件：REAL API / MOCK·模拟 / SIMULATED / 未标记 | 流程 A 断言「数据来源 + MOCK·模拟」PASS |
| 5 | **Repair Studio 默认选中无候选的 Trace** | **P2** | 仅按「有故障」选取，未考虑故障类型是否在修复白名单 | 优先选择 `tool_timeout`/`tool_exception`/`invalid_arguments`/`permission_denied` 的 Trace | 流程 D 候选卡片正常出现 |
| 6 | **验证修复后详情面板消失** | **P1** | 验证后候选对象重新生成（`repair_id` 变化），旧的 `selected` 失效 | 收到报告后自动选中「推荐方案」或首个候选 | 流程 D PASS |

---

## 8. 未解决的问题（NOT_TESTED / 已知限制）

| # | 项 | 状态 | 说明 |
|---|----|------|------|
| 1 | 真实 DeepSeek API 的浏览器端到端流程 | **NOT_TESTED** | 需服务端开关 + 付费调用；本轮**未**开启（`TRACEPILOT_ALLOW_REAL_API` 未设置），页面显示 Real 模式不可用 |
| 2 | `/comparison`、`/attribution` 的功能验收 | **NOT_TESTED** | 页面仍为占位（已断言其明确标注「尚未实现」，不计为功能 PASS） |
| 3 | 视觉像素级回归（配色/间距截图对比） | **NOT_TESTED** | 未引入视觉快照基线 |
| 4 | 跨浏览器（Chrome/Firefox/Safari） | **NOT_TESTED** | 仅验证 Edge；Playwright 配置可扩展 projects |
| 5 | 无障碍自动化（axe-core） | **NOT_TESTED** | 仅手工修了折叠按钮的 aria；未做全站 a11y 扫描 |
| 6 | 并发/压力下的 UI 行为 | **NOT_TESTED** | 单 worker 顺序执行 |
| 7 | 浏览器控制台非致命警告 | 记录 | 存在 React/ReactFlow 的 dev 警告，未作为失败项（非 error 级） |

---

## 9. 浏览器测试覆盖情况

| 覆盖类别 | 覆盖情况 |
|---------|---------|
| 页面可访问性（11 真实页） | ✅ 100% |
| 刷新与深链接 | ✅ 100% |
| 导航交互 | ✅ 100%（11 项导航 + 折叠 + 移动端展开） |
| 核心业务流程 | ✅ 8 条（A/B/C/D/E/F1/F2/F3） |
| 响应式（3 视口 × 11 页） | ✅ 33 组 |
| 弹窗/抽屉 | ✅ 1 条（Evaluation 证据抽屉） |
| DAG 交互 | ✅ 缩放/适应视图（时间轴播放未自动化） |
| 错误/空态 | ✅ 部分（403 错误框、Repair 空候选） |
| 占位页诚实性 | ✅ 2 条（断言显示「尚未实现」） |

**未自动化**：Trace Time Travel 播放时序、DAG 画布拖拽/平移、节点点击聚焦高亮的视觉弱化、跨浏览器。

---

## 10. 复现步骤（关键缺陷）

**缺陷 1（校准 404）**

1. 打开 `http://localhost:5173/calibration`
2. 点击「运行校准」
3. 修复前：页面显示「请求失败（HTTP 404）」，Network 中 `POST /api/evaluation/calibration/runs` 为 404
4. 根因：请求体 `dataset_id` 为 `judge-calibration-v1`（JSON 内部 id），后端按文件名 `calibration_set_v1` 查找失败
5. 修复后：返回 200 并渲染 Exact Agreement / Confusion Matrix

**缺陷 2（窄屏横向溢出）**

1. 浏览器视口设为 390×844
2. 访问 `/traces`（或 `/`、`/runs`、`/repair`）
3. 修复前：`document.documentElement.scrollWidth` 比 `clientWidth` 大 427px，页面可横向拖动
4. 根因：`table.tbl` 宽 782px 且无滚动容器
5. 修复后：差值为 0，表格在卡片内横向滚动

---

## 11. 与其它验收的关系

- 本报告只覆盖**浏览器/UI 层**。后端能力验收见 `docs/FEATURE_AUDIT.md`（357 项 pytest 全通过）。
- 真实 API 执行证据见 `docs/REAL_EXECUTION.md`（1 次经用户批准的真实调用）。
- 本轮所有浏览器测试使用 **mock 数据**，`data_source=mock`，未冒充真实 API 结果。

---

## 12. 第 43 步补充：P1 页面与时间轴验收

### 新增页面验收（P1-1 / P1-2）

| 页面 | 用例 | 覆盖 | 结果 |
|------|------|------|------|
| `/comparison` | 2 | 双 Trace 选择 → 运行对比 → 概览对照/顶层差异/差异节点/步骤匹配/观察-推断-未验证分区/跳转 DAG；相同 Trace 拒绝 | **PASS** |
| `/attribution` | 1 | 故障 Trace + 基线 → 指标卡/故障节点/传播路径/节点分类表/筛选/结论口径 | **PASS** |
| 导航 | 1 | 两页面均已实现（无「待实现」）、无占位提示 | **PASS** |

### 新增时间轴验收（P1-4）

| 用例 | 覆盖 | 结果 |
|------|------|------|
| 时间轴可展开并逐步回放 | 展开/收起、进度条推进、播放→暂停、回到起点、跳转故障、速度切换 | **PASS** |
| 缺时间戳步骤被排除并说明 | 「缺少可用时间戳…未推断」或全有时间戳时无该提示 | **PASS** |

### 套件变化

| 项 | 第 42 步 | 第 43 步 |
|----|---------|---------|
| 页面清单 | 11 真实 + 2 占位 | **13 真实 + 0 占位** |
| 浏览器用例总数 | 41 | **49** |
| 结果 | 41 PASS | **49 PASS / 0 FAIL / 0 BLOCKED** |

### 修复的 UI/可访问性问题

| # | 问题 | 修复 |
|---|------|------|
| 1 | DAG「起点」按钮 `aria-label` 与可见文字不一致（违反 Label-in-Name） | 改为「回到起点」 |
| 2 | 两个 P1 页面缺失时导航显示「待实现」 | 页面实现后移除标记 |

### 仍未覆盖（NOT_TESTED，延续登记）

- 真实 DeepSeek API 的浏览器端到端流程（需付费授权）
- 视觉像素级回归、跨浏览器矩阵、axe-core 全站 a11y 扫描
- 并发/压力场景

---

## 13. 第 55 步浏览器验收（需求回溯后的全量复验）

> 本章节覆盖此前验收结论；项目当前共 **19 个真实路由**、**102 项浏览器用例**全部通过。

### 13.1 环境与范围

- Playwright 1.65.1 + Microsoft Edge（`channel: 'msedge'`），桌面 1440×900；
- 响应式视口：PC 1440×900 / Tablet 834×1112 / Mobile 390×844；
- 后端 `127.0.0.1:8000`，前端 `localhost:5173`；
- 全部使用本地确定性数据（mock/模拟评测），**0 次付费 API 调用**。

### 13.2 覆盖范围与用例数（102）

| 类别 | 文件 | 用例数 | 覆盖 |
|------|------|--------|------|
| 页面冒烟 | `e2e/pages.spec.ts` | 40 | 19 个路由打开/刷新不 404、无白屏/严重 JS 异常、侧栏导航全部可跳转且高亮、折叠 |
| 核心流程 | `e2e/flows.spec.ts` | 8 | Agent 运行→Trace→DAG、故障分析、反事实、修复验证、评测、Mock Judge、校准、多 Judge |
| 响应式 | `e2e/responsive.spec.ts` | 7 | 3 视口 × 19 页无横向溢出与字号可读、手机侧栏、表格容器滚动、DAG 缩放、抽屉不超屏 |
| 专题页面 | `attribution/time-travel/pipeline/pareto/cost-analytics/version-comparison/redteam/adversarial/capabilities` 等 | 47 | 归因抽屉/批量、时间旅行、流水线、帕累托、成本、版本对比/回归/门禁、红队、对抗、第 55 步新能力 |

第 55 步新增 `e2e/capabilities.spec.ts` 4 项：

1. `/architecture`：20 项能力矩阵 + 7 个创新点表格、COMPLETE/PARTIAL 状态、诚实性提示；
2. `/evaluation`：跑一次受控故障评测 →「计算指标」→ 根因 Top-1/Top-3 指标卡、成本口径「模拟执行，非真实计费」、修复成功率「未验证（不按 0 或 100% 处理）」；
3. `/traces/:id`：决策解释卡展示证据化理由，明确「不是思维链」；
4. HITL API：创建审批 → 列表可见 → 人工批准（PENDING→APPROVED，记录 decided_by/decided_at）。

### 13.3 关键验收步骤（真实执行）

| # | 步骤 | 结果 |
|---|------|------|
| 1 | 运行 `npx playwright test --project=desktop-edge` | **102 passed / 0 failed**（6.0m） |
| 2 | 打开 19 条路由并刷新 | 全部 HTTP 200、无白屏 |
| 3 | Agent 运行（mock）→ 结果 → Trace → DAG | PASS |
| 4 | 评测 → 失败模式 → 自动归因 → 根因指标 | PASS |
| 5 | 能力矩阵加载（`/api/capabilities/matrix`） | PASS（20+7 行） |
| 6 | Trace 决策解释加载 | PASS（6 步 rationale，`is_chain_of_thought=false`） |
| 7 | HITL 审批（API） | PASS |
| 8 | 成本分析 / 帕累托 / 版本对比 / 回归 / 门禁 / 流水线 | PASS |

### 13.4 发现与修复的问题

| 编号 | 现象 | 处理 |
|------|------|------|
| P2-51（第 54 步延续） | 全站并发时 `/api/traces` 偶发超时 | 文件级增量解析 + 单飞锁 + 摘要缓存；前端 60s 超时；本轮全量验收稳定 |
| P2-52（第 54 步延续） | 帕累托约束切换数据集误判不可比 | 切换时重算可比性；本轮相关用例通过 |
| 测试稳定性 | 归因页「执行步骤」「数据来源」出现多匹配 strict 冲突；架构页「7 个创新点」同时出现在说明与表标题 | 改为带数量/作用域的精确定位；属于测试选择器修正，不改变产品行为 |
| 第 55 步新增 | 能力矩阵表格移动端横向溢出风险 | 表格容器 `overflow-x:auto`，响应式三视口通过 |

### 13.5 未覆盖项（NOT_TESTED）

- 真实 DeepSeek API 的浏览器端到端流程（需付费授权，本步 0 次调用）；
- Docker 容器部署后的浏览器验证（本机无 Docker，部署运行时 NOT_VERIFIED）；
- 视觉像素级回归、跨浏览器矩阵（Chrome/Firefox/Safari）、完整 a11y 扫描；
- 并发/压力场景；
- 作品集 PDF/演示视频。

### 13.6 结论

第 55 步浏览器验收 **102/102 PASS**；与后端 661 项测试、前端构建共同构成当前质量基线。
所有新增能力均有浏览器或 API 测试证据；未覆盖项与限制已在 `docs/TECH_DEBT.md` 与
`docs/REQUIREMENTS_AUDIT.md` 中显式记录，未用模拟数据冒充真实 API 结果。
