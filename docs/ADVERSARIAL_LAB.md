# Adversarial Evaluation Lab（对抗评测实验室）

- 后端：`backend/app/services/red_team_generator.py`（扩展）+ `/api/red-team/*`
- 前端：`/adversarial`（`frontend/src/pages/AdversarialLab.tsx`）
- 兼容：既有 `/api/redteam/*` 与 `/redteam` 页面保留

---

## 1. API（第 46 步新增 `/api/red-team/*`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/red-team/types` | 6 类攻击 + 3 难度 + 11 安全约束 + **限制声明** |
| POST | `/api/red-team/generate` | 生成受控案例（`attack_types`/`difficulties`/`variations`/`seed`/`include_canary`） |
| POST | `/api/red-team/runs` | 创建并执行运行（支持 `case_ids` 选案例） |
| GET | `/api/red-team/runs` | 运行列表 |
| GET | `/api/red-team/runs/{run_id}` | 运行详情（指标 + 覆盖率 + 限制） |
| GET | `/api/red-team/runs/{run_id}/cases` | 案例查询（按类型/难度/状态/安全判定筛选） |
| GET | `/api/red-team/runs/{run_id}/cases/{case_id}` | 单案例详情（约束逐条 + Trace 链接） |
| GET | `/api/red-team/runs/{run_id}/bad-cases` | Bad Case（含失败原因 + Trace/DAG 链接） |
| GET | `/api/red-team/runs/{run_id}/coverage` | 攻击覆盖地图 |

## 2. 安全限制（第 46 步）

| 限制 | 值 |
|------|-----|
| 单次生成案例 | ≤120 |
| 单次执行案例 | ≤60（`case_ids` 亦受限） |
| 变体数 | ≤4 |
| 攻击类型 | ≤6 |
| 载荷长度 | ≤1200 字符 |
| 并发运行 | 1（超出返回 429） |
| 执行环境 | `controlled_execution`（受控执行：工具白名单 + 有界步数） |
| 付费 API | 禁用（`paid_api_called=false`，Token 恒 0） |

## 3. Attack Coverage Map 计算口径

| 字段 | 定义 |
|------|------|
| `generated_cases` | 数据集内该类型案例数（含未执行） |
| `executed_cases` | 实际执行完成数 |
| `not_executed_cases` | 生成但未执行（**不并入通过**） |
| `safety_passed` | `safety.verdict == SAFE` |
| `safety_failed` | `safety.verdict == UNSAFE` |
| `unknown_cases` | 执行异常或 `INCONCLUSIVE`（**不算通过也不算失败**） |
| `coverage_status` | `FULL`（全执行且无 unknown）/ `PARTIAL` / `NOT_COVERED` |

自洽约束（有测试断言）：
`passed + failed + unknown == executed`，`not_executed == generated - executed`。

## 4. 页面交互

1. 选择测试类型（6 类复选）
2. 选择难度（easy/medium/hard）
3. 设置变体数量（1–4）
4. 设置随机种子（可复现）
5. 点击「生成案例」
6. 查看生成结果表（案例 ID / 类型 / 难度 / 变体 / 安全约束徽标）
7. 展开查看案例载荷与预期安全行为
8. 勾选案例（支持全选/清空）并「运行所选」
9. 运行中显示受控沙盒提示
10. 查看任务成功率（独立指标）
11. 查看安全通过率
12. 查看 Bad Case Explorer（可按风险类型/执行状态/安全状态/失败原因筛选）
13. 查看关联 Trace（跳转 Trace 详情）
14. 跳转 DAG 分析
15. 查看 Attack Coverage Map（热力卡片 + 七列明细表）
16. 查看数据来源标记（`synthetic`）与付费 API 状态

## 5. 已知限制

- 执行模型为确定性探针，非真实 LLM（衡量门禁而非模型行为）
- 覆盖率只反映"是否执行并得出判定"，不反映攻击强度
- 金丝雀负控属合成违规，仅验证检测器
- 运行记录仅存内存（重启丢失；Trace 已落盘）