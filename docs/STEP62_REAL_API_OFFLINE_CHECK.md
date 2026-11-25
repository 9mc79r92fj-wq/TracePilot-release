# 第 62 步离线检查报告：真实 Agent 执行与评测闭环验证

> 状态：**离线准备完成，等待用户显式授权后才能执行任何真实 DeepSeek API 调用。**
> 本步 **未执行任何付费 API 调用**：未设置 `TRACEPILOT_ALLOW_REAL_API`，未提交 `real_api` 运行；未授权的真实入口均验证为 403 且零调用（主目录 Trace 数保持 8759 不变）。
> 本报告只包含真实检查结果；没有把 mock/simulated 数据当作真实调用证据，也没有预先填写任何“执行结果”。

---

## 0. 结论摘要

| 结论 | 说明 |
| --- | --- |
| 真实执行入口与成本门禁 | 可用，未授权时 agent runner / pipeline 两条入口都返回 403 且零付费调用 |
| DeepSeek 配置读取 | 后端启动时从 `.env` 加载；`deepseek-chat` 在官方白名单内；Key 存在且未外泄；`TRACEPILOT_ALLOW_REAL_API` 未设置（默认拒绝） |
| Trace 采集与持久化 | 代码链路完整；历史真实 Trace 可加载（6 步）、DAG 可解析（6 节点/6 边）、前端可展示 |
| 评测读取真实 Trace | **引擎级已验证**（用历史真实 Trace 跑六维评测，全维度可判定/PASS）；**产品级缺口**：评测中心只支持白名单合成数据集，没有“按 trace_id 评测任意 Trace”的 API/UI；产品级真实评测唯一路径是 Evaluation Pipeline 的 `real_api` 模式（已存在于代码，但授权后路径未实际验证，P3-28） |
| 归因读取 Trace | 单条 Trace 归因 API 对历史真实 Trace 可用（返回结构化归因，UNKNOWN 原因如实标注）；“评测结果 → 归因”路径仅存在于 Pipeline 真实模式，待授权验证 |
| 本轮修复 | 修复 1 个真实分类缺陷：真实运行缺少独立证据标记，导致真实 Trace 永远只能显示 UNKNOWN。现在真实运行在 **Provider 返回 usage** 时写入 `real_api_verified` 证据；缺少证据绝不写入，历史数据不升级 |

**评估结论：链路在代码层面已打通到“Trace 持久化 + DAG + 治理/成本/归因”，但“真实 Trace → 评测/归因闭环”必须通过授权后的 1 次 Pipeline `real_api` 运行才能完成最终验证。** 详见第 2、7 节。

---

## 1. 链路完整性清单（数据是否真正流动）

| # | 环节 | 入口 / 实现 | 离线验证方式 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | Agent Runs 真实入口 | 前端 `/runs`；`POST /api/agent/runs` | 真实模式未授权 → **403**，`detail` 明确要求 `confirm_real=true` + 服务端开关；Trace 数不变 | ✅ 门禁已验证；执行待授权 |
| 2 | DeepSeek 配置 / 环境变量 | `main.py` 用 `python-dotenv` 加载项目根 `.env`；`agent_runner._resolve_real_model` 读 `DEEPSEEK_MODEL` 并校验白名单 | `/api/agent/status` 返回 `configured_model=deepseek-chat`、`configured_model_valid=true`、`real_api.available=false`；`.env` 仅含 Key/BaseURL/Model/Timeout/DB 路径，Key 长度 35 但从未输出；`TRACEPILOT_ALLOW_REAL_API` 缺失 | ✅ 配置读取已验证 |
| 3 | Agent executor + 工具调用 | `basic_agent.run_agent_sync`：LangGraph `understand → plan → agent ⇄ tools`；真实调用 `_chat`（OpenAI 兼容 SDK，`temperature=0.3`，无 reasoning 输出），工具结果回传；`usage_meta` 写入每个 model_call 步骤 metadata | 代码链路核查 + 现有桩测试（`test_agent_runner.py` 替换 `run_agent_sync`，零网络）；**真实 HTTP/Function Calling 层只能靠授权后的实际调用验证** | ⚠️ 代码与桩测试通过；真实网络层待授权 |
| 4 | TraceCollector | `run_agent_task` 统一用 `TraceCollector(task_id, trace_id, run_id, started_at, DEFAULT_TRACES_DIR).save(trace)`；真实模式先 `run_agent_sync(collect_trace=True, save_trace=False)` 再统一落盘 | 桩测试断言 `run.trace_path` 文件真实存在且可 `load_trace`；真实 Trace 文件可被 API 读取 | ✅ 已验证 |
| 5 | Trace 持久化 | `data/traces/<trace_id>.json`（主目录）；`/api/traces/{id}` 返回完整 `TraceRecord` | 历史真实 Trace 详情：6 步、状态 succeeded、`metadata.data_source=real_api`；文件系统计数主目录 8759 | ✅ 已验证 |
| 6 | 评测读取真实 Trace | 引擎函数 `evaluation_engine.evaluate_trace(trace, case)` 接受任意 `TraceRecord`；但评测中心 `POST /api/evaluation/runs` 只执行白名单数据集（合成案例）；无 `POST /api/evaluation/traces/{trace_id}`（实测 404） | 用历史真实 Trace 直接调用引擎：**overall PASS**（task_success/tool_correctness/process_quality/efficiency/safety PASS，recovery NOT_APPLICABLE；3336ms、1852 tokens、2 次工具调用）；产品级“按 Trace 评测”入口缺失 | ⚠️ 引擎级 ✅；产品级 ❌（见 2-B2） |
| 7 | 归因读取评测结果 | `POST /api/attribution/evaluations/{run_id}/cases/{case_id}`；单条 Trace 归因 `POST /api/attribution/traces/{trace_id}` | 历史真实 Trace 单条归因返回 200（`attr-095e857c…`，`evaluation_run_id=null`，候选原因 UNKNOWN，如实标注）；评测结果归因路径只随 Pipeline 真实模式产生 | ⚠️ Trace 归因 ✅；评测结果归因待授权 |
| 8 | 前端 Trace 详情 / DAG / 评测展示 | `/traces/:traceId`、`/dag/:traceId`、`/evaluation`、`/runs` | Playwright（Edge，1440×1000）实测：Trace 详情正常渲染（分类 UNKNOWN + 原因 + 四类受控实验资格）；DAG 渲染 6 个节点；Agent Runs 真实模式选项为禁用态「Real API · 服务端未开启」；评测中心显示 2 个合成数据集并明确“不调用付费 API”。无 console error | ✅ 已验证（本轮无前端改动） |

补充：执行器 `_run_real` 把 `max_tool_rounds`、`api_max_retries=0`、`overall_timeout_seconds` 传入 basic_agent；真实模式 Trace metadata 由 `_run_real` 重写，`data_source=real_api`、`simulated=false`、`model_provider=deepseek`，`run_id` 归一化一致。

---

## 2. 阻断问题与处理（按影响排序）

### B1（已修复）真实运行无法被治理识别为 REAL_API
- **问题**：治理分类要求 `real_api_verified=true`，但代码中没有任何地方写入该标记；因此即使真实调用成功，Trace 也永远归为 UNKNOWN（前端“真实 API Trace”永远为 0，成本归途也标 UNKNOWN）。
- **修复（最小）**：`backend/app/services/agent_runner.py` 的 `_run_real` 中，仅当 **至少 1 个 model_call 步骤带有 Provider 上报 usage**（`step.metadata.usage_meta.usage_source == "PROVIDER_REPORTED"`）时，写入 `real_api_verified=true` 与 `real_api_verification` 证据（provider/model/usage_source/证据步骤数/总 tokens/时间/验证方式）；缺少证据时**不写入**并记录 limitation；`data_source`/`simulated` 标记保持不变，历史 Trace 不自动升级、不手工改文件。
- **回归测试**：新增 2 项测试（有 Provider usage → REAL_API；无证据 → UNKNOWN 且不加标记），`tests/test_agent_runner.py` 27 passed。

### B2（阻断“任意真实 Trace → 评测”闭环；需授权后补验证）
- **问题**：评测中心是“数据集驱动”的受控评测（固定 2 个白名单数据集、确定性模拟执行），**不支持把 Agent Runs 产生的某条真实 Trace 直接送去评测**；不存在对应 API（实测 `POST /api/evaluation/traces/{id}` → 404）和 UI 入口。
- **现有产品级真实评测路径**：`POST /api/pipelines` 的 `execution_mode=real_api`（`_stage_agent_execution` 调用 `run_agent_task(..., mode="real_api")`，随后 `_stage_trace_collection` 从磁盘 `load_trace`，`_stage_deterministic_evaluation` 用真实 Trace 调用 `evaluate_trace`，再进入归因/模式/对比/回归/质量门禁/报告）。该路径目前只有“未授权 403”测试，**已授权真实路径未实际运行过（P3-28 / NOT_VERIFIED）**。
- **处理建议（授权后）**：Phase 2 用 **`max_cases=1`** 的 Pipeline 真实运行验证（预期 3–4 次模型调用，上限 6 次），一次覆盖“真实执行 → Trace 落盘 → 评测读取真实 Trace → 归因 → 门禁 → 报告”。
- **备选（本步未做，属新增功能）**：增加 `POST /api/evaluation/traces/{trace_id}`（按案例定义评测任意 Trace）及前端入口；需要改评测报告语义（当前报告 limitations 固定写“全部合成/模拟模型 0 token”），不建议在离线验证步骤里临时加。

### B3（风险，不影响授权执行）工具声明与实际暴露不一致
- `/api/agent/status` 与 agent_runner 文档声明白名单仅 `calculator / text_analyzer / sqlite_query`；但 `_run_real → run_agent_sync` 未传 `tools=`，basic_agent 默认暴露 **9 个工具**（上述 3 个 + `knowledge_search / composite_sales_report / review_context_tool / remember_fact / recall_facts / request_human_approval`）。
- 这 6 个能力工具均为**本地、无副作用**（RAG 本地检索 / 只读 SQLite / 短期记忆 / 人工审批创建），不产生外部费用、不违反安全边界；但与 UI/状态声明的“白名单 3 个”不一致。**本步未改动**（改动会缩减真实运行的能力面，属产品决策），建议后续统一：要么把真实执行限制为 BASIC_TOOLS，要么更新状态/文档声明。

### B4（成本控制边界，需知情授权）
- `max_model_calls=4`、`max_total_tokens=4000` 是**事后审计**（超限会把 run 标为 `limit_exceeded`，但调用已发生）；真正的硬预控是：单次调用输出 ≤512 tokens、`max_tool_rounds`（建议 2 → 每任务模型调用 ≤5 次）、单任务 90s 墙钟、`api_max_retries=0`（失败不自动重试）、工具调用 ≤3 次。本报告的调用/Token 上限按此给出。

### B5（数据一致性，非阻断）历史真实 Trace 在“证据血缘”卡显示 REAL API × 11，而治理分类为 UNKNOWN
- Trace 详情页同时出现两种口径：治理分类（保守规则）为 UNKNOWN，血缘卡按 metadata 的 `data_source` 把该 Trace 及其 11 个节点标为 REAL API。历史数据缺少独立验证标记，属既有展示差异；**授权后的新真实 Trace 因带 `real_api_verified`，两处口径将一致**。本步未改血缘模块（避免跨模块回归）。

### B6（成本诚实性）真实调用成本无法给出金额
- `config/model_pricing.yaml` 中 DeepSeek 条目的输入/输出单价均为 `null`（不编造官方价格）→ 真实调用成本状态为 **PRICE_UNKNOWN**，金额为 `null`，不会显示 0 或“账单”。只有用户提供有来源、有生效日期的官方价格后才能得到估算（仍非实际账单）。

---

## 3. 修复 / 改动文件

| 类型 | 文件 | 说明 |
| --- | --- | --- |
| 修复 | `backend/app/services/agent_runner.py`（原为用户第 59 步未提交 WIP） | `_run_real` 增加 Provider-usage 证据判定，写入 `real_api_verified` + `real_api_verification`；模块文档同步说明。**不修改 `data_source`/`simulated` 语义，不修改历史 Trace** |
| 回归测试 | `tests/test_agent_runner.py` | 新增 2 项：证据齐全 → 治理 REAL_API；缺证据 → 不加标记且治理 UNKNOWN |
| 准备物 | `docs/step62_real_api_tasks.json` | 3 个真实测试任务的完整执行规格（任务原文、期望结果、验证条件、限额、预算） |
| 准备物 | `docs/step62_task_b_reference.txt` | 任务 B 的上传参考文档（284 字符、**40 词**，实测 `text_analyzer.word_count=40`；纯虚构技术说明，无敏感信息） |
| 证据 | `docs/step62_evidence/trace-detail-legacy-real.png` 等 4 张 | 浏览器实测截图：Trace 详情 / DAG / Agent Runs 门禁 / 评测中心 |
| 备份 | `backups/step62_prework_20261120_084933/` | 改动前 `agent_runner.py`、`main.py`、`test_agent_runner.py` 原件 + WIP diff + git status |

说明：`backend/app/main.py`、`frontend/src/components/Layout.tsx`、`frontend/src/index.css`、`frontend/src/pages/AgentRuns.tsx` 及未跟踪的第 59 步文件仍是用户未提交 WIP，本步**未覆盖**；其中 main.py 里第 61 步补的 CORS DELETE 与 `/api/health` 文件计数修复仍需连同用户 WIP 一起提交。

---

## 4. 三个真实测试任务（离线准备，未执行）

完整机器可读规格见 `docs/step62_real_api_tasks.json`；任务原文与验证条件如下（任何一项失败都如实记录，不修改期望值）。

### 任务 A：确定性数值计算
- **任务原文**：`请必须调用 calculator 工具计算 37*89，并在最终回答中只写出精确乘积数字。`
- **期望结果**：`3293`；必须出现 `calculator(expression="37*89")`，工具结果 `result=3293`。
- **关键验证**：run 200 / `mode=real_api` / `status=succeeded`；Trace 落盘；`metadata.real_api_verified=true`；model_call 有 `PROVIDER_REPORTED` usage；工具参数/结果匹配；final answer 含 3293；DAG 可解析（≥3 节点）；治理分类 REAL_API；成本 PRICE_UNKNOWN。
- **限额**：max_model_calls=4、max_total_tokens=4000、max_tool_calls=3、max_wall_seconds=90、max_tool_rounds=2、max_tokens_per_call=512、api_max_retries=0。
- **预期模型调用**：4 次（3–5）；**预期 Token**：约 1600–2200。

### 任务 B：文档引用分析
- **任务原文**：`请参考随任务附带的参考文档，必须调用 text_analyzer 工具统计文档正文的词数（word_count），并在最终回答中只写出该数字。text 参数必须只包含文档正文，不要包含 [REFERENCE_DOCUMENT] 标记行。`
- **上传文档**：`docs/step62_task_b_reference.txt`（正文实测 40 词）。
- **期望结果**：`40`；必须出现 `text_analyzer` 工具调用且返回 `word_count=40`；Trace 首步 metadata 记录 `referenced_documents`（id/filename/format）。
- **验证失败处理**：未调用工具或词数不为 40 → 记录 FAIL（不调期望、不改文档）。
- **限额**：同上；**预期模型调用**：4 次（3–5）；**预期 Token**：约 1700–2400（文档片段最多 1800 字符）。

### 任务 C：工具多步推理
- **任务原文**：`请分两步调用 calculator 工具：第一步计算 12+8 得到中间值，第二步基于该中间值计算乘以 5 的结果（可用表达式 (12+8)*5 或 20*5）。最终回答只写出最后结果 100。`
- **期望结果**：`100`；至少 2 次 calculator 工具调用，第一步 `result=20`，最后一步 `result=100`。
- **关键验证**：两次工具调用步骤齐全；final answer 含 100；Trace 来源/证据/DAG/治理同 A。
- **限额**：同上；**预期模型调用**：5 次（4–5）；**预期 Token**：约 1800–2500。

---

## 5. 预计调用次数

| 阶段 | 内容 | 预期 | 上限 |
| --- | --- | --- | --- |
| Phase 1（必须） | 3 个 Agent Runs（A/B/C），`max_tool_rounds=2` | 11–15 次模型调用（规划 13） | **15 次** |
| Phase 2（建议，用于闭环验证） | Pipeline `real_api` + `max_cases=1`（case-01：计算 1+1） | 3–4 次（规划 3） | **6 次** |
| **合计** | | **14–21 次（规划 17）** | **21 次** |

- 每个请求独立提交，**不自动重试、不后台批量、不并行**；任何失败按 0 次重试处理并记录错误。
- 未授权时全部请求在入口即 403（已验证），不会产生模型调用。

---

## 6. 预计 Token 与成本

| 阶段 | 预期 Token | 审计上限 | 说明 |
| --- | --- | --- | --- |
| Phase 1 | 约 5,100–7,100 | 3 × 4,000 = 12,000 | 历史真实样本为 1,852 tokens/任务（4 次模型调用 + 2 次工具调用）；A/B/C 复杂度接近，B 多文档片段 |
| Phase 2 | 约 1,600–2,200 | 4,000 | Pipeline 使用默认 RunLimits（工具轮上限 3），单案例 |
| **合计** | **约 6,700–9,300** | **16,000** | 审计上限为“超限标记”，不等同硬熔断；硬预控见 B4 |

**成本：`PRICE_UNKNOWN`（无法给出金额，也不会编造）。** 原因：`config/model_pricing.yaml` 中 `deepseek-chat` / `deepseek-reasoner` 的输入/输出单价均为 `null`，仓库未配置可信官方价格。可计算时使用：

```text
cost = prompt_tokens / 1e6 * input_price_per_million + completion_tokens / 1e6 * output_price_per_million
```

历史唯一真实样本：`trace-4d3d82dc…`，Provider 上报 **1,852 tokens**（prompt 1,631 / completion 221，4 次 model 调用），成本状态 `PRICE_UNKNOWN`。若你提供官方价格（含生效日期与来源），可补算估算值，但**仍不是 Provider 实际账单**。

---

## 7. 执行前需要你确认的事项（逐项回复即可）

1. **是否授权 Phase 1？** 即：允许设置服务端 `TRACEPILOT_ALLOW_REAL_API=1`（我会在运行后立即移除并重启后端）并执行 A/B/C 三次真实运行；每次请求都带 `confirm_real=true`。若你愿意自行设置环境变量后告诉我，也可。
2. **是否执行 Phase 2？** 即：额外执行 1 次 Pipeline `real_api`（`tracepilot-eval-v1`，`max_cases=1`），用于验证“真实 Trace → 评测 → 归因 → 质量门禁 → 报告”闭环（预计 +3–4 次调用）。若不做，该链路保持 `NOT_VERIFIED`（P3-28）。
3. **是否接受新真实 Trace 自动写入 `real_api_verified` 证据标记？** 规则：仅当 Provider 返回 usage 时写入；无证据不写；历史数据不升级。若你倾向“人工核验后再标记”，我可以回退该改动并改为离线校验脚本。
4. **是否提供 DeepSeek 官方价格？** 提供输入/输出单价、币种、生效日期、来源 URL 后才可能给出成本估算；否则保持 `PRICE_UNKNOWN`。
5. **确认任务 B 的上传文档**：`docs/step62_task_b_reference.txt`（40 词虚构技术说明，无敏感信息）会被上传到本地文档存储，并在任务中作为**不可信参考数据**（不作为指令）使用。
6. **是否接受 Phase 2 结果可能为 FAIL？** 合成案例 `case-01` 的 `evaluation_rules.max_duration_ms=10000` 对真实网络调用偏紧，真实运行可能效率维度 FAIL；这是真实结果，我不会改数据集或伪造通过。
7. **模型选择**：默认 `deepseek-chat`（`.env` 已配置且白名单有效）；如需 `deepseek-reasoner`（通常 token 更多）请在授权时说明。

> 以上任一项未确认时，我不会执行真实调用；当前环境保持 `TRACEPILOT_ALLOW_REAL_API` 未设置、真实入口 403。

---

## 8. 离线测试结果（本步实际执行）

### 8.1 门禁与零调用证明
- `POST /api/agent/runs`（`mode=real_api, confirm_real=true`）→ **403**：`真实 DeepSeek API 调用未获授权：需请求显式 confirm_real=True 且服务端设置 TRACEPILOT_ALLOW_REAL_API=1（当前环境该开关为关闭状态）`。
- `POST /api/pipelines`（`execution_mode=real_api, confirm_real=true`）→ **403**：`真实 API 模式未获授权：需请求显式 confirm_real=True 且服务端设置 TRACEPILOT_ALLOW_REAL_API=1；本次未发起任何付费调用`。
- `/api/health` 的 `trace_count` 在门禁测试前后均为 **8759**（未新增 Trace、无付费调用）。

### 8.2 环境与前端
- `/api/agent/status`：真实模式 `available=false`、`configured_model=deepseek-chat`、白名单有效、`paid_api_called=false`。
- Playwright（Edge）实测 4 个页面：Trace 详情正常显示历史真实 Trace 的 UNKNOWN 分类与原因；DAG 渲染 6 个节点（`svg=30, .dag-node=6`）；`/runs` 真实模式为禁用态；`/evaluation` 显示 2 个合成数据集及“不调用付费 API”声明；**0 console error**。
- 截图：`docs/step62_evidence/trace-detail-legacy-real.png`、`dag-legacy-real.png`、`agent-runs-gate.png`、`evaluation-center.png`。

### 8.3 真实 Trace 的引擎级链路
- 历史真实 Trace `trace-4d3d82dc7a67496dbc2842cd3b31aa47`：读取 6 步成功；DAG 6 节点 / 6 边 / 1 根；治理分类 **UNKNOWN**（原因：历史数据缺 `real_api_verified`，按保守规则不升级）。
- 用该 Trace 直接调用 `evaluation_engine.evaluate_trace`（真实 Trace + 从任务文本推导的案例定义）：**overall PASS**；task_success / tool_correctness / process_quality / efficiency / safety = PASS，recovery = NOT_APPLICABLE；evidence 含 `duration_ms=3336`、`token_total=1852`、`tool_call_count=2`、`dag_ok=true`。
- `POST /api/attribution/traces/{id}` → 200（`attr-095e857c…`，候选原因 UNKNOWN，如实标注证据不足）。
- `GET /api/cost/traces/{id}` → tokens 1,852（`PROVIDER_REPORTED: 4` 次调用，prompt 1,631 / completion 221），`cost.status=PRICE_UNKNOWN`、`amount=null`，`paid_api_called=false`。
- 评测入口探测：`POST /api/evaluation/traces/{id}` → **404**（确认产品级“按 Trace 评测”入口缺失，见 B2）。

### 8.4 自动化测试
- `pytest tests/test_agent_runner.py tests/test_agent_runner_api.py tests/test_data_governance.py -q` → **68 passed**（37.30s）。
- `pytest tests/test_agent_runner.py -q`（最终版本）→ **27 passed**（5.44s，含新增 2 项证据标记测试）。
- `python -m py_compile backend/app/services/agent_runner.py tests/test_agent_runner.py` → OK。
- 后端全量 `pytest tests -q` → **687 passed**（441.58s，0 failed；较第 55-A 基线 685 增加本步新增 2 项证据标记测试）。
- 本步未改前端代码，未重跑前端 build/e2e（第 61 步基线：前端 build 通过、浏览器 106 passed）。

### 8.5 本步执行边界
- 未设置 `TRACEPILOT_ALLOW_REAL_API`；未调用 DeepSeek 或其他付费 API；未新增/修改任何“真实运行”数据；未手工修改 Trace 数据库或 JSON 文件；未将 mock/simulated 结果标为真实；未覆盖用户未提交 WIP。

---

## 附录 A：授权后的执行顺序（等你确认后才会执行）

1. 备份当前数据（已有 `backups/step58/step55a` 等；执行前再补一次 `data/traces` 与 `data/pipelines` 快照），记录 Trace 数基线。
2. 设置 `TRACEPILOT_ALLOW_REAL_API=1` 并重启后端；确认 `/api/agent/status` 的 `real_api.available=true`。
3. 上传任务 B 文档 → 得到 `document_id`。
4. 依次执行 A → B → C（每个请求独立、`confirm_real=true`、带第 4 节限额），**不自动重试**；每次记录：run_id、trace_id、status、elapsed_ms、模型调用次数、工具调用、token_usage、limit_violations。
5. 对每条 Trace 验证：Trace 详情、DAG、治理分类 REAL_API、成本 PRICE_UNKNOWN（不显示 0）、前端截图。
6. 如执行 Phase 2：创建并启动 Pipeline（`max_cases=1`），轮询到终态，验证 stages、`trace_ids` 真实、评测 case_results 引用真实 Trace、归因报告、质量门禁与最终报告。
7. 移除 `TRACEPILOT_ALLOW_REAL_API` 并重启后端；汇总实际调用次数/Token/成本状态，更新本报告与 `PROJECT_STATE.md`；绝不把实际结果与预估混写。

## 附录 B：本步证据文件
- 离线检查报告（本文件）
- `docs/step62_real_api_tasks.json`（3 任务完整规格与预算）
- `docs/step62_task_b_reference.txt`（40 词参考文档）
- `docs/step62_evidence/*.png`（浏览器实测截图）
- 备份：`backups/step62_prework_20261120_084933/`
