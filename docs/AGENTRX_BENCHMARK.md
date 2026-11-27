# AgentRx 公开故障 Benchmark 接入（AgentRx Benchmark Adapter）

- 代码：`backend/app/services/agentrx_benchmark.py`
- 数据子集：`data/benchmarks/agentrx/`
- Manifest：`data/benchmarks/agentrx/manifest.json`
- 测试：`tests/test_agentrx_benchmark.py`
- 数据来源：Microsoft AgentRx（MIT），<https://github.com/microsoft/AgentRx>
- 论文：*AgentRx: Diagnosing AI Agent Failures from Execution Trajectories*，[arXiv:2602.02475](https://arxiv.org/abs/2602.02475)

---

## 1. 一句话说明

本接入把 AgentRx **公开 GitHub 仓库** `data/` 目录中的 tau-bench retail（29 条）
与 Magentic-One（44 条）失败轨迹转换为 TracePilot V2 统一的 `BenchmarkCase`，
提供稳定的 dev/test 划分、标签隔离 API 与 `TraceRecord` 转换，用于根因定位 /
故障归因评测。

> **不是** Hugging Face 完整数据集的下载结果。Hugging Face 上的
> `microsoft/AgentRx` 数据集为 gated（受限访问），本环境未下载；本接入只使用
> 公开、MIT 许可的 GitHub 数据子集，不声称覆盖官方完整 170 条。

---

## 2. 数据来源与许可

| 项目 | 值 |
|------|----|
| 仓库 | <https://github.com/microsoft/AgentRx> |
| 固定 commit | `7a18c79708e7671be15124460f4f7296107c2a55`（2026-09-14T22:18:36Z） |
| 许可 | MIT（Copyright (c) Microsoft Corporation） |
| 许可文件 | `data/benchmarks/agentrx/LICENSE.txt`（逐字节原样复制） |
| 论文 | arXiv:2602.02475，<https://arxiv.org/abs/2602.02475> |
| 快照校验 | 48 个复制文件的 git blob SHA-1 与 GitHub commit tree 一致（见 manifest） |

数据以**逐字节复制**方式保存，未改写内容；`manifest.json` 记录了每个文件的
`upstream_path`、`bytes`、`sha256`、`git_blob_sha1` 与 `upstream_blob_sha1_at_commit`。

---

## 3. 数据子集与案例数

| 来源 | 领域 | 案例数 | 消息数 | 失败标注数 | 原始文件 |
|------|------|--------|--------|------------|----------|
| tau | tau-bench retail | 29 | 1064 | 39 | `ground_truth/tau_ground_truth.json` + `tau_retail/tau_dataset_failed.json` |
| magentic | Magentic-One | 44 | 2201 | 295 | `ground_truth/magentic_one_ground_truth.json` + `magentic_dataset/<uuid>.json` |
| **合计** | 2 个领域 | **73** | **3265** | **334** | 48 个复制文件（含 LICENSE，约 4.0 MiB） |

复制到 `data/benchmarks/agentrx/` 的内容：

```text
data/benchmarks/agentrx/
├── LICENSE.txt
├── manifest.json
├── ground_truth/
│   ├── tau_ground_truth.json              # 29 条
│   └── magentic_one_ground_truth.json     # 44 条
├── tau_retail/
│   └── tau_dataset_failed.json            # 29 条失败轨迹（含 traj）
└── magentic_dataset/
    └── <44 个 UUID>.json                  # 仅复制有 ground truth 的轨迹
```

**没有复制**（详见 manifest `subset.not_copied`）：

- `data/failure_timelines.pdf`、`data/policies/*.txt`、`data/tool_schemas/`；
- `data/ground_truth/tau_retail.json`（24 条部分标注文件；本接入使用 29 条的
  `tau_ground_truth.json`）；
- `magentic_dataset/` 中 14 个没有 ground truth 的轨迹 JSON，以及
  `steps_by_id.json` / `count.py` / `magentic_count.jsonl`；
- `magentic_dataset_whowhen/`、代码、`trajectories/` 等与数据加载无关的内容。

上述 44 个 Magentic-One 轨迹文件覆盖 44 条 ground truth；公开 GitHub 的
`data/` 目录不包含 Flash 领域 ground truth，因此本子集只覆盖 tau 与 Magentic-One
两个领域。

---

## 4. 统一数据结构

`BenchmarkCase`（Pydantic，`extra="forbid"`、`frozen=True`，只读）字段：

| 字段 | 说明 |
|------|------|
| `case_id` | 稳定 ID：`tau-<task_id>` / `magentic-<uuid>` |
| `source` | `tau` / `magentic` |
| `domain` | `tau_retail` / `magentic_one` |
| `task` | tau：`info.task.instruction`；magentic：第一条 `human` 消息（原样） |
| `steps` | `BenchmarkStep` 列表（`step_index` 为 1-based） |
| `root_cause_step` | 根因步骤（1-based 消息下标） |
| `root_cause_failure_id` | 根因对应的原始 `failure_id`（保留原始类型） |
| `root_cause_category` | 根因 `failure_category` |
| `failure_steps` | 全部失败步骤（1-based，升序） |
| `failure_categories` | 与 `failure_steps` 严格对齐的失败类别 |
| `failed_agent` | 根因步骤的 `failed_agent`（保留原始大小写） |
| `split` | `dev` / `test`（由 `case_id + seed` 稳定计算） |
| 扩展字段 | `root_cause_reason`、`failure_summary`、`failure_ids`、`failure_agents` |

`BenchmarkStep` 字段：`step_index`、`role`（原始 role）、`agent`（归一化
Agent 名，便于与 `failed_agent` 比较）、`content`（允许为 `None`）、
`raw_message`（原始消息，原样保留）、`tool_name` / `tool_call_id` /
`tool_arguments`（仅原始消息确实存在时）。

---

## 5. step 映射规则

AgentRx 的 `failures[].step_number` 是 **1-based 消息下标**：

- **tau**：`tau_dataset_failed[*].traj[step_number - 1]`；轨迹内 `index` 字段
  与 1-based 下标一致（适配器逐条校验 `raw_message["index"] == step_index`）。
  `task` 取 `info.task.instruction`。
- **magentic**：`magentic_dataset/<trajectory_id>.json[step_number - 1]`；
  `task` 取第一条 `role == "human"` 的消息内容。

role 与 agent 归一化：

| 原始 role | `agent` |
|-----------|---------|
| `system` / `user` / `assistant` / `tool`（tau） | `System` / `User` / `Assistant` / `Tool` |
| `human`（magentic） | `User` |
| `Orchestrator (...)` | `Orchestrator` |
| `WebSurfer` / `Websurfer` | `WebSurfer` |
| `FileSurfer` / `ComputerTerminal` / `Assistant` | 同名归一化 |

允许 tau 的 assistant 消息 `content=null`（仅携带 `tool_calls`，共 274 条），
原样保存为 `content=None` + `raw_message`。

---

## 6. 标签结构与标签隔离

AgentRx ground truth 的标签结构：

```json
{
  "trajectory_id": "...",
  "failures": [
    {
      "failure_id": 1,
      "step_number": 13,
      "step_reason": "...",
      "failure_category": "Instruction/Plan Adherence Failure",
      "category_reason": "...",
      "failed_agent": "WebSurfer"
    }
  ],
  "root_cause": {"failure_id": 1, "reason_for_root_cause": "..."},
  "failure_summary": "..."
}
```

适配器将 `root_cause.failure_id` 关联到 `failures[]` 中对应项，得到
`root_cause_step` / `root_cause_category` / `failed_agent`。上游存在一处
`root_cause.failure_id="1"`（字符串）而 `failures[].failure_id=1`（整数）的
类型不一致，适配器按 `str()` 匹配并保留原始值。

**标签隔离（硬约束）**：

- 评测标签只通过 `BenchmarkCase` / `case_labels(case)` 暴露；
- `case_without_labels(case)` 返回可 JSON 序列化的诊断输入，只含
  `case_id / source / domain / task / split / steps`，不含任何
  `LABEL_FIELDS`（root cause、failure 标注、failed_agent 等）；
- `to_trace_record(case)` 生成的 TraceRecord 同样不包含任何标签字段；
- 测试会递归检查 key，并断言序列化文本中不出现 `root_cause` 及全部标签字段名。

---

## 7. 数据划分（dev / test）

- 方法：`sha256(case_id + "|" + seed)` 的前 8 个十六进制字符视为 uint32；
  `< 2^31 → dev`，否则 `→ test`。`seed` 默认 `42`。
- 稳定：同一 `case_id + seed` 永远落入同一 split；
- 互斥：dev 与 test 无交集，并集等于全部 73 条（严格 train/test 隔离）；
- 纯函数：`split_of(case_id, seed)` / `split_cases(cases, seed)` 均只读。

seed=42 的划分：

| 来源 | dev | test | 合计 |
|------|-----|------|------|
| tau | 15 | 14 | 29 |
| magentic | 23 | 21 | 44 |
| **合计** | **38** | **35** | **73** |

`dev` 作为开发 / 校准分区，`test` 留出评测；两者不可混用。更换 seed 可复现
不同的划分实验，但同一 seed 下划分完全确定。

---

## 8. TraceRecord 转换

`to_trace_record(case) -> TraceRecord`：

| 项 | 规则 |
|----|------|
| `trace_id` | `trace-agentrx-<sha256(case_id)[:16]>`，确定性且唯一 |
| `task_id` / `run_id` | `case_id` / `run-agentrx-<...>` |
| `status` | `failed`（AgentRx 案例本身是已知失败轨迹；非逐步诊断结论） |
| `step_type` | 原始消息含 `tool_calls` 或 `role=tool` → `tool_call`；Agent 消息 → `model_call`；user / system / human → `generic` |
| 工具调用 | **不伪造**：Magentic-One 对话消息全部不是 `tool_call`；tau 的 568 个工具步骤均来自原始 `tool_calls` / `tool` 消息 |
| 原始消息 | 保存在 `step.input` 与 `step.metadata["raw_message"]`；`step.output` 为消息 content |
| 时间戳 | AgentRx 不提供逐消息时间戳；使用固定基准 + 1-based 下标的**合成时间**，metadata 标注 `timestamp_synthesized=true` |
| 标签 | 不进入 TraceRecord；`metadata.labels_excluded=true` |
| 副作用 | 只读，不写 `data/traces` 或任何生产数据 |

转换后的 step type 分布（73 条案例）：

| step_type | 数量 |
|-----------|------|
| `model_call` | 2376 |
| `tool_call` | 568 |
| `generic` | 321 |

---

## 9. 使用方式

```python
from backend.app.services import agentrx_benchmark as ab

cases = ab.load_cases()                 # 73 条；默认 seed=42 写入 split
tau_cases = ab.load_cases(source="tau") # 29 条
magentic_cases = ab.load_cases(source="magentic")  # 44 条

groups = ab.split_cases(cases)          # {"dev": [...], "test": [...]}
assert not ({c.case_id for c in groups["dev"]} & {c.case_id for c in groups["test"]})

summary = ab.summarize(cases)           # 数量 / 步骤 / 标签分布 / 划分
manifest = ab.load_manifest()           # 来源 commit、许可、校验和、限制

case = cases[0]
model_input = ab.case_without_labels(case)  # 诊断模型输入：无标签
labels = ab.case_labels(case)               # 评测标签：仅用于打分
record = ab.to_trace_record(case)           # 可被 TraceRecord 校验
```

只读函数：`load_manifest`、`load_cases`、`split_of`、`split_cases`、
`case_labels`、`case_without_labels`、`to_trace_record`、`summarize`。

---

## 10. 与 Hugging Face gated 数据集的差异

| 维度 | Hugging Face `microsoft/AgentRx` | 本接入（公开 GitHub 子集） |
|------|----------------------------------|----------------------------|
| 访问方式 | gated（需申请 / 授权） | 公开 MIT GitHub 仓库，随仓库保存 |
| 数据规模 | 完整发布（任务背景提到约 170 条，本环境未下载、未验证） | **73 条**：tau 29 + magentic 44 |
| 领域 | 完整发布包含多领域（如 tau、Flash、Magentic-One） | 仅 tau_retail 与 Magentic-One（GitHub `data/` 无 Flash ground truth） |
| 用途 | 官方完整评测 | 公开可复现的子集接入与评测 |
| 声明 | — | **不冒充官方完整 170 条，不以 HF 数据名义发布** |

Hugging Face 数据集为 gated，本环境无法下载；本接入不访问网络、不包含任何
HF 下载产物。所有数据均可追溯到固定 GitHub commit。

---

## 11. 限制与已知数据问题

1. **仅公开子集**：73 条（tau 29 / magentic 44）不是官方完整评测集；论文 /
   HF 版本的完整规模未在此验证，不声称覆盖。
2. **领域不完整**：GitHub `data/` 无 Flash 领域 ground truth，本接入不含 Flash。
3. **标签为上游人工/LLM 判定**：类别与根因步骤是 AgentRx 的标注结果，不保证
   绝对正确；只能作为评测金标准，不能当作因果真值。
4. **标签绝不进入模型输入**：`case_without_labels` / `to_trace_record` 均已隔离，
   测试覆盖 73 条全部案例。
5. **时间戳为合成**：轨迹无逐消息时间，TraceRecord 使用确定性合成时间仅满足
   schema，不代表真实执行时序之外的新信息。
6. **step 角色错位**：上游有 3 处 `failed_agent` 与消息 role 不一致
   （magentic 2 处 `FileSurfer`/`Assistant` 标注落在 `Orchestrator (thought)`，
   tau 1 处 `Assistant` 标注落在 `user`）；适配器保留原始标注，仅做
   case-insensitive 对齐检查。
7. **上游类型/大小写不一致**：1 处 `root_cause.failure_id` 为字符串；部分
   Magentic-One `failed_agent` 写成 `Websurfer`；适配器按字符串匹配、归一化
   agent 名，但原始值不改写。
8. **只读**：所有函数不写生产 `data/traces`；测试不访问网络。

---

## 12. 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_agentrx_benchmark.py
# 22 passed
```

覆盖：

1. manifest 来源 / commit / MIT / 数量 / 限制，48 个文件 SHA-256 与磁盘一致；
2. tau 29 + magentic 44 = 73，task / role / content / raw message 逐条对齐；
3. root cause step 在范围内，failure_steps / categories / ids / agents 对齐；
4. 划分稳定、互斥、并集完整，seed=42 计数（38/35）与按来源计数（15/14、23/21）；
5. 标签隔离：`case_without_labels` 与 `to_trace_record` 序列化无 `root_cause`
   或 failure 标签字段；
6. 全部 73 条 TraceRecord 通过 Pydantic 校验、原始消息保留、无伪造工具调用、
   转换确定性可复现。

---

## 13. 引用与许可

```bibtex
@article{barke2026agentrx,
  title={AgentRx: Diagnosing AI Agent Failures from Execution Trajectories},
  author={Barke, Shraddha and Goyal, Arnav and Khare, Alind and Singh, Avaljot and Nath, Suman and Bansal, Chetan},
  journal={arXiv preprint arXiv:2602.02475},
  year={2026}
}
```

数据子集按上游 MIT 许可使用与再分发，`LICENSE.txt` 原样保存于
`data/benchmarks/agentrx/LICENSE.txt`。使用时请同时引用 AgentRx 论文与
<https://github.com/microsoft/AgentRx>。
