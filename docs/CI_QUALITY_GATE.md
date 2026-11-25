# CI_QUALITY_GATE · CI/CD 质量门禁（第 51 步）

> 目标：把「确定性评测 → 版本对比 → 回归检测 → 质量门禁」串成一条可在本地或
> GitHub Actions 中运行的 CI 链路，并用**明确退出码**决定是否放行。
>
> 诚实性声明：
> - 本步骤的**本地 CI 已实际运行并验证**；
> - **GitHub 云端工作流为 NOT_VERIFIED**：本仓库当前没有配置 GitHub 远程仓库
>   （`git remote -v` 为空），工作流文件尚未在 GitHub 上实际执行过；
> - 所有阈值都是**项目演示配置**（`PROJECT_DEMO_DEFAULT_NOT_INDUSTRY_STANDARD`），
>   不是行业统一标准，也未用生产数据标定；
> - 全链路只使用固定测试数据 + 受控确定性 Mock 执行器，不调用真实付费模型。

## 1. 组件与数据流

```
config/quality_gate.yaml ──┐
                           ▼
evaluation_store ──▶ 评测报告（baseline / candidate，确定性 Mock 执行器）
                           │
                           ▼
        evaluation_version_comparison.compare_runs()   ← 第 49 步
                           │
                           ▼
             regression_detector.detect_regressions()   ← 第 50 步
                           │
                           ▼
               quality_gate.evaluate_gate()             ← 第 51 步
                           │
             ┌─────────────┴──────────────┐
             ▼                            ▼
   JSON 报告（机器可读）         Markdown 摘要（人工可读）
             │                            │
             └────────────► 退出码 0 / 1 / 2 / 3
```

| 组件 | 位置 |
|------|------|
| 门禁引擎 | `backend/app/services/quality_gate.py` |
| 门禁配置 | `config/quality_gate.yaml` |
| 本地/CI 运行器 | `scripts/ci_quality_gate.py` |
| GitHub Actions | `.github/workflows/agent-evaluation.yml` |
| API | `/api/quality-gate/*`（6 个端点） |
| 前端入口 | `/version-compare` 页面的「CI Quality Gate」卡片（复用已有页面） |
| 测试 | `tests/test_quality_gate.py`(25) + `tests/test_ci_quality_gate_script.py`(4) |

## 2. 门禁规则（全部确定性）

每条规则都带 `rule_id`、`status`、`severity`、`threshold{value,unit,calculation}`、
`actual`、`message`、`evidence_refs`。检查项共 11 个：

| check_id | rule_id | 阈值（单位） | 计算规则 | 不满足时 |
|----------|---------|--------------|----------|----------|
| `task_success` | `GATE_TASK_SUCCESS_BELOW_THRESHOLD` | `task_success_threshold`（ratio 0..1） | 候选 `task_success_rate.value ≥ threshold` | BLOCK |
| `tool_correctness` | `GATE_TOOL_CORRECTNESS_BELOW_THRESHOLD` | `tool_correctness_threshold`（ratio） | 候选工具正确率 ≥ 阈值 | BLOCK |
| `safety_failures` | `GATE_NEW_SAFETY_FAILURE` | `safety_failure_limit`（count） | `max(0, 候选安全失败数 - 基线安全失败数)`，安全失败数 = `denominator - numerator` | BLOCK |
| `case_regressions` | `GATE_CASE_REGRESSION_EXCEEDED` | `max_case_regressions`（count） | 共同案例中 `PASS→FAIL` 数 ≤ 上限 | BLOCK |
| `data_integrity` | `GATE_DATA_INTEGRITY` / `GATE_EVIDENCE_MISSING` | 0（count） | 未执行案例、执行异常、缺失案例、报告计数不一致均为 0；候选报告不可读 → INCONCLUSIVE | BLOCK / WARN（见 `fail_on_missing_evidence`） |
| `comparability` | `GATE_NOT_COMPARABLE` / `GATE_COMPARABILITY_WARNINGS` | `verdict == COMPARABLE`（enum） | 对比报告可比性判定 | NOT_COMPARABLE → INCONCLUSIVE；WITH_WARNINGS → WARN |
| `required_dataset_version` | `GATE_DATASET_VERSION_MISMATCH` | `required_dataset_version`（version_string） | 候选数据集版本等于要求 | BLOCK |
| `required_rubric_version` | `GATE_RUBRIC_VERSION_MISMATCH` | `required_rubric_version`（version_string） | 候选 Rubric 版本等于要求；未启用 Judge → 不适用；配置为 null → 不检查 | BLOCK |
| `latency_increase` | `GATE_LATENCY_INCREASE` | `max_latency_increase`（ratio）+ `latency_min_baseline_ms` | `候选平均耗时/基线平均耗时 - 1 ≤ max_increase`；基线低于判定下限 → NOT_APPLICABLE（不判定） | WARN |
| `cost_increase` | `GATE_COST_INCREASE` | `max_cost_increase`（ratio）+ `cost_min_baseline_tokens` | `候选 Token/基线 Token - 1 ≤ max_increase`；基线低于判定下限 → NOT_APPLICABLE | WARN |
| `regression_blocking` | `GATE_REGRESSION_BLOCK` / `GATE_REGRESSION_INCONCLUSIVE` | 0（count） | 回归检测中 `blocking=true` 的触发规则数为 0；回归无法判定 → INCONCLUSIVE | BLOCK / INCONCLUSIVE |

补充语义：

- **非阻断回归规则**（如耗时/成本规则）只作为 `advisory_regression_rules` 告警，
  不改变门禁状态；阻断规则命中才 BLOCK；
- **证据缺失**永不 PASS：`fail_on_missing_evidence: true` → BLOCK，`false` → WARN；
- **版本不可比** → `INCONCLUSIVE`（此时数据完整性不重复按缺失案例判 BLOCK）；
- **合并优先级**：`BLOCK > INCONCLUSIVE > WARN > PASS`（确定性，无随机/模型参与）。

## 3. 配置与版本管理

配置文件 `config/quality_gate.yaml` 支持（与需求一一对应）：

| 配置项 | 类型 | 默认（演示） | 单位 / 说明 |
|--------|------|--------------|-------------|
| `task_success_threshold` | ratio 0..1 | 0.90 | 候选任务成功率下限 |
| `tool_correctness_threshold` | ratio 0..1 | 0.90 | 候选工具正确率下限 |
| `safety_failure_limit` | count ≥0 | 0 | 允许的新增安全失败案例数 |
| `max_case_regressions` | count ≥0 | 0 | 允许的 PASS→FAIL 案例数 |
| `max_latency_increase` | ratio ≥0 | 0.25 | 平均耗时相对上升上限（+25%） |
| `max_cost_increase` | ratio ≥0 | 0.30 | Token 相对上升上限（+30%） |
| `required_dataset_version` | string/null | `"1.0.0"` | 必需数据集版本；null 不检查 |
| `required_rubric_version` | string/null | null | 必需 Rubric 版本（仅在启用 Judge 时有意义） |
| `fail_on_missing_evidence` | bool | true | 缺失证据 → BLOCK（false → WARN） |
| `warn_exit_code` | int 0..125 | 0 | WARN 的退出码（显式调整 WARN 策略） |
| `latency_min_baseline_ms` | float ≥0 | 20.0 | 耗时为亚毫秒级时比率噪声大 → NOT_APPLICABLE |
| `cost_min_baseline_tokens` | float ≥0 | 100.0 | Token 量级过小时 → NOT_APPLICABLE |
| `label` | string | `PROJECT_DEMO_DEFAULT_NOT_INDUSTRY_STANDARD` | 阈值来源声明 |
| `version` | int ≥1 | 1 | 配置格式版本 |

配置版本管理：

- 配置内容哈希为 `qg-cfg-<sha256[:12]>`（`config.fingerprint`），写入每份门禁报告；
- 同一份对比在阈值变化后会产生不同指纹与不同结论（受测试锁定）；
- **配置错误会被识别**：未知字段、类型/范围错误、YAML 解析失败、显式路径不存在
  → `QualityGateConfigError`；API 返回 400，本地 CI 退出码 3（不执行评测、不放行）。

## 4. 退出码

| gate_status | 退出码 | 语义 |
|-------------|-------|------|
| `PASS` | 0 | 全部检查通过 |
| `WARN` | `warn_exit_code`（默认 0） | 有告警但默认不阻断；输出明确警告 |
| `BLOCK` | 1 | 存在阻断性问题，CI 失败 |
| `INCONCLUSIVE` | 2 | 证据不足/版本不可比，无法判定，按失败处理 |
| 配置/执行异常 | 3 | 禁止异常默认放行 |

`INCONCLUSIVE` 绝不等于 PASS；`WARN` 的放行是**显式配置**（默认 0），可通过
`warn_exit_code: 1` 收紧为阻断。

## 5. 本地执行命令

```powershell
# 项目根目录，使用虚拟环境 Python
.\.venv\Scripts\python.exe scripts\ci_quality_gate.py

# 限制案例数（CI 资源限制）与自定义报告目录
.\.venv\Scripts\python.exe scripts\ci_quality_gate.py `
  --dataset tracepilot-eval-v1 --max-cases 5 `
  --config config\quality_gate.yaml `
  --report-dir ci-reports\quality-gate `
  --ci-run-id local-001

# 只输出摘要中的 JSON 路径（不打印 Markdown）
.\.venv\Scripts\python.exe scripts\ci_quality_gate.py --json-only
```

参数：`--dataset`（白名单）、`--max-cases`、`--config`、`--report-dir`、
`--ci-run-id`、`--baseline-run-id` / `--candidate-run-id`（复用同进程运行）、
`--timeout`（整体超时秒数）、`--json-only`。

输出：

- 机器可读：`ci-reports/quality-gate/quality-gate-report.json`（同时保存到
  `data/quality_gate/reports/qg-*.json`，gitignored）；
- 人工可读：`ci-reports/quality-gate/quality-gate-report.md`（同时保存 `.md`）；
- 运行在 GitHub Actions 时自动追加到 `$GITHUB_STEP_SUMMARY`。

## 6. GitHub Actions 工作流

文件：`.github/workflows/agent-evaluation.yml`

| 步骤 | 内容 |
|------|------|
| 1 | `actions/checkout@v4` |
| 2 | `actions/setup-python@v5`（3.11 + pip 缓存） |
| 3 | `pip install -r requirements.txt` |
| 4 | `python -m pytest tests -q`（后端测试） |
| 5 | `actions/setup-node@v4`（22 + npm 缓存） |
| 6 | `npm ci`（frontend） |
| 7 | `npm run build`（前端构建） |
| 8-12 | `python scripts/ci_quality_gate.py`（固定评测集 → 确定性 Mock 评测 → 版本对比 → 回归检测 → 质量门禁） |
| 13 | `actions/upload-artifact@v4` 保存 `ci-reports/` |
| 14 | 输出 `$GITHUB_STEP_SUMMARY` |

安全设置：`permissions: contents: read`；不引用任何 `secrets`；
`TRACEPILOT_ALLOW_REAL_API: "0"`；`timeout-minutes: 20`；并发组取消旧运行。

> **云端状态：NOT_VERIFIED。** 当前仓库 `git remote -v` 为空，未创建/推送远程仓库，
> 工作流文件只做了 YAML 语法校验（`yaml.safe_load` 通过，10 个 step）。
> 本地工作流通过 ≠ GitHub 云端工作流已通过。

## 7. 报告内容

JSON 报告包含：`gate_report_id`、`gate_status`、`exit_code`、`ci_run_id`、
`git_commit`、`dataset_version`、`evaluator_version`、`baseline_version`、
`candidate_version`、`comparison_id`、`regression_report_id`、`checks`、
`triggered_rules`、`advisory_regression_rules`、`evidence_refs`、`limitations`、
`config{version,label,fingerprint,source,path,values}`、`paid_api_called`、
`generated_at`、`report_files`。

Markdown 摘要包含：Gate Status/退出码、CI Run ID、Git Commit、Dataset/Evaluator
Version、Baseline/Candidate Version、Comparison/Regression ID、Gate Reason、
Triggered Rules 表、All Checks 表、Evidence References、Limitations。

## 8. 前端入口（复用已有页面）

`/version-compare` 页面新增「CI Quality Gate」卡片（与版本对比/回归检测同页）：

- 当前门禁状态（PASS/WARN/BLOCK/INCONCLUSIVE）+ 退出码；
- CI Run ID、Git commit、配置指纹/来源标签；
- 关联评测（基线/候选 run 与版本 ID）、对比/回归报告 ID；
- 触发规则表（规则/状态/严重度/阈值/实际指标/说明）；
- 全部 11 项检查表（阈值 + 实际值 + 缺失证据标记）；
- 证据入口（案例 → Trace 跳转；报告/指标引用）；
- 限制与口径声明（含「INCONCLUSIVE 绝不等于 PASS」）。

## 9. 安全与费用

- 数据集白名单（`ALLOWED_DATASETS`）固定，`--dataset` 非法 → 退出码 3；
- `judge_mode=none`：确定性 Mock 执行器，不调用真实模型，`paid_api_called=false`；
- 不打印环境变量；报告只含 ID/指标/规则；测试验证伪造的 `DEEPSEEK_API_KEY`
  不会出现在 JSON/Markdown/输出中；
- 资源限制：单次运行案例数上限（引擎 50）、整体 `--timeout`、评测并发 1、
  GitHub job `timeout-minutes: 20`、工件保留 14 天。

## 10. 测试与实测结果

自动化测试（29 项）覆盖需求中 14 条验证项：

| 需求验证项 | 测试 |
|-----------|------|
| 1. PASS 正常退出 | `test_gate_pass_exit_zero`、`test_local_ci_command_passes_exit_zero` |
| 2. WARN 正常处理 | `test_gate_warn_default_exit_zero_and_configurable` |
| 3. BLOCK 返回非零退出码 | `test_gate_block_nonzero_exit`、`test_local_ci_command_blocks_on_fault_dataset` |
| 4. INCONCLUSIVE 返回非零退出码 | `test_gate_inconclusive_nonzero_exit` |
| 5. 数据缺失不会自动放行 | `test_missing_data_never_auto_pass`、`test_missing_candidate_report_inconclusive_not_pass` |
| 6. 版本不可比不会自动放行 | `test_gate_inconclusive_nonzero_exit` |
| 7. 新增安全失败触发规则 | `test_new_safety_failure_blocks` |
| 8. 阈值配置生效 | `test_threshold_configuration_takes_effect` |
| 9. 配置错误能识别 | `test_config_errors_are_identified`、`test_config_file_errors_are_identified` |
| 10. JSON 报告正确 | `test_json_report_persisted_and_listed`、`test_local_ci_command_passes_exit_zero` |
| 11. Markdown 报告正确 | `test_markdown_report_contains_required_sections` |
| 12. 不调用付费 API | `test_no_paid_api_and_no_secret_leakage` |
| 13. 不泄露密钥 | `test_no_paid_api_and_no_secret_leakage` |
| 14. 本地 CI 命令可运行 | `tests/test_ci_quality_gate_script.py`（4 项） |

实测（本地）：

| 场景 | 结果 |
|------|------|
| 后端测试 | **565 passed / 0 failed**（536 → 565，+29） |
| 浏览器验收 | **82 passed / 0 failed**（80 → 82，+2：Quality Gate 面板 PASS/BLOCK 与 INCONCLUSIVE UI） |
| 前端构建 | 通过（tsc + Vite） |
| 本地 CI（`tracepilot-eval-v1`） | `gate_status=PASS`，退出码 0，JSON+Markdown 落盘 |
| 本地 CI（`tracepilot-fault-v1`，阈值 0.90） | `gate_status=BLOCK`，退出码 1，触发 `GATE_TASK_SUCCESS_BELOW_THRESHOLD` |
| 配置错误 | 退出码 3，输出「未知配置项」，不产出通过报告 |
| 付费 API | 0 次 |
| GitHub 云端 CI | **NOT_VERIFIED**（无远程仓库，未实际运行） |

## 11. 限制与后续

- **云端 CI 未验证**：需要在配置 GitHub 远程仓库后运行一次工作流并人工确认；
- 门禁依赖内存中的评测运行记录（进程重启后报告中的 run ID 不可再查询，
  但已落盘的 JSON/Markdown 报告仍保留结论与证据引用）；
- 阈值是演示配置，需按项目历史分布/业务损失重新标定（见 TECH_DEBT P2-33）；
- 质量门禁只覆盖已知规则；未覆盖的退化类型不会被发现（需要持续补充规则）；
- WARN 默认放行是显式配置（`warn_exit_code: 0`），严格发布场景应改为 1；
- 本地 CI 使用确定性 Mock 执行器，结论不能替代真实模型评测（真实 API 调用仍
  受双重授权限制，CI 中永不启用）。
