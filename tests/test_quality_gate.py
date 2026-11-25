"""第 51 步：CI/CD 质量门禁引擎测试。

覆盖：
- 四种门禁状态（PASS / WARN / BLOCK / INCONCLUSIVE）与退出码语义
- 数据缺失、版本不可比均不会自动放行
- 新增安全失败 / 案例回归 / 关键指标阈值 / 数据完整性 / 版本要求检查
- 阈值配置生效、配置错误识别
- JSON + Markdown 报告正确性
- 不调用付费 API、不泄露密钥
- API 契约

所有运行均由受控确定性执行器或测试注入的合成报告产生（零成本、无真实模型调用）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient  # noqa: E402

import eval_fixtures as fx  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.services import quality_gate as qg  # noqa: E402
from backend.app.services.evaluation_store import get_evaluation_store  # noqa: E402
from backend.app.services.evaluation_version_comparison import compare_runs  # noqa: E402

client = TestClient(app)


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

def _metrics(
    *,
    task: tuple[int, int] = (8, 8),
    tool: tuple[int, int] = (8, 8),
    safety: tuple[int, int] = (8, 8),
    recovery: tuple[int, int] = (1, 1),
    duration: float = 1.0,
    tokens: float = 0.0,
) -> dict:
    return fx.make_metrics(
        task_success=task,
        tool_correctness=tool,
        safety=safety,
        recovery=recovery,
        average_duration_ms=duration,
        total_tokens=tokens,
    )


def _inject(
    *,
    statuses: tuple[str, ...] = ("PASS",) * 8,
    metrics: dict | None = None,
    dataset_id: str = "tracepilot-fault-v1",
    dataset_version: str = "1.0.0",
    not_executed: int = 0,
    execution_errors: int = 0,
    case_ids: list[str] | None = None,
    judge_mode: str = "none",
    rubric_id: str | None = None,
) -> str:
    ids = case_ids or [f"case-{i + 1:02d}" for i in range(len(statuses))]
    cases = [fx.make_case(cid, status) for cid, status in zip(ids, statuses)]
    run_id = fx.new_run_id()
    report = fx.build_report(
        run_id,
        cases,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        metrics=metrics or _metrics(),
        not_executed=not_executed,
        execution_errors=execution_errors,
    )
    fx.inject_run(report, judge_mode=judge_mode, rubric_id=rubric_id)
    return run_id


def _pair(**kwargs) -> tuple[str, str]:
    """注入一对可控运行；base_*/cand_* 前缀参数分别作用于两侧。"""
    base_kwargs = {k[5:]: v for k, v in kwargs.items() if k.startswith("base_")}
    cand_kwargs = {k[5:]: v for k, v in kwargs.items() if k.startswith("cand_")}
    return _inject(**base_kwargs), _inject(**cand_kwargs)


def _write_config(tmp_path: Path, mapping: dict, name: str = "quality_gate.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(mapping, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _rule_ids(report: dict) -> list[str]:
    return [r["rule_id"] for r in report["triggered_rules"]]


def _check(report: dict, check_id: str) -> dict:
    return next(c for c in report["checks"] if c["check_id"] == check_id)


# ---------------------------------------------------------------------------
# 1-4：四种状态与退出码
# ---------------------------------------------------------------------------

def test_gate_pass_exit_zero():
    base, cand = _pair()
    report = qg.evaluate_gate(baseline_run_id=base, candidate_run_id=cand)
    assert report["gate_status"] == "PASS"
    assert report["exit_code"] == 0
    assert report["gate_reason"] == "所有质量门禁检查通过"
    assert report["triggered_rules"] == []
    assert report["paid_api_called"] is False
    assert report["config"]["label"] == qg.DEFAULTS_LABEL
    assert report["config"]["values"]["task_success_threshold"] == 0.9
    # 11 项确定性检查全部有结论（PASS / NOT_APPLICABLE）
    assert len(report["checks"]) == 11
    assert {c["status"] for c in report["checks"]} <= {"PASS", "NOT_APPLICABLE"}
    assert _check(report, "task_success")["actual"]["candidate_value"] == 1.0
    assert _check(report, "safety_failures")["actual"]["new_safety_failures"] == 0
    # 证据引用覆盖对比报告与回归报告
    kinds = {ref["kind"] for ref in report["evidence_refs"]}
    assert {"comparison_report", "regression_report", "evaluation_run"} <= kinds


def test_gate_warn_default_exit_zero_and_configurable():
    base, cand = _pair(
        base_metrics=_metrics(duration=100.0),
        cand_metrics=_metrics(duration=200.0),
    )
    report = qg.evaluate_gate(baseline_run_id=base, candidate_run_id=cand)
    assert report["gate_status"] == "WARN"
    assert report["exit_code"] == 0
    assert "GATE_LATENCY_INCREASE" in _rule_ids(report)
    latency = _check(report, "latency_increase")
    assert latency["status"] == "WARN"
    assert latency["actual"]["increase_ratio"] == 1.0
    # 非阻断回归规则只作为 advisory，不作为门禁状态来源
    assert "LATENCY_INCREASE" in {
        r["rule_id"] for r in report["advisory_regression_rules"]
    }

    strict = qg.evaluate_gate(
        baseline_run_id=base,
        candidate_run_id=cand,
        config_overrides={"warn_exit_code": 2},
    )
    assert strict["gate_status"] == "WARN"
    assert strict["exit_code"] == 2


def test_gate_block_nonzero_exit():
    base, cand = _pair(base_metrics=_metrics(task=(4, 8)), cand_metrics=_metrics(task=(4, 8)))
    report = qg.evaluate_gate(baseline_run_id=base, candidate_run_id=cand)
    assert report["gate_status"] == "BLOCK"
    assert report["exit_code"] == 1
    assert "GATE_TASK_SUCCESS_BELOW_THRESHOLD" in _rule_ids(report)
    task = _check(report, "task_success")
    assert task["status"] == "BLOCK"
    assert task["actual"]["candidate_value"] == 0.5
    assert task["threshold"]["unit"] == "ratio"


def test_gate_inconclusive_nonzero_exit():
    base = _inject(dataset_id="tracepilot-fault-v1")
    cand = _inject(dataset_id="tracepilot-eval-v1")
    report = qg.evaluate_gate(baseline_run_id=base, candidate_run_id=cand)
    assert report["gate_status"] == "INCONCLUSIVE"
    assert report["exit_code"] == 2
    assert "GATE_NOT_COMPARABLE" in _rule_ids(report)
    comparison = _check(report, "comparability")
    assert comparison["status"] == "INCONCLUSIVE"
    assert comparison["actual"]["verdict"] == "NOT_COMPARABLE"
    assert "绝不等于 PASS" in " ".join(report["limitations"])


# ---------------------------------------------------------------------------
# 5-8：缺失证据 / 安全 / 案例 / 阈值
# ---------------------------------------------------------------------------

def test_missing_data_never_auto_pass():
    base, cand = _pair(cand_not_executed=2)
    report = qg.evaluate_gate(baseline_run_id=base, candidate_run_id=cand)
    assert report["gate_status"] == "BLOCK"
    assert report["exit_code"] == 1
    assert "GATE_DATA_INTEGRITY" in _rule_ids(report)
    integrity = _check(report, "data_integrity")
    assert integrity["missing_evidence"] is True
    assert integrity["actual"]["not_executed_cases"] == 2

    lenient = qg.evaluate_gate(
        baseline_run_id=base,
        candidate_run_id=cand,
        config_overrides={"fail_on_missing_evidence": False},
    )
    assert lenient["gate_status"] == "WARN"
    assert lenient["exit_code"] == 0
    assert lenient["gate_status"] != "PASS"


def test_missing_candidate_report_inconclusive_not_pass():
    base, cand = _pair()
    comparison = compare_runs(base, cand)
    store = get_evaluation_store()
    with store._lock:  # noqa: SLF001 - 模拟进程重启后运行记录丢失
        store._runs.pop(cand, None)
    report = qg.evaluate_gate(comparison_id=comparison["comparison_id"])
    assert report["candidate_version"]["evaluation_run_id"] == cand
    assert _check(report, "data_integrity")["status"] == "INCONCLUSIVE"
    assert report["gate_status"] == "INCONCLUSIVE"
    assert report["exit_code"] == 2


def test_new_safety_failure_blocks():
    base, cand = _pair(
        base_metrics=_metrics(safety=(8, 8)),
        cand_metrics=_metrics(safety=(7, 8)),
    )
    report = qg.evaluate_gate(baseline_run_id=base, candidate_run_id=cand)
    assert report["gate_status"] == "BLOCK"
    assert report["exit_code"] == 1
    assert "GATE_NEW_SAFETY_FAILURE" in _rule_ids(report)
    safety = _check(report, "safety_failures")
    assert safety["actual"]["new_safety_failures"] == 1
    assert safety["threshold"]["value"] == 0
    assert safety["threshold"]["unit"] == "count"
    # 回归检测的阻断规则同样命中（双证据）
    assert "GATE_REGRESSION_BLOCK" in _rule_ids(report)


def test_case_regression_blocks():
    base, cand = _pair(
        base_statuses=("PASS",) * 8,
        cand_statuses=("FAIL",) + ("PASS",) * 7,
        base_metrics=_metrics(task=(8, 8)),
        cand_metrics=_metrics(task=(7, 8)),
    )
    report = qg.evaluate_gate(
        baseline_run_id=base,
        candidate_run_id=cand,
        config_overrides={"task_success_threshold": 0.5, "tool_correctness_threshold": 0.5},
    )
    assert report["gate_status"] == "BLOCK"
    assert "GATE_CASE_REGRESSION_EXCEEDED" in _rule_ids(report)
    cases = _check(report, "case_regressions")
    assert cases["actual"]["regressed_cases"] == 1
    assert cases["actual"]["case_ids"] == ["case-01"]
    case_refs = [r for r in report["evidence_refs"] if r["kind"] == "case"]
    assert any(r["ref_id"] == "case-01" for r in case_refs)


def test_threshold_configuration_takes_effect():
    base, cand = _pair(
        base_metrics=_metrics(task=(4, 8)),
        cand_metrics=_metrics(task=(4, 8)),
    )
    strict = qg.evaluate_gate(baseline_run_id=base, candidate_run_id=cand)
    assert strict["gate_status"] == "BLOCK"

    relaxed = qg.evaluate_gate(
        baseline_run_id=base,
        candidate_run_id=cand,
        config_overrides={"task_success_threshold": 0.4},
    )
    assert relaxed["gate_status"] == "PASS"
    assert _check(relaxed, "task_success")["status"] == "PASS"
    assert relaxed["config"]["values"]["task_success_threshold"] == 0.4
    # 配置指纹随配置内容变化（可版本管理/可追溯）
    assert relaxed["config"]["fingerprint"] != strict["config"]["fingerprint"]


def test_required_dataset_version_block():
    base, cand = _pair()
    report = qg.evaluate_gate(
        baseline_run_id=base,
        candidate_run_id=cand,
        config_overrides={"required_dataset_version": "9.9.9"},
    )
    assert report["gate_status"] == "BLOCK"
    assert "GATE_DATASET_VERSION_MISMATCH" in _rule_ids(report)
    check = _check(report, "required_dataset_version")
    assert check["actual"] == {"required": "9.9.9", "candidate": "1.0.0"}


# ---------------------------------------------------------------------------
# 9：配置错误识别
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_config, expected",
    [
        ({"task_success_threshold": 1.5}, "0..1"),
        ({"task_success_threshold": "high"}, "数值"),
        ({"safety_failure_limit": -1}, ">= 0"),
        ({"fail_on_missing_evidence": "yes"}, "布尔"),
        ({"unknown_option": 1}, "未知配置项"),
        ({"warn_exit_code": 999}, "0..125"),
        ({"required_dataset_version": 1}, "字符串或 null"),
    ],
)
def test_config_errors_are_identified(bad_config, expected):
    with pytest.raises(qg.QualityGateConfigError) as excinfo:
        qg.load_gate_config(overrides=bad_config)
    assert expected in str(excinfo.value)


def test_config_file_errors_are_identified(tmp_path):
    with pytest.raises(qg.QualityGateConfigError) as missing:
        qg.load_gate_config(tmp_path / "no-such.yaml")
    assert "不存在" in str(missing.value)

    with pytest.raises(qg.QualityGateConfigError) as broken:
        qg.load_gate_config(_write_config(tmp_path, {"task_success_threshold": 1.5}))
    assert "0..1" in str(broken.value)

    non_mapping = tmp_path / "list.yaml"
    non_mapping.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(qg.QualityGateConfigError):
        qg.load_gate_config(non_mapping)


def test_valid_config_file_loads_and_fingerprints(tmp_path):
    config = dict(qg.DEFAULT_CONFIG)
    config["task_success_threshold"] = 0.8
    path = _write_config(tmp_path, config)
    loaded = qg.load_gate_config(path)
    assert loaded["config_source"] == "file"
    assert loaded["config"]["task_success_threshold"] == 0.8
    assert loaded["config_fingerprint"].startswith("qg-cfg-")
    assert loaded["config_path"].endswith("quality_gate.yaml")


def test_repository_config_file_is_valid():
    loaded = qg.load_gate_config()
    assert loaded["config_source"] == "file"
    assert loaded["config"]["label"] == qg.DEFAULTS_LABEL
    assert loaded["config"]["fail_on_missing_evidence"] is True
    assert loaded["config"]["required_dataset_version"] == "1.0.0"


# ---------------------------------------------------------------------------
# 10-11：JSON / Markdown 报告
# ---------------------------------------------------------------------------

def test_json_report_persisted_and_listed():
    base, cand = _pair()
    report = qg.evaluate_gate(
        baseline_run_id=base, candidate_run_id=cand, ci_run_id="ci-test-001"
    )
    required_keys = {
        "gate_report_id", "gate_status", "exit_code", "ci_run_id", "git_commit",
        "dataset_version", "evaluator_version", "baseline_version", "candidate_version",
        "comparison_id", "regression_report_id", "checks", "triggered_rules",
        "evidence_refs", "limitations", "config", "paid_api_called", "report_files",
    }
    assert required_keys <= set(report)
    assert report["gate_report_id"].startswith("qg-")
    assert report["ci_run_id"] == "ci-test-001"
    assert report["git_commit"] and report["git_commit"] != "unknown"

    json_path = PROJECT_ROOT / report["report_files"]["json"]
    md_path = PROJECT_ROOT / report["report_files"]["markdown"]
    assert json_path.exists() and md_path.exists()
    stored = json.loads(json_path.read_text(encoding="utf-8"))
    assert stored["gate_report_id"] == report["gate_report_id"]
    assert stored["gate_status"] == "PASS"
    json.dumps(stored)  # 必须可被 JSON 序列化

    loaded = qg.get_gate_report(report["gate_report_id"])
    assert loaded["gate_report_id"] == report["gate_report_id"]
    items = qg.list_gate_reports(limit=50)
    assert report["gate_report_id"] in {i["gate_report_id"] for i in items}
    with pytest.raises(qg.QualityGateError):
        qg.get_gate_report("qg-not-exist")
    with pytest.raises(qg.QualityGateError):
        qg.get_gate_report("../../etc/passwd")


def test_markdown_report_contains_required_sections():
    base, cand = _pair(
        base_metrics=_metrics(task=(4, 8)),
        cand_metrics=_metrics(task=(4, 8)),
    )
    report = qg.evaluate_gate(baseline_run_id=base, candidate_run_id=cand)
    markdown = qg.render_markdown(report)
    assert "# TracePilot Quality Gate Report" in markdown
    assert "`BLOCK`" in markdown
    assert "exit code `1`" in markdown
    assert "- CI Run ID:" in markdown
    assert "- Git Commit:" in markdown
    assert "- Dataset Version:" in markdown
    assert "- Evaluator Version:" in markdown
    assert "- Baseline Version:" in markdown
    assert "- Candidate Version:" in markdown
    assert "## Triggered Rules" in markdown
    assert "GATE_TASK_SUCCESS_BELOW_THRESHOLD" in markdown
    assert "## All Checks" in markdown
    assert "## Evidence References" in markdown
    assert "## Limitations" in markdown
    assert "INCONCLUSIVE" in markdown  # 安全性声明


# ---------------------------------------------------------------------------
# 12-13：付费 API / 密钥
# ---------------------------------------------------------------------------

def test_no_paid_api_and_no_secret_leakage(monkeypatch):
    fake_key = "sk-ci-test-secret-should-not-appear-0123456789"
    monkeypatch.setenv("DEEPSEEK_API_KEY", fake_key)
    monkeypatch.setenv("TRACEPILOT_ALLOW_REAL_API", "0")

    base, cand = _pair()
    report = qg.evaluate_gate(
        baseline_run_id=base,
        candidate_run_id=cand,
        ci_run_id="ci-secret-check",
    )
    payload = json.dumps(report, ensure_ascii=False)
    markdown = qg.render_markdown(report)
    assert fake_key not in payload
    assert fake_key not in markdown
    assert report["paid_api_called"] is False
    assert report["baseline_version"]["judge_mode"] == "none"
    assert report["candidate_version"]["judge_mode"] == "none"
    # 报告中不应出现常见密钥字段名
    for token in ("DEEPSEEK_API_KEY", "sk-", "Authorization", "Bearer "):
        assert token not in payload


# ---------------------------------------------------------------------------
# API 契约
# ---------------------------------------------------------------------------

def test_api_gate_config_endpoint():
    response = client.get("/api/quality-gate/config")
    assert response.status_code == 200
    body = response.json()
    assert body["config"]["task_success_threshold"] == 0.9
    assert body["config"]["required_dataset_version"] == "1.0.0"
    assert body["label"] == qg.DEFAULTS_LABEL
    assert body["defaults_label"] == qg.DEFAULTS_LABEL
    assert body["exit_code_semantics"] == {
        "PASS": 0, "WARN": 0, "BLOCK": 1, "INCONCLUSIVE": 2,
    }
    assert "INCONCLUSIVE 绝不等于 PASS" in body["note"]
    assert body["gate_statuses"] == ["PASS", "WARN", "BLOCK", "INCONCLUSIVE"]


def test_api_gate_flow_and_reports_endpoints():
    base, cand = _pair()
    created = client.post(
        "/api/quality-gate/runs",
        json={"baseline_run_id": base, "candidate_run_id": cand, "ci_run_id": "api-ci-001"},
    )
    assert created.status_code == 200, created.text
    report = created.json()
    gate_id = report["gate_report_id"]
    assert report["gate_status"] == "PASS"
    assert report["exit_code"] == 0

    one = client.get(f"/api/quality-gate/reports/{gate_id}")
    assert one.status_code == 200
    assert one.json()["report"]["gate_report_id"] == gate_id

    checks = client.get(f"/api/quality-gate/reports/{gate_id}/checks")
    assert checks.status_code == 200
    assert len(checks.json()["checks"]) == 11
    assert checks.json()["gate_status"] == "PASS"

    markdown = client.get(f"/api/quality-gate/reports/{gate_id}/markdown")
    assert markdown.status_code == 200
    assert "# TracePilot Quality Gate Report" in markdown.json()["markdown"]

    listing = client.get("/api/quality-gate/reports", params={"limit": 50})
    assert listing.status_code == 200
    assert gate_id in {r["gate_report_id"] for r in listing.json()["reports"]}

    # 错误路径
    assert client.post("/api/quality-gate/runs", json={}).status_code == 400
    assert client.post(
        "/api/quality-gate/runs", json={"comparison_id": "cmp-not-exist"}
    ).status_code == 400
    assert client.get("/api/quality-gate/reports/qg-not-exist").status_code == 404
