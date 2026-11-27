# TracePilot V2 · 公开 Agent 故障 Benchmark 报告

本报告只包含实际下载、实际运行和实际计量的数据。实验日期：2026-11-27。

## 1. 数据来源

| 项 | 实际值 |
|---|---|
| Benchmark | Microsoft AgentRx（Agent Root Cause Attribution Benchmark） |
| 仓库 | <https://github.com/microsoft/AgentRx>（MIT） |
| 上游 commit | `7a18c79708e7671be15124460f4f7296107c2a55`（GitHub main，2026-09-14） |
| 论文 | Barke et al., *AgentRx: Diagnosing AI Agent Failures from Execution Trajectories*, arXiv:2602.02475 |
| 许可 | MIT；`data/benchmarks/agentrx/LICENSE.txt` 原样保留 |
| 数据子集 | 73 条：tau_retail 29 + magentic_one 44；3,265 条消息、334 条人工失败标注 |
| 校验 | 48 个数据文件逐一核对上游 GitHub blob SHA-1；manifest 记录 sha256/bytes/upstream_path |
| 未使用 | HuggingFace `microsoft/AgentRx` 为 gated，本环境未获得访问授权，**未使用 HF 产物**，也不宣称覆盖 HF 完整 170 条 |

详细字段、标签结构与限制见 `docs/AGENTRX_BENCHMARK.md`。

## 2. 数据划分

- 按 `sha256(case_id + "|" + seed)` 稳定划分，seed=42；
- **dev 38 / test 35**；tau 15/14，magentic 23/21；两组严格互斥；
- 本轮真实对照只使用 **test split**；
- 由于真实调用预算有限，选取 test 中按 case_id 排序的 **14 条 tau + 11 条 magentic = 25 条**（固定选择，可复现）。

标签隔离：诊断 Prompt 只包含 `case_id/source/domain/task/steps`；`root_cause_step`、`failure_category` 等标签只在指标计算阶段使用，测试对 73 条案例逐一验证无标签泄漏。

## 3. 三组方法

| 方法 | 描述 | 模型调用 |
|---|---|---|
| Baseline B：确定性规则 | 错误关键词、数字一致性、tau 政策-认证顺序等规则打分，选最高分候选 | 0 次，零成本 |
| Baseline A：LLM 直接诊断 | 将完整轨迹交给 DeepSeek Flash，一次 JSON 诊断 | 每案例 1 次 |
| TracePilot V2：规则+DAG+证据+LLM | 先由规则/DAG/证据血缘产生候选与理由，再让同模型基于轨迹与候选裁决；Prompt 明确要求区分最早根因与下游症状 | 每案例 1 次 |

统一条件：同一模型 `deepseek-flash`、temperature=0、JSON Output、thinking disabled、相同 test 案例与相同标签。真实调用经持久预算模块按每次上游调用扣减。

## 4. 实际结果（25 案例，真实 DeepSeek 调用）

### v2.1（最终版本；Prompt 要求优先最早根因、候选可能含症状）

| 指标 | Baseline B | Baseline A | TracePilot V2 |
|---|---:|---:|---:|
| Root Cause Top-1 Accuracy | 0.04 | **0.32** | 0.20 |
| Root Cause Top-3 Hit Rate | 0.08 | 0.32 | **0.40** |
| Failure Step Accuracy（Top-1 命中任一标注失败步骤） | 0.08 | **0.48** | 0.36 |
| Failure Category Accuracy | 0.12 | 0.16 | **0.20** |
| Failure Category Macro-F1 | 0.0351 | 0.0873 | **0.0926** |
| 模型调用次数 | 0 | 25 | 25 |
| 实测 Token 总数 | 0 | 97,001 | 108,642 |
| 平均诊断延迟 | 0 ms（本地规则） | 1,293.9 ms | 1,596.6 ms |

### v2.0 与 v2.1 对比（同一批案例，仅改 Prompt）

| 版本 | Top-1 | Top-3 | Failure Step | Category Acc | Macro-F1 |
|---|---:|---:|---:|---:|---:|
| v2.0 | 0.20 | 0.32 | 0.32 | 0.16 | 0.0556 |
| v2.1 | 0.20 | **0.40** | 0.36 | **0.20** | **0.0926** |

### 按期一致性

- Baseline A 与 V2 同时命中：5/25；
- 仅 A 命中：3/25；仅 V2 命中：0/25；均未命中：17/25。

**结论（如实）**：V2 的证据辅助显著提升了 Top-3 召回（0.32→0.40）与类别指标，但 **Top-1 低于 Baseline A（0.20 vs 0.32）**。本轮不宣称 V2 超过直接 LLM 诊断；Bad Case 分析表明，规则候选中的“显式报错/停滞”证据会把模型锚定到下游症状步骤，损害最早根因的精确命中。详见 `docs/V2_BAD_CASE_ANALYSIS.md`。

## 5. Token 与费用

- Baseline A：25 次调用，97,001 tokens；
- V2：50 次调用（v2.0 + v2.1 各 25），209,490 input / 5,964 output，合计 215,454 tokens；
- 两组合计真实调用 75 次；
- 本地预算模块记录全部 V2 实验（含校准/反事实/修复）共 142 次上游调用、369,830 input / 58,278 output；按配置单价（1 元/M input、2 元/M output）估算 **¥0.486386**，明确标注为估算，不是 Provider 账单；
- 未调用付费 API 之外的资源，未使用付费算力。

## 6. 复现方法

```bash
# 1) 配置 .env（不提交密钥）
DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=deepseek-flash
TRACEPILOT_ALLOW_REAL_API=1
TRACEPILOT_REAL_TOTAL_CALL_LIMIT=160
TRACEPILOT_REAL_VISITOR_DAILY_LIMIT=200

# 2) 运行：确定性 + 真实三组对照（25 条 test 子集）
python scripts/run_v2_benchmark.py   # 若脚本不存在，可使用本报告记录的内联流程
```

原始结果：`data/v2_experiments/benchmark_rootcause_results.json`（运行时产物）；
GitHub 归档：`docs/v2_results/benchmark_rootcause_results.json`。

## 7. 限制

1. 只使用公开 GitHub 子集 73 条，不是 HF 完整 170 条；样本量 25，置信区间较宽；
2. 步骤编号映射为消息下标，和 AgentRx 原始工具调用边界可能存在偏差；
3. 单次调用、单一模型（deepseek-flash）；未做多次采样与显著性检验；
4. 真实模型存在随机性；temperature=0 也不能保证完全确定性；
5. 类别集合按 AgentRx 标注归一化，大小写/近义词合并规则见 `rootcause_v2._normalize_category`；
6. 本实验不能证明生产环境因果，只能描述固定样本、固定 Prompt、固定模型下的诊断表现。
