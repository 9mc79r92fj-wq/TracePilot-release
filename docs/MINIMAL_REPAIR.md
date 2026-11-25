# Minimal Repair Engine 最小修复引擎

- 对应代码：`backend/app/services/minimal_repair.py`
- API：`POST /api/repair/report`、`GET /api/repair/types`
- 状态：已实现，14 项离线测试通过（模拟环境，未接入真实模型）

---

## 1. 产品目标

从故障 Trace 自动生成**安全、可验证、可解释**的最小修复候选，并用受控反事实实验验证哪些候选**确实**能消除故障，输出带排序与推荐依据的 `RepairReport`。核心价值：把「修复建议」从主观猜测变成「有受控实验证据支撑的候选列表」。

## 2. 修复搜索策略

不搜索任意补丁，而是在**有限修复空间**内按故障类型映射候选：

```
读取故障 Trace（只读）
  → 提取故障类型 / 故障节点 / 关联工具
  → 读取故障传播分析（标注潜在下游影响，仅作风险提示）
  → 按故障类型映射白名单修复类型
  → 生成 ≤5 个候选（每个含 modification / 预期作用 / 风险 / 适用条件）
  → 逐候选走 CounterfactualEngine 受控验证
  → 可解释评分 + 排序
  → 输出 RepairReport（含推荐与未验证事项）
```

## 3. 候选生成规则

| 触发故障特征 | 生成的修复类型 | 修改内容 |
|--------------|----------------|----------|
| 含 `timeout` | `INCREASE_TIMEOUT` | `timeout_ms`（≤30000） |
| 含 `timeout` | `LIMITED_RETRY` | `max_retries`（≤3） |
| 含 `exception` / `tool_error` | `FALLBACK_RESULT` | 明确标记 `__fallback__` 的安全降级结果 |
| 含 `argument` / `invalid`（非 result） | `ARGUMENT_CORRECTION` | 白名单参数合法值 |
| 含 `permission` | `SAFE_TOOL_ALTERNATIVE` | 预批准等价工具间切换 |
| 存在可跳过步骤 | `SKIP_OPTIONAL_STEP` | 仅 `context_review` / `reflection` / `evaluation` |

不支持的修复类型直接拒绝（`UnsupportedRepairError`）；无故障 Trace 返回空候选（不伪造）。

## 4. 验证流程

每个候选复用 **CounterfactualEngine**：

1. 候选映射为引擎允许的受控干预（映射不到的标记 `not_tested`）；
2. 生成独立 `experiment_id` / `trace_id` / `run_id`（原始 Trace 只读）；
3. 受控重跑（模拟模型 + 白名单工具，`replays=2`）；
4. 判定有效性——三选一即视为**已验证**：
   - 故障集合被移除；
   - 任务状态由失败转为成功；
   - 工具最终恢复成功（如重试后成功）且原始 Trace 中工具为失败；
5. 记录 before/after 状态、耗时、Token、工具调用、修复后 Trace ID；
6. 未通过 → `failed`；无法执行 → `not_tested`。

## 5. 评分机制

评分 = 加权归一化分数（0..1），**仅为排序启发式，不表示真实成功概率**。

| 维度 | 默认权重 | 说明 |
|------|----------|------|
| verified | 0.45 | 验证通过 1.0 / 失败 0.0 / 未测 0.3 |
| scope | 0.20 | 修改字段数越少越高 |
| latency | 0.10 | 修复后耗时对比 |
| token | 0.10 | Token 成本对比 |
| tool_calls | 0.05 | 工具调用增量 |
| side_effect | 0.10 | 风险条数越少越高 |

- 权重可配置（`weights` 参数），实际权重记录在 `weights_used` 与每个候选的 `score_breakdown.weights`；
- **缺失指标按中性 0.5 处理并显式标注 `note: 数据缺失，按中性处理（未填 0）`**，不冒充 0；
- 排序规则：`verified` > `failed` > `not_tested`，同组内按得分降序；
- 仅当第一名状态为 `verified` 时才给出推荐。

## 6. 安全边界

- 原始 Trace 只读（`original_trace_modified: false`，测试断言字节级不变）；
- 修复类型固定白名单，禁止任意 Shell / Python / 外部 URL；
- 修改内容仅在白名单值与预批准工具范围内；
- 工具限制为 `calculator` / `text_analyzer`（本地无副作用）；
- 默认模拟模型，不调用付费 API；
- 有界：候选 ≤5、超时 ≤30s、重试 ≤3。

## 7. 已知限制

- `SKIP_OPTIONAL_STEP` 无法在受控模拟执行器中表达 → 恒为 `not_tested`；
- `SAFE_TOOL_ALTERNATIVE` 当前等价工具组每类只有 1 个工具，实际无法切换；
- 修复空间有限，无法表达复杂代码级修复；
- 验证在模拟模型与简化执行路径下完成，结论不可直接外推到生产；
- 评分权重为经验值，未做标定；
- 无故障 Trace 不产生候选（返回空列表）。

## 8. 与反事实实验的关系

Minimal Repair 是 Counterfactual Engine 的**上层应用**：

| 层次 | 职责 |
|------|------|
| CounterfactualEngine | 通用干预执行器：给定干预 → 受控重跑 → 观察/干预结果 + 因果证据等级 |
| MinimalRepair | 领域层：故障类型 → 修复候选；候选 → 干预映射；按验证结果与成本排序 |

典型链路：故障 Trace → 候选生成 → 每个候选一次受控实验 → 评分排序 → `RepairReport`。其中 `experiment_id` / `repair_trace_id` 可直接回溯到反事实实验记录，便于审计与复现。

---

## 9. Repair Studio 产品化（第 34 步）

### 9.1 异步报告与资源控制（repair_store.py）

| 机制 | 取值 | 说明 |
|------|------|------|
| 并发上限 | 2 | 固定线程池，超出返回 HTTP 429 |
| 单次报告超时 | 60s | 超时标记 `failed` |
| 重复提交抑制 | 60s 窗口 | 相同 (trace_id, max_candidates, verify, baseline) 直接复用 report_id |
| 内存保留上限 | 50 条 | 防无界增长；进程重启清空 |

### 9.2 API 契约

| 方法与路径 | 说明 |
|-----------|------|
| `GET /api/repairs/candidates/{trace_id}` | 获取候选（仅生成、不验证、快速），含故障节点与候选的修改内容/适用条件/风险 |
| `POST /api/repairs/validate` | 提交受控验证（异步），返回 `report_id` + 运行状态 + links |
| `GET /api/repairs/reports/{report_id}` | 查询状态（running/succeeded/failed）、完整报告、修复后 Trace、前后对比、评分、推荐依据、未验证事项 |

错误约定：Trace 不存在 404、report 不存在 404、非法 trace_id 400、Pydantic 校验失败 422、并发满 429。

### 9.3 Repair Confidence Breakdown

页面为每个候选展示完全由真实报告字段推导的置信分解（**不生成任何成功率**）：

| 字段 | 数据来源 |
|------|----------|
| `verification_status` | 候选真实验证状态 |
| `experiment_count` | 有 `experiment_id` 记 1，否则 0（未执行） |
| `modification_scope` | `score_breakdown.scope.changed_fields` |
| `latency_cost` | `cost_delta.duration_before_ms → after_ms` |
| `token_cost` | `cost_delta.token_before → token_after` |
| `tool_call_cost` | `cost_delta.tool_calls_delta`（缺失显示 Not Recorded） |
| `risk_level` | `score_breakdown.side_effect.risk_count` |
| `evidence_limitations` | `score_breakdown.causal_evidence_level` + 模拟环境提示 |

页脚固定声明：以上为受控模拟环境下的验证元数据，不是成功率，不代表真实生产结果。
