# TracePilot V2 工程报告

日期：2026-11-27。本报告只记录实际执行、实际测试和实际实验结果；未完成的项明确标注。

## 1. 本轮目标

在不改变项目名称、不重写 Git 历史、不破坏原公网地址/仓库地址的前提下，完成 V2 技术升级：

- 修复现有测试失败，建立可重复的全量回归基线；
- 真实 DeepSeek API 接入 Agent / Judge / 反事实 / 修复验证，并建立**按每次上游调用扣减**的持久化预算；
- 接入公开 Agent 故障 Benchmark（Microsoft AgentRx 公开子集）；
- 深化根因归因：确定性规则候选 + DAG/证据血缘 + LLM 裁决，并做三组真实对照实验；
- 真实模型反事实与最小修复验证；
- 真实 Judge 校准；
- QueryForge Trace 互操作协议与导入；
- 管理员认证与前端 V2 展示；
- 原 ModelScope 空间更新部署。

## 2. 工程保护与基线

- 原开发目录：`C:\Users\yyxxy\Desktop\TracePilot`；安全发布目录：`C:\Users\yyxxy\Desktop\TracePilot-release`。
- 升级前 HEAD：`6c852bd`；已创建 `v2.0` 分支、`checkpoint/v2-start` 分支、`pre-v2-checkpoint` 标签，以及独立回滚快照（仓库外 `TracePilot-v2-backup-20261127`）。
- 升级前全量 pytest（本地实测，项目 venv Python 3.11.5）：**收集 721 项，703 passed / 18 failed**。
  与旧 README 记录的"704 passed / 17 failed"相差 1 项（环境相关，未逐项归档），总量 721 一致；
  失败集中在缺失旧 fixture Trace、质量门禁依赖 git 元数据、真实模型/预算语义变更等。
- 本轮没有删除测试、没有降低断言；修复的是测试 fixture 选择与真实模型/预算语义。
- 升级后全量 pytest（同一 venv）：**848 passed / 0 failed**（最近一次 158.31s），无 skip 掩盖、无 xfail 新增。

## 3. 关键工程改动

### 3.1 测试基线修复（P0）

| 问题 | 处理 |
|---|---|
| 旧 `data/traces` 中的 3 条历史 fixture（`trace-bf1f…`、`trace-3071…`、`trace-4d3d…`）在真实性整改时被移出生产数据 | 放入 `tests/fixtures/traces/`，新增 `tests/fixture_data.py`，测试会话内临时安装、结束后移除；生产数据不混入测试样本 |
| 多个测试取「排序后第一条故障 Trace」，迁移后误取到新证据集的 `sqlite_query/tool_exception` 记录 | 改为显式加载固定 fixture（step21 正常/故障 Trace） |
| `test_failure_propagation` 缺少 `import pytest` | 补齐 |
| 质量门禁测试在无 `.git` 的临时副本中运行导致 `git_commit=unknown` | 以仓库内带 `.git` 环境执行全量测试；CI 同样具备 git |
| 旧模型白名单断言 | 按 2026-11-27 实际 `/models` 返回更新为 `deepseek-flash` / `deepseek-v4-pro`，旧名仅保留兼容别名 |

### 3.2 真实 API 与预算（P0）

新增：

- `backend/app/services/real_api_budget.py`：持久化 JSON 预算（总量 + 访客日额度 + Token/估算费用），文件锁 + 原子替换，损坏状态 fail-closed；
- `backend/app/services/request_context.py`：按请求 IP 的访客标识 ContextVar（哈希后落盘，原始 IP 不保存）；
- `backend/app/services/real_llm.py`：V2 实验统一真实调用入口（预算扣减、`thinking=disabled`、JSON Output、Token 记录）；
- `backend/app/services/real_counterfactual.py`：真实模型受控反事实执行（只允许可审计输入/限额干预，拒绝伪造工具结果）；
- `backend/app/security.py`：管理员令牌 + HMAC 签名 HttpOnly Cookie（兼容 ModelScope 网关；不依赖 Authorization 头）。

真实调用扣减点：

| 调用点 | 预算扣减 |
|---|---|
| Agent 每次 `chat.completions` | `agent_model_call`，调用前 `reserve_real_call`，返回后记录 usage |
| Judge 每次评审 | `judge_model_call`，同上 |
| V2 Benchmark / 实验辅助调用 | `v2_experiment`，同上 |

模型（2026-11-27 实际 `/models` 与 `chat/completions` 核对）：

- `deepseek-flash`（默认，实测返回 `model=deepseek-flash`）；
- `deepseek-v4-pro`（高能力对照）；
- 旧 `deepseek-chat` / `deepseek-reasoner` 仅兼容映射，不再对外宣称可用；
- 真实调用关闭 SDK 自动重试（`max_retries=0`），并在请求体关闭 thinking，避免隐藏推理消耗。

### 3.3 管理员与公开演示

- 管理员登录：`POST /api/admin/login` → 签名 HttpOnly Cookie；`GET /api/admin/session`、`POST /api/admin/logout`；
- 上传/删除文件在 `TRACEPILOT_ADMIN_REQUIRED=1` 时强制服务端校验；
- 真实 Judge 校准/多 Judge 在管理员强制模式下要求管理员会话；
- 公开演示可显式开启真实 RAG/Function Calling：`TRACEPILOT_PUBLIC_REAL_API=1` + `TRACEPILOT_ALLOW_REAL_API=1` + 持久预算；否则保持禁止；
- 真实反事实/修复验证接口需管理员会话；
- 预算可通过 `GET/POST /api/admin/budget` 管理（持久化，重启不清零）。

### 3.4 QueryForge Trace 互操作

- 新增协议 v1 Pydantic 模型、JSON Schema、校验/映射/导入服务与示例；
- 新 `POST /api/interop/queryforge/import`（管理员）与 `GET /api/interop/queryforge/schema`；
- 详见 `docs/TRACE_INTEROPERABILITY.md`。

### 3.5 前端与交互

- 新增「V2 实验」页（真实 API 状态/预算、Benchmark 对照、Judge 校准、QueryForge 导入、管理员登录）；
- 文件面板增加管理员登录态；真实 API 预算状态展示；反事实/修复实验的真实执行入口；
- 具体见 `frontend/src/pages/V2Experiments.tsx` 等改动与最终构建结果。

## 4. 真实 API 验收（实际执行）

| 项目 | 实际结果 |
|---|---|
| `/models` | 200，返回 `deepseek-flash`、`deepseek-v4-pro` |
| 真实 Agent 成功执行 | `trace-63d1ae6a744648bf920216a0361b982b`；模型 `deepseek-flash`；4 段 model_call；Provider 上报 3,202 input / 628 output / 3,830 total；真实工具 calculator；`real_api_verified=true` |
| 真实受控反事实 | 以 step21 故障 Trace 为源，`remove_fault` 真实重放成功：`trace-03544cd2d26047f8929a72455c55da79`，3,626 tokens，7.3s，因果证据等级 `weak` |
| 真实最小修复验证 | `INCREASE_TIMEOUT` 与 `LIMITED_RETRY` 两个候选均真实重放成功（`trace-3656cc2e…`、`trace-e284a57d…`），等级 `weak` |
| 真实 Judge 校准 | DeepSeek Flash，16 案例、13 个可评估；exact agreement 0.3077、MAE 0.9192、within-1 0.6154；无执行错误 |
| 预算扣减 | 142 次上游调用；369,830 input / 58,278 output；按配置单价估算 ¥0.486386（明确是估算，不是 Provider 账单）；未发生超预算调用 |
| 付费 API Key | 仅存于本地 `.env` 与 ModelScope Secrets；未提交、未打印、未写入 Trace/前端 |

说明：DeepSeek 未提供按 key 的逐笔账单明细接口，本地余额接口在实验前后显示 11.94 → 11.98 元（存在展示/结算延迟），因此**不以余额差宣称精确实际费用**；上表为 Token 实测 + 本地单价估算。

## 5. 全量回归

- 新增测试：AgentRx 适配 22 项、QueryForge 互操作 85 项（含运行时 schema 兜底）、预算/管理员 7 项、
  真实反事实 6 项、根因实验 4 项、同源部署静态托管 3 项等。
- 升级后全量执行：**848 passed / 0 failed**（`.venv\Scripts\python.exe -m pytest -q`，最近一次 158.31s）。
  升级前基线 721 项 / 703 通过 / 18 失败，净增 127 项新测试。
- 前端 `npm run build` 通过（tsc --noEmit + vite build，2,242 modules，953 kB JS / 57.9 kB CSS）。
- E2E（Playwright + 系统 Edge）：修复第 74 步 UI 文案统一后与 spec 的漂移，并补齐两个受控 fixture
  Trace 生成前置（`frontend/e2e/README.md`）；最终整轮结果见第 8 节。
- 未调用真实 API 的测试使用 fake client / mock executor，不依赖付费凭据；真实 API 验收以独立脚本执行并受预算保护。

### 5.1 CI 门禁（GitHub Actions，实际运行）

- 发布仓库：<https://github.com/9mc79r92fj-wq/TracePilot-release>（沿用原地址，`main` 历史 fast-forward，无强改）
- 工作流 `agent-evaluation`：后端全量 pytest → 前端 `npm run build` → 确定性评测质量门禁
  （固定数据集 + Mock 执行器，不调用付费 API）
- 最近一次运行（commit `de2f784`）：**success**
  - 后端：**848 passed**（17.74s）
  - 前端：`tsc && vite build` 成功
  - 质量门禁：`gate_status=PASS`、`exit_code=0`、无阻断规则命中
  - 运行记录：<https://github.com/9mc79r92fj-wq/TracePilot-release/actions/runs/45934089157>
- 本轮为让 CI 在 Ubuntu 上稳定，修复了三类**环境/可移植性**问题（不改变功能与断言强度）：
  1. 演示 sales SQLite 库 `*.db` 不入库 → 新增 `scripts/init_demo_db.py`，CI 在测试前显式初始化；
  2. 流水线并发测试存在线程清理窗口，偶发误报「并发已达上限」→ 测试等待/对账计数器（生产代码不变）；
  3. Pareto 约束筛选用例依赖本机是否恰好落盘过门禁报告 → 加入确定性的不存在 `dataset_version` 约束。
  另外去掉了 `scripts/generate_evidence_runs.py` 中硬编码的 Windows 绝对路径。
- 真实 API 冒烟测试独立为 `.github/workflows/real-api-smoke.yml`：**仅 workflow_dispatch 手动触发**，
  必须配置 `DEEPSEEK_API_KEY` Secret，工作流内硬限总调用 6 次；普通 CI / PR 永不调用付费接口。

## 6. 原 ModelScope 部署（实际执行）

**未改动**：账号 `luis123XI394`、创空间名 `TracePilot`、可见性 `protected`、免费 CPU 实例、
公网地址 `https://www.modelscope.cn/studios/luis123XI394/TracePilot`（短域名
`https://luis123xi394-tracepilot.ms.show`）。没有新建第二个公网地址。

| 项目 | 实际结果 |
|---|---|
| 空间仓库提交 | `3393690`（V2 首次推送）→ `6dcd13b`（修复 `frontend/dist` 未提交导致的资源 404 后重新部署）→ 最终同步提交（文档/CI 修复后第三次部署，2026-11-27 11:34 完成） |
| 云端构建 | 成功；studio/runtime 均 `Running` |
| 公网健康检查 | `GET /api/health` → `{"status":"ok","trace_count":48}` |
| 浏览器验收 | 真实 Edge 打开官方创空间页 → iframe 内工作台正常；「系统 → V2 实验」显示真实 API「不可用」、预算 0/0、AgentRx 三组对照（25 例）、Judge 校准、QueryForge 导入；管理员令牌登录成功且刷新后保持（HttpOnly SameSite=Lax Cookie） |
| 持久化验收 | 13/13 通过：触发重新部署后 `runtime.created_at` 变化（容器确实重建），Trace 48→48、上传文件保留、V2 归档保留、预算仍 fail-closed |
| 真实 API | 公开空间 **0 次**付费调用：`TRACEPILOT_PUBLIC_REAL_API=0`、`TRACEPILOT_REAL_TOTAL_CALL_LIMIT=0`（未配置 Key） |
| 环境变量 | 创空间 Variables：`PUBLIC_DEMO=1`、`PUBLIC_REAL_API=0`、`ADMIN_REQUIRED=0`、`REAL_TOTAL_CALL_LIMIT=0`、`REAL_VISITOR_DAILY_LIMIT=0`、`DEEPSEEK_MODEL=deepseek-flash`；Secret：`TRACEPILOT_ADMIN_TOKEN` |
| 密钥卫生 | 部署副本已移除 `backend/.env`；`.dockerignore` 增加 `**/.env`/`**/.env.*`；对主仓库、发布仓库、部署副本做敏感信息扫描，无 API Key/令牌入库；主仓库 Git 历史扫描无 `sk-*` 命中 |

**过程中发现并修复的部署问题**：

1. 生产 bundle 默认后端地址是 `http://127.0.0.1:8000`（开发默认值被打进生产包）→ 改为
   “开发默认 localhost:8000 / 生产同源相对路径”，并在本地单端口模拟中验证无 CORS/404。
2. `frontend/dist` 被 `.gitignore` 忽略，空间仓库只提交了旧 `index.html` 而缺少带哈希的
   `assets/*.js`，导致公网白屏 → 部署脚本改为 `git add -f frontend/dist` 后重新部署；
   公网 `index-*.js` 返回 200 `text/javascript` 验证通过。
3. `/api/interop/queryforge/schema` 原先只读 `docs/schemas/`，而镜像不含 docs → 增加模型即时生成
   兜底（与文件 schema 有测试保证一致）。

**回滚方式**：空间仓库 `git revert 6dcd13b`（或 reset 到上一可用提交 `3c2167c`）后重新部署；
本地回滚检查点为分支 `checkpoint/v2-start`、标签 `pre-v2-checkpoint` 与仓库外快照
`TracePilot-v2-backup-20261127`。

## 7. E2E 与浏览器验收状态

E2E 套件共 **108 项**（17 个 spec 文件，Playwright + 系统 Edge，1 worker）。

**最终结果：108 passed / 0 failed / 0 skipped（193.6s）。**

修复内容（只改 `frontend/e2e/`，16 个 spec + 新增 `frontend/e2e/README.md`；未删除测试、未 skip、
未弱化断言，每个文件 `test()` 数量与修复前一致）：

1. 第 74 步统一 UI 文案后旧断言大量漂移：`Fault Lab → 故障重放`、`Attack Coverage Map → 攻击覆盖图`、
   侧栏导航全部中文化（`Agent 执行与轨迹`、`DAG 分析`、`Judge 校准`、`对抗评测` …）、
   `INCONCLUSIVE 绝不等于 → INCONCLUSIVE 视为无法判定` 等，按当前真实 UI 更新。
2. 更精确 locator：按钮加 `exact`、限定卡片范围、侧栏先展开分组再点击、`getByLabel('选择 Trace')`。
3. 数据状态显式化：当前数据目录没有已验证 REAL_API Trace（REAL_API 全库 0），
   相关断言改为真实 MOCK/SIMULATED 口径，不伪造 Token 数字。
4. 两个受控 fixture Trace 由仓库脚本生成（E2E 前置，非生产种子数据）：
   `create_mock_failure_trace.py`（7 步 calculator timeout，`data_source=mock`）与
   `create_simulated_retry_trace.py`（13 步重试链，`data_source=simulated`，需 `TRACEPILOT_ENV=local`）；
   复现命令见 `frontend/e2e/README.md`。
5. 为避免脚本重建后 trace_id 变化导致脆弱，4 处硬编码 id 改为通过 API 动态发现 fixture。

E2E 数据前置记录：40 条生产种子 Trace 中两条故障注入 Trace 的目标工具是 `sqlite_query`，
而反事实白名单只允许 `calculator`/`text_analyzer`，因此仅用种子数据时「运行实验」不可用——
这是数据设计约束，已在 `frontend/e2e/README.md` 与本报告如实记录，未通过放宽白名单掩盖。

## 8. V2 交付文档

| 文档 | 内容 |
|---|---|
| `docs/V2_ENGINEERING_REPORT.md` | 本文件：工程改动、真实 API 验收、回归结果 |
| `docs/V2_BENCHMARK_REPORT.md` | AgentRx 数据来源/许可/划分、三组对照完整指标、Token 与费用、限制 |
| `docs/V2_BAD_CASE_ANALYSIS.md` | 25 条案例的误差账、误差分类、Top-3 结构性不对等的说明 |
| `docs/TRACE_INTEROPERABILITY.md` | QueryForge Trace 协议 v1 字段、校验规则、映射、导入与示例 |
| `docs/INTERVIEW_GUIDE.md` | 讲解口径、高频问答、简历条目、不宣称事项 |
| `docs/AGENTRX_BENCHMARK.md` | Benchmark 适配器、标签结构与使用方式 |
| `docs/v2_results/` | 原始实验产物归档（benchmark / judge 校准 / 修复验证 / 真实执行） |
