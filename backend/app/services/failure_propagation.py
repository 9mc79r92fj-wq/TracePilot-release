"""故障传播路径分析服务（第 23 步）。

基于 DAG 依赖关系 + TraceComparator 对比结果，分析故障节点的传播范围，
输出供 React Flow 直接使用的结构化 JSON（节点 / 边 / 路径 / 状态分类）。

节点状态分类（严格区分证据强度）：
- confirmed_fault       已确认故障节点（metadata.fault_injection.injected 为真）；
- observed_difference   与正常 Trace 对比后实际观察到差异的节点（非故障节点）；
- potentially_affected  故障节点的 DAG 后代（仅依赖可达性推断，无观察差异证据）；
- unaffected            与正常 Trace 无差异且不在故障传播范围内的节点；
- unknown               无法匹配或缺乏证据的节点。

重要边界：
- 依赖关系（可达性）不等同于因果证明，potentially_affected 仅表示"可能受影响"；
- 不将没有观察证据的节点标记为"实际受影响"；
- 无法与正常 Trace 匹配的步骤一律标记 unknown，绝不伪造。
"""

from __future__ import annotations

from typing import Any

from backend.app.schemas.trace import TraceRecord
from backend.app.services.trace_comparator import TraceComparator
from backend.app.services.trace_dag import build_dag

__all__ = ["analyze_failure_propagation", "CLASSIFICATIONS"]

CLASSIFICATIONS = (
    "confirmed_fault",
    "observed_difference",
    "potentially_affected",
    "unaffected",
    "unknown",
)


def _confirmed_fault_ids(record: TraceRecord) -> dict[str, Any]:
    """识别已确认故障节点（含注入标记的步骤）。"""
    faults: dict[str, Any] = {}
    for s in record.steps:
        fi = s.metadata.get("fault_injection")
        if fi and fi.get("injected") is True:
            faults[s.step_id] = fi
    return faults


def _adjacency(record: TraceRecord) -> tuple[dict[str, list[str]], set[str]]:
    """构建父->子邻接与叶节点集合。"""
    children: dict[str, list[str]] = {s.step_id: [] for s in record.steps}
    for s in record.steps:
        for pid in s.parent_step_ids:
            children.setdefault(pid, []).append(s.step_id)
    leaves = {sid for sid, ch in children.items() if not ch}
    return children, leaves


def _descendants(children: dict[str, list[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = list(children.get(start, []))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(children.get(node, []))
    return seen


def _paths_from(children: dict[str, list[str]], start: str, leaves: set[str]) -> list[list[str]]:
    """枚举从 start 到各叶节点的全部依赖路径。"""
    results: list[list[str]] = []

    def dfs(node: str, path: list[str]) -> None:
        if node in leaves:
            results.append(path + [node])
            return
        nxt = children.get(node, [])
        if not nxt:  # 孤立无子节点（非叶却无下游）也终止
            results.append(path + [node])
            return
        for child in nxt:
            dfs(child, path + [node])

    dfs(start, [])
    return results


def analyze_failure_propagation(
    fault_trace: TraceRecord,
    normal_trace: TraceRecord | None = None,
) -> dict[str, Any]:
    """分析故障 Trace 的传播路径。

    Args:
        fault_trace: 含故障的执行轨迹。
        normal_trace: 可选，同一任务的无故障基线 Trace，用于识别实际差异。

    Returns:
        结构化 JSON（dict）：nodes / edges / paths / summary / note。
    """
    dag = build_dag(fault_trace)
    children, leaves = _adjacency(fault_trace)
    faults = _confirmed_fault_ids(fault_trace)

    observed_diff_ids: set[str] = set()
    unmatched_ids: set[str] = set()
    has_normal = normal_trace is not None
    if has_normal:
        report = TraceComparator().compare(normal_trace, fault_trace)
        observed_diff_ids = {n["step_id"] for n in report["diff_nodes"]}
        unmatched_ids = set(report["unmatched"]["target_only"])

    # 故障节点的全部后代（并集）
    descendants_of_fault: set[str] = set()
    for fid in faults:
        descendants_of_fault |= _descendants(children, fid)

    # 节点分类
    classification: dict[str, str] = {}
    for sid in dag.topological_order:
        if sid in faults:
            classification[sid] = "confirmed_fault"
        elif sid in unmatched_ids:
            classification[sid] = "unknown"
        elif sid in observed_diff_ids:
            classification[sid] = "observed_difference"
        elif sid in descendants_of_fault:
            classification[sid] = "potentially_affected"
        elif has_normal:
            classification[sid] = "unaffected"
        else:
            classification[sid] = "unknown"

    # 节点输出（React Flow）
    by_id = {s.step_id: s for s in fault_trace.steps}
    nodes = [
        {
            "id": sid,
            "step_type": by_id[sid].step_type.value,
            "classification": classification[sid],
            "is_fault": sid in faults,
            "fault_type": faults[sid].get("type") if sid in faults else None,
            "observed_difference": sid in observed_diff_ids,
        }
        for sid in dag.topological_order
    ]

    # 边输出
    edges = [
        {"source": pid, "target": s.step_id}
        for s in fault_trace.steps
        for pid in s.parent_step_ids
    ]

    # 传播路径（故障节点 -> 各叶节点）
    paths: list[dict[str, Any]] = []
    for fid in faults:
        for path in _paths_from(children, fid, leaves):
            paths.append({"fault_node": fid, "terminal": path[-1], "path": path})

    summary = {
        "total_nodes": len(nodes),
        "confirmed_fault": [sid for sid, c in classification.items() if c == "confirmed_fault"],
        "observed_difference": [sid for sid, c in classification.items() if c == "observed_difference"],
        "potentially_affected": [sid for sid, c in classification.items() if c == "potentially_affected"],
        "unaffected": [sid for sid, c in classification.items() if c == "unaffected"],
        "unknown": [sid for sid, c in classification.items() if c == "unknown"],
    }

    return {
        "nodes": nodes,
        "edges": edges,
        "paths": paths,
        "summary": summary,
        "note": (
            "依赖关系（DAG 可达性）不等同于因果证明；potentially_affected 仅为"
            "推断的传播范围，observed_difference 才是观察到的实际差异。"
        ),
    }
