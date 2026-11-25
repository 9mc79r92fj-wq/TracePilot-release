# 故障注入模块设计文档

- 对应代码：`backend/app/services/fault_injector.py`
- 集成点：`backend/app/agents/basic_agent.py`（工具执行链路）
- 状态：已实现，离线测试通过（真实 DeepSeek API 端到端未验证）

---

## 1. 功能目标

为 TracePilot 的故障归因实验提供**确定性、可控、可记录**的故障注入能力：在 Agent 工具执行链路的指定环节人为制造故障，构造"带金标准标注"的失败场景，用于后续验证归因算法能否定位到被注入的根因。

## 2. 用户使用场景

- 评测工程师对某个工具（如 sqlite_query）注入"越权拒绝"故障，观察 Agent 如何应对并记录轨迹；
- 在检索工具上注入"结果为空"故障，验证归因算法能否定位到该步骤；
- 批量注入故障生成带标注的失败用例集，作为归因准确率的金标准。

## 3. 六种故障类型

| 类型 | 触发阶段 | 行为 |
|------|----------|------|
| `TOOL_TIMEOUT` | 前置（before） | 抛 `FaultInjectionError`，模拟工具超时 |
| `TOOL_EXCEPTION` | 前置（before） | 抛 `FaultInjectionError`，模拟工具异常 |
| `INVALID_ARGUMENTS` | 前置（before） | 抛 `FaultInjectionError`，模拟参数不合法 |
| `PERMISSION_DENIED` | 前置（before） | 抛 `FaultInjectionError`，模拟越权被拒绝 |
| `INVALID_TOOL_RESULT` | 后置（after） | 工具正常执行后，结果被篡改为错误格式 |
| `EMPTY_RETRIEVAL` | 后置（after） | 工具正常执行后，结果被替换为空列表 |

## 4. 输入 / 处理 / 输出

- **输入**：`FaultRule` 规则（故障类型 + 目标工具名 / 目标步骤 ID + 触发次数）；
- **处理**：`FaultInjector.before_tool(tool, step)` 在前置阶段匹配并抛异常；`after_tool(tool, step, result)` 在后置阶段匹配并篡改结果；命中后 `remaining` 递减并写入 `fired`；
- **输出**：`FaultInjectionError`（前置）或 `(篡改结果, 故障记录)`（后置）；`get_config()` 返回可序列化配置，`fired` 记录全部触发。

## 5. 故障注入与真实故障的区别

| 维度 | 注入故障 | 真实（自然）故障 |
|------|----------|------------------|
| 来源 | FaultInjector 规则命中 | 工具自身/环境异常 |
| 可复现性 | 确定性，可重复 | 不可控 |
| 标注 | `metadata.fault_injection = {type, injected: true, message}` | 无该标记 |
| 用途 | 金标准根因标注 | 生产排查 |

Trace 中通过 `metadata.fault_injection.injected == true` 严格区分两者。

## 6. 与 Trace 及 DAG 的关系

- 注入故障的步骤被记录为失败 `TraceStep`（含 `error.error_type=故障类型`、`timestamp`、`status`），`metadata.fault_injection` 标记注入事实；
- 故障步骤通过 `parent_step_ids` 关联到触发它的模型调用步骤，可被 DAG 模块解析为节点；
- `TraceRecord.metadata` 记录 `fault_injection_config`（规则）、`fault_injections`（触发清单）与 `recovery_observed`（Agent 是否在故障后继续执行）。

## 7. 后续反事实实验如何复用

- 故障注入构造"已知根因"的失败 Trace → 归因算法输出候选根因 → 与 `fault_injection` 标注比对计算 Top-1/Top-3 准确率；
- 干预重放产生新 Trace 时，可对同一 `target_step` 应用不同的 `FaultRule` 做对照实验；
- `fired` 中的 `target_step` 直接作为反事实干预的锚点步骤。

## 8. 已知限制

- 故障仅覆盖工具执行链路，尚未覆盖模型调用、规划、检索等节点（可通过 `target_step` 扩展）；
- `TOOL_TIMEOUT` 为即时抛异常模拟，非真实挂起超时；
- 未实现随机概率注入（当前为确定性规则计数）；
- 真实 DeepSeek API 端到端行为尚未验证。
