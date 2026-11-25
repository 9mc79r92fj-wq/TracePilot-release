# AUTOMATED_EVALUATION_PIPELINE · 自动化评测流水线（第 52 步）

> 目标：把已有评测能力按依赖关系编排为一条**可复现、可观测、可取消**的流水线，
> 复用而非重写既有引擎；每个阶段都有明确状态、真实工件引用与错误详情。
>
> 诚实性声明：
> - 默认 **Mock 模式**（受控确定性模拟执行器），不调用真实模型、不产生付费 API 调用；
> - `real_api` 模式需**双重授权**（请求 `confirm_real=true` + 服务端
>   `TRACEPILOT_ALLOW_REAL_API=1`），未授权返回 403 且零调用；
> - 已授权的真实 API 流水线路径**未实际验证**（不产生付费调用），标记为 NOT_VERIFIED；
> - `SKIPPED` 绝不标记为 `SUCCEEDED`；未完成的流水线不展示伪造结论。

## 1. 复用矩阵（不重复开发引擎）

| 阶段 | 复用的既有模块 |
|------|----------------|
| `dataset_validation` | `evaluation_engine.load_dataset` + 数据集白名单 |
| `agent_execution` | `evaluation_engine.execute_case`（Mock）/ `agent_runner.run_agent_task`（real_api，双重授权） |
| `trace_collection` | `evaluation_engine.persist_trace` + `trace_store.load_trace` |
| `deterministic_evaluation` | `evaluation_engine.evaluate_trace` + `build_evaluation_report_from_results` |
| `judge_evaluation`（可选） | `llm_judge.build_judge`（仅 Mock，零成本） |
| `bad_case_attribution`（可选） | `bad_case_attribution.attribute_evaluation_run` |
| `failure_pattern_mining`（可选） | `failure_pattern_mining.analyze_run` |
| `version_comparison`（可选） | `evaluation_version_comparison.compare_runs` |
| `regression_detection`（可选） | `regression_detector.detect_regressions` |
| `quality_gate` | `quality_gate.evaluate_gate` |
| `report_generation` | 本模块（JSON + Markdown 最终报告） |

为支持复用，对 `evaluation_engine` 做了一次**无行为变更**的抽取：
新增 `build_evaluation_report_from_results()`（由已执行结果聚合报告）与
`persist_trace()`（公开 Trace 持久化包装）；`run_evaluation()` 行为保持不变（既有测试回归通过）。

## 2. 阶段定义（11 个）

| # | stage_id | 名称 | 必选 | 依赖 | 说明 |
|---|----------|------|------|------|------|
| 1 | dataset_validation | 数据集验证 | ✅ | — | 白名单 + 文件存在 + 案例结构/ID 唯一 + 数量上限 + task 字段 |
| 2 | agent_execution | Agent 执行 | ✅ | dataset_validation | Mock 受控执行器逐案例执行（可取消/超时） |
| 3 | trace_collection | Trace 采集 | ✅ | agent_execution | 持久化 Trace + 逐个校验可读，输出 trace_id |
| 4 | deterministic_evaluation | 确定性评测 | ✅ | trace_collection | 六维度确定性验证器 + 聚合报告 + 登记���行 |
| 5 | judge_evaluation | Judge 评测 | 可选 | deterministic_evaluation | 仅 `judge_mode=mock` 时执行；无 Judge → SKIPPED |
| 6 | bad_case_attribution | Bad Case 归因 | 可选 | deterministic_evaluation | 无 Bad Case → SKIPPED |
| 7 | failure_pattern_mining | 失败模式挖掘 | 可选 | bad_case_attribution | 无 Bad Case → SKIPPED |
| 8 | version_comparison | 版本对比 | 可选 | deterministic_evaluation | 无基线 → SKIPPED |
| 9 | regression_detection | 回归检测 | 可选 | version_comparison | 无基线 → SKIPPED |
| 10 | quality_gate | 质量门禁 | ✅ | deterministic_evaluation / version_comparison / regression_detection | 无基线 → SKIPPED（原因明确） |
| 11 | report_generation | 报告生成 | ✅ | —（always_run） | 失败/取消/超时也生成报告 |

## 3. 状态机

阶段状态（7 种）：`PENDING / RUNNING / SUCCEEDED / FAILED / SKIPPED / BLOCKED / CANCELLED`。

| 状态 | 含义 |
|------|------|
| `PENDING` | 尚未开始（取消时批量转 CANCELLED） |
| `RUNNING` | 正在执行（含有限重试的中间态） |
| `SUCCEEDED` | 实际执行完成且成功 |
| `FAILED` | 实际执行失败（含 `error_details.type`，如 TIMEOUT） |
| `SKIPPED` | **未执行**：未启用 / 无基线 / 无 Bad Case（`skip_reason` 必填） |
| `BLOCKED` | 依赖阶段失败/阻塞导致无法执行（`skip_reason` 必填） |
| `CANCELLED` | 用户取消 |

流水线终态：`SUCCEEDED / FAILED / BLOCKED / CANCELLED`（`PENDING / RUNNING` 为进行中），
合并规则（确定性）：

```
CANCELLED  → 存在取消请求或任一阶段 CANCELLED
FAILED     → 任一阶段 FAILED
BLOCKED    → 任一阶段 BLOCKED
SUCCEEDED  → 其余（允许 SKIPPED，如未启用可选阶段/无基线）
```

不允许：`SKIPPED` 标 `SUCCEEDED`；未执行阶段出现在成功统计中；失败无错误信息。

## 4. 可复现性

每次运行记录：

| 字段 | 来源 | 缺失处理 |
|------|------|----------|
| `pipeline_run_id` | `pipe-<uuid>` | — |
| `dataset_version` | dataset_validation 阶段从数据集读取 | `null` + notes |
| `agent_version` | Mock 执行器无独立版本 | `null` + note「未伪造」 |
| `prompt_version` | Mock Judge 的 Prompt 版本 | `null` + notes |
| `evaluator_version` | `evaluation_engine.ENGINE_VERSION` | — |
| `rubric_version` | Mock Judge Rubric 版本 | `null` + notes |
| `execution_mode` | mock / real_api | — |
| `random_seed` | 请求参数（确定性执行器不依赖随机） | — |
| `config_fingerprint` | `pipe-cfg-<sha256[:12]>` | — |
| `created_at` | UTC ISO 时间 | — |

`reproducibility.notes` 明确说明每个缺失字段的原因。

## 5. 执行控制与限额

| 能力 | 入口 |
|------|------|
| 创建 | `POST /api/pipelines`（PENDING，不自动执行） |
| 启动 | `POST /api/pipelines/{id}/start`（仅 PENDING，后台线程） |
| 查询状态 | `GET /api/pipelines/{id}` |
| 查询阶段 | `GET /api/pipelines/{id}/stages` |
| 查询报告 | `GET /api/pipelines/{id}/report` |
| 取消 | `POST /api/pipelines/{id}/cancel`（PENDING 立即；RUNNING 置标记） |
| 历史 | `GET /api/pipelines`（内存 + 磁盘快照，页面刷新可恢复） |
| 元信息 | `GET /api/pipelines/meta`（阶段/状态/限制） |

限制（服务端强制）：

| 限制 | 值 |
|------|-----|
| 单次案例数 | ≤ 50（`max_cases`） |
| 最大并发流水线 | 1（超出启动 → 429） |
| 最大等待队列 | 10（超出创建 → 429） |
| 最大运行时间 | 1..300 秒（默认 120；超时 → 阶段 FAILED/TIMEOUT） |
| 最大重试次数 | 0..2（默认 0；**不自动无限重试**） |
| 输入长度 | label ≤ 120；baseline_run_id ≤ 64；Pydantic 字段长度校验 |
| 单次执行超时 | `evaluation_store` 单次评测 120s（既有），流水线整体另有限制 |

## 6. 数据一致性（真实工件引用）

流水线记录 `refs`：

| 引用 | Mock | real_api | 说明 |
|------|------|----------|------|
| `evaluation_run_id` | ✅ | ✅ | 登记进 `evaluation_store`（`register_report`，不重新执行） |
| `agent_run_ids` | 空（无独立 Agent Run） | ✅ | `agent_runner.run_agent_task` 产生 |
| `trace_ids` | ✅（逐条可 `load_trace`） | ✅ | 不修改原始 Trace |
| `bad_case_ids` | ✅ | ✅ | 来自评测报告 `is_bad_case` |
| `attribution_report_ids` | ✅（有 Bad Case 时） | ✅ | 归因报告持久化 |
| `failure_pattern_report_id` | ✅（有 Bad Case 时） | ✅ | 第 48 步报告 |
| `comparison_id` | ✅（有基线时） | ✅ | 第 49 步报告 |
| `regression_report_id` | ✅（有基线时） | ✅ | 第 50 步报告 |
| `quality_gate_report_id` | ✅（有基线时） | ✅ | 第 51 步报告 |

最终报告包含 `evidence_refs`（kind/ref_id/detail），所有引用使用真实产生的 ID；
流水线**不修改任何原始 Trace**，也不复制粘贴报告内容伪造新结果。

## 7. 安全与费用

- 默认 `mock`：受控确定性执行器，无网络、无付费 API、Token 恒为 0；
- `real_api`：需请求 `confirm_real=true` **且** 服务端 `TRACEPILOT_ALLOW_REAL_API=1`，
  缺一返回 403 且不发起任何请求；公开接口不自动触发；
- Pipeline 不接收/不打印任何 API Key；上游密钥由服务端 `.env` 管理，前端不可见；
- 并发/队列/时长/重试/案例数/输入长度全部受限（见第 5 节）；
- 数据源白名单（`ALLOWED_DATASETS`），不接受任意文件路径。

## 8. API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/pipelines/meta` | 阶段定义/状态枚举/执行模式/限制 |
| POST | `/api/pipelines` | 创建（400 配置非法 / 403 未授权 / 429 队列满） |
| POST | `/api/pipelines/{id}/start` | 启动（404/409/429） |
| POST | `/api/pipelines/{id}/cancel` | 取消（404/409） |
| GET | `/api/pipelines` | 历史列表 |
| GET | `/api/pipelines/{id}` | 完整状态 |
| GET | `/api/pipelines/{id}/stages` | 阶段结果 |
| GET | `/api/pipelines/{id}/report` | 最终报告 |

## 9. 前端 Pipeline Run Inspector

访问路径：**`/pipelines`**（导航「Evaluation Pipeline」）。

布局（浅色专业 SaaS 风格，支持 PC/平板/手机）：

- **左侧**：流水线配置（数据集、案例上限、随机种子、执行模式、Judge 模式/Rubric、
  基线版本、可选阶段开关、运行上限、最大重试、标签、创建/启动/取消）；
- **中部**：可交互阶段流程图（11 个阶段按钮 + 状态徽标 + 箭头依赖顺序 + 进度条 +
  失败阶段摘要）；点击阶段选中；
- **右侧**：阶段详情（状态/开始结束/耗时/尝试次数/输入输出引用/跳过原因/错误详情）
  + 全阶段运行日志；
- **底部**：最终报告（可复现性字段、阶段统计、关联工件、证据入口、评测摘要、
  版本对比、回归检测、质量门禁、限制声明、报告文件路径）+ **历史流水线**列表
  （刷新、查看历史、恢复状态）。

未运行/运行中：显示「报告尚未生成（不展示未完成结论）」，绝不显示空白页；
失败：显示失败阶段与错误类型/消息；跳过：显示跳过原因。

## 10. 错误处理矩阵

| 场景 | 结果 |
|------|------|
| 数据集缺失 | dataset_validation FAILED（错误含文件名），下游 BLOCKED，报告仍生成 |
| 配置无效 | 创建阶段 400（未知数据集/越界参数/非法 judge_mode/baseline 不存在/输入超长） |
| Agent 执行失败 | agent_execution FAILED（含案例数与首个错误），下游 BLOCKED，报告生成 |
| Trace 保存失败 | trace_collection FAILED（明确「Trace 保存失败」） |
| Judge 不可用 | judge_evaluation FAILED（确定性评测结果保留） |
| 归因失败 | bad_case_attribution FAILED，失败模式挖掘 BLOCKED |
| 版本不可比 | version_comparison SUCCEEDED（报告含 NOT_COMPARABLE），quality_gate 输出 INCONCLUSIVE / exit 2 |
| 门禁证据不足 | quality_gate INCONCLUSIVE（不缺证据时 BLOCK/WARN；绝不 PASS） |
| 任务取消 | PENDING 立即 CANCELLED；RUNNING 阶段 CANCELLED、后续 CANCELLED，报告生成 |
| 页面刷新后查询历史 | 从磁盘快照恢复；历史列表可查看；运行中流水线继续轮询 |

## 11. 实测（本地，Mock 模式）

| 场景 | 结果 |
|------|------|
| 完整 Mock 流水线（`tracepilot-fault-v1`） | SUCCEEDED；8/11 阶段 SUCCEEDED，4 阶段 SKIPPED（judge/对比/回归/门禁，原因明确）；归因与失败模式报告可查询 |
| 无基线流水线（`tracepilot-eval-v1`） | SUCCEEDED；version_comparison / regression / quality_gate SKIPPED |
| 带基线 + 质量门禁（同数据集） | SUCCEEDED；version_comparison/regression/quality_gate SUCCEEDED；gate `PASS` exit 0 |
| 基线/候选不可比 | comparison NOT_COMPARABLE；gate `INCONCLUSIVE` exit 2 |
| Judge 启用（mock） | judge_evaluation SUCCEEDED；报告含 judge_breakdown（is_mock=true） |
| Agent 执行失败（注入） | FAILED，下游 BLOCKED，报告含错误详情 |
| Trace 保存失败（注入） | FAILED（trace_collection） |
| Judge 不可用（注入） | FAILED（judge_evaluation），确定性评测仍 SUCCEEDED |
| 归因失败（注入） | FAILED（bad_case_attribution），failure_pattern_mining BLOCKED |
| 超时（1s 运行上限 + 慢执行） | agent_execution FAILED（TIMEOUT），下游 BLOCKED，报告生成 |
| 取消（PENDING / RUNNING） | CANCELLED；阶段 CANCELLED；报告仍生成 |
| 重试（max_retries=1） | 首次失败后重试 1 次；持续失败 attempts=2 后 FAILED（不无限重试） |
| real_api 未授权 | 403，零调用，不创建流水线 |
| 付费 API | **0 次** |
| 密钥泄露 | 伪造 `DEEPSEEK_API_KEY` 不出现在任何流水线记录/报告/输出 |

记录：流水线快照与报告写入 `data/pipelines/reports/`（gitignored），页面刷新与历史查询可恢复。

最终 HTTP 验证（真实后端进程）：共创建/运行 3 条流水线，全部 `SUCCEEDED`：

| 流水线 | 场景 | 阶段结果 | 门禁 |
|--------|------|----------|------|
| `pipe-49e3e97f327b4bedb86d8141b3981f5c` | eval-v1 / 3 例 / 无基线 | 5 SUCCEEDED + 6 SKIPPED | 未执行（无基线） |
| `pipe-6748a7729a364dde9a420111ae564192` | eval-v1 / 3 例 / 带基线 | 8 SUCCEEDED + 3 SKIPPED | **PASS（退出码 0）** |
| `pipe-a0368b06cd1a4c0181c48bd54c68b664` | fault-v1 / 无基线 | 7 SUCCEEDED + 4 SKIPPED（含归因/失败模式） | 未执行（无基线） |

历史列表可检索上述 3 条；归因报告 / 失败模式报告 / 质量门禁报告均可通过既有 API 独立查询
（HTTP 200）。

## 12. 测试

- `tests/test_evaluation_pipeline.py`（**31 项**）：完整 Mock 流水线、可选阶段跳过、无基线跳过、
  阶段顺序/状态转换、Trace/Bad Case 关联、质量门禁集成（PASS/INCONCLUSIVE）、失败/超时/取消、
  限额/重试/配置校验、真实 API 授权、历史恢复、密钥不泄露、API 契约；
- `frontend/e2e/pipeline.spec.ts`（**4 项**）：创建→启动→阶段流程图→报告、基线/门禁、
  real_api 拒绝、刷新后历史恢复；
- 全量回归：后端 **596 passed / 0 failed**；浏览器 **88 passed / 0 failed**（82 → 88：新增
  `/pipelines` 页面冒烟 2 项 + pipeline e2e 4 项；响应式矩阵按视口覆盖 17 页）；
  前端构建通过（tsc + Vite）。

## 13. 限制与后续

- 评测运行/流水线记录主要驻留内存（磁盘快照可恢复展示，但下游证据 ID 在进程重启后不可查询）；
- 已授权的 `real_api` 流水线执行路径未实际验证（NOT_VERIFIED，不产生付费调用）；
- Mock 模式不产生独立 Agent Run 记录（以评测案例为单位执行），已写入 limitations；
- 流水线为顺序执行（单并发），无分布式调度/队列持久化；
- 报告生成不含 HTML 可视化；历史保留无自动清理策略。
