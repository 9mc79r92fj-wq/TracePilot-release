# 术语规范与"防雷"对照表

本文件是 TracePilot 术语的唯一事实来源，由 `scripts/check_terminology.py` 在本地测试与 CI 中强制检查。
**除本文件与检查脚本自身外，仓库内任何文件都不得出现本文"禁用词"列中的词。**

> 为什么需要这份文件：这些词在面试/技术评审中一旦用错，会被理解为架构理解偏差。
> 例如把显式计划的执行范式说成 ReAct，或把"受控执行"泛称为"沙箱"。
> 同时必须避免"过度替换"——`React` 是前端框架，与 `ReAct` 仅差大小写，盲目全局替换会直接破坏前端代码。

## 1. 标准术语表

| 场景 | 标准写法（中文/英文） | 代码落点 |
|---|---|---|
| Agent 执行范式 | **Plan-and-Execute**：`understand → plan → act/tool loop` | `backend/app/agents/basic_agent.py`（LangGraph: understand / plan / agent / tools） |
| 执行环境与安全边界 | **受控执行（工具白名单 + 有界步数）** | `backend/app/agents/basic_agent.py`、`backend/app/services/execution_config.py`、红队接口 `execution_environment=controlled_execution` |
| 评测编排 | **评测流水线**（Evaluation Pipeline） | `backend/app/services/evaluation_pipeline.py` |
| 评测门禁 | **质量门禁**（Quality Gate） | `backend/app/services/quality_gate.py`、`scripts/ci_quality_gate.py` |
| 根因分析 | **根因归因**（Root Cause Attribution / RCA） | `backend/app/services/rootcause_v2.py` |
| 可观测性导出 | **OTLP/HTTP 导出**（OpenTelemetry Protocol） | `backend/app/services/langfuse_export.py` |
| 增量导出状态 | **内容指纹 + 幂等 ID** | 同上（`traceId/spanId` 由 TracePilot ID 确定性派生） |

## 2. 禁用词 → 标准词映射

| 禁用词 | 标准词 | 说明 |
|---|---|---|
| `ReAct` | `Plan-and-Execute` | 本项目计划是显式产物（`plan` 节点），不是推理-行动隐式交替；**不能把 `React` 前端框架一起替换** |
| `沙箱`（含 `sandbox` / `controlled_sandbox`） | `受控执行（工具白名单 + 有界步数）` / `controlled_execution` | 安全边界靠白名单工具、步数/Token 上限、无 shell/文件/网络副作用实现 |
| `Harness` | 按语境：**评测流水线**（评测编排）/ **智能体开发工具**（开发工具）/ **被测 Agent**（运行时） | 不允许再用 `Harness` 泛指任何一层；历史文档按语境改写为准确术语 |

## 3. 例外与解释（为什么不做"无脑全局替换"）

1. `React` / `react` 是前端框架，和 `ReAct` 是不同术语。检查脚本用大小写敏感 + 单词边界匹配，只拦 `ReAct`。
2. 历史需求文档里出现的 `Harness` 指"DeepSeek 智能体开发工具"，不是本项目的评测流水线。
   若机械替换成"评测流水线"，会把"用什么开发工具"写成"用评测流水线开发"，属于事实性错误。
   本次已按语境统一为**智能体开发工具**。
3. `backend/app/services/red_team_generator.py` 中"载荷不得被执行"的主语是**被测 Agent**，
   不是评测流水线，因此改写为"被测 Agent"。
4. 本文件与 `scripts/check_terminology.py` 因需要写出禁用词，被检查脚本显式放行。

## 4. 本次实际修正清单（2026-11-28）

| 文件 | 修改 |
|---|---|
| `README.md` | "安全沙箱" → "受控执行（工具白名单 + 有界步数）"；新增 Plan-and-Execute 架构说明、术语规范入口、JD 术语映射 |
| `docs/INTERVIEW_GUIDE.md` | "沙箱限制" → "受控执行限制（工具白名单 + 有界步数）" |
| `backend/app/main.py` | 接口值 `controlled_sandbox` → `controlled_execution`，并新增 `execution_environment_note` 说明约束 |
| `tests/test_adversarial_lab.py` | 同步断言新值 + 校验说明字段 |
| `docs/ADVERSARIAL_LAB.md` | 接口值同步为 `controlled_execution` |
| `DECISIONS.md` / `PROJECT_STATE.md` | "DeepSeek Harness 工具/工作区" → "DeepSeek 智能体开发工具 / 智能体开发工具工作区" |
| `docs/PRD.md` / `docs/REQUIREMENTS_AUDIT.md` | 历史需求中的开发工具名称按语境统一为"智能体开发工具" |
| `backend/app/services/red_team_generator.py` | 注释主语改为"被测 Agent" |

`ReAct` 全库扫描结果为 **0 处**（`React` 前端框架不计入），因此没有替换动作；
README 与本文新增了 Plan-and-Execute 的显式表述，保证对外口径一致。

## 5. 检查方式

```bash
python scripts/check_terminology.py          # 全库
python scripts/check_terminology.py --json   # 机器可读
python -m pytest tests/test_terminology.py -q
```

CI 的 `agent-evaluation` 工作流在跑测试前会执行同一检查；发现禁用词会让流水线失败。
