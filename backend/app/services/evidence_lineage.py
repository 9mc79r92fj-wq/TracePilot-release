"""Evidence Lineage 证据血缘追踪（第 44 步）。

目标：从任一下游产物（评测结果 / 案例 / 实验 / 修复报告 / Trace）出发，
逐层追溯到上游证据：

```
Evaluation Run → Evaluation Case → Trace → Agent Run → Step → Tool Call → Tool Result
Counterfactual Experiment → Source Trace → …
Repair Report → Candidate → Repair Trace (derived) → Source Trace → …
```

设计原则（诚实性优先）：
1. **不伪造关系**：无法解析的关联一律 `status = NOT_AVAILABLE` 并给出原因；
2. **来源与存储分离**：`origin`（REAL_API / MOCK / SIMULATED / UNKNOWN）表示数据从哪来，
   `storage`（RECORDED / MEMORY / NOT_AVAILABLE）表示是否已落盘。
   `RECORDED` 只说明"有保存的历史记录"，**不代表**来自真实 API；
3. **原始来源保留**：派生 Trace 通过 `metadata.derived_from` 指回原始 Trace，
   并携带 `source_data_source`；
4. 只读：本模块不修改任何既有数据。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.app.schemas.trace import TraceRecord
from backend.app.services.trace_store import load_trace

__all__ = [
    "ORIGINS",
    "STORAGE_STATES",
    "classify_origin",
    "classify_storage",
    "trace_node",
    "agent_run_node",
    "step_nodes",
    "build_trace_lineage",
    "build_evaluation_case_lineage",
    "build_experiment_lineage",
    "build_repair_lineage",
    "consistency_check",
    "LineageError",
    "NotFoundEvidence",
]

ENGINE_VERSION = "1.0.0"

# 数据来源维度（从哪来）
ORIGINS = ("REAL_API", "MOCK", "SIMULATED", "UNKNOWN")
# 存储维度（是否已落盘）—— 与来源正交，RECORDED 不代表真实 API
STORAGE_STATES = ("RECORDED", "MEMORY", "NOT_AVAILABLE")


class LineageError(ValueError):
    """血缘查询请求非法。"""


class NotFoundEvidence(LineageError):
    """上游证据不存在。"""


# ---------------------------------------------------------------------------
# 分类器
# ---------------------------------------------------------------------------

def classify_origin(metadata: dict[str, Any] | None) -> tuple[str, list[str]]:
    """判定数据来源（REAL_API / MOCK / SIMULATED / UNKNOWN）并返回判定依据。

    严格依据元数据，**不根据页面名称或 ID 前缀推断**。
    """
    meta = metadata or {}
    evidence: list[str] = []

    ds = meta.get("data_source")
    if ds is not None:
        evidence.append(f"metadata.data_source={ds!r}")
    if "simulated" in meta:
        evidence.append(f"metadata.simulated={meta.get('simulated')!r}")
    if meta.get("simulated_model") is not None:
        evidence.append(f"metadata.simulated_model={meta.get('simulated_model')!r}")

    # 自相矛盾：声称 real_api 却标记 simulated=true → 不可判定，绝不偏向 REAL_API
    if ds == "real_api" and meta.get("simulated") is True:
        evidence.append("data_source=real_api 与 simulated=true 自相矛盾，判为 UNKNOWN")
        return "UNKNOWN", evidence
    if ds == "real_api":
        return "REAL_API", evidence
    if ds == "mock":
        return "MOCK", evidence
    if ds == "simulated":
        return "SIMULATED", evidence
    if meta.get("simulated") is True:
        # 旧数据可能缺少 data_source，但 simulated 明确为真 → 归为 SIMULATED
        evidence.append("缺少 data_source，但 simulated=true，按 SIMULATED 归类")
        return "SIMULATED", evidence
    if ds is None and not evidence:
        return "UNKNOWN", ["元数据缺少 data_source / simulated 字段（无法判定来源）"]
    return "UNKNOWN", evidence


def classify_storage(trace_id: str | None) -> tuple[str, str | None]:
    """判定存储状态：主目录 / 评测目录存在即为 RECORDED。"""
    if not trace_id:
        return "NOT_AVAILABLE", "缺少 trace_id"
    from backend.app.services.trace_store import EVAL_TRACES_DIR, DEFAULT_TRACES_DIR

    for d, label in ((DEFAULT_TRACES_DIR, "data/traces"), (EVAL_TRACES_DIR, "data/evaluation/traces")):
        if (Path(d) / f"{trace_id}.json").exists():
            return "RECORDED", f"已保存于 {label}"
    return "NOT_AVAILABLE", "未找到已保存的 Trace 文件"


def _node(
    node_id: str,
    kind: str,
    label: str,
    *,
    status: str = "AVAILABLE",
    origin: str = "UNKNOWN",
    storage: str = "NOT_AVAILABLE",
    detail: dict[str, Any] | None = None,
    note: str | None = None,
    source_api: str | None = None,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "kind": kind,
        "label": label,
        "status": status,          # AVAILABLE | NOT_AVAILABLE
        "origin": origin,          # REAL_API | MOCK | SIMULATED | UNKNOWN
        "storage": storage,        # RECORDED | MEMORY | NOT_AVAILABLE
        "detail": detail or {},
        "note": note,
        "source_api": source_api,
    }


def _unavailable(node_id: str, kind: str, label: str, reason: str, *, source_api: str | None = None) -> dict[str, Any]:
    return _node(node_id, kind, label, status="NOT_AVAILABLE", note=reason, source_api=source_api)


# ---------------------------------------------------------------------------
# Trace / Run / Step
# ---------------------------------------------------------------------------

def trace_node(trace: TraceRecord) -> dict[str, Any]:
    origin, why = classify_origin(trace.metadata)
    storage, storage_note = classify_storage(trace.trace_id)
    meta = trace.metadata or {}

    detail: dict[str, Any] = {
        "trace_id": trace.trace_id,
        "task_id": trace.task_id,
        "run_id": trace.run_id,
        "status": trace.status.value,
        "step_count": len(trace.steps),
        "origin_evidence": why,
        "storage_note": storage_note,
        "model": meta.get("model"),
        "tool_call_count": meta.get("tool_call_count"),
    }
    # 派生 Trace：保留原始来源
    if meta.get("derived_from"):
        detail["derived_from"] = meta["derived_from"]
        detail["derivation"] = meta.get("derivation")
        detail["source_data_source"] = meta.get("source_data_source")

    return _node(
        trace.trace_id, "trace", f"Trace {trace.trace_id}",
        origin=origin, storage=storage, detail=detail,
        note=meta.get("lineage_note"),
        source_api=f"/api/traces/{trace.trace_id}",
    )


def agent_run_node(trace: TraceRecord) -> dict[str, Any]:
    """Agent Run 记录（内存登记表）；Trace 已落盘但 Run 记录可能已随重启丢失。"""
    run_id = trace.run_id
    if not run_id:
        return _unavailable("agent-run-NOT_AVAILABLE", "agent_run", "Agent Run", "Trace 未记录 run_id")

    try:
        from backend.app.services.agent_runner import AgentRunError, get_run

        run = get_run(run_id)
        return _node(
            run_id, "agent_run", f"Agent Run {run_id}",
            origin=classify_origin({"data_source": run.data_source, "simulated": run.simulated})[0],
            storage="MEMORY",
            detail={
                "run_id": run_id,
                "task": run.task,
                "mode": run.mode,
                "status": run.status,
                "model": run.model,
                "elapsed_ms": run.elapsed_ms,
                "token_usage": run.token_usage,
                "trace_id": run.trace_id,
                "limit_violations": run.limit_violations,
            },
            note="运行登记表为进程内存储（重启后丢失），Trace 本身已落盘",
            source_api=f"/api/agent/runs/{run_id}",
        )
    except Exception:
        return _unavailable(
            run_id, "agent_run", f"Agent Run {run_id}",
            "运行记录不在当前进程登记表中（可能由评测/重放生成，或后端已重启）；Trace 仍可追溯",
            source_api=f"/api/agent/runs/{run_id}",
        )


def step_nodes(trace: TraceRecord) -> list[dict[str, Any]]:
    """步骤节点；tool_call 步骤附带工具调用与结果节点。"""
    origin, _why = classify_origin(trace.metadata)
    storage, _ = classify_storage(trace.trace_id)
    out: list[dict[str, Any]] = []

    for s in trace.steps:
        detail: dict[str, Any] = {
            "step_id": s.step_id,
            "step_type": s.step_type.value,
            "status": s.status.value,
            "parent_step_ids": list(s.parent_step_ids),
        }
        if s.error:
            detail["error"] = s.error.model_dump() if hasattr(s.error, "model_dump") else s.error
        if s.token_usage:
            detail["token_usage"] = s.token_usage.model_dump()

        out.append(_node(
            s.step_id, "step", f"{s.step_type.value} · {s.step_id}",
            origin=origin, storage=storage, detail=detail,
        ))

        if s.step_type.value == "tool_call":
            out.append(_node(
                f"{s.step_id}-tool", "tool_call", f"工具调用 {s.tool_name}",
                origin=origin, storage=storage,
                detail={
                    "tool_name": s.tool_name,
                    "tool_arguments": s.tool_arguments,
                    "step_id": s.step_id,
                },
            ))
            if s.tool_result is not None:
                out.append(_node(
                    f"{s.step_id}-result", "tool_result", "工具结果",
                    origin=origin, storage=storage,
                    detail={"tool_name": s.tool_name, "tool_result": s.tool_result},
                ))
            else:
                out.append(_unavailable(
                    f"{s.step_id}-result", "tool_result", "工具结果",
                    "该 tool_call 未记录 tool_result",
                ))
    return out


# ---------------------------------------------------------------------------
# 血缘图构造
# ---------------------------------------------------------------------------

def _edges() -> list[dict[str, str]]:
    return []


def _edge(edges: list[dict[str, str]], src: str, dst: str, relation: str) -> None:
    edges.append({"from": src, "to": dst, "relation": relation})


def build_trace_lineage(trace_id: str) -> dict[str, Any]:
    """以 Trace 为根的完整血缘（Trace → Run → Step → Tool）。"""
    try:
        trace = load_trace(trace_id)
    except Exception as exc:
        raise NotFoundEvidence(f"Trace 不存在: {trace_id}") from exc

    nodes: list[dict[str, Any]] = [trace_node(trace)]
    edges: list[dict[str, str]] = []

    run = agent_run_node(trace)
    nodes.append(run)
    if run["status"] == "AVAILABLE":
        _edge(edges, trace.trace_id, run["node_id"], "executed_by")

    steps = step_nodes(trace)
    nodes.extend(steps)
    for s in steps:
        if s["kind"] in {"step", "tool_call", "tool_result"}:
            _edge(edges, trace.trace_id, s["node_id"], "contains")
        if s["kind"] == "tool_result":
            _edge(edges, s["node_id"].replace("-result", "-tool"), s["node_id"], "produced")

    return _finalize("trace", trace_id, nodes, edges, root=trace)


def build_evaluation_case_lineage(evaluation_run_id: str, case_id: str) -> dict[str, Any]:
    """评测案例血缘：Evaluation Run → Case → Trace → Run → Step → Tool。"""
    from backend.app.services.evaluation_store import (
        EvaluationNotFoundError,
        get_evaluation_store,
    )

    try:
        run = get_evaluation_store().get(evaluation_run_id)
    except EvaluationNotFoundError as exc:
        raise NotFoundEvidence(str(exc)) from exc

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    report = run.get("result") if "result" in run else run.get("report")
    set_ok = run.get("status") == "succeeded" and report
    nodes.append(_node(
        evaluation_run_id, "evaluation_run", f"评测运行 {evaluation_run_id}",
        origin="SIMULATED" if set_ok else "UNKNOWN",
        storage="MEMORY",
        detail={
            "run_id": evaluation_run_id,
            "status": run.get("status"),
            "dataset_id": run.get("dataset_id"),
            "status_note": "评测运行登记表为进程内存储；Trace 与案例结果已随响应返回并可追溯",
        },
        source_api=f"/api/evaluation/runs/{evaluation_run_id}",
    ))

    if not set_ok:
        nodes.append(_unavailable(
            f"{evaluation_run_id}-report", "evaluation_report", "评测报告",
            f"运行状态为 {run.get('status')!r}，无可追溯报告",
        ))
        return _finalize("evaluation_case", f"{evaluation_run_id}/{case_id}", nodes, edges)

    internal_run_id = report.get("evaluation_run_id")
    # 一致性：报告内 run_id 必须与请求一致
    if internal_run_id is not None and internal_run_id != evaluation_run_id:
        nodes.append(_unavailable(
            f"{evaluation_run_id}-inconsistent", "consistency_warning", "ID 一致性告警",
            f"报告 evaluation_run_id={internal_run_id} 与查询 ID 不一致",
        ))

    case = next((c for c in report.get("case_results", []) if c.get("case_id") == case_id), None)
    if case is None:
        nodes.append(_unavailable(
            f"{case_id}-case", "evaluation_case", f"评测案例 {case_id}",
            "该评测运行中不存在此案例",
        ))
        return _finalize("evaluation_case", f"{evaluation_run_id}/{case_id}", nodes, edges)

    case_origin, case_why = classify_origin({
        "data_source": case.get("data_source"),
        "simulated": True,
    })
    nodes.append(_node(
        case_id, "evaluation_case", f"评测案例 {case_id}",
        origin=case_origin, storage="RECORDED",
        detail={
            "case_id": case_id,
            "scenario_type": case.get("scenario_type"),
            "overall_status": case.get("overall_status"),
            "is_bad_case": case.get("is_bad_case"),
            "data_source": case.get("data_source"),
            "origin_evidence": case_why,
            "has_judge": bool(case.get("judge")),
        },
        source_api=f"/api/evaluation/runs/{evaluation_run_id}/cases/{case_id}",
    ))
    _edge(edges, evaluation_run_id, case_id, "contains_case")

    trace_id = case.get("trace_id")
    if not trace_id:
        nodes.append(_unavailable(f"{case_id}-trace", "trace", "案例 Trace", "案例未关联 trace_id"))
        return _finalize("evaluation_case", f"{evaluation_run_id}/{case_id}", nodes, edges)

    try:
        trace = load_trace(trace_id)
    except Exception:
        nodes.append(_unavailable(
            trace_id, "trace", f"Trace {trace_id}",
            "案例声明了 trace_id，但未找到已保存的 Trace 文件（关联断裂）",
        ))
        return _finalize("evaluation_case", f"{evaluation_run_id}/{case_id}", nodes, edges)

    tnode = trace_node(trace)
    nodes.append(tnode)
    _edge(edges, case_id, trace_id, "evaluated_trace")

    run_node = agent_run_node(trace)
    nodes.append(run_node)
    if run_node["status"] == "AVAILABLE":
        _edge(edges, trace_id, run_node["node_id"], "executed_by")

    steps = step_nodes(trace)
    nodes.extend(steps)
    for s in steps:
        _edge(edges, trace_id, s["node_id"], "contains")
        if s["kind"] == "tool_result":
            _edge(edges, s["node_id"].replace("-result", "-tool"), s["node_id"], "produced")

    return _finalize("evaluation_case", f"{evaluation_run_id}/{case_id}", nodes, edges)


def build_experiment_lineage(experiment_id: str) -> dict[str, Any]:
    """反事实实验血缘：Experiment → Source Trace / Variant Trace → Step。"""
    from backend.app.services.experiment_store import (
        ExperimentNotFoundError,
        get_store,
    )

    try:
        exp = get_store().get(experiment_id)
    except ExperimentNotFoundError as exc:
        raise NotFoundEvidence(str(exc)) from exc

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    nodes.append(_node(
        experiment_id, "experiment", f"反事实实验 {experiment_id}",
        origin="SIMULATED", storage="MEMORY",
        detail={
            "experiment_id": experiment_id,
            "status": exp.get("status"),
            "intervention": exp.get("intervention"),
            "scenario": exp.get("scenario"),
            "replays": exp.get("replays"),
            "source_trace_id": exp.get("source_trace_id"),
            "elapsed_ms": exp.get("elapsed_ms"),
            "storage_note": "实验登记表为进程内存储；结果随响应返回",
        },
        source_api=f"/api/counterfactual/experiments/{experiment_id}",
    ))

    source_trace_id = exp.get("source_trace_id")
    result = exp.get("result") or {}
    if result.get("experiment_id") and result["experiment_id"] != experiment_id:
        nodes.append(_unavailable(
            f"{experiment_id}-id-warning", "consistency_warning", "实验 ID 一致性告警",
            "引擎结果 experiment_id 与登记表不一致",
        ))

    if source_trace_id:
        try:
            src = load_trace(source_trace_id)
            nodes.append(trace_node(src))
            _edge(edges, experiment_id, source_trace_id, "based_on")
        except Exception:
            nodes.append(_unavailable(
                source_trace_id, "trace", f"原始 Trace {source_trace_id}", "原始 Trace 文件缺失")
            )
    else:
        nodes.append(_unavailable(
            f"{experiment_id}-source", "trace", "原始 Trace", "实验未记录 source_trace_id"))

    variant_tid = (result.get("intervention_outcome") or {}).get("trace_id")
    if variant_tid:
        try:
            variant = load_trace(variant_tid)
            vnode = trace_node(variant)
            nodes.append(vnode)
            _edge(edges, experiment_id, variant_tid, "produced_variant")
            if source_trace_id:
                _edge(edges, variant_tid, source_trace_id, "derived_from")
        except Exception:
            nodes.append(_unavailable(
                variant_tid, "trace", f"变体 Trace {variant_tid}",
                "变体 Trace 未落盘（历史实验），无法查询",
            ))
    else:
        nodes.append(_unavailable(
            f"{experiment_id}-variant", "trace", "变体 Trace", "结果未记录干预后 trace_id"))

    return _finalize("experiment", experiment_id, nodes, edges)


def build_repair_lineage(report_id: str) -> dict[str, Any]:
    """修复报告血缘：Report → Candidate → Repair Trace → Source Trace。"""
    from backend.app.services.repair_store import (
        RepairNotFoundError,
        get_repair_store,
    )

    try:
        rep = get_repair_store().get(report_id)
    except RepairNotFoundError as exc:
        raise NotFoundEvidence(str(exc)) from exc

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    nodes.append(_node(
        report_id, "repair_report", f"修复报告 {report_id}",
        origin="SIMULATED", storage="MEMORY",
        detail={
            "report_id": report_id,
            "status": rep.get("status"),
            "source_trace_id": rep.get("source_trace_id"),
            "candidate_count": len((rep.get("result") or {}).get("candidates") or []),
            "storage_note": "修复报告登记表为进程内存储；候选 Trace 已落盘",
        },
        source_api=f"/api/repairs/reports/{report_id}",
    ))

    src_id = rep.get("source_trace_id")
    if src_id:
        try:
            nodes.append(trace_node(load_trace(src_id)))
            _edge(edges, report_id, src_id, "based_on")
        except Exception:
            nodes.append(_unavailable(src_id, "trace", f"原始 Trace {src_id}", "原始 Trace 文件缺失"))
    else:
        nodes.append(_unavailable(f"{report_id}-source", "trace", "原始 Trace", "报告未记录 source_trace_id"))

    result = rep.get("result") or {}
    for i, cand in enumerate((result.get("candidates") or [])[:5]):
        rid = cand.get("repair_id") or f"{report_id}-cand{i}"
        nodes.append(_node(
            f"{report_id}::{rid}", "repair_candidate", f"候选 {cand.get('repair_type', rid)}",
            origin="SIMULATED", storage="MEMORY",
            detail={
                "repair_id": cand.get("repair_id"),
                "repair_type": cand.get("repair_type"),
                "verification_status": cand.get("verification_status"),
                "target_step_id": cand.get("target_step_id"),
                "rank": cand.get("rank"),
                "experiment_id": cand.get("experiment_id"),
                "repair_trace_id": cand.get("repair_trace_id"),
            },
        ))
        _edge(edges, report_id, f"{report_id}::{rid}", "contains_candidate")

        rt = cand.get("repair_trace_id")
        if rt:
            try:
                rnode = trace_node(load_trace(rt))
                nodes.append(rnode)
                _edge(edges, f"{report_id}::{rid}", rt, "validated_by_trace")
                if src_id:
                    _edge(edges, rt, src_id, "derived_from")
            except Exception:
                nodes.append(_unavailable(
                    rt, "trace", f"修复 Trace {rt}",
                    "修复验证 Trace 未落盘（历史记录），无法查询",
                ))
        else:
            nodes.append(_unavailable(
                f"{report_id}::{rid}-trace", "trace", "修复 Trace",
                "该候选未产生修复验证 Trace",
            ))

    return _finalize("repair_report", report_id, nodes, edges)


# ---------------------------------------------------------------------------
# 汇总与一致性检查
# ---------------------------------------------------------------------------

def _finalize(kind: str, root_id: str, nodes: list[dict[str, Any]], edges: list[dict[str, str]], *, root: TraceRecord | None = None) -> dict[str, Any]:
    available = [n for n in nodes if n["status"] == "AVAILABLE"]
    missing = [n for n in nodes if n["status"] == "NOT_AVAILABLE"]

    origins: dict[str, int] = {}
    for n in nodes:
        origins[n["origin"]] = origins.get(n["origin"], 0) + 1

    return {
        "lineage_engine_version": ENGINE_VERSION,
        "root_kind": kind,
        "root_id": root_id,
        "nodes": nodes,
        "edges": edges,
        "summary": {
            "node_count": len(nodes),
            "available_count": len(available),
            "not_available_count": len(missing),
            "origin_counts": origins,
            "has_real_api_evidence": "REAL_API" in origins,
            "has_missing_evidence": bool(missing),
        },
        "origin_vocabulary": list(ORIGINS),
        "storage_vocabulary": list(STORAGE_STATES),
        "notes": [
            "origin 表示数据来源（REAL_API / MOCK / SIMULATED / UNKNOWN），严格依据元数据判定，不依据页面名称或 ID 前缀",
            "storage 表示是否已落盘（RECORDED / MEMORY / NOT_AVAILABLE）；RECORDED 不代表来自真实 API",
            "不可追溯的关联显示为 NOT_AVAILABLE 并给出原因，绝不伪造关系",
        ],
        "trace": root.model_dump(mode="json") if root is not None else None,
    }


def consistency_check(trace_ids: list[str] | None = None, *, limit: int = 200) -> dict[str, Any]:
    """只读一致性检查：step/trace ID 一致、DAG 节点一致、派生来源可解析。"""
    from backend.app.services.trace_dag import build_dag
    from backend.app.services.trace_store import list_traces

    recs = list_traces()
    if trace_ids:
        wanted = set(trace_ids)
        recs = [r for r in recs if r.trace_id in wanted]
    recs = recs[:limit]

    issues: list[dict[str, Any]] = []
    checked = 0

    for t in recs:
        checked += 1
        # 1) step.trace_id 必须等于 trace.trace_id
        for s in t.steps:
            if s.trace_id != t.trace_id:
                issues.append({
                    "trace_id": t.trace_id, "kind": "step_trace_id_mismatch",
                    "detail": f"step {s.step_id} 的 trace_id={s.trace_id} 与所属 Trace 不一致",
                })
        # 2) step_id 唯一
        ids = [s.step_id for s in t.steps]
        if len(ids) != len(set(ids)):
            issues.append({"trace_id": t.trace_id, "kind": "duplicate_step_id", "detail": "存在重复 step_id"})
        # 3) DAG 节点集合 == step 集合
        try:
            dag = build_dag(t)
            node_ids = {n["step_id"] for n in dag.to_dict()["nodes"]}
            if node_ids != set(ids):
                issues.append({
                    "trace_id": t.trace_id, "kind": "dag_step_mismatch",
                    "detail": f"DAG 节点 {len(node_ids)} 与步骤 {len(ids)} 不一致",
                })
        except Exception as exc:
            issues.append({"trace_id": t.trace_id, "kind": "dag_build_error", "detail": str(exc)})
        # 4) 派生来源可解析
        meta = t.metadata or {}
        derived_from = meta.get("derived_from")
        if derived_from:
            if not any(r.trace_id == derived_from for r in list_traces()):
                issues.append({
                    "trace_id": t.trace_id, "kind": "broken_derived_from",
                    "detail": f"derived_from={derived_from} 指向的 Trace 不存在",
                })
            if meta.get("data_source") == "real_api":
                issues.append({
                    "trace_id": t.trace_id, "kind": "derived_marked_real_api",
                    "detail": "派生 Trace 被错误标记为 real_api",
                })

    known = {"step_trace_id_mismatch", "duplicate_step_id", "dag_step_mismatch",
             "dag_build_error", "broken_derived_from", "derived_marked_real_api"}
    return {
        "lineage_engine_version": ENGINE_VERSION,
        "checked_traces": checked,
        "issue_count": len(issues),
        "issues": issues,
        "issue_kinds": sorted(known),
        "ok": not issues,
        "note": "只读检查：不修改任何 Trace 文件；派生 Trace 必须保留 derived_from 且不得标记为 real_api",
    }
