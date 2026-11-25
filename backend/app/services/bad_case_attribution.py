"""Automated Bad Case Attribution 自动化归因（第 47 步）。

输入一次评测运行（或单案例 / 单 Trace），输出结构化归因报告：

```
Evaluation Run → Bad Case → Trace → 步骤 / 工具错误 / 故障注入 /
                权限拒绝 / 失败指标 / DAG 依赖 / 对比 / 反事实实验
              → 候选归因（多条并存）+ 证据等级 + 限制
```

诚实性约束（硬性）：
1. **Trace 直接记录的异常**才能标记 `CONFIRMED_EVENT`；
2. **DAG 后继节点只说明依赖关系**，一律 `POTENTIAL_CAUSE`，注释明确"不构成因果"；
3. 对照实验结论必须**引用真实 experiment_id**，且注明控制条件 → `INTERVENTION_SUPPORTED`；
4. 没有证据时返回 `UNKNOWN`，**不伪造故障原因**；
5. **不生成无来源的置信概率**（只输出证据等级与依据，不输出 0~1 数字）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.app.schemas.trace import StepStatus, StepType, TraceRecord
from backend.app.services.trace_store import load_trace

__all__ = [
    "ATTRIBUTION_TYPES",
    "EVIDENCE_LEVELS",
    "AttributionError",
    "attribute_case",
    "attribute_evaluation_run",
    "attribute_trace",
    "get_report",
    "list_reports",
    "persist_report",
    "ATTRIBUTION_DIR",
    "MAX_BATCH_CASES",
    "MAX_REPORT_SECONDS",
]

ENGINE_VERSION = "1.0.0"
ATTRIBUTION_DIR = Path(__file__).resolve().parents[3] / "data" / "attribution" / "reports"
MAX_BATCH_CASES = 20
MAX_REPORT_SECONDS = 60.0

ATTRIBUTION_TYPES = (
    "TOOL_TIMEOUT",
    "TOOL_EXCEPTION",
    "INVALID_ARGUMENTS",
    "INVALID_TOOL_RESULT",
    "EMPTY_RETRIEVAL",
    "PERMISSION_DENIED",
    "TASK_INCOMPLETE",
    "OUTPUT_MISMATCH",
    "PROCESS_VIOLATION",
    "UNKNOWN",
)

EVIDENCE_LEVELS = (
    "CONFIRMED_EVENT",
    "OBSERVED_DIFFERENCE",
    "POTENTIAL_CAUSE",
    "INTERVENTION_SUPPORTED",
    "UNKNOWN",
)

# 故障注入类型 → 归因类型
_FAULT_TO_ATTRIBUTION = {
    "tool_timeout": "TOOL_TIMEOUT",
    "tool_exception": "TOOL_EXCEPTION",
    "invalid_arguments": "INVALID_ARGUMENTS",
    "invalid_tool_result": "INVALID_TOOL_RESULT",
    "empty_retrieval": "EMPTY_RETRIEVAL",
    "permission_denied": "PERMISSION_DENIED",
}

# Trace 索引缓存：避免每案例都全量解析 data/traces（第 47 步性能修复）
_INDEX_TTL_SECONDS = 30.0
_INDEX_CACHE: dict[str, Any] = {"ts": 0.0, "by_task": {}}

_METRIC_LABELS = {
    "task_success": "任务成功",
    "tool_correctness": "工具正确性",
    "process_quality": "过程质量",
    "recovery": "故障恢复",
    "efficiency": "效率",
    "safety": "安全性",
}


class AttributionError(ValueError):
    """归因请求非法。"""


# ---------------------------------------------------------------------------
# 证据工具
# ---------------------------------------------------------------------------

def _ref(kind: str, ref_id: str, detail: str) -> dict[str, Any]:
    """证据引用（必须可在原始数据中查证）。"""
    return {"kind": kind, "ref_id": ref_id, "detail": detail}


def _candidate(
    cause_type: str,
    level: str,
    summary: str,
    *,
    evidence: list[dict[str, Any]],
    affected_steps: list[str],
    basis: str,
    uncertainty: str | None = None,
    experiment_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    assert cause_type in ATTRIBUTION_TYPES, cause_type
    assert level in EVIDENCE_LEVELS, level
    return {
        "cause_type": cause_type,
        "evidence_level": level,
        "summary": summary,
        "evidence": evidence,
        "affected_steps": affected_steps,
        "confidence_basis": basis,          # 文字依据，不含数字概率
        "uncertainty": uncertainty,
        "experiment_refs": experiment_refs or [],
        "is_conclusive": level in {"CONFIRMED_EVENT", "INTERVENTION_SUPPORTED"},
    }


def _failed_metrics(case: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    failed: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for name, m in (case.get("metrics") or {}).items():
        status = (m or {}).get("status")
        item = {
            "metric": name,
            "label": _METRIC_LABELS.get(name, name),
            "status": status,
            "metric_value": (m or {}).get("metric_value"),
            "method": (m or {}).get("evaluation_method"),
            "evidence": (m or {}).get("evidence") or [],
            "limitations": (m or {}).get("confidence_or_limitations"),
        }
        if status == "FAIL":
            failed.append(item)
        elif status == "UNKNOWN":
            unknown.append(item)
    return failed, unknown


def _step_events(trace: TraceRecord) -> list[dict[str, Any]]:
    """从 Trace 中提取可直接确认的异常事件。"""
    events: list[dict[str, Any]] = []
    for step in trace.steps:
        fault = (step.metadata or {}).get("fault_injection")
        error = step.error
        if isinstance(fault, dict) and fault.get("injected") is True:
            events.append({
                "event_id": f"evt-fault-{step.step_id}",
                "kind": "fault_injection",
                "step_id": step.step_id,
                "fault_type": fault.get("type"),
                "detail": fault.get("message") or "Trace 记录到故障注入",
                "evidence_level": "CONFIRMED_EVENT",
                "evidence_ref": _ref("trace_step", step.step_id, f"metadata.fault_injection={fault.get('type')}"),
            })
        elif isinstance(fault, dict) and fault.get("type"):
            events.append({
                "event_id": f"evt-faultmeta-{step.step_id}",
                "kind": "fault_injection",
                "step_id": step.step_id,
                "fault_type": fault.get("type"),
                "detail": "Trace 记录到故障注入元数据",
                "evidence_level": "CONFIRMED_EVENT",
                "evidence_ref": _ref("trace_step", step.step_id, "metadata.fault_injection"),
            })

        if error is not None:
            events.append({
                "event_id": f"evt-error-{step.step_id}",
                "kind": "step_error",
                "step_id": step.step_id,
                "error_type": getattr(error, "error_type", None),
                "error_message": getattr(error, "error_message", None),
                "status": step.status.value,
                "detail": f"步骤 {step.step_id} 记录错误：{getattr(error, 'error_type', '')}",
                "evidence_level": "CONFIRMED_EVENT",
                "evidence_ref": _ref("trace_step", step.step_id, "error 字段"),
            })

        if step.step_type == StepType.TOOL_CALL and step.status == StepStatus.FAILED and error is None:
            events.append({
                "event_id": f"evt-toolfail-{step.step_id}",
                "kind": "tool_failed",
                "step_id": step.step_id,
                "tool_name": step.tool_name,
                "detail": f"工具 {step.tool_name} 执行失败但未记录 error 详情",
                "evidence_level": "CONFIRMED_EVENT",
                "evidence_ref": _ref("trace_step", step.step_id, "status=failed"),
            })
    return events


def _final_answer(trace: TraceRecord) -> str:
    for step in reversed(trace.steps):
        if step.step_type == StepType.MODEL_CALL and step.output:
            return str(step.output)
    return ""


def _dependency_dependents(trace: TraceRecord, faulty_steps: set[str]) -> dict[str, list[str]]:
    """DAG 中从故障节点可达的节点（**仅依赖关系，不是因果**）。"""
    try:
        from backend.app.services.trace_dag import build_dag

        dag = build_dag(trace)
    except Exception:
        return {}
    out: dict[str, list[str]] = {}
    for sid in faulty_steps:
        try:
            out[sid] = sorted(dag.descendants(sid))
        except Exception:
            out[sid] = []
    return out


def _find_experiments(trace_id: str) -> list[dict[str, Any]]:
    """查找以该 Trace 为原始 Trace 的反事实实验（真实 experiment_id）。"""
    try:
        from backend.app.services.experiment_store import get_store

        store = get_store()
        items = store.list_all() if hasattr(store, "list_all") else []
    except Exception:
        return []

    found: list[dict[str, Any]] = []
    for item in items or []:
        exp_id = item.get("experiment_id") if isinstance(item, dict) else None
        if not exp_id:
            continue
        try:
            exp = store.get(exp_id)
        except Exception:
            continue
        if exp.get("source_trace_id") != trace_id:
            continue
        result = exp.get("result") or {}
        found.append({
            "experiment_id": exp_id,
            "status": exp.get("status"),
            "intervention": exp.get("intervention"),
            "scenario": exp.get("scenario"),
            "replays": exp.get("replays"),
            "causal_evidence_level": result.get("causal_evidence_level"),
            "control_conditions": result.get("control_conditions") or result.get("environment"),
            "causal_evidence_note": result.get("causal_evidence_note"),
            "status_ok": exp.get("status") == "succeeded",
        })
    return found


def _trace_index() -> dict[str, list[TraceRecord]]:
    """按 task_id 建立 Trace 索引（TTL 缓存，避免重复全量扫描）。"""
    import time as _time

    now = _time.monotonic()
    if now - float(_INDEX_CACHE.get("ts") or 0.0) < _INDEX_TTL_SECONDS and _INDEX_CACHE.get("by_task"):
        return _INDEX_CACHE["by_task"]  # type: ignore[return-value]

    from backend.app.services.trace_store import list_traces

    by_task: dict[str, list[TraceRecord]] = {}
    for rec in list_traces():
        by_task.setdefault(rec.task_id, []).append(rec)
    _INDEX_CACHE["by_task"] = by_task
    _INDEX_CACHE["ts"] = now
    return by_task


def _find_baseline_and_diff(trace: TraceRecord) -> dict[str, Any] | None:
    """尝试找到同任务的对照 Trace 并做结构对比（不构成因果，仅观察差异）。"""
    try:
        from backend.app.services.trace_comparator import compare_traces

        baseline = None
        for rec in _trace_index().get(trace.task_id, []):
            if rec.trace_id == trace.trace_id:
                continue
            faults = (rec.metadata or {}).get("fault_injections") or []
            if faults:
                continue
            baseline = rec
            break
        if baseline is None:
            return None
        report = compare_traces(baseline, trace)
        earliest = report.get("earliest_difference") if isinstance(report, dict) else None
        return {
            "baseline_trace_id": baseline.trace_id,
            "earliest_difference": earliest,
            "observations": (report.get("observations") or [])[:5] if isinstance(report, dict) else [],
            "diff_node_count": len(report.get("diff_nodes") or []) if isinstance(report, dict) else 0,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 单案例归因
# ---------------------------------------------------------------------------

def attribute_case(
    *,
    evaluation_run_id: str | None,
    case: dict[str, Any],
    dataset_id: str | None = None,
) -> dict[str, Any]:
    """对单个评测案例生成归因报告。"""
    case_id = case.get("case_id")
    trace_id = case.get("trace_id")
    if not case_id:
        raise AttributionError("案例缺少 case_id")
    if not trace_id:
        return _report(
            evaluation_run_id=evaluation_run_id,
            case_id=case_id,
            trace_id=None,
            failed_metrics=[], unknown_metrics=[],
            observed_events=[],
            candidates=[_candidate(
                "UNKNOWN", "UNKNOWN",
                "案例未关联 Trace，无法归因",
                evidence=[_ref("evaluation_case", case_id, "trace_id 缺失")],
                affected_steps=[],
                basis="无可分析执行轨迹",
                uncertainty="评测案例未落盘或执行未产生 Trace",
            )],
            limitations=["案例未关联 Trace，归因不可用"],
            dataset_id=dataset_id,
        )

    try:
        trace = load_trace(trace_id)
    except Exception:
        return _report(
            evaluation_run_id=evaluation_run_id,
            case_id=case_id,
            trace_id=trace_id,
            failed_metrics=[], unknown_metrics=[],
            observed_events=[],
            candidates=[_candidate(
                "UNKNOWN", "UNKNOWN",
                "关联 Trace 文件不可读取，历史证据缺失",
                evidence=[_ref("trace", trace_id, "load_trace 失败")],
                affected_steps=[],
                basis="证据不可访问",
                uncertainty="Trace 可能未落盘（历史数据）",
            )],
            limitations=["关联 Trace 不可读，归因不可用"],
            dataset_id=dataset_id,
        )

    failed, unknown = _failed_metrics(case)
    failed_names = {f["metric"] for f in failed}
    events = _step_events(trace)
    candidates: list[dict[str, Any]] = []

    # --- 1) 故障注入（直接证据 → CONFIRMED_EVENT）---
    faulty_steps: set[str] = set()
    for ev in events:
        if ev["kind"] == "fault_injection":
            faulty_steps.add(ev["step_id"])
            mapped = _FAULT_TO_ATTRIBUTION.get(str(ev.get("fault_type")))
            if mapped:
                candidates.append(_candidate(
                    mapped, "CONFIRMED_EVENT",
                    f"步骤 {ev['step_id']} 被注入 {ev.get('fault_type')} 故障",
                    evidence=[ev["evidence_ref"]],
                    affected_steps=[ev["step_id"]],
                    basis="Trace 的 step.metadata.fault_injection 直接记录",
                    uncertainty="故障为注入产生，非生产环境自然发生",
                ))

    # --- 2) 工具错误（有 error 记录 → CONFIRMED_EVENT）---
    for ev in events:
        if ev["kind"] != "step_error":
            continue
        etype = str(ev.get("error_type") or "")
        mapped = None
        if "Timeout" in etype:
            mapped = "TOOL_TIMEOUT"
        elif "Permission" in etype:
            mapped = "PERMISSION_DENIED"
        elif "ToolError" in etype or "ValueError" in etype:
            mapped = "INVALID_ARGUMENTS"
        elif "FaultInjection" in etype:
            mapped = None  # 已由 fault_injection 覆盖
        if mapped and not any(c["cause_type"] == mapped and ev["step_id"] in c["affected_steps"]
                              for c in candidates):
            candidates.append(_candidate(
                mapped, "CONFIRMED_EVENT",
                f"步骤 {ev['step_id']} 记录 {etype}",
                evidence=[ev["evidence_ref"]],
                affected_steps=[ev["step_id"]],
                basis="Trace 的 step.error 直接记录",
                uncertainty="错误类型来自异常类名映射，可能与业务语义不完全一致",
            ))

    # --- 3) 失败指标 → OUTPUT_MISMATCH / TASK_INCOMPLETE / PROCESS_VIOLATION ---
    if "task_success" in failed_names:
        final_answer = _final_answer(trace)
        candidates.append(_candidate(
            "OUTPUT_MISMATCH", "OBSERVED_DIFFERENCE",
            "确定性验证器判定任务成功指标不满足",
            evidence=[_ref("metric", "task_success", "; ".join(failed[0]["evidence"][:3]) or "metric status=FAIL")],
            affected_steps=[],
            basis="评测指标 status=FAIL（确定性验证器）",
            uncertainty="指标只比对答案，未验证解释质量；最终回答长度 "
                        f"{len(final_answer)} 字符",
        ))
        if any(c["cause_type"].startswith("TOOL_") or c["cause_type"] in
               {"INVALID_ARGUMENTS", "PERMISSION_DENIED", "EMPTY_RETRIEVAL", "INVALID_TOOL_RESULT"}
               for c in candidates):
            candidates.append(_candidate(
                "TASK_INCOMPLETE", "POTENTIAL_CAUSE",
                "工具侧异常后任务未能完成（异常与失败的先后关系来自步骤顺序）",
                evidence=[_ref("metric", "task_success", "status=FAIL")],
                affected_steps=sorted(faulty_steps),
                basis="存在已确认的工具异常 + 任务指标失败；两者同时出现",
                uncertainty="同时出现不等于因果，需反事实干预验证",
            ))

    if "tool_correctness" in failed_names:
        tc = next((f for f in failed if f["metric"] == "tool_correctness"), None)
        ev_text = "; ".join((tc or {}).get("evidence") or [])
        affected = [s.step_id for s in trace.steps if s.step_type == StepType.TOOL_CALL]
        # 依据证据内容区分：工具错误预期不符 ≠ 工具选择违规
        is_error_expectation = (
            "非预期工具失败" in ev_text
            or "unexpected tool failure" in ev_text
            or "expects_tool_error" in ev_text
            or "错误预期" in ev_text
        )
        if is_error_expectation:
            candidates.append(_candidate(
                "TASK_INCOMPLETE", "CONFIRMED_EVENT",
                "工具执行结果与该案例声明的错误预期不一致",
                evidence=[_ref("metric", "tool_correctness", ev_text or "status=FAIL")],
                affected_steps=affected,
                basis="评测指标 tool_correctness 的规则证据（expects_tool_error 判定）",
                uncertainty="规则只核对声明预期，不评价业务合理性",
            ))
        else:
            candidates.append(_candidate(
                "PROCESS_VIOLATION", "CONFIRMED_EVENT",
                "工具选择/调用规则（required/forbidden/min_calls）未被满足",
                evidence=[_ref("metric", "tool_correctness", ev_text or "status=FAIL")],
                affected_steps=affected,
                basis="评测指标 tool_correctness status=FAIL（规则可直接核对）",
                uncertainty="仅覆盖已声明的工具规则，未覆盖工具选择的语义合理性",
            ))

    # --- 4) DAG 依赖（只说明依赖，不是因果）---
    dependents = _dependency_dependents(trace, faulty_steps)
    for sid, kids in dependents.items():
        if not kids:
            continue
        # 归因类型里没有「依赖」这一类别：DAG 只给出影响范围，故类型记 UNKNOWN，
        # 证据等级记 POTENTIAL_CAUSE，并在 summary/uncertainty 明确「不是因果」。
        candidates.append(_candidate(
            "UNKNOWN", "POTENTIAL_CAUSE",
            f"步骤 {sid} 的 DAG 后继节点（{len(kids)} 个）落在可能影响范围内（仅依赖关系）",
            evidence=[_ref("dag_dependency", sid, f"descendants={kids[:5]}")],
            affected_steps=kids,
            basis="DAG 可达性分析",
            uncertainty="**仅为依赖关系，不构成因果**；可达不等于受故障影响",
        ))

    # --- 5) 与基线对比（观察差异）---
    diff = _find_baseline_and_diff(trace)
    if diff:
        candidates.append(_candidate(
            "OUTPUT_MISMATCH", "OBSERVED_DIFFERENCE",
            f"与同任务基线 Trace 存在最早差异（{diff['baseline_trace_id']}）",
            evidence=[_ref("trace_comparison", diff["baseline_trace_id"],
                           json.dumps(diff.get("earliest_difference"), ensure_ascii=False, default=str)[:200])],
            affected_steps=[],
            basis="TraceComparator 结构化对比",
            uncertainty="对比为相关性观察，未做干预，不能据此断定因果",
        ))

    # --- 6) 反事实实验支持 ---
    experiments = _find_experiments(trace.trace_id)
    for exp in experiments:
        if not exp.get("status_ok"):
            continue
        level = exp.get("causal_evidence_level")
        candidates.append(_candidate(
            "POTENTIAL_CAUSE", "INTERVENTION_SUPPORTED",
            f"反事实实验 {exp['experiment_id']} 提供了干预证据（因果强度：{level}）",
            evidence=[_ref("experiment", exp["experiment_id"],
                           f"intervention={exp.get('intervention')}, replays={exp.get('replays')}")],
            affected_steps=[],
            basis=f"真实实验引用；实验引擎自评因果证据等级={level}",
            uncertainty=exp.get("causal_evidence_note") or "受控环境为模拟，结论不能外推到真实生产",
            experiment_refs=[{
                "experiment_id": exp["experiment_id"],
                "control_conditions": exp.get("control_conditions"),
                "causal_evidence_level": level,
                "intervention": exp.get("intervention"),
                "replays": exp.get("replays"),
                "note": "intervention_supported 结论必须结合控制条件解读",
            }],
        ))

    # --- 7) UNKNOWN 兜底 ---
    if not candidates:
        candidates.append(_candidate(
            "UNKNOWN", "UNKNOWN",
            "现有证据不足以归因",
            evidence=[_ref("trace", trace.trace_id, f"steps={len(trace.steps)}")],
            affected_steps=[],
            basis="未发现故障注入、工具错误、指标失败证据或实验引用",
            uncertainty="可能缺少基线 Trace / 实验未运行 / 失败模式未被规则覆盖",
        ))

    limitations = [
        "归因只基于本仓库内可核对的数据（Trace / 指标 / DAG / 实验），不访问外部系统",
        "DAG 可达性仅代表依赖关系，绝不是因果证明",
        "置信度以证据等级表达，不输出无来源的概率数字",
        "受控执行使用模拟模型与本地工具，结论不能直接外推到真实生产环境",
    ]
    if unknown:
        limitations.append(
            f"存在 {len(unknown)} 个 UNKNOWN 指标（{', '.join(u['metric'] for u in unknown)}），"
            "它们既不算通过也不算失败，未纳入归因结论"
        )
    if not experiments:
        limitations.append("未找到该 Trace 的反事实实验，无法提供 INTERVENTION_SUPPORTED 级别的证据")

    return _report(
        evaluation_run_id=evaluation_run_id,
        case_id=case_id,
        trace_id=trace.trace_id,
        failed_metrics=failed,
        unknown_metrics=unknown,
        observed_events=events,
        candidates=candidates,
        limitations=limitations,
        dataset_id=dataset_id,
    )


def _merge_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (cause_type, evidence_level) 合并同类候选，保留全部证据与受影响步骤。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for c in candidates:
        key = (c["cause_type"], c["evidence_level"])
        if key not in merged:
            merged[key] = dict(c)
            merged[key]["evidence"] = list(c["evidence"])
            merged[key]["affected_steps"] = list(c["affected_steps"])
            merged[key]["experiment_refs"] = list(c.get("experiment_refs") or [])
            merged[key]["summaries"] = [c["summary"]]
            order.append(key)
            continue
        target = merged[key]
        for e in c["evidence"]:
            if e not in target["evidence"]:
                target["evidence"].append(e)
        for sid in c["affected_steps"]:
            if sid not in target["affected_steps"]:
                target["affected_steps"].append(sid)
        for r in c.get("experiment_refs") or []:
            if r not in target["experiment_refs"]:
                target["experiment_refs"].append(r)
        if c["summary"] not in target["summaries"]:
            target["summaries"].append(c["summary"])

    out: list[dict[str, Any]] = []
    for key in order:
        item = merged[key]
        summaries = item.pop("summaries", [])
        if len(summaries) > 1:
            item["summary"] = summaries[0] + f"（同类异常共 {len(summaries)} 处）"
            item["related_observations"] = summaries[1:]
        out.append(item)
    return out


def _report(
    *,
    evaluation_run_id: str | None,
    case_id: str,
    trace_id: str | None,
    failed_metrics: list[dict[str, Any]],
    unknown_metrics: list[dict[str, Any]],
    observed_events: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    limitations: list[str],
    dataset_id: str | None = None,
) -> dict[str, Any]:
    candidates = _merge_candidates(candidates)
    attribution_id = f"attr-{uuid.uuid4().hex}"
    confirmed = [c for c in candidates if c["evidence_level"] == "CONFIRMED_EVENT"]
    supported = [c for c in candidates if c["evidence_level"] == "INTERVENTION_SUPPORTED"]

    if len(confirmed) > 1:
        root_status = "MULTI_ANOMALY"
    elif len(confirmed) == 1:
        root_status = "SINGLE_CONFIRMED"
    elif supported:
        root_status = "INTERVENTION_SUPPORTED_ONLY"
    else:
        root_status = "UNKNOWN"

    evidence_refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for c in candidates:
        for e in c["evidence"]:
            key = (e["kind"], e["ref_id"])
            if key in seen:
                continue
            seen.add(key)
            evidence_refs.append(e)
    for ev in observed_events:
        e = ev["evidence_ref"]
        key = (e["kind"], e["ref_id"])
        if key not in seen:
            seen.add(key)
            evidence_refs.append(e)

    levels: dict[str, int] = {}
    for c in candidates:
        levels[c["evidence_level"]] = levels.get(c["evidence_level"], 0) + 1

    affected: list[str] = []
    for c in candidates:
        for sid in c["affected_steps"]:
            if sid not in affected:
                affected.append(sid)

    return {
        "engine_version": ENGINE_VERSION,
        "attribution_id": attribution_id,
        "evaluation_run_id": evaluation_run_id,
        "dataset_id": dataset_id,
        "case_id": case_id,
        "trace_id": trace_id,
        "failed_metrics": failed_metrics,
        "unknown_metrics": unknown_metrics,
        "observed_events": observed_events,
        "candidate_causes": candidates,
        "evidence_refs": evidence_refs,
        "evidence_levels": levels,
        "affected_steps": affected,
        "experiment_refs": [r for c in candidates for r in c.get("experiment_refs") or []],
        "root_cause_status": root_status,
        "ambiguity_notes": _ambiguity_notes(candidates),
        "limitations": limitations,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "analysis_over_recorded_evidence",
        "notes": [
            "候选归因可多条并存，不强行归并为单一根因",
            "evidence_level 表示证据强度，不是概率",
            "UNKNOWN 表示证据不足，不代表无问题",
        ],
    }


def _ambiguity_notes(candidates: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    types = [c["cause_type"] for c in candidates]
    if len(set(types)) > 1:
        notes.append("存在多个不同归因类型，无法在不做干预实验的情况下确定唯一根因")
    if any(c["evidence_level"] == "POTENTIAL_CAUSE" for c in candidates):
        notes.append("包含 POTENTIAL_CAUSE 级结论：由依赖/相关性推断，需实验验证")
    if not any(c["is_conclusive"] for c in candidates):
        notes.append("没有 CONFIRMED_EVENT 级证据，结论仅为候选")
    return notes


# ---------------------------------------------------------------------------
# 批量 / Trace 归因
# ---------------------------------------------------------------------------

def attribute_evaluation_run(
    evaluation_run_id: str,
    *,
    case_ids: list[str] | None = None,
    max_cases: int | None = None,
) -> dict[str, Any]:
    """批量归因一次评测运行中的 Bad Case。"""
    from backend.app.services.evaluation_store import (
        EvaluationNotFoundError,
        get_evaluation_store,
    )

    try:
        run = get_evaluation_store().get(evaluation_run_id)
    except EvaluationNotFoundError as exc:
        raise AttributionError(str(exc)) from exc

    if run.get("status") != "succeeded":
        raise AttributionError(
            f"评测运行状态为 {run.get('status')!r}，无可归因报告"
        )
    report = run.get("result") or run.get("report") or {}
    cases = report.get("case_results") or []

    bad = [c for c in cases if c.get("is_bad_case")]
    if case_ids:
        wanted = set(case_ids)
        known = {c["case_id"] for c in cases}
        unknown = sorted(wanted - known)
        if unknown:
            raise AttributionError(f"未知案例: {unknown[:5]}")
        # 只能在 Bad Case 范围内归因：非 Bad Case 不生成报告（调用方据此返回 409）
        bad = [c for c in bad if c["case_id"] in wanted]

    limit = min(max_cases or MAX_BATCH_CASES, MAX_BATCH_CASES)
    bad = bad[:limit]

    reports: list[dict[str, Any]] = []
    for case in bad:
        rep = attribute_case(
            evaluation_run_id=evaluation_run_id,
            case=case,
            dataset_id=report.get("dataset_id"),
        )
        _persist(rep)
        reports.append(rep)

    levels: dict[str, int] = {}
    types: dict[str, int] = {}
    for r in reports:
        for lv, n in r["evidence_levels"].items():
            levels[lv] = levels.get(lv, 0) + n
        for c in r["candidate_causes"]:
            types[c["cause_type"]] = types.get(c["cause_type"], 0) + 1

    return {
        "engine_version": ENGINE_VERSION,
        "evaluation_run_id": evaluation_run_id,
        "dataset_id": report.get("dataset_id"),
        "bad_case_count": len([c for c in cases if c.get("is_bad_case")]),
        "attributed_count": len(reports),
        "case_ids": [r["case_id"] for r in reports],
        "reports": reports,
        "summary": {
            "evidence_level_counts": levels,
            "cause_type_counts": types,
            "root_cause_status_counts": {
                s: sum(1 for r in reports if r["root_cause_status"] == s)
                for s in {r["root_cause_status"] for r in reports}
            },
        },
        "limitations": [
            "归因证据来自本仓库可核对数据；DAG 依赖不等于因果",
            "未生成无来源的置信概率；请以证据等级解读",
        ],
        "paid_api_called": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def attribute_trace(trace_id: str) -> dict[str, Any]:
    """直接对单个 Trace 归因（无评测上下文时使用）。"""
    try:
        trace = load_trace(trace_id)
    except Exception as exc:
        raise AttributionError(f"Trace 不存在: {trace_id}") from exc

    pseudo_case = {
        "case_id": f"trace-case-{trace_id}",
        "trace_id": trace_id,
        "metrics": {},
        "is_bad_case": None,
    }
    rep = attribute_case(evaluation_run_id=None, case=pseudo_case, dataset_id=None)
    _persist(rep)
    return rep


# ---------------------------------------------------------------------------
# 持久化与查询
# ---------------------------------------------------------------------------

def _persist(report: dict[str, Any]) -> Path:
    ATTRIBUTION_DIR.mkdir(parents=True, exist_ok=True)
    path = ATTRIBUTION_DIR / f"{report['attribution_id']}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def persist_report(report: dict[str, Any]) -> Path:
    """持久化归因报告（公开接口，供失败模式挖掘等模块复用）。"""
    return _persist(report)


def get_report(attribution_id: str) -> dict[str, Any]:
    """按 ID 读取归因报告（持久化查询）。"""
    if not attribution_id.startswith("attr-") or not attribution_id.replace("attr-", "").isalnum():
        raise AttributionError(f"非法归因 ID: {attribution_id!r}")
    path = ATTRIBUTION_DIR / f"{attribution_id}.json"
    if not path.exists():
        raise AttributionError(f"归因报告不存在: {attribution_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_reports(limit: int = 50) -> list[dict[str, Any]]:
    """列出最近归因报告（按生成时间倒序）。"""
    if not ATTRIBUTION_DIR.exists():
        return []
    files = sorted(ATTRIBUTION_DIR.glob("attr-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "attribution_id": r.get("attribution_id"),
            "evaluation_run_id": r.get("evaluation_run_id"),
            "case_id": r.get("case_id"),
            "trace_id": r.get("trace_id"),
            "root_cause_status": r.get("root_cause_status"),
            "candidate_count": len(r.get("candidate_causes") or []),
            "evidence_levels": r.get("evidence_levels"),
            "generated_at": r.get("generated_at"),
        })
    return out
