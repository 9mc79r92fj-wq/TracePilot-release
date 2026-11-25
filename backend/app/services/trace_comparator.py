"""Trace 对比分析服务（第 22 步）。

对两份 Agent 执行轨迹做结构化对比，输出可解释的 JSON 报告。

关键设计：
- 步骤匹配不依赖 step_id（两次运行的 step_id 必然不同），而采用可解释的
  匹配键 = (step_type, tool_name) + 该键在拓扑顺序中的出现次序；
  无法可靠匹配的步骤标记为 unmatched，绝不伪造匹配；
- 未采集到的 Token / 耗时输出为 unknown（null），不擅自填 0；
- 报告严格区分三类结论：观察到的差异 / 推测的故障影响 / 尚未验证的因果关系；
- 时间上最早的差异仅作为"最早可确认差异"输出，并显式声明"不等于根因"。
"""

from __future__ import annotations

from typing import Any

from backend.app.schemas.trace import TraceRecord, TraceStep
from backend.app.services.trace_dag import build_dag

__all__ = ["compare_traces", "compare_trace_files", "TraceComparator"]


# ---------------------------------------------------------------------------
# 基础度量
# ---------------------------------------------------------------------------

def _total_duration_ms(record: TraceRecord) -> int | None:
    """整条 Trace 的墙钟耗时；缺少起止时间返回 None。"""
    if record.started_at is None or record.ended_at is None:
        return None
    return int((record.ended_at - record.started_at).total_seconds() * 1000)


def _token_summary(record: TraceRecord) -> dict[str, Any]:
    """Token 汇总：仅累加已采集的值，任何缺失不填 0，以 has_unknown 标注。"""
    values = [
        s.token_usage.total_tokens
        for s in record.steps
        if s.token_usage is not None
    ]
    has_unknown = any(
        s.token_usage is None and s.step_type.value == "model_call"
        for s in record.steps
    )
    return {"sum": sum(values) if values else None, "has_unknown": has_unknown}


def _error_type(step: TraceStep) -> str | None:
    return step.error.error_type if step.error is not None else None


def _fault_info(step: TraceStep) -> Any:
    return step.metadata.get("fault_injection")


# ---------------------------------------------------------------------------
# 步骤匹配
# ---------------------------------------------------------------------------

def _step_key(step: TraceStep) -> tuple[str, str]:
    """匹配键 = (step_type, tool_name)。tool_call 用工具名区分，其余用类型。"""
    return (step.step_type.value, step.tool_name or "")


def _topo_steps(record: TraceRecord) -> tuple[list[TraceStep], Any]:
    """按 DAG 拓扑顺序返回步骤列表与 DAG 对象。"""
    dag = build_dag(record)
    by_id = {s.step_id: s for s in record.steps}
    return [by_id[sid] for sid in dag.topological_order], dag


def _match_steps(ref: list[TraceStep], tgt: list[TraceStep]):
    """按 (step_type, tool_name) + 出现次序配对；多余步骤标为 unmatched。"""
    ref_groups: dict[tuple[str, str], list[TraceStep]] = {}
    tgt_groups: dict[tuple[str, str], list[TraceStep]] = {}
    for s in ref:
        ref_groups.setdefault(_step_key(s), []).append(s)
    for s in tgt:
        tgt_groups.setdefault(_step_key(s), []).append(s)

    matches: list[tuple[TraceStep, TraceStep]] = []
    unmatched_ref: list[TraceStep] = []
    unmatched_tgt: list[TraceStep] = []

    for key in sorted(set(ref_groups) | set(tgt_groups)):
        rl = ref_groups.get(key, [])
        tl = tgt_groups.get(key, [])
        for i in range(min(len(rl), len(tl))):
            matches.append((rl[i], tl[i]))
        unmatched_ref.extend(rl[len(tl):])
        unmatched_tgt.extend(tl[len(rl):])
    return matches, unmatched_ref, unmatched_tgt


# ---------------------------------------------------------------------------
# 步骤差异
# ---------------------------------------------------------------------------

def _step_diff(a: TraceStep, b: TraceStep) -> dict[str, Any]:
    """单步骤语义差异（status / error_type / tool_result / fault_injection）。"""
    diff: dict[str, Any] = {}
    if a.status.value != b.status.value:
        diff["status"] = {"ref": a.status.value, "tgt": b.status.value}
    if _error_type(a) != _error_type(b):
        diff["error_type"] = {"ref": _error_type(a), "tgt": _error_type(b)}
    if a.step_type.value == "tool_call" and a.tool_result != b.tool_result:
        diff["tool_result"] = {"ref": a.tool_result, "tgt": b.tool_result}
    if bool(_fault_info(a)) != bool(_fault_info(b)) or _fault_info(a) != _fault_info(b):
        diff["fault_injection"] = {"ref": _fault_info(a), "tgt": _fault_info(b)}
    return diff


# ---------------------------------------------------------------------------
# 主对比
# ---------------------------------------------------------------------------

class TraceComparator:
    """两份 Trace 的结构化对比器。"""

    def compare(self, reference: TraceRecord, target: TraceRecord) -> dict[str, Any]:
        ref_steps, ref_dag = _topo_steps(reference)
        tgt_steps, tgt_dag = _topo_steps(target)

        matches, unmatched_ref, unmatched_tgt = _match_steps(ref_steps, tgt_steps)

        # 逐步骤差异
        step_rows: list[dict[str, Any]] = []
        diff_nodes: list[dict[str, Any]] = []
        earliest: dict[str, Any] | None = None
        observations: list[str] = []
        inferred: list[str] = []

        for a, b in matches:
            d = _step_diff(a, b)
            key = "|".join(_step_key(a))
            row = {
                "match_key": key,
                "ref_step_id": a.step_id,
                "tgt_step_id": b.step_id,
                "differences": d or None,
            }
            step_rows.append(row)
            if d:
                node = {
                    "step_id": b.step_id,
                    "ref_step_id": a.step_id,
                    "match_key": key,
                    "differences": d,
                    "upstream": tgt_dag.ancestors(b.step_id) if b.step_id in tgt_dag.topological_order else [],
                    "downstream": tgt_dag.descendants(b.step_id) if b.step_id in tgt_dag.topological_order else [],
                }
                diff_nodes.append(node)
                if earliest is None:
                    earliest = {
                        "step_id": b.step_id,
                        "kind": list(d.keys()),
                        "note": "最早可确认的执行差异（观察结果，不构成根因结论）",
                    }
                observations.append(f"步骤 {b.step_id} 出现差异: {list(d.keys())}")
                if d.get("fault_injection") and d["fault_injection"].get("tgt"):
                    fi = d["fault_injection"]["tgt"]
                    inferred.append(
                        f"目标 Trace 在 {b.step_id} 注入 {fi.get('type')} 故障，"
                        f"导致该步骤执行失败（observed，非因果结论）"
                    )

        # 未匹配步骤
        for s in unmatched_ref:
            step_rows.append({"match_key": _step_key(s)[0], "ref_step_id": s.step_id,
                              "tgt_step_id": None, "differences": None, "status": "unmatched_ref"})
        for s in unmatched_tgt:
            step_rows.append({"match_key": _step_key(s)[0], "ref_step_id": None,
                              "tgt_step_id": s.step_id, "differences": None, "status": "unmatched_tgt"})

        # Trace 级指标
        final_status_diff = None
        if reference.status.value != target.status.value:
            final_status_diff = {"ref": reference.status.value, "tgt": target.status.value}

        ref_tokens = _token_summary(reference)
        tgt_tokens = _token_summary(target)
        ref_dur = _total_duration_ms(reference)
        tgt_dur = _total_duration_ms(target)

        ref_faults = [f.get("fault_type") for f in reference.metadata.get("fault_injections", [])]
        tgt_faults = [f.get("fault_type") for f in target.metadata.get("fault_injections", [])]

        ref_recovery = reference.metadata.get("recovery_observed")
        tgt_recovery = target.metadata.get("recovery_observed")

        report: dict[str, Any] = {
            "traces": {
                "reference": {
                    "trace_id": reference.trace_id,
                    "status": reference.status.value,
                    "step_count": len(reference.steps),
                    "total_duration_ms": ref_dur,
                    "token_usage": ref_tokens,
                    "fault_types": ref_faults,
                    "recovery_observed": ref_recovery,
                },
                "target": {
                    "trace_id": target.trace_id,
                    "status": target.status.value,
                    "step_count": len(target.steps),
                    "total_duration_ms": tgt_dur,
                    "token_usage": tgt_tokens,
                    "fault_types": tgt_faults,
                    "recovery_observed": tgt_recovery,
                },
            },
            "differences": {
                "final_status": final_status_diff,
                "step_count": {"ref": len(reference.steps), "tgt": len(target.steps)},
                "total_duration_ms": {"ref": ref_dur, "tgt": tgt_dur},
                "token_usage": {"ref": ref_tokens, "tgt": tgt_tokens},
                "fault_types": {"ref": ref_faults, "tgt": tgt_faults},
                "recovery_observed": {"ref": ref_recovery, "tgt": tgt_recovery},
            },
            "step_matches": step_rows,
            "unmatched": {
                "reference_only": [s.step_id for s in unmatched_ref],
                "target_only": [s.step_id for s in unmatched_tgt],
            },
            "diff_nodes": diff_nodes,
            "earliest_difference": earliest,
            "observations": observations or ["两份 Trace 未观察到语义差异"],
            "inferred_impacts": inferred or ["未观察到可推测的故障影响"],
            "unverified": [
                "最早差异为时间上最先出现的观察差异，不等于根因；"
                "本对比为相关性观察，因果需通过反事实干预实验验证。"
            ],
        }
        return report


def compare_traces(reference: TraceRecord, target: TraceRecord) -> dict[str, Any]:
    """对比两份 TraceRecord，返回结构化 JSON 报告（dict）。"""
    return TraceComparator().compare(reference, target)


def compare_trace_files(ref_path: str, tgt_path: str) -> dict[str, Any]:
    """读取并用 Trace Schema 校验两份 Trace JSON 后对比。"""
    ref = TraceRecord.model_validate_json(_read(ref_path))
    tgt = TraceRecord.model_validate_json(_read(tgt_path))
    return compare_traces(ref, tgt)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()
