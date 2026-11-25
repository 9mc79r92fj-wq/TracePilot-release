"""第 51 步：本地 CI 命令（scripts/ci_quality_gate.py）端到端测试。

覆盖：
- 本地 CI 命令可以运行（确定性 Mock 评测 → 版本对比 → 回归检测 → 门禁）
- PASS → 退出码 0
- BLOCK → 非零退出码（1）
- 配置错误 → 退出码 3（禁止异常默认放行）
- JSON / Markdown 报告落盘
- 不调用付费 API（命令输出与报告均为本地确定性执行）

说明：这是**本地** CI 验证；GitHub 云端工作流未实际运行，状态为 NOT_VERIFIED。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "ci_quality_gate.py"
PYTHON = sys.executable


def _run_cli(args: list[str], *, timeout: float = 300.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(SCRIPT), *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _write_config(tmp_path: Path, *, task_threshold: float = 0.9, extra: dict | None = None) -> Path:
    config = {
        "version": 1,
        "label": "PROJECT_DEMO_DEFAULT_NOT_INDUSTRY_STANDARD",
        "warn_exit_code": 0,
        "fail_on_missing_evidence": True,
        "required_dataset_version": "1.0.0",
        "required_rubric_version": None,
        "task_success_threshold": task_threshold,
        "tool_correctness_threshold": 0.9,
        "safety_failure_limit": 0,
        "max_case_regressions": 0,
        "max_latency_increase": 0.25,
        "max_cost_increase": 0.30,
        "latency_min_baseline_ms": 20.0,
        "cost_min_baseline_tokens": 100.0,
    }
    if extra:
        config.update(extra)
    path = tmp_path / "quality_gate.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_local_ci_command_passes_exit_zero(tmp_path):
    report_dir = tmp_path / "reports-pass"
    result = _run_cli(
        [
            "--max-cases", "3",
            "--report-dir", str(report_dir),
            "--ci-run-id", "ci-local-pass",
            "--json-only",
        ]
    )
    assert result.returncode == 0, result.stderr
    assert "gate_status=PASS" in result.stdout
    assert "exit_code=0" in result.stdout
    # 报告落盘（JSON + Markdown）
    json_path = report_dir / "quality-gate-report.json"
    md_path = report_dir / "quality-gate-report.md"
    assert json_path.exists() and md_path.exists()
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["gate_status"] == "PASS"
    assert report["exit_code"] == 0
    assert report["ci_run_id"] == "ci-local-pass"
    assert report["paid_api_called"] is False
    assert report["candidate_version"]["evaluation_run_id"].startswith("eval-")
    markdown = md_path.read_text(encoding="utf-8")
    assert "Gate Status: `PASS`" in markdown
    # 没有密钥泄露
    assert "DEEPSEEK_API_KEY" not in result.stdout + result.stderr


def test_local_ci_command_blocks_on_fault_dataset(tmp_path):
    config_path = _write_config(tmp_path, task_threshold=0.9)
    report_dir = tmp_path / "reports-block"
    result = _run_cli(
        [
            "--dataset", "tracepilot-fault-v1",
            "--config", str(config_path),
            "--report-dir", str(report_dir),
            "--ci-run-id", "ci-local-block",
            "--json-only",
        ]
    )
    assert result.returncode == 1, (result.returncode, result.stderr)
    assert "gate_status=BLOCK" in result.stdout
    report = json.loads((report_dir / "quality-gate-report.json").read_text(encoding="utf-8"))
    assert report["gate_status"] == "BLOCK"
    assert report["exit_code"] == 1
    triggered = [r["rule_id"] for r in report["triggered_rules"]]
    assert "GATE_TASK_SUCCESS_BELOW_THRESHOLD" in triggered
    assert "BLOCK：质量门禁未通过" in result.stderr


def test_local_ci_command_config_error_exit_three(tmp_path):
    bad_config = _write_config(tmp_path, extra={"unknown_option": True})
    result = _run_cli(
        [
            "--config", str(bad_config),
            "--report-dir", str(tmp_path / "reports-bad"),
            "--json-only",
        ]
    )
    assert result.returncode == 3, (result.returncode, result.stderr)
    assert "未知配置项" in result.stderr
    assert "quality-gate-report.json" not in result.stdout
    # 配置错误不得产出"通过"报告
    assert not (tmp_path / "reports-bad" / "quality-gate-report.json").exists()


def test_local_ci_command_rejects_unknown_dataset(tmp_path):
    result = _run_cli(
        [
            "--dataset", "not-allowed-dataset",
            "--report-dir", str(tmp_path / "reports-dataset"),
            "--json-only",
        ]
    )
    assert result.returncode == 3
    assert "不支持的评测集" in result.stderr
