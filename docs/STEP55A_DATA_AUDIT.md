# 第 55-A 步：数据治理与可信度修复（Data Governance & Credibility Audit）

> 本文件只记录**本步实际执行并验证**的事实。所有计数来自本机扫描（可复算命令见文末），
> 所有"修复"均有测试或浏览器证据；未验证项明确标注 NOT_VERIFIED。
> 付费 API：本步 **0 次**（全部本地确定性计算/本地工具执行）。

---

## 1. 备份结果

### 1.1 可恢复快照

| 项 | 值 |
|----|----|
| 备份目录 | `backups/step55a_20261119_183831/`（已 gitignore，不进版本库） |
| Git 检查点 | 分支 `fix/step55a-data-governance`，起点 commit `84025e0`；`GIT_CHECKPOINT.txt` 记录分支/commit |
| 备份内容 | `db/`（root + backend 两个 SQLite）· `config/`（`.env.backup`、`model_pricing.yaml` 等）· `traces/traces`（8617 个 Agent Trace）· `traces/evaluation_traces`（7790 个评测 Trace）· `SHA256_MANIFEST.txt` |
| 文件规模 | **16,414 个文件 / 94.2 MB**；SHA256 清单逐文件记录，可用于完整性复核 |
| 数据库完整性 | 两个 DB `PRAGMA integrity_check = ok`；root DB `sales` 3 行；backend DB `traces`/`steps` 0 行（Trace 实际存储在文件目录，DB 表为空是已知事实） |
| 恢复检查 | `python scripts/audit_data_sources.py --restore-check` 输出恢复步骤（sidecar 非破坏迁移可回滚；DB/配置文件按备份恢复；整体回滚走 git 检查点） |
| 隔离区 | 测试污染文件单独隔离在 `data/governance/quarantine/step55a_20261119_105430/`（见 2.4，可回溯，未删除） |

### 1.2 恢复路径

1. 单条/批量 Trace 恢复：从 `backups/step55a_20261119_183831/traces/` 复制回 `data/traces/` 或 `data/evaluation/traces/`；
2. 数据库/配置恢复：按 `SHA256_MANIFEST.txt` 校验后复制回原路径；
3. 整体回滚：`git checkout master`（或在当前分支 `git reset --hard 84025e0`，本步未执行强制重置）；
4. 来源分类 sidecar（`data/governance/source_index.json`）只是**只读派生索引**，删除后会自动重建，不影响原始 Trace。

---

## 2. 数据审计结果

### 2.1 全库来源分类（复算命令：`python scripts/audit_data_sources.py --apply --json data/governance/audit_report.json`）

| 来源分类 | 全库数量 | Agent 目录 | 评测目录 | 定义 |
|----------|---------:|-----------:|---------:|------|
| **REAL_API** | **0** | 0 | 0 | 真实模型 API 调用且有独立 `real_api_verified` 标记 |
| MOCK | **895** | 895 | 0 | 本地 Mock Agent 实际执行（脚本化模型 + 真实本地工具） |
| SIMULATED | **13,147** | 5,357 | 7,790 | 评测批量合成 / 离线构造 / 模拟器输出 |
| REPLAY | **2,365** | 2,365 | 0 | 重放 / 反事实 / 修复验证派生记录 |
| UNKNOWN | **1** | 1 | 0 | 来源无法可靠确认（**不自动归入 REAL_API**） |
| **合计** | **16,408** | **8,618** | **7,790** | — |

- 重复 `trace_id`：**0**；坏文件：**0**（全量 16,408 份 JSON 可解析）。
- 故障 Trace（含至少一次 `fault_injections`）：按来源分别为 MOCK 1 / SIMULATED 49 / REPLAY 1,896。
- **历史 real_api 声明**：`trace-4d3d82dc7a67496dbc2842cd3b31aa47`（1852 Token，历史唯一一次真实调用）缺少独立的 `real_api_verified` 证据标记 → 按保守规则归 **UNKNOWN**，**不自动升级**。
  分类端点会返回原因：`data_source=real_api` 但缺少 `real_api_verified` 独立调用证据；按保守规则归为 UNKNOWN，不自动升级。
- UNKNOWN 样本（全库唯一 1 条）：该 `real_api` 声明记录；`unknown_examples` 已写入 `data/governance/audit_report.json`。

### 2.2 状态分布（全库 / 主 Trace 目录）

| 范围 | 成功 | 失败 | 合计 |
|------|-----:|-----:|-----:|
| 全库（16,408） | 11,006 | 5,402 | 16,408 |
| 主 Trace 目录（8,618，`/api/traces` 口径） | 5,345 | **3,273** | 8,618 |

失败 Trace 并未丢失：主目录 3,273 条失败记录中 925 条含故障注入（REPLAY 924 / SIMULATED 2,348 / MOCK 1 的来源分布见 `/api/governance/failed-summary`）。

### 2.3 "为什么不同页面数字不同"（统计口径根因）

| 页面 | 修复前读取范围 | 修复前展示 | 根因 |
|------|----------------|------------|------|
| Overview | `data/traces` 单目录 | Trace 总数 **8,617**、成功 5,345、失败 3,272 | 只读主 Trace 目录，且**未区分数据来源** |
| Trace Explorer | `data/traces` 单目录 | 8,617 行无筛选（一次性渲染全部行） | 无来源/状态/故障筛选，无分页 |
| Cost Analytics | 两个目录合计 | "数据覆盖 **16,407**"、总 Token 7,732、成本估算 `CNY 0.00000000`（PRICE_UNKNOWN） | 全库口径 + 把 UNKNOWN/PRICE_UNKNOWN 的金额按 0 展示，且未按来源分栏 |

差异 **8,617 vs 16,407 = 7,790 条评测 Trace**（`data/evaluation/traces`），两个口径**没有重复计入**（同一 trace_id 只计一次）。修复后各页面显式标注范围（见第 3 节）。

### 2.4 数据混用 / 污染审计（含本步发现的真实问题）

1. **测试污染生产数据目录（P1，已修复）**：完整后端测试套件运行时会通过生产默认路径写入 Trace。
   - 本次实测：一次完整测试运行后 Agent 目录 +252、评测目录 +313，共 **565 个测试 Trace**（SIMULATED 452 / MOCK 22 / REPLAY 91）混入统计。
   - 处置：新增 `scripts/quarantine_test_pollution.py`（默认 dry-run，`--apply` 才移动），把相对第 55-A 备份新增的 565 个文件**移动**（不删除）到 `data/governance/quarantine/step55a_20261119_105430/`，并写 `manifest.json`；移动后两个目录恢复到 8,617 / 7,790，与备份一致。
   - 防复发：`tests/conftest.py` 新增会话级 autouse fixture `_quarantine_test_data_pollution`，测试结束把**本会话新增**的 Trace 移到系统临时隔离目录，并还原被测试重写的 `data/evaluation/redteam/redteam_v1.json`（本次测试曾重写其 `created_at`，已用 `git checkout` 还原）。
   - 浏览器全量验收同样会产生产品运行 Trace（Agent 运行/评测/流水线）。验收结束后用 `scripts/quarantine_test_pollution.py --keep trace-f3b36afd…` 将 **392 个**验收产生的 Trace（MOCK 4 / REPLAY 12 / SIMULATED 376）移入 `data/governance/quarantine/step55a_20261119_115651/`（保留第 4 节的失败 Trace 证据文件），目录恢复到 8,618 / 7,790。
2. **REAL_API 零数据，但历史 UI 会"自动升级"**：Trace 详情旧逻辑把 `data_source=real_api` 且 `simulated=false` 直接显示为 "REAL API"；本次改为以后端分类结果为准，缺 `real_api_verified` 时显示 UNKNOWN。
3. **评测 Trace 不在主列表**：评测 Trace 保存于独立目录（不进入 Trace Explorer），只能按 `trace_id` 下钻；这是设计约定，但此前页面未说明，易被误解为"失败 Trace 丢失"。
4. **其他 data 目录（报告类）仍会被测试写入**：`data/attribution/reports`、`data/failure_patterns/reports`、`data/pipelines/reports` 等报告类目录在测试后会新增文件（多为 gitignored）。本步**未清理**这些历史报告（避免破坏演示数据），登记为 P2；它们不参与来源分类与统计 KPI。

---

## 3. 修改内容

### 3.1 后端

| 文件 | 变更 |
|------|------|
| `backend/app/services/data_governance.py`（新增） | 来源分类（`SOURCE_CLASSES` / `classify_metadata` / `classify_trace_record`）、全库扫描（`scan_sources`：sidecar + 30s 缓存 + 目录签名 + **单飞锁 + stale-while-revalidate**，目录变化时返回旧快照并后台重扫回写，避免页面请求被 35 秒扫描阻塞）、sidecar 写入（`write_source_index`，非破坏性）、单条分类（`get_trace_classification`）、四类受控实验资格（`trace_eligibility`：只读重放 / 受控重跑 / 反事实实验 / 修复候选，逐项给出 `reasons` / `requirements`） |
| `backend/app/services/trace_store.py` | `_summarize()` 增加 `source_class` / `source_reasons` / `data_source` / `simulated` / `derivation` 字段（列表接口一次返回来源信息，避免前端逐条再查） |
| `backend/app/services/token_analytics.py` | 每条 Trace 分析结果增加 `source_class`；`analyze_overview()` 与 `cost_attribution()` 支持 `source_class` 收窄范围，并在响应中返回 `source_scope`（命中数 / 全库数 / 各来源计数，**不改变金额计算口径**）；`analyze_pipeline_run()` 对历史失效 Trace 引用跳过并计入 `missing_trace_ids`，不再逐条触发全库强制重载（实测可将流水线归因从 >5 分钟恢复到秒级） |
| `backend/app/main.py` | `/api/traces` 支持 `source_class` / `status` / `fault_only` 服务端过滤并返回 `total_count` / `by_source` / `by_status` / `scope_note`；新增 5 个治理端点（见 3.3）；`/api/cost/overview`、`/api/cost/attribution` 支持 `source_class`（非法值 400） |
| `scripts/audit_data_sources.py`（新增） | 非破坏性 sidecar 迁移 + `--verify` 一致性复验 + `--restore-check` 恢复指引 |
| `scripts/quarantine_test_pollution.py`（新增） | 测试污染隔离（dry-run 默认，可回溯 manifest） |
| `scripts/create_mock_failure_trace.py`（新增） | 真实本地执行的 MOCK 失败 Trace 创建/复验脚本（见第 4 节） |

**新增 API（全部只读或非破坏性派生）：**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/governance/sources` | 全库来源分类汇总（定义、各来源计数、各范围计数、状态/故障分布；sidecar 优先） |
| POST | `/api/governance/scan` | 重新扫描；`persist=true` 时写 sidecar（非破坏性） |
| GET | `/api/governance/failed-summary` | 主目录失败 Trace 口径汇总（失败数、含故障数、来源分布、最近样本） |
| GET | `/api/governance/traces/{trace_id}` | 单条 Trace 来源分类 + 证据/原因（主目录与评测目录均可下钻） |
| GET | `/api/governance/traces/{trace_id}/eligibility` | 四类受控操作资格 + 逐项原因/要求（不执行任何实验） |

### 3.2 前端

| 页面/文件 | 变更 |
|-----------|------|
| `frontend/src/types.ts` | 新增 `SourceClass`、Trace 摘要来源字段、`GovernanceSourcesResponse`、`FailedSummaryResponse`、`TraceEligibility`、`TraceClassificationResponse` 等类型 |
| `frontend/src/api.ts` | `fetchTraces()` 支持来源/状态/故障过滤；新增 `fetchGovernanceSources` / `fetchFailedSummary` / `fetchTraceClassification` / `fetchTraceEligibility` |
| `Overview` | 显式"数据口径"说明（Overview/Trace Explorer = 主目录；Cost Analytics = 全库，二者无重复）；全库来源分类表（REAL_API 为 0 时显示 **"暂无已验证的真实模型运行记录。"**）；失败 Trace 口径面板（失败数/含故障数/来源分布/最近样本，可点开）；最近 Trace 增加来源徽标 |
| `Trace Explorer` | 服务端来源/状态/故障筛选 + 分页（每页 50，避免一次性渲染 8,600+ 行）+ 来源徽标与原因 tooltip + 筛选计数（当前/全量/失败数）+ REAL_API 空结果提示 |
| `Trace 详情` | 数据来源以 `/api/governance/traces/{id}` 分类为准（**不再自动把 `data_source=real_api` 升级为 REAL API**）；新增"数据来源分类（只读派生）"卡与"受控实验资格"卡（四类操作 可用/不可用 + 原因） |
| `Cost Analytics` | 新增"数据来源"范围选择（全部/REAL_API/MOCK/SIMULATED/REPLAY/UNKNOWN）；统计口径横幅（命中/全库 + REAL_API 空态文案）；**UNKNOWN / PRICE_UNKNOWN 不再显示为 0**（KPI 与表格显示状态文本，已定价部分单独说明）；模型分布/归因空态按范围提示；请求超时放宽到 120s（冷启动解析全库仍需数十秒） |
| `frontend/e2e/data-governance.spec.ts`（新增） | 4 项浏览器验收（Overview 口径/来源表、Explorer 筛选与空态、Trace 详情分类+资格、Cost 来源范围与 UNKNOWN 不按 0） |

### 3.3 其他

- `.gitignore`：新增 `backups/`、`data/governance/`（备份与治理产物不进入版本库；`data/governance/` 内含 sidecar、审计报告与隔离区）。
- `tests/conftest.py`：新增测试数据污染隔离 fixture（见 2.4）。
- 文档：本文件 + `PROJECT_STATE.md` / `docs/FEATURE_AUDIT.md` / `docs/TECH_DEBT.md` 增量更新。

### 3.4 验收过程中发现并修复的性能/健壮性缺陷

| 编号 | 问题 | 根因 | 修复 | 证据 |
|------|------|------|------|------|
| **P2-59** | 成本归因「按流水线」维度从秒级恶化到 **>5 分钟**（浏览器用例 60s 超时找不到新流水线行） | `analyze_pipeline_run()` 对每条历史流水线引用逐个调用 `_get_trace()`；历史引用中已被清理/隔离的 Trace 会触发 `list_all_traces(force=True)` **全库重解析 1.7 万份 JSON**，200 条流水线 → 分钟级 | 流水线归因读取改为 `_get_trace(allow_full_reload=False)`：失效引用直接计入 `missing_trace_ids` 并跳过；单条流水线分析异常不阻断整体归因 | 新增 `test_get_trace_can_skip_full_reload`；`e2e/cost-analytics.spec.ts` 归因用例恢复通过（14 项专题回归 3.0 分钟内） |
| **P2-60** | `/api/governance/sources` 在 Trace 目录变化后对每个页面请求同步执行 35s 全量扫描，Overview 页面与浏览器用例超时 | `scan_sources()` 只在 sidecar 签名匹配时走快路径，签名失效则同步全量扫描；并发请求无单飞，可重复扫描 | `scan_sources()` 增加**单飞锁**与 **stale-while-revalidate**：页面读取返回上一次快照（`stale=true`）并在后台单飞重扫 + 回写 sidecar；审计/脚本仍用同步扫描保证准确 | 新增 `test_sources_stale_while_revalidate`；`e2e/data-governance.spec.ts` Overview 用例恢复通过 |

---

## 4. 失败 Trace 验证（真实本地执行）

### 4.1 创建方式（可复算）

`python scripts/create_mock_failure_trace.py`：

- **真实执行链路**：`basic_agent.run_agent_sync`（多步 LangGraph Agent 循环）+ 脚本化本地模型（fake `_chat`，**不发起网络请求**）+ **真实本地工具** `text_analyzer` 与 `calculator`；
- **受控故障注入**：`FaultInjector(TOOL_TIMEOUT, target_tool="calculator", count=1)`；
- 执行结果：`text_analyzer` 真实执行成功（返回真实字符统计）、`calculator` 被注入超时失败、最终回答明确声明任务失败 → Trace 状态 `failed`（判定规则与 `evaluation_engine` 一致）；
- 来源标记：`data_source=mock` / `simulated=true` / `simulated_model=true` / `model_provider=local-simulation`，**不含** `real_api_verified` → 分类 **MOCK**（不是 REAL_API，也不是 SIMULATED/REPLAY）；
- 脚本具有幂等保护（同一 `scenario_id` 已存在则不重复创建）。

### 4.2 证据

| 项 | 值 |
|----|----|
| Trace ID | `trace-f3b36afd4f034bbea8715fd64a3b1719` |
| 文件 | `data/traces/trace-f3b36afd4f034bbea8715fd64a3b1719.json` |
| 状态 / 步骤 | `failed` / 7 步：model_call → planning → model_call → tool_call(text_analyzer, **succeeded**) → model_call → tool_call(calculator, **failed** `tool_timeout`，`fault_injection.injected=true`) → model_call |
| 最终回答 | `任务失败：calculator 工具持续超时，无法得到确定性计算结果。` |
| 来源分类 | **MOCK**（理由：`data_source=mock（本地 Mock Agent 实际执行）`） |
| 受控实验资格 | recorded_replay ✅ / controlled_rerun ✅ / counterfactual_experiment ✅ / repair_candidates ✅（全部满足且无原因项） |
| 付费 API | `paid_api_called=false`（0 次） |

### 4.3 API 关联查询验证（真实 HTTP 结果）

| 查询 | 结果 |
|------|------|
| `GET /api/traces?status=failed&fault_only=true&source_class=MOCK` | `count=1`（目标 Trace），`total_count=8618`，`by_source` 含 `MOCK=895` |
| `GET /api/traces?status=failed&fault_only=true&source_class=REAL_API` | `count=0`（不伪造真实数据） |
| `GET /api/traces/{id}` | 200，7 步，状态 failed |
| `GET /api/traces/{id}/dag` | 200，7 节点 / 6 边（故障节点可在 DAG 中定位） |
| `GET /api/governance/traces/{id}` | 200，`source_class=MOCK` |
| `GET /api/governance/traces/{id}/eligibility` | 200，四类资格全部 `eligible=true` |
| `GET /api/governance/failed-summary` | `failed_count=3273`，`failed_with_fault_injection=925`，`failed_by_source={REPLAY:924, SIMULATED:2348, MOCK:1}`，样本含该 Trace |
| `GET /api/cost/overview?source_class=MOCK` | `trace_count=895`，最近 Trace 列表包含该 Trace（`cost_status=SIMULATED`） |
| `GET /api/cost/attribution?level=trace&source_class=MOCK` | 返回行包含该 Trace |

### 4.4 失败 Trace 存储/查询/过滤/关联问题结论

- **存储**：失败 Trace 一直真实存在（主目录 3,273 条失败），但此前 UI 只按全量表格展示，无法筛选，且来源未分栏，导致"看不到失败 Trace"的观感；
- **查询/过滤**：`/api/traces` 原无状态/故障/来源过滤，前端亦无筛选控件与分页（8,617 行全量渲染）；本步补齐服务端过滤 + 分页 + 空态文案；
- **关联**：失败 Trace 的故障步骤可通过 DAG / 受控实验资格关联；评测目录中的失败 Trace 不在主列表，需按 `trace_id` 下钻（页面已说明）；
- **语义**：`basic_agent` 正常运行即使工具失败也会产出最终回答（状态 succeeded/partial），只有异常/受控失败场景才落 `failed`；本步已用真实执行脚本固化这一事实，避免后续误判"失败数据丢失"。

---

## 5. 自动化测试

| 项目 | 结果 | 说明 |
|------|------|------|
| 后端全量 | **685 passed / 0 failed / 0 skipped** | 基线 661；本步新增 `tests/test_data_governance.py` **24 项**（22 项治理/口径 + 2 项性能回归） |
| 前端构建 | **通过** | `tsc + vite build` |
| 浏览器全量 | **106 passed / 0 failed** | 基线 102；本步新增 `e2e/data-governance.spec.ts` **4 项** |
| 付费 API | **0 次** | 无网络调用、无付费调用 |

新增后端测试覆盖：来源分类 9 条规则（含 REAL_API 缺验证不升级、派生 REPLAY 优先、矛盾标记 UNKNOWN）、扫描口径一致性（总数/目录/来源/坏文件/重复）、sidecar 写入-复验-失效重扫、四类资格规则（无步骤/白名单/非白名单/无故障）、`/api/traces` 过滤与非法参数 400、治理端点契约、失败口径一致性、成本范围过滤（REAL_API 空范围 `tokens/cost` 为 null/UNKNOWN 而非 0）、成本端点非法来源 400。

---

## 6. 未解决问题

### P0（阻塞，需用户授权）

- **P0-4（延续）真实 API 自动化验证缺失**：全库 REAL_API = **0**；历史唯一真实调用缺少 `real_api_verified` 证据 → UNKNOWN。真实 API 会否产生费用，需用户逐次授权后才能补测；本步未调用、未伪造真实数据。

### P1（本步新发现并已修复）

- **P1-55A-1 测试污染生产 Trace 目录**：已隔离 565 个测试 Trace + conftest 防复发（见 2.4）。
- **P1-55A-2 数据来源"自动升级" REAL_API**：Trace 详情旧逻辑会把 `data_source=real_api` 直接展示为真实 API；已改为后端分类 + 保守规则（缺 `real_api_verified` → UNKNOWN）。
- **P1-55A-3 统计口径不一致**：Overview 8,617 / Cost 16,407 / Trace Explorer 无筛选；已统一为「显式范围 + 来源分栏 + 筛选/空态」。

### P2（登记，未修复）

- Fault Lab / Counterfactual Lab / Repair Studio 仍展示全部 8,618 条 Trace，未按"受控实验资格"过滤（本步已提供 `eligibility` 端点，UI 接入留待 55-B，避免本步扩大改动面）。
- 报告类数据目录（`data/attribution|failure_patterns|pipelines|quality_gate|regression|version_comparison|pareto|rootcause_metrics/reports`）仍会被测试写入，未清理（不参与来源统计）。
- 成本分析冷启动仍为全量计算（约 30–45s，热态 <2s）；来源筛选复用同一分析缓存，但进程重启后需重算。
- `/api/traces` 默认返回主目录全部 8,618 条摘要（约 2–3 MB），分页在前端完成；服务端分页留待后续。
- 来源分类索引在目录变化后返回的旧快照会带 `stale=true`（后台刷新后自动恢复 `stale=false`）；页面不阻塞，但审计/脚本仍使用同步扫描保证准确。
- MOCK/SIMULATED Trace 的 `token_usage` 多为 0（模拟执行器不消耗 Token），成本展示为 SIMULATED 0；需在产品文案中持续强调"0 表示未调用付费 API，不是真实计费"（本步已在 KPI 明细中标注）。

---

## 7. 下一阶段（55-B）准备

1. **受控实验资格接入执行入口**：Fault Lab / Counterfactual Lab / Repair Studio 用 `/api/governance/traces/{id}/eligibility` 过滤候选并在不可用时显示具体原因（本步已完成数据与 API）。
2. **服务端分页/聚合**：`/api/traces` 支持 `limit/offset` 与全量聚合分离，进一步降低 8,600+ 条列表的传输与渲染成本。
3. **真实 API 授权决策**：若用户授权，补 3–5 个真实任务并写入 `real_api_verified` 证据标记，使 REAL_API 分类与真实成本/延迟展示具备可验证样本；同时保持 `PRICE_UNKNOWN`（除非配置可信官方价格）。
4. **报告类目录测试隔离**：把测试写入的报告目录纳入 conftest 隔离或改为临时目录（本步仅隔离 Trace 与 redteam 数据集）。
5. **sidecar 生命周期**：`data/governance/source_index.json` 约 7.2 MB，随 Trace 增长；可评估压缩/分片或改为 SQLite 索引（当前 30s 签名 + sidecar 快速路径已满足本步需求）。

---

## 附：复算命令

```powershell
# 全库来源扫描 + sidecar + 审计报告（约 35s）
.\.venv\Scripts\python.exe scripts\audit_data_sources.py --apply --json data\governance\audit_report.json
# 一致性复验（sidecar 与实时扫描一致）
.\.venv\Scripts\python.exe scripts\audit_data_sources.py --verify
# 测试污染隔离（默认 dry-run；--apply 才移动）
.\.venv\Scripts\python.exe scripts\quarantine_test_pollution.py --apply
# 创建/验证真实本地执行的 MOCK 失败 Trace（幂等）
.\.venv\Scripts\python.exe scripts\create_mock_failure_trace.py
# 后端测试
.\.venv\Scripts\python.exe -m pytest tests -q
# 浏览器验收（前端目录）
cd frontend; npx playwright test --project=desktop-edge --reporter=line
```
