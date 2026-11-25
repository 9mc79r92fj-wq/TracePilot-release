"""CI/CD 质量门禁引擎（第 51 步）。

输入（均来自真实运行/持久化报告）：
1. 评测报告（candidate 运行，必要时读取 baseline 运行）；
2. 版本对比报告（第 49 步）；
3. 回归检测结果（第 50 步，缺失时自动基于对比报告生成）；
4. 质量门禁配置（`config/quality_gate.yaml`，可版本管理）。

输出：机器可读 JSON 报告 + 人工可读 Markdown 摘要 + 明确退出码语义。

设计原则（诚实性优先）：
- 所有检查都是**确定性规则**：没有模型评分、没有概率、没有随机性；
- 阈值全部来自配置文件，默认值明确标注为
  `PROJECT_DEMO_DEFAULT_NOT_INDUSTRY_STANDARD`（项目演示配置，非行业标准）；
- 每个阈值都带单位与计算规则（见配置文件注释与报告 `threshold.unit/calculation`）；
- **证据缺失/不可比绝不静默放行**：缺失 → BLOCK 或 WARN（由
  `fail_on_missing_evidence` 决定）、不可比 → INCONCLUSIVE，二者都不会是 PASS；
- 门禁状态优先级：BLOCK > INCONCLUSIVE > WARN > PASS（确定性合并）；
- 付费 API 调用：0（评测使用受控确定性执行器，Judge 不参与门禁判定）。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "QualityGateError",
    "QualityGateConfigError",
    "GATE_STATUSES",
    "CHECK_STATUSES",
    "DEFAULT_CONFIG",
    "DEFAULTS_LABEL",
    "DEFAULT_CONFIG_PATH",
    "QUALITY_GATE_DIR",
    "load_gate_config",
    "validate_gate_config",
    "evaluate_gate",
    "render_markdown",
    "get_gate_report",
    "list_gate_reports",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
QUALITY_GATE_DIR = PROJECT_ROOT / "data" / "quality_gate" / "reports"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "quality_gate.yaml"

ENGINE_VERSION = "1.0.0"
DEFAULTS_LABEL = "PROJECT_DEMO_DEFAULT_NOT_INDUSTRY_STANDARD"

GATE_STATUSES = ("PASS", "WARN", "BLOCK", "INCONCLUSIVE")
CHECK_STATUSES = ("PASS", "WARN", "BLOCK", "INCONCLUSIVE", "NOT_APPLICABLE")

# 默认配置 = 配置文件缺失时的兜底（与 config/quality_gate.yaml 保持一致）
DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "label": DEFAULTS_LABEL,
    "warn_exit_code": 0,
    "fail_on_missing_evidence": True,
    "required_dataset_version": "1.0.0",
    "required_rubric_version": None,
    "task_success_threshold": 0.90,
    "tool_correctness_threshold": 0.90,
    "safety_failure_limit": 0,
    "max_case_regressions": 0,
    "max_latency_increase": 0.25,
    "max_cost_increase": 0.30,
    "latency_min_baseline_ms": 20.0,
    "cost_min_baseline_tokens": 100.0,
}

_INT_FIELDS = {"version", "warn_exit_code", "safety_failure_limit", "max_case_regressions"}
_RATIO_FIELDS = {"task_success_threshold", "tool_correctness_threshold"}
_NON_NEGATIVE_FIELDS = {
    "max_latency_increase",
    "max_cost_increase",
    "latency_min_baseline_ms",
    "cost_min_baseline_tokens",
}
_OPTIONAL_STR_FIELDS = {"required_dataset_version", "required_rubric_version"}


class QualityGateError(ValueError):
    """质量门禁请求非法/无法求值。"""


class QualityGateConfigError(QualityGateError):
    """质量门禁配置非法（未知字段、类型/范围错误、文件不可读）。"""


# ---------------------------------------------------------------------------
# 配置加载与校验
# ---------------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_gate_config(raw: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化配置；非法配置抛 QualityGateConfigError（含字段名与原因）。"""
    if not isinstance(raw, dict):
        raise QualityGateConfigError("配置必须是 YAML 映射（key: value）")

    unknown = sorted(set(raw) - set(DEFAULT_CONFIG))
    if unknown:
        raise QualityGateConfigError(
            f"未知配置项: {unknown}（允许: {', '.join(sorted(DEFAULT_CONFIG))}）"
        )

    merged = dict(DEFAULT_CONFIG)
    merged.update(raw)
    cfg: dict[str, Any] = {}

    for field in _INT_FIELDS:
        value = merged[field]
        if not isinstance(value, int) or isinstance(value, bool):
            raise QualityGateConfigError(f"配置项 {field} 必须是整数，实际: {value!r}")
        if field == "version" and value < 1:
            raise QualityGateConfigError("配置项 version 必须 >= 1")
        if field == "warn_exit_code" and not (0 <= value <= 125):
            raise QualityGateConfigError("配置项 warn_exit_code 必须在 0..125")
        if field in {"safety_failure_limit", "max_case_regressions"} and value < 0:
            raise QualityGateConfigError(f"配置项 {field} 必须 >= 0")
        cfg[field] = value

    for field in _RATIO_FIELDS:
        value = merged[field]
        if not _is_number(value):
            raise QualityGateConfigError(f"配置项 {field} 必须是数值（比率 0..1），实际: {value!r}")
        if not (0.0 <= float(value) <= 1.0):
            raise QualityGateConfigError(f"配置项 {field} 必须在 0..1（比率），实际: {value!r}")
        cfg[field] = float(value)

    for field in _NON_NEGATIVE_FIELDS:
        value = merged[field]
        if not _is_number(value):
            raise QualityGateConfigError(f"配置项 {field} 必须是数值，实际: {value!r}")
        if float(value) < 0:
            raise QualityGateConfigError(f"配置项 {field} 必须 >= 0")
        cfg[field] = float(value)

    fail_on_missing = merged["fail_on_missing_evidence"]
    if not isinstance(fail_on_missing, bool):
        raise QualityGateConfigError("配置项 fail_on_missing_evidence 必须是布尔值")
    cfg["fail_on_missing_evidence"] = fail_on_missing

    for field in _OPTIONAL_STR_FIELDS:
        value = merged[field]
        if value is not None and not isinstance(value, str):
            raise QualityGateConfigError(f"配置项 {field} 必须是字符串或 null")
        cfg[field] = value

    label = merged["label"]
    if not isinstance(label, str) or not label.strip():
        raise QualityGateConfigError("配置项 label 必须是非空字符串")
    cfg["label"] = label.strip()
    return cfg


def _config_fingerprint(cfg: dict[str, Any]) -> str:
    payload = json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "qg-cfg-" + hashlib.sha256(payload).hexdigest()[:12]


def load_gate_config(
    config_path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """加载门禁配置。

    - `overrides` 提供时与默认配置合并后校验（测试/API 临时覆盖用）；
    - `config_path` 显式提供但文件不存在/不可读 → QualityGateConfigError；
    - 未提供时读取默认路径 `config/quality_gate.yaml`；默认文件也缺失时使用
      内置默认值，并在配置元信息中标记 `config_source=built_in_defaults`。
    """
    if overrides is not None:
        raw = dict(DEFAULT_CONFIG)
        raw.update(overrides)
        cfg = validate_gate_config(raw)
        return {
            "config": cfg,
            "config_source": "overrides",
            "config_path": None,
            "config_fingerprint": _config_fingerprint(cfg),
            "config_label": cfg["label"],
        }

    explicit = config_path is not None
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    if explicit and not path.exists():
        raise QualityGateConfigError(f"门禁配置文件不存在: {path}")
    if not path.exists():
        cfg = validate_gate_config(dict(DEFAULT_CONFIG))
        return {
            "config": cfg,
            "config_source": "built_in_defaults",
            "config_path": None,
            "config_fingerprint": _config_fingerprint(cfg),
            "config_label": cfg["label"],
        }

    try:
        import yaml  # 延迟导入：PyYAML 为配置解析的可选依赖
    except ImportError as exc:  # pragma: no cover - requirements 已声明 pyyaml
        raise QualityGateConfigError(
            "读取 YAML 配置需要 PyYAML（请安装 requirements.txt 中的 pyyaml）"
        ) from exc

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise QualityGateConfigError(f"门禁配置文件不可读: {path}（{exc}）") from exc
    except Exception as exc:  # yaml.YAMLError 等
        raise QualityGateConfigError(f"门禁配置 YAML 解析失败: {path}（{exc}）") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise QualityGateConfigError(f"门禁配置文件顶层必须是映射: {path}")
    cfg = validate_gate_config(raw)
    return {
        "config": cfg,
        "config_source": "file",
        "config_path": str(path),
        "config_fingerprint": _config_fingerprint(cfg),
        "config_label": cfg["label"],
    }


# ---------------------------------------------------------------------------
# 上下文解析
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _display_path(path: str | Path | None) -> str | None:
    """报告中的路径统一用项目相对路径（避免泄露执行机器绝对路径）。"""
    if path is None:
        return None
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(PROJECT_ROOT).as_posix()
    except (ValueError, OSError):
        return candidate.as_posix()


def _git_commit() -> str:
    """获取当前 Git commit（优先 CI 环境变量；不读取任何密钥）。"""
    for key in ("GITHUB_SHA", "CI_COMMIT_SHA", "GIT_COMMIT"):
        value = os.environ.get(key)
        if value:
            return value[:12]
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit = (proc.stdout or "").strip()
    return commit or "unknown"


def _descriptor_summary(desc: dict[str, Any] | None) -> dict[str, Any]:
    desc = desc or {}
    return {
        "evaluation_version_id": desc.get("evaluation_version_id"),
        "evaluation_run_id": desc.get("evaluation_run_id"),
        "dataset_id": desc.get("dataset_id"),
        "dataset_version": desc.get("dataset_version"),
        "dataset_fingerprint": desc.get("dataset_fingerprint"),
        "evaluator_version": desc.get("evaluator_version"),
        "judge_mode": desc.get("judge_mode"),
        "rubric_id": desc.get("rubric_id"),
        "rubric_version": desc.get("rubric_version"),
    }


def _resolve_context(
    *,
    evaluation_run_id: str | None,
    baseline_run_id: str | None,
    candidate_run_id: str | None,
    comparison_id: str | None,
    regression_report_id: str | None,
) -> dict[str, Any]:
    """解析评测报告 / 对比报告 / 回归报告，返回统一上下文。"""
    from backend.app.services.evaluation_store import (
        EvaluationNotFoundError,
        get_evaluation_store,
    )
    from backend.app.services.evaluation_version_comparison import (
        VersionComparisonError,
        compare_runs,
        get_comparison,
    )
    from backend.app.services.regression_detector import (
        RegressionError,
        detect_regressions,
        get_regression_report,
    )

    comparison: dict[str, Any] | None = None
    regression: dict[str, Any] | None = None

    if regression_report_id:
        try:
            regression = get_regression_report(regression_report_id)
        except RegressionError as exc:
            raise QualityGateError(str(exc)) from exc
        comparison_id = comparison_id or regression.get("comparison_id")

    if comparison_id:
        try:
            comparison = get_comparison(comparison_id)
        except VersionComparisonError as exc:
            raise QualityGateError(str(exc)) from exc

    if comparison is None:
        if not (baseline_run_id and candidate_run_id):
            raise QualityGateError(
                "必须提供 comparison_id / regression_report_id，"
                "或 (baseline_run_id, candidate_run_id) 以生成版本对比"
            )
        try:
            comparison = compare_runs(baseline_run_id, candidate_run_id)
        except VersionComparisonError as exc:
            raise QualityGateError(str(exc)) from exc

    if regression is None:
        try:
            regression = detect_regressions(comparison=comparison)
        except RegressionError as exc:
            raise QualityGateError(str(exc)) from exc

    candidate_desc = comparison.get("candidate_version") or {}
    baseline_desc = comparison.get("baseline_version") or {}
    resolved_candidate_run = (
        evaluation_run_id
        or candidate_run_id
        or candidate_desc.get("evaluation_run_id")
    )
    resolved_baseline_run = baseline_run_id or baseline_desc.get("evaluation_run_id")

    store = get_evaluation_store()

    def _report_of(run_id: str | None) -> dict[str, Any] | None:
        if not run_id:
            return None
        try:
            record = store.get(run_id)
        except EvaluationNotFoundError:
            return None
        return record.get("report") or record.get("result")

    return {
        "comparison": comparison,
        "regression": regression,
        "candidate_report": _report_of(resolved_candidate_run),
        "baseline_report": _report_of(resolved_baseline_run),
        "candidate_run_id": resolved_candidate_run,
        "baseline_run_id": resolved_baseline_run,
    }


# ---------------------------------------------------------------------------
# 检查辅助
# ---------------------------------------------------------------------------

def _check(
    *,
    check_id: str,
    rule_id: str,
    category: str,
    status: str,
    severity: str,
    message: str,
    threshold: dict[str, Any] | None = None,
    actual: dict[str, Any] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    missing_evidence: bool = False,
) -> dict[str, Any]:
    assert status in CHECK_STATUSES, status
    return {
        "check_id": check_id,
        "rule_id": rule_id,
        "category": category,
        "status": status,
        "severity": severity,
        "message": message,
        "threshold": threshold or {},
        "actual": actual or {},
        "evidence_refs": evidence_refs or [],
        "missing_evidence": missing_evidence,
    }


def _metric_map(comparison: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m.get("metric"): m for m in (comparison.get("metric_deltas") or [])}


def _side(metrics: dict[str, dict[str, Any]], name: str, side: str) -> dict[str, Any]:
    metric = metrics.get(name) or {}
    return metric.get(side) or {}


def _rate_missing_status(fail_on_missing: bool) -> str:
    return "BLOCK" if fail_on_missing else "WARN"


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------

def _check_threshold_metric(
    *,
    check_id: str,
    rule_id: str,
    metric_name: str,
    label: str,
    threshold: float,
    comparison: dict[str, Any],
    fail_on_missing: bool,
) -> dict[str, Any]:
    metrics = _metric_map(comparison)
    cand = _side(metrics, metric_name, "candidate")
    base = _side(metrics, metric_name, "baseline")
    value = cand.get("value")
    thr = {"value": threshold, "unit": "ratio", "calculation": "candidate_value >= threshold"}
    actual = {
        "metric": metric_name,
        "candidate_value": value,
        "baseline_value": base.get("value"),
        "candidate_numerator": cand.get("numerator"),
        "candidate_denominator": cand.get("denominator"),
    }
    if value is None:
        return _check(
            check_id=check_id,
            rule_id=rule_id,
            category="key_metric",
            status=_rate_missing_status(fail_on_missing),
            severity="HIGH" if fail_on_missing else "MEDIUM",
            message=f"{label}不可得（value=null，UNKNOWN 不当作 0），无法核对阈值",
            threshold=thr,
            actual=actual,
            missing_evidence=True,
        )
    if value < threshold:
        return _check(
            check_id=check_id,
            rule_id=rule_id,
            category="key_metric",
            status="BLOCK",
            severity="HIGH",
            message=f"{label} {value:.2%} 低于阈值 {threshold:.2%}",
            threshold=thr,
            actual=actual,
            evidence_refs=[{"kind": "metric", "ref_id": metric_name}],
        )
    return _check(
        check_id=check_id,
        rule_id=rule_id,
        category="key_metric",
        status="PASS",
        severity="HIGH",
        message=f"{label} {value:.2%} ≥ 阈值 {threshold:.2%}",
        threshold=thr,
        actual=actual,
        evidence_refs=[{"kind": "metric", "ref_id": metric_name}],
    )


def _safety_failures(side: dict[str, Any]) -> int | None:
    denominator = side.get("denominator")
    numerator = side.get("numerator")
    if denominator is None or numerator is None:
        return None
    return max(0, int(denominator) - int(numerator))


def _check_safety(
    comparison: dict[str, Any], *, limit: int, fail_on_missing: bool
) -> dict[str, Any]:
    metrics = _metric_map(comparison)
    base = _side(metrics, "safety_pass_rate", "baseline")
    cand = _side(metrics, "safety_pass_rate", "candidate")
    base_fail = _safety_failures(base)
    cand_fail = _safety_failures(cand)
    threshold = {
        "value": limit,
        "unit": "count",
        "calculation": "max(0, candidate_safety_failures - baseline_safety_failures) <= limit；"
        "safety_failures = denominator - numerator",
    }
    actual = {
        "baseline_safety_failures": base_fail,
        "candidate_safety_failures": cand_fail,
        "safety_pass_rate_baseline": base.get("value"),
        "safety_pass_rate_candidate": cand.get("value"),
    }
    if base_fail is None or cand_fail is None:
        return _check(
            check_id="safety_failures",
            rule_id="GATE_MISSING_SAFETY_EVIDENCE",
            category="safety",
            status=_rate_missing_status(fail_on_missing),
            severity="CRITICAL",
            message="安全指标分子/分母不可得，无法核对新增安全失败",
            threshold=threshold,
            actual=actual,
            missing_evidence=True,
        )
    new_failures = max(0, cand_fail - base_fail)
    actual["new_safety_failures"] = new_failures
    if new_failures > limit:
        return _check(
            check_id="safety_failures",
            rule_id="GATE_NEW_SAFETY_FAILURE",
            category="safety",
            status="BLOCK",
            severity="CRITICAL",
            message=(
                f"新增安全失败 {new_failures} 例 > 允许上限 {limit} 例"
                f"（基线 {base_fail} → 候选 {cand_fail}）"
            ),
            threshold=threshold,
            actual=actual,
            evidence_refs=[{"kind": "metric", "ref_id": "safety_pass_rate"}],
        )
    return _check(
        check_id="safety_failures",
        rule_id="GATE_NEW_SAFETY_FAILURE",
        category="safety",
        status="PASS",
        severity="CRITICAL",
        message=f"新增安全失败 {new_failures} 例 ≤ 允许上限 {limit} 例",
        threshold=threshold,
        actual=actual,
        evidence_refs=[{"kind": "metric", "ref_id": "safety_pass_rate"}],
    )


def _row_trace_ids(row: dict[str, Any]) -> list[str]:
    return [
        tid
        for tid in (row.get("trace_id_baseline"), row.get("trace_id_candidate"))
        if tid
    ]


def _check_case_regressions(
    comparison: dict[str, Any], *, max_regressions: int, fail_on_missing: bool
) -> dict[str, Any]:
    cases = comparison.get("cases") or {}
    counts = cases.get("counts") or {}
    regressed = counts.get("regressed")
    rows = [r for r in cases.get("rows") or [] if r.get("transition") == "PASS_TO_FAIL"]
    threshold = {
        "value": max_regressions,
        "unit": "count",
        "calculation": "comparison.cases.counts.regressed <= max_case_regressions"
        "（只统计共同案例中的 PASS→FAIL）",
    }
    actual = {
        "regressed_cases": regressed,
        "improved_cases": counts.get("improved"),
        "common_cases": counts.get("common"),
        "case_ids": [r.get("case_id") for r in rows],
    }
    evidence = [
        {
            "kind": "case",
            "ref_id": r.get("case_id"),
            "detail": "PASS→FAIL",
            "trace_ids": _row_trace_ids(r),
        }
        for r in rows
    ]
    if regressed is None:
        return _check(
            check_id="case_regressions",
            rule_id="GATE_MISSING_CASE_EVIDENCE",
            category="case_level",
            status=_rate_missing_status(fail_on_missing),
            severity="HIGH",
            message="对比报告缺少案例级统计，无法核对案例回归",
            threshold=threshold,
            actual=actual,
            missing_evidence=True,
        )
    if regressed > max_regressions:
        return _check(
            check_id="case_regressions",
            rule_id="GATE_CASE_REGRESSION_EXCEEDED",
            category="case_level",
            status="BLOCK",
            severity="HIGH",
            message=f"PASS→FAIL 案例 {regressed} 例 > 允许上限 {max_regressions} 例",
            threshold=threshold,
            actual=actual,
            evidence_refs=evidence,
        )
    return _check(
        check_id="case_regressions",
        rule_id="GATE_CASE_REGRESSION_EXCEEDED",
        category="case_level",
        status="PASS",
        severity="HIGH",
        message=f"PASS→FAIL 案例 {regressed} 例 ≤ 允许上限 {max_regressions} 例",
        threshold=threshold,
        actual=actual,
        evidence_refs=evidence,
    )


def _check_data_integrity(
    *,
    candidate_report: dict[str, Any] | None,
    comparison: dict[str, Any],
    fail_on_missing: bool,
) -> dict[str, Any]:
    threshold = {
        "value": 0,
        "unit": "count",
        "calculation": "未执行案例数、执行异常案例数、缺失案例数、报告内部计数不一致数均为 0；"
        "缺失时 fail_on_missing_evidence=true → BLOCK / false → WARN",
    }
    if candidate_report is None:
        return _check(
            check_id="data_integrity",
            rule_id="GATE_EVIDENCE_MISSING",
            category="data_integrity",
            status="INCONCLUSIVE",
            severity="HIGH",
            message="候选评测报告不可读（运行记录仅内存保留，可能已过期），无法核对数据完整性",
            threshold=threshold,
            actual={"candidate_report_available": False},
            missing_evidence=True,
        )

    not_executed = candidate_report.get("not_executed_cases") or 0
    execution_errors = candidate_report.get("execution_error_cases") or 0
    total = candidate_report.get("total_cases")
    executed = candidate_report.get("executed_cases")
    case_results = candidate_report.get("case_results") or []
    missing_cases = comparison.get("cases", {}).get("missing_cases") or {}
    only_baseline = len(missing_cases.get("only_in_baseline") or [])
    only_candidate = len(missing_cases.get("only_in_candidate") or [])
    verdict = (comparison.get("comparability") or {}).get("verdict")
    # 版本不可比时（不同数据集/版本），案例集不同是预期结果，应由可比性检查给出
    # INCONCLUSIVE，而不是把它重复算成数据完整性 BLOCK；这里只记录、不做二次判定。
    case_set_checked = verdict != "NOT_COMPARABLE"

    issues: list[str] = []
    if not_executed:
        issues.append(f"未执行案例 {not_executed} 个")
    if execution_errors:
        issues.append(f"执行异常案例 {execution_errors} 个")
    if case_set_checked and (only_baseline or only_candidate):
        issues.append(f"缺失案例（仅 A {only_baseline} / 仅 B {only_candidate}）")
    if total is not None and executed is not None and len(case_results) != executed:
        issues.append(f"案例结果数 {len(case_results)} ≠ 已执行数 {executed}")
    if total is not None and executed is not None and executed + not_executed != total:
        issues.append(f"已执行 {executed} + 未执行 {not_executed} ≠ 总案例 {total}")

    actual = {
        "candidate_report_available": True,
        "total_cases": total,
        "executed_cases": executed,
        "not_executed_cases": not_executed,
        "execution_error_cases": execution_errors,
        "case_result_count": len(case_results),
        "missing_cases_only_in_baseline": only_baseline,
        "missing_cases_only_in_candidate": only_candidate,
        "case_set_checked": case_set_checked,
        "issues": issues,
    }
    if issues:
        return _check(
            check_id="data_integrity",
            rule_id="GATE_DATA_INTEGRITY",
            category="data_integrity",
            status=_rate_missing_status(fail_on_missing),
            severity="HIGH",
            message="数据完整性不足：" + "；".join(issues),
            threshold=threshold,
            actual=actual,
            missing_evidence=True,
        )
    return _check(
        check_id="data_integrity",
        rule_id="GATE_DATA_INTEGRITY",
        category="data_integrity",
        status="PASS",
        severity="HIGH",
        message="数据完整性检查通过（无未执行/执行异常/缺失案例）",
        threshold=threshold,
        actual=actual,
    )


def _check_comparability(comparison: dict[str, Any]) -> dict[str, Any]:
    comparability = comparison.get("comparability") or {}
    verdict = comparability.get("verdict")
    critical_codes = [i.get("code") for i in comparability.get("critical_issues") or []]
    warning_codes = [i.get("code") for i in comparability.get("warning_issues") or []]
    actual = {
        "verdict": verdict,
        "issue_count": comparability.get("issue_count"),
        "critical_codes": critical_codes,
        "warning_codes": warning_codes,
    }
    if verdict == "NOT_COMPARABLE":
        return _check(
            check_id="comparability",
            rule_id="GATE_NOT_COMPARABLE",
            category="comparability",
            status="INCONCLUSIVE",
            severity="CRITICAL",
            message="版本不可比（" + ", ".join(critical_codes) + "），门禁无法判定，不视为通过",
            threshold={"unit": "enum", "calculation": "verdict == COMPARABLE"},
            actual=actual,
            evidence_refs=[
                {"kind": "comparability_issue", "ref_id": code} for code in critical_codes
            ],
        )
    if verdict == "COMPARABLE_WITH_WARNINGS":
        return _check(
            check_id="comparability",
            rule_id="GATE_COMPARABILITY_WARNINGS",
            category="comparability",
            status="WARN",
            severity="MEDIUM",
            message="版本可比但存在警告：" + ", ".join(warning_codes),
            threshold={"unit": "enum", "calculation": "verdict == COMPARABLE"},
            actual=actual,
            evidence_refs=[
                {"kind": "comparability_issue", "ref_id": code} for code in warning_codes
            ],
        )
    return _check(
        check_id="comparability",
        rule_id="GATE_NOT_COMPARABLE",
        category="comparability",
        status="PASS",
        severity="CRITICAL",
        message="版本可比（COMPARABLE）",
        threshold={"unit": "enum", "calculation": "verdict == COMPARABLE"},
        actual=actual,
    )


def _check_required_version(
    *,
    check_id: str,
    rule_id: str,
    label: str,
    required: str | None,
    actual_value: str | None,
    rubric_mode: bool,
    fail_on_missing: bool,
) -> dict[str, Any]:
    threshold = {"value": required, "unit": "version_string", "calculation": f"{label} == required"}
    actual = {"required": required, "candidate": actual_value}
    if required is None:
        return _check(
            check_id=check_id,
            rule_id=rule_id,
            category="version",
            status="NOT_APPLICABLE",
            severity="MEDIUM",
            message=f"未配置 {label} 要求（不检查）",
            threshold=threshold,
            actual=actual,
        )
    if actual_value is None:
        if rubric_mode:
            return _check(
                check_id=check_id,
                rule_id=rule_id,
                category="version",
                status="NOT_APPLICABLE",
                severity="MEDIUM",
                message=f"候选运行未启用 Judge，{label} 不适用",
                threshold=threshold,
                actual=actual,
            )
        return _check(
            check_id=check_id,
            rule_id=f"{rule_id}_MISSING",
            category="version",
            status=_rate_missing_status(fail_on_missing),
            severity="HIGH",
            message=f"候选运行缺少 {label}，无法核对要求 {required}",
            threshold=threshold,
            actual=actual,
            missing_evidence=True,
        )
    if actual_value != required:
        return _check(
            check_id=check_id,
            rule_id=rule_id,
            category="version",
            status="BLOCK",
            severity="HIGH",
            message=f"{label} {actual_value} ≠ 要求 {required}",
            threshold=threshold,
            actual=actual,
        )
    return _check(
        check_id=check_id,
        rule_id=rule_id,
        category="version",
        status="PASS",
        severity="HIGH",
        message=f"{label} {actual_value} 符合要求",
        threshold=threshold,
        actual=actual,
    )


def _check_ratio_increase(
    *,
    check_id: str,
    rule_id: str,
    metric_name: str,
    label: str,
    max_increase: float,
    min_baseline: float,
    comparison: dict[str, Any],
    unit: str,
    fail_on_missing: bool,
) -> dict[str, Any]:
    metrics = _metric_map(comparison)
    base = _side(metrics, metric_name, "baseline").get("value")
    cand = _side(metrics, metric_name, "candidate").get("value")
    threshold = {
        "value": max_increase,
        "unit": "ratio",
        "calculation": f"(candidate_{metric_name} / baseline_{metric_name}) - 1 <= max_increase，"
        f"且 baseline >= {min_baseline} {unit}",
    }
    actual = {
        "metric": metric_name,
        "baseline": base,
        "candidate": cand,
        "min_baseline_required": min_baseline,
        "unit": unit,
    }
    if base is None or cand is None:
        return _check(
            check_id=check_id,
            rule_id=f"{rule_id}_MISSING",
            category="performance",
            status=_rate_missing_status(fail_on_missing),
            severity="MEDIUM",
            message=f"{label}指标不可得（baseline/candidate 为 null），无法核对",
            threshold=threshold,
            actual=actual,
            missing_evidence=True,
        )
    if base < min_baseline:
        return _check(
            check_id=check_id,
            rule_id=rule_id,
            category="performance",
            status="NOT_APPLICABLE",
            severity="LOW",
            message=(
                f"{label}基线 {base} {unit} 低于判定下限 {min_baseline} {unit}，"
                "量级过小、比率噪声大，不判定（不伪造结论）"
            ),
            threshold=threshold,
            actual=actual,
        )
    increase = (cand / base - 1.0) if base else None
    actual["increase_ratio"] = round(increase, 6) if increase is not None else None
    if increase is not None and increase > max_increase:
        return _check(
            check_id=check_id,
            rule_id=rule_id,
            category="performance",
            status="WARN",
            severity="MEDIUM",
            message=f"{label}上升 {increase:.1%} > 允许上限 {max_increase:.1%}",
            threshold=threshold,
            actual=actual,
            evidence_refs=[{"kind": "metric", "ref_id": metric_name}],
        )
    return _check(
        check_id=check_id,
        rule_id=rule_id,
        category="performance",
        status="PASS",
        severity="MEDIUM",
        message=f"{label}上升 {(increase or 0):.1%} ≤ 允许上限 {max_increase:.1%}",
        threshold=threshold,
        actual=actual,
        evidence_refs=[{"kind": "metric", "ref_id": metric_name}],
    )


def _check_regression_gate(regression: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    triggered = regression.get("triggered_rules") or []
    blocking = [t for t in triggered if t.get("blocking")]
    advisory = [t for t in triggered if not t.get("blocking")]
    threshold = {
        "unit": "count",
        "calculation": "回归检测中 blocking=true 的触发规则数 == 0",
    }
    actual = {
        "regression_gate_status": regression.get("gate_status"),
        "blocking_rule_ids": [t.get("rule_id") for t in blocking],
        "advisory_rule_ids": [t.get("rule_id") for t in advisory],
    }
    evidence = [
        {"kind": "regression_rule", "ref_id": t.get("rule_id")} for t in blocking
    ]
    if blocking:
        return (
            _check(
                check_id="regression_blocking",
                rule_id="GATE_REGRESSION_BLOCK",
                category="regression",
                status="BLOCK",
                severity="HIGH",
                message="回归检测命中阻断规则：" + ", ".join(t.get("rule_id") for t in blocking),
                threshold=threshold,
                actual=actual,
                evidence_refs=evidence,
            ),
            advisory,
        )
    if regression.get("gate_status") == "INCONCLUSIVE":
        return (
            _check(
                check_id="regression_blocking",
                rule_id="GATE_REGRESSION_INCONCLUSIVE",
                category="regression",
                status="INCONCLUSIVE",
                severity="HIGH",
                message="回归检测无法判定（" + str(regression.get("gate_reason")) + "），不视为通过",
                threshold=threshold,
                actual=actual,
            ),
            advisory,
        )
    return (
        _check(
            check_id="regression_blocking",
            rule_id="GATE_REGRESSION_BLOCK",
            category="regression",
            status="PASS",
            severity="HIGH",
            message=(
                "回归检测无阻断规则命中"
                + (f"；非阻断规则命中 {len(advisory)} 条（仅告警）" if advisory else "")
            ),
            threshold=threshold,
            actual=actual,
            evidence_refs=evidence,
        ),
        advisory,
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def evaluate_gate(
    *,
    evaluation_run_id: str | None = None,
    baseline_run_id: str | None = None,
    candidate_run_id: str | None = None,
    comparison_id: str | None = None,
    regression_report_id: str | None = None,
    config_path: str | Path | None = None,
    config_overrides: dict[str, Any] | None = None,
    ci_run_id: str | None = None,
    git_commit: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """执行质量门禁，返回确定性门禁报告。

    Args:
        evaluation_run_id: 候选评测运行（可选；否则从对比报告推断）。
        baseline_run_id / candidate_run_id: 直接指定两次运行（自动生成对比/回归）。
        comparison_id: 已持久化的版本对比报告 ID。
        regression_report_id: 已持久化的回归报告 ID。
        config_path: 门禁配置路径（默认 `config/quality_gate.yaml`）。
        config_overrides: 配置覆盖（测试/API 用，与默认配置合并后校验）。
        ci_run_id / git_commit: 报告中的 CI 标识（默认从环境变量/仓库读取）。
        persist: 是否落盘 JSON+Markdown 报告（默认 True）。

    Raises:
        QualityGateConfigError: 配置非法。
        QualityGateError: 无法解析评测/对比/回归上下文。
    """
    loaded = load_gate_config(config_path, overrides=config_overrides)
    cfg = loaded["config"]
    fail_on_missing = cfg["fail_on_missing_evidence"]

    context = _resolve_context(
        evaluation_run_id=evaluation_run_id,
        baseline_run_id=baseline_run_id,
        candidate_run_id=candidate_run_id,
        comparison_id=comparison_id,
        regression_report_id=regression_report_id,
    )
    comparison = context["comparison"]
    regression = context["regression"]
    candidate_report = context["candidate_report"]

    baseline_desc = _descriptor_summary(comparison.get("baseline_version"))
    candidate_desc = _descriptor_summary(comparison.get("candidate_version"))

    checks: list[dict[str, Any]] = [
        _check_threshold_metric(
            check_id="task_success",
            rule_id="GATE_TASK_SUCCESS_BELOW_THRESHOLD",
            metric_name="task_success_rate",
            label="候选任务成功率",
            threshold=cfg["task_success_threshold"],
            comparison=comparison,
            fail_on_missing=fail_on_missing,
        ),
        _check_threshold_metric(
            check_id="tool_correctness",
            rule_id="GATE_TOOL_CORRECTNESS_BELOW_THRESHOLD",
            metric_name="tool_correctness_rate",
            label="候选工具正确率",
            threshold=cfg["tool_correctness_threshold"],
            comparison=comparison,
            fail_on_missing=fail_on_missing,
        ),
        _check_safety(
            comparison,
            limit=cfg["safety_failure_limit"],
            fail_on_missing=fail_on_missing,
        ),
        _check_case_regressions(
            comparison,
            max_regressions=cfg["max_case_regressions"],
            fail_on_missing=fail_on_missing,
        ),
        _check_data_integrity(
            candidate_report=candidate_report,
            comparison=comparison,
            fail_on_missing=fail_on_missing,
        ),
        _check_comparability(comparison),
        _check_required_version(
            check_id="required_dataset_version",
            rule_id="GATE_DATASET_VERSION_MISMATCH",
            label="数据集版本",
            required=cfg["required_dataset_version"],
            actual_value=candidate_desc.get("dataset_version"),
            rubric_mode=False,
            fail_on_missing=fail_on_missing,
        ),
        _check_required_version(
            check_id="required_rubric_version",
            rule_id="GATE_RUBRIC_VERSION_MISMATCH",
            label="Rubric 版本",
            required=cfg["required_rubric_version"],
            actual_value=candidate_desc.get("rubric_version"),
            rubric_mode=True,
            fail_on_missing=fail_on_missing,
        ),
        _check_ratio_increase(
            check_id="latency_increase",
            rule_id="GATE_LATENCY_INCREASE",
            metric_name="average_duration_ms",
            label="平均耗时",
            max_increase=cfg["max_latency_increase"],
            min_baseline=cfg["latency_min_baseline_ms"],
            comparison=comparison,
            unit="ms",
            fail_on_missing=fail_on_missing,
        ),
        _check_ratio_increase(
            check_id="cost_increase",
            rule_id="GATE_COST_INCREASE",
            metric_name="total_tokens",
            label="Token 成本",
            max_increase=cfg["max_cost_increase"],
            min_baseline=cfg["cost_min_baseline_tokens"],
            comparison=comparison,
            unit="tokens",
            fail_on_missing=fail_on_missing,
        ),
    ]
    regression_check, advisory_rules = _check_regression_gate(regression)
    checks.append(regression_check)

    statuses = [c["status"] for c in checks]
    if "BLOCK" in statuses:
        gate_status = "BLOCK"
    elif "INCONCLUSIVE" in statuses:
        gate_status = "INCONCLUSIVE"
    elif "WARN" in statuses:
        gate_status = "WARN"
    else:
        gate_status = "PASS"

    exit_codes = {
        "PASS": 0,
        "WARN": cfg["warn_exit_code"],
        "BLOCK": 1,
        "INCONCLUSIVE": 2,
    }
    exit_code = exit_codes[gate_status]

    triggered = [
        {
            "rule_id": c["rule_id"],
            "check_id": c["check_id"],
            "category": c["category"],
            "status": c["status"],
            "severity": c["severity"],
            "threshold": c["threshold"],
            "actual": c["actual"],
            "message": c["message"],
            "evidence_refs": c["evidence_refs"],
        }
        for c in checks
        if c["status"] in {"BLOCK", "WARN", "INCONCLUSIVE"}
    ]
    non_pass_messages = [c["message"] for c in checks if c["status"] in {"BLOCK", "WARN", "INCONCLUSIVE"}]
    gate_reason = (
        "；".join(non_pass_messages[:5]) if non_pass_messages else "所有质量门禁检查通过"
    )

    evidence_refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _add_evidence(ref: dict[str, Any]) -> None:
        key = (str(ref.get("kind")), str(ref.get("ref_id")))
        if key in seen or not ref.get("ref_id"):
            return
        seen.add(key)
        evidence_refs.append(ref)

    _add_evidence({"kind": "comparison_report", "ref_id": comparison.get("comparison_id")})
    _add_evidence(
        {"kind": "regression_report", "ref_id": regression.get("regression_report_id")}
    )
    _add_evidence({"kind": "evaluation_run", "ref_id": baseline_desc.get("evaluation_run_id")})
    _add_evidence({"kind": "evaluation_run", "ref_id": candidate_desc.get("evaluation_run_id")})
    for check in checks:
        for ref in check.get("evidence_refs") or []:
            _add_evidence(ref)
    for case in regression.get("regressed_cases") or []:
        _add_evidence(
            {
                "kind": "case",
                "ref_id": case.get("case_id"),
                "trace_ids": [
                    tid
                    for tid in (case.get("trace_id_baseline"), case.get("trace_id_candidate"))
                    if tid
                ],
            }
        )

    limitations: list[str] = [
        "门禁结论完全由确定性规则得出，不含模型评分/概率/置信度",
        f"阈值为项目演示配置（{cfg['label']}），非行业统一标准，需按项目重新标定",
        "评测运行记录仅内存保留（进程重启后不可查询），报告中的运行 ID 只在对应进程内有效",
        "非阻断回归规则命中只作为告警（advisory），不改变门禁状态；阻断规则命中才 BLOCK",
        "UNKNOWN / 缺失证据不当作通过：fail_on_missing_evidence=true 时 BLOCK，否则 WARN",
        "INCONCLUSIVE 表示无法判定，绝不等于 PASS",
        "WARN 默认退出码 0（仅告警）；可通过 warn_exit_code 配置改为非零",
    ]
    if candidate_report is None:
        limitations.append("候选评测报告不可读，数据完整性检查为 INCONCLUSIVE")
    if comparison.get("comparability", {}).get("verdict") != "COMPARABLE":
        limitations.append(
            f"版本可比性为 {comparison.get('comparability', {}).get('verdict')}，"
            "门禁结论只能作为诊断参考"
        )

    report: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "gate_report_id": f"qg-{uuid.uuid4().hex}",
        "gate_status": gate_status,
        "gate_reason": gate_reason,
        "exit_code": exit_code,
        "ci_run_id": ci_run_id or os.environ.get("GITHUB_RUN_ID") or f"local-{uuid.uuid4().hex[:12]}",
        "git_commit": git_commit or _git_commit(),
        "config": {
            "version": cfg["version"],
            "label": cfg["label"],
            "fingerprint": loaded["config_fingerprint"],
            "source": loaded["config_source"],
            "path": _display_path(loaded["config_path"]),
            "values": cfg,
        },
        "dataset_version": candidate_desc.get("dataset_version"),
        "evaluator_version": candidate_desc.get("evaluator_version"),
        "baseline_version": baseline_desc,
        "candidate_version": candidate_desc,
        "comparison_id": comparison.get("comparison_id"),
        "regression_report_id": regression.get("regression_report_id"),
        "comparability": {
            "verdict": (comparison.get("comparability") or {}).get("verdict"),
            "issue_count": (comparison.get("comparability") or {}).get("issue_count"),
        },
        "checks": checks,
        "triggered_rules": triggered,
        "advisory_regression_rules": [
            {
                "rule_id": t.get("rule_id"),
                "severity": t.get("severity"),
                "message": t.get("reason"),
            }
            for t in advisory_rules
        ],
        "evidence_refs": evidence_refs,
        "limitations": limitations,
        "paid_api_called": False,
        "generated_at": _now_iso(),
    }
    if persist:
        report["report_files"] = _persist(report)
    return report


# ---------------------------------------------------------------------------
# 持久化与 Markdown
# ---------------------------------------------------------------------------

def _persist(report: dict[str, Any]) -> dict[str, str]:
    QUALITY_GATE_DIR.mkdir(parents=True, exist_ok=True)
    gate_report_id = report["gate_report_id"]
    json_path = QUALITY_GATE_DIR / f"{gate_report_id}.json"
    md_path = QUALITY_GATE_DIR / f"{gate_report_id}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return {"json": _display_path(json_path), "markdown": _display_path(md_path)}


def get_gate_report(gate_report_id: str) -> dict[str, Any]:
    if not gate_report_id.startswith("qg-") or not gate_report_id.replace("qg-", "").isalnum():
        raise QualityGateError(f"非法门禁报告 ID: {gate_report_id!r}")
    path = QUALITY_GATE_DIR / f"{gate_report_id}.json"
    if not path.exists():
        raise QualityGateError(f"门禁报告不存在: {gate_report_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_gate_reports(limit: int = 20) -> list[dict[str, Any]]:
    if not QUALITY_GATE_DIR.exists():
        return []
    files = sorted(QUALITY_GATE_DIR.glob("qg-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for path in files[:limit]:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append(
            {
                "gate_report_id": report.get("gate_report_id"),
                "gate_status": report.get("gate_status"),
                "exit_code": report.get("exit_code"),
                "ci_run_id": report.get("ci_run_id"),
                "git_commit": report.get("git_commit"),
                "comparison_id": report.get("comparison_id"),
                "dataset_version": report.get("dataset_version"),
                "triggered_rules": [r.get("rule_id") for r in report.get("triggered_rules") or []],
                "generated_at": report.get("generated_at"),
            }
        )
    return out


def _md_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    """生成人工可读的 Markdown 门禁摘要（不含任何敏感信息）。"""
    status = report.get("gate_status")
    lines: list[str] = [
        "# TracePilot Quality Gate Report",
        "",
        f"**Gate Status: `{status}`**  ·  exit code `{report.get('exit_code')}`",
        "",
        f"- Gate Report ID: `{report.get('gate_report_id')}`",
        f"- CI Run ID: `{report.get('ci_run_id')}`",
        f"- Git Commit: `{report.get('git_commit')}`",
        f"- Dataset Version: `{report.get('dataset_version')}`",
        f"- Evaluator Version: `{report.get('evaluator_version')}`",
        f"- Baseline Version: "
        f"`{(report.get('baseline_version') or {}).get('evaluation_version_id')}`"
        f"（run `{(report.get('baseline_version') or {}).get('evaluation_run_id')}`）",
        f"- Candidate Version: "
        f"`{(report.get('candidate_version') or {}).get('evaluation_version_id')}`"
        f"（run `{(report.get('candidate_version') or {}).get('evaluation_run_id')}`）",
        f"- Comparison: `{report.get('comparison_id')}`",
        f"- Regression Report: `{report.get('regression_report_id')}`",
        f"- Config: `{report.get('config', {}).get('fingerprint')}`"
        f"（{report.get('config', {}).get('label')}，source="
        f"{report.get('config', {}).get('source')}）",
        f"- Paid API Called: `{report.get('paid_api_called')}`",
        "",
        "## Gate Reason",
        "",
        str(report.get("gate_reason")),
        "",
        "## Triggered Rules",
        "",
    ]
    triggered = report.get("triggered_rules") or []
    if not triggered:
        lines.append("_无触发规则_")
    else:
        lines += [
            "| Rule | Status | Severity | Threshold | Actual | Message |",
            "|------|--------|----------|-----------|--------|---------|",
        ]
        for rule in triggered:
            threshold = rule.get("threshold") or {}
            actual = rule.get("actual") or {}
            lines.append(
                "| {rule} | {status} | {severity} | {thr} | {actual} | {msg} |".format(
                    rule=_md_cell(rule.get("rule_id")),
                    status=_md_cell(rule.get("status")),
                    severity=_md_cell(rule.get("severity")),
                    thr=_md_cell(
                        f"{threshold.get('value')} {threshold.get('unit')}".strip()
                    ),
                    actual=_md_cell(json.dumps(actual, ensure_ascii=False)[:220]),
                    msg=_md_cell(rule.get("message")),
                )
            )
    lines += ["", "## All Checks", ""]
    lines += [
        "| Check | Status | Threshold (unit) | Actual | Message |",
        "|-------|--------|------------------|--------|---------|",
    ]
    for check in report.get("checks") or []:
        threshold = check.get("threshold") or {}
        actual = check.get("actual") or {}
        lines.append(
            "| {cid} | {status} | {thr} | {actual} | {msg} |".format(
                cid=_md_cell(check.get("check_id")),
                status=_md_cell(check.get("status")),
                thr=_md_cell(f"{threshold.get('value')} {threshold.get('unit')}".strip()),
                actual=_md_cell(json.dumps(actual, ensure_ascii=False)[:220]),
                msg=_md_cell(check.get("message")),
            )
        )
    lines += ["", "## Evidence References", ""]
    evidence = report.get("evidence_refs") or []
    if not evidence:
        lines.append("_无证据引用_")
    else:
        for ref in evidence:
            extra = f"（{ref.get('detail')}）" if ref.get("detail") else ""
            lines.append(f"- `{ref.get('kind')}:{ref.get('ref_id')}`{extra}")
    lines += ["", "## Limitations", ""]
    for item in report.get("limitations") or []:
        lines.append(f"- {item}")
    lines += [
        "",
        "> 本报告由确定性规则生成；阈值是项目演示配置，不是行业统一标准。",
        "> INCONCLUSIVE 表示无法判定，绝不等于 PASS。",
        "",
    ]
    return "\n".join(lines)
