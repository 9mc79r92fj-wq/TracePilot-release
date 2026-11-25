# TracePilot 真实性整改与上线验收证据（INTERVIEW_EVIDENCE）

- 项目：TracePilot（Agent 执行可观测性与可靠性评测平台）
- 整改日期：2026-11-22
- 公网地址（保持不变）：<https://www.modelscope.cn/studios/luis123XI394/TracePilot>
- 短域名：<https://luis123xi394-tracepilot.ms.show>（302 到官方创空间页面，应用以 iframe 内嵌展示）
- 主仓库整改提交：`bdce0011b5440ce67f40b096f878200b37c39e77`
- ModelScope 部署仓库提交：`3c2167c10a17a3223de55056a59e74084bbe58e0`（前置 `6811c9bff2ea16d40154fca56c3d8c153878e545`）
- 整改前主仓库 HEAD（备份记录）：`a2fffa7016ea2e722910b01c34690c78603566f3`

> 本文件所有数据来自实际执行、实际接口返回或实际部署日志；未调用任何付费模型 API，未编造测试结果。
> 唯一模型说明：本次 40 条证据的模型响应由仓库内本地脚本化服务生成（`simulated`），工具为真实本地实现；
> Trace metadata 统一含 `data_source=mock`、`simulated=true`、`model_provider=local-scripted-llm`，**不含** `real_api_verified`。

---

## 1. 历史 8,000+ 记录来源排查结果（整改前实测）

排查对象：仓库 `data/traces`、`data/evaluation/traces`、各派生产物目录、线上旧持久卷（整改前 API 快照）。
判定依据：Trace 内 `metadata.data_source`、`simulated`、`real_api_verified`、ID 形态（`trace-cf-*` / `trace-eval-*`）、
生成链路（评测/反事实/重放引擎）与执行时间分布；不以文件名称单独判断。

| 数据集合 | 数量（实测） | 判定 | 依据 |
| --- | --- | --- | --- |
| `data/traces` | 8,765 | 批量生成 | 6,373 条 metadata 标记 simulated/模拟；2,389 条 `trace-cf-*` 为反事实/重放派生；2 条 `real_api_verified=true`（历史真实 API）；1 条来源不可确证 |
| `data/evaluation/traces` | 7,855 | 批量生成 | 全部为 `trace-eval-*` 评测合成 Trace（SIMULATED） |
| attribution 报告 | 3,203 | 批量派生 | 由上述评测 Trace 批量归因产生 |
| failure_patterns 报告 | 3,994 | 批量派生 | 同上 |
| pipelines 报告 | 1,724 | 批量派生 | 同上 |
| version_comparison 报告 | 1,489 | 批量派生 | 同上 |
| regression 报告 | 1,178 | 批量派生 | 同上 |
| quality_gate 报告 | 1,186 | 批量派生 | 同上 |
| governance 产物 | 974 | 批量派生 | 来源索引/快照 |
| pareto 分析 | 130 | 批量派生 | 评测运行批量对比 |
| rootcause_metrics | 59 | 批量派生 | 同上 |
| 线上旧持久卷（整改前快照） | 122 条主 Trace + 12 归因 + 20 失败模式 + 9 流水线 + 9 门禁 + 15 回归 + 17 版本对比 + 4 Pareto + 20 HITL | 混合 | 2 REAL_API、9 MOCK、95 SIMULATED、16 REPLAY |

结论：上述展示库中的绝大多数记录由模拟/重放/评测批量生成，**不能证明来自真实执行**；仅 2 条带
Provider usage 证据的真实 API Trace 可核验（见 §8 保留说明）。

## 2. 清理、隔离与保留（实际处置）

整改前已完整备份（可回滚，未删除任何备份）：

- `backups/step74_authenticity_20261122_155140/10_repo_data/`：原仓库 `data/` 全量
- `backups/step74_authenticity_20261122_155140/20_backend_data/`：SQLite + 上传文件
- `backups/step74_authenticity_20261122_155140/30_deploy_copy/`：整改前部署副本
- `backups/step74_authenticity_20261122_155140/40_production_snapshot/`：整改前线上 API 快照（含 122 条 Trace）
- `backups/step74_legacy_moved/`：重新生成前的旧目录（按时间戳二次归档）
- `backups/step74_smoke_cleanup/`、`backups/step74_pytest_pollution_20261122_165237/`：验收脚本/测试污染清理留档

隔离/删除结果：

| 项目 | 结果 |
| --- | --- |
| 仓库主 Trace | 8,765 条旧批量记录全部移出正式展示目录（归档），不参与统计/列表/评测 |
| 评测 Trace | 7,855 条旧批量记录归档 |
| 派生产物 | 13,937 份旧报告/分析归档 |
| 线上旧持久卷 | 首次启动 v2 迁移整体归档到容器内 `/mnt/workspace/tracepilot/archive_legacy_20261122_170317`；迁移日志已确认 |
| 用户上传文件 | 保留（迁移脚本只在旧种子文档为唯一文件时归档该文档） |
| SQLite 业务库 | 保留（sales 3 行：手机 2999 / 电脑 6999 / 耳机 399） |
| 旧版 frontend 硬编码统计 | 逐一替换为 API 数据；旧 Trace ID 引用已清除 |

## 3. 新增 30–50 条真实执行记录（实际生成 40 条）

生成方式：`scripts/generate_evidence_runs.py` 调用项目自身 LangGraph Agent 引擎
（任务理解 → 计划 → 工具循环 → 结论），模型响应由 `scripts/mock_llm_server.py` 本地确定性脚本服务提供，
工具为真实本地实现（calculator / 只读 SQLite / 文本统计 / 知识检索 / 销售汇总 / 上下文审查 / 记忆 / HITL 审批）。
全程 **0 次付费 API 调用**。

- Agent 运行记录：40 条；主 Trace：40 条；覆盖 **31 个类别**（≥ 要求的 20 类）
- 状态：成功 36 / 部分完成 1（工具调用上限）/ 失败 3（Token 预算预检查、模型服务 500、整体执行超时）
- 执行步骤合计 222，工具调用合计 54
- 每条均有真实 Run ID、Trace ID、执行时间、步骤、状态、工具调用与失败原因；Token 未由 Provider 上报，按估算/未知标注，不生成费用
- 评测：3 次真实评测运行、27 条评测 Trace；归因报告 6、失败模式 4、流水线 1、门禁 1、回归 1、
  版本对比 1、根因指标 1、Pareto 3、文件资料 4；治理扫描 1

关键证据 ID（线上可核验）：

| 类别 | Run ID | Trace ID | 结果 |
| --- | --- | --- | --- |
| E01 数学计算 | `run-50c02c223e3a406483d7d9bd6bc2fa44` | `trace-d7e115ccb18847559c63032f24c97202` | succeeded |
| E27 工具调用上限 | `run-a372998aedd8446e8d603f581dfe761a` | `trace-64c0bcf58c3e46d4b14016184d701d0a` | partial（工具调用达到上限） |
| E28 Token 预算限制 | `run-3c327a7dbc1647c1bfe1d4864085c89c` | `trace-a7f80cb661004b7d8ffae1c68c0d0bd5` | failed（预算预检查：已用 333，剩余 567，本次输入预计 1427，不足最小输出额度 128） |
| E29 接口异常 | `run-3a5bdda0fddd4dd084cb0f5fec4cc392` | `trace-b81b4b1fcdcf45eeab012b8872038c9a` | failed（500 模拟模型服务异常） |
| E30 超时处理 | `run-acbe6a7bd69c45c89a87ad69c88919d4` | `trace-4598863040804e1aa4cc30239b222834` | failed（Agent 整体执行超时 >1s） |

下游链路 ID：
`eval-ec637bcf168d4921933b378d7243c393`（eval-v1）、`eval-ddac124703d0484490cbeb3102587a8f`（fault-v1）、
`eval-9fcc75771eca4e6081e95a8270232673`（pipeline baseline）、`pipe-d2fb16cf2821465eae0ef62900bebb81`（SUCCEEDED）、
`attr-5460b15b9537447b8b7e1364ded9361d`、`fp-report-22cf93724a0e48da93a7576f4e0ea9b5`、
`rcm-c0ec849f0fe740cda487c6576e673d66`、`cmp-aae33c62fd164fee9767ff062eb53ed5`、
`reg-c27f20cbe48141c9ae71a9fe754744bb`、`qg-08d12e39056645a5be433a17e6759524`。

## 4. 可信链路一致性（实测通过）

- Run ↔ Trace：40 条 Run 全部有 `trace_id` 且 Trace 文件存在；40 条 Trace 全部有 `run_id`，无悬空 ID
- Trace ↔ DAG：抽样 `trace-08516c986887457d9a125b4952a4a701` 的 DAG 节点数 = 步骤数 = 5
- 评测 ↔ Trace：3 次评测运行的 27 个案例 Trace 全部存在
- 归因/失败模式 ↔ 评测 Trace：6 份归因与 4 份失败模式报告关联 Trace 均存在
- 流水线 ↔ 评测/归因/门禁/对比/回归：`pipe-d2fb...` 引用的 6 个 Trace、baseline、门禁、回归、对比、质量门禁均可查
- 治理口径：主 Trace 40 + 评测 Trace 27 = 67；`REAL_API=0`、`MOCK=40`、`SIMULATED=27`、`UNKNOWN=0`、`REPLAY=0`
- 首页/Trace Explorer/评测/归因/成本等页面全部使用同一 API 数据源；不再出现“首页 50 条、Trace Explorer 8000 条”

## 5. 前端文案整改

- 审计：`docs/UI_COPY_AUDIT.md`，共 114 条目 / 29 个文件（21 pages + 8 components）
- 已完成 114/114 处替换，并统一处理中英混排、原始枚举直出、内部“口径/不伪造”语气、重复免责声明
- 19 个页面、6 组导航、整体设计风格与核心功能保持不变
- 保留真实数据来源标识（REAL_API/MOCK/SIMULATED/REPLAY/UNKNOWN）、安全提示、失败状态与限制说明
- 额外修正：`AgentRuns` 将环境变量名转换为访客可读描述；治理范围说明改为“只读汇总，不改写原始记录”；
  驳回的真实 API 请求不再写入无 Trace 的 Run（避免悬空运行记录）

## 6. 线上验收（整改后实际执行）

- 部署：ModelScope Docker 创空间，云端构建成功，状态 `Running`；公网地址未变；`/api/health` 返回 `trace_count=40`
- 线上 API/安全/数据验收：**68/68 通过**（19 页面 HTTP、静态资源、健康、Trace/评测/归因/门禁/回归/版本/Pareto/文档、
  DAG 一致性、治理来源、真实 API 403、限流 429、上传/删除、烟雾后数据不变）
- 线上浏览器验收：**57/57 通过**（19 页面 × 桌面/平板/手机；ModelScope 包装页 iframe 内实际渲染，
  6/6 导航、无 console error、无 API 4xx/5xx、无横向溢出）
- 本地（部署副本同源单端口）：HTTP 冒烟 **52/52 通过**；Playwright 19 页 × 3 视口 **57/57 通过**
- 前端构建：主仓库与部署副本 `npm run build` 均通过
- 重启/重新部署持久化：第二次部署（镜像重建 + 容器重启）后，Trace 40、Run 40、评测运行 3、文档 4、Pareto 3 全部保留；
  v2 迁移标记生效，**未重复归档**；启动日志显示“清理无 Trace 的探测运行记录: 2”后运行记录恢复为 40
- 安全边界：公开演示 `real_api_ready=false`；`POST /api/agent/runs`（real_api）返回 **403** 且不发起任何 API 调用、
  不写入 Run；高成本接口连续请求返回 **429**；状态接口无密钥；无 `.env`/Key 进入镜像
- 未上传 `.env`、未泄露 Key、未删除 GitHub 仓库、未修改 Vercel/Render、未使用付费资源、未调用付费 API

## 7. 自动化测试结果（如实记录）

在隔离副本中执行（避免测试写入正式数据）：

- 全量 pytest：**701 passed / 20 failed**，耗时 112.96s
- 失败原因：20 项均为测试硬编码了整改前的旧演示 Trace ID/夹具（如 `REAL_TRACE_ID`、step21 样例），
  新证据集不再包含这些旧 ID；属于测试数据夹具过期，不是本次修改引入的功能回归
- 针对本次修改的测试子集：**118 passed / 3 failed**（3 项同样是旧 `REAL_TRACE_ID` 夹具缺失）
- 正式数据目录未被测试污染（曾在测试写正式目录时终止，并已从 seed 完整恢复）

## 8. 保留的 2 条历史 REAL_API 记录（未展示说明）

整改前有 2 条真实 DeepSeek API Trace 带 `real_api_verified=true` 与 Provider usage 证据：

- `trace-2dedf32280ab4b94a5bbbade7ad1770e`（run-cdaae61c003a4489b312311b5c6f211e）
- `trace-3f6eb1d00d654f449bfa3e921b6b699b`（run-1374df84b96749ecb1b929c2569a5909）

它们已完整保留在备份（`10_repo_data` / `40_production_snapshot` / 整改前部署 seed）中，但**未纳入公开展示库**：
旧版 Agent 运行记录仅保存在进程内存，已没有与这两条 Trace 配对的 Run 文件；若直接展示会出现
“Trace 有、Run 缺失”的断链。为保证展示库 100% 可追溯，本次不把无 Run 证据的历史 Trace 混入。
如需展示，应重新发起一次真实 API 调用（需明确授权并承担费用）生成配套 Run + Trace。

## 9. 未解决问题与限制

1. 本次未调用付费模型 API，因此新证据集不含 REAL_API 记录（这是如实状态，不是遗漏）。
2. 2 条历史 REAL_API Trace 仅保留在备份，未展示（原因见 §8）。
3. 旧 pytest 夹具仍引用已归档的旧 Trace ID，全量测试有 20 项数据夹具失败（功能与线上不受影响）。
4. Pareto 页面首次访问会基于当前评测运行生成本地派生分析并落盘，3 份为当前实测数量。
5. 免费 CPU 实例空闲休眠，冷启动约 10–30s；评测/红队过程态日志仍在内存，最终结果已持久化。
6. `data/` 运行时数据按仓库既有策略不入 Git；本次证据集在本地 `data/` 与 ModelScope 部署 `seed/` 中，
   可用 `scripts/generate_evidence_runs.py` 复现。

## 10. 声明

- 未删除用户上传文件、真实日志与核验真实记录（历史真实 API Trace 已留档）；
- 未伪造测试结果、未把模拟数据标注为真实 API、未为好看调整成功率/耗时/费用/故障数；
- 公网链接保持不变，仅在原地址上更新数据与页面文案。
