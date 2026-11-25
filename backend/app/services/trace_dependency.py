"""Trace 依赖关系解析服务（第 10 步）。

将 TraceRecord 中的执行步骤解析为标准依赖关系（节点 / 边 / 根 / 叶），
为后续 DAG 构建、故障归因与反事实实验提供可靠数据。

规则：
- 节点 = TraceStep（step_id 在 trace 内唯一）；
- 有向边 = parent_step_ids 中的每个父节点 -> 本步骤（父指向子）；
- 根节点 = parent_step_ids 为空的步骤；
- 叶节点 = 没有任何子节点的步骤。

支持的结构：
- 串行执行（链式依赖）；
- 并行执行（同一父节点多个子节点 / 多个根节点）；
- 多父节点（parent_step_ids 长度 > 1）；
- 分支汇合（fan-in，多个分支收敛到同一子节点）。

异常检测（抛出 DependencyError 子类，附清晰错误信息）：
- 重复 step_id -> DuplicateStepIdError；
- 引用不存在的父节点 -> MissingParentError；
- 自引用（parent_step_ids 含自身）-> SelfReferenceError；
- 循环依赖 -> CycleError（错误信息包含实际环路路径）。

解析结果与步骤在 JSON 数组中的排列顺序无关：输出节点 / 边 / 根 / 叶 /
拓扑顺序均基于依赖关系本身推导，并采用确定性排序（按 step_id 字典序）。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from backend.app.schemas.trace import TraceRecord, TraceStep

__all__ = [
    "DependencyError",
    "DuplicateStepIdError",
    "MissingParentError",
    "SelfReferenceError",
    "CycleError",
    "DependencyNode",
    "DependencyEdge",
    "DependencyGraph",
    "resolve_dependencies",
    "resolve_from_steps",
]


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------

class DependencyError(ValueError):
    """依赖关系非法时抛出的基类异常。"""


class DuplicateStepIdError(DependencyError):
    """同一 Trace 中出现重复的 step_id。"""


class MissingParentError(DependencyError):
    """parent_step_ids 引用了不存在的步骤。"""


class SelfReferenceError(DependencyError):
    """步骤的 parent_step_ids 中包含其自身。"""


class CycleError(DependencyError):
    """步骤之间构成循环依赖。"""


# ---------------------------------------------------------------------------
# 输出数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DependencyNode:
    """DAG 节点（步骤摘要）。"""

    step_id: str
    step_type: str
    status: str
    parent_step_ids: tuple[str, ...]
    child_step_ids: tuple[str, ...]

    def to_dict(self) -> dict:
        """转为 JSON 兼容 dict。"""
        return {
            "step_id": self.step_id,
            "step_type": self.step_type,
            "status": self.status,
            "parent_step_ids": list(self.parent_step_ids),
            "child_step_ids": list(self.child_step_ids),
        }


@dataclass(frozen=True)
class DependencyEdge:
    """DAG 有向边：parent_id -> child_id。"""

    parent_id: str
    child_id: str

    def to_dict(self) -> dict:
        """转为 JSON 兼容 dict。"""
        return {"parent_id": self.parent_id, "child_id": self.child_id}


@dataclass(frozen=True)
class DependencyGraph:
    """标准依赖关系输出。

    字段：
    - nodes: 节点列表（按拓扑顺序）；
    - edges: 有向边列表（父 -> 子）；
    - root_nodes: 根节点 step_id 列表（升序）；
    - leaf_nodes: 叶节点 step_id 列表（升序）；
    - topological_order: 拓扑排序结果（与 nodes 顺序一致）。
    """

    nodes: list[DependencyNode]
    edges: list[DependencyEdge]
    root_nodes: list[str]
    leaf_nodes: list[str]
    topological_order: list[str]

    def to_dict(self) -> dict:
        """转为 JSON 兼容 dict。"""
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "root_nodes": list(self.root_nodes),
            "leaf_nodes": list(self.leaf_nodes),
            "topological_order": list(self.topological_order),
        }


# ---------------------------------------------------------------------------
# 核心解析函数
# ---------------------------------------------------------------------------

def resolve_dependencies(record: TraceRecord) -> DependencyGraph:
    """解析 TraceRecord 中所有 TraceStep 的依赖关系。

    Args:
        record: 一条完整的执行轨迹。

    Returns:
        DependencyGraph: 标准依赖关系（nodes / edges / root_nodes / leaf_nodes）。

    Raises:
        DuplicateStepIdError: 存在重复 step_id。
        MissingParentError: parent_step_ids 引用了不存在的步骤。
        SelfReferenceError: 步骤自引用。
        CycleError: 存在循环依赖。
    """
    return resolve_from_steps(record.steps)


def resolve_from_steps(steps: list[TraceStep]) -> DependencyGraph:
    """解析步骤列表的依赖关系（核心实现）。

    Args:
        steps: TraceStep 列表，顺序无关。

    Returns:
        DependencyGraph: 标准依赖关系（nodes / edges / root_nodes / leaf_nodes）。

    Raises:
        同 resolve_dependencies。
    """
    # 1. 建立步骤索引，检测重复 step_id
    index: dict[str, TraceStep] = {}
    for step in steps:
        if step.step_id in index:
            raise DuplicateStepIdError(
                f"检测到重复的 step_id: {step.step_id!r}（同一 Trace 内必须唯一）"
            )
        index[step.step_id] = step

    # 2. 校验父节点引用：自引用与不存在的父节点
    for step in steps:
        for parent_id in step.parent_step_ids:
            if parent_id == step.step_id:
                raise SelfReferenceError(
                    f"步骤 {step.step_id!r} 自引用：parent_step_ids 包含自身，"
                    f"依赖关系必须是有向无环的"
                )
            if parent_id not in index:
                raise MissingParentError(
                    f"步骤 {step.step_id!r} 引用了不存在的父节点 {parent_id!r}，"
                    f"当前 Trace 中没有该 step_id"
                )

    # 3. 构建子节点映射（父 -> [子...]）
    children: dict[str, list[str]] = {sid: [] for sid in index}
    for step in steps:
        for parent_id in step.parent_step_ids:
            children[parent_id].append(step.step_id)

    # 4. Kahn 拓扑排序，检测循环依赖。
    #    使用最小堆保证输出顺序与输入排列顺序无关（确定性）。
    in_degree: dict[str, int] = {
        sid: len(index[sid].parent_step_ids) for sid in index
    }
    heap: list[str] = [sid for sid, d in in_degree.items() if d == 0]
    heapq.heapify(heap)

    topological_order: list[str] = []
    while heap:
        sid = heapq.heappop(heap)
        topological_order.append(sid)
        for child_id in children[sid]:
            in_degree[child_id] -= 1
            if in_degree[child_id] == 0:
                heapq.heappush(heap, child_id)

    if len(topological_order) != len(index):
        cycle_path = _find_cycle_path(index, children, topological_order)
        raise CycleError(
            f"检测到循环依赖: {' -> '.join(cycle_path)}，"
            f"步骤依赖关系必须构成有向无环图（DAG）"
        )

    # 5. 构建节点（children 已确定，按拓扑顺序输出）
    nodes: list[DependencyNode] = []
    for sid in topological_order:
        step = index[sid]
        nodes.append(
            DependencyNode(
                step_id=sid,
                step_type=step.step_type.value,
                status=step.status.value,
                parent_step_ids=tuple(sorted(step.parent_step_ids)),
                child_step_ids=tuple(sorted(children[sid])),
            )
        )

    # 6. 构建边（父 -> 子），每步的父节点按字典序排列，保证确定性
    edges: list[DependencyEdge] = [
        DependencyEdge(parent_id=pid, child_id=sid)
        for sid in topological_order
        for pid in sorted(index[sid].parent_step_ids)
    ]

    # 7. 根节点与叶节点
    root_nodes: list[str] = [
        sid for sid in topological_order if not index[sid].parent_step_ids
    ]
    leaf_nodes: list[str] = [
        sid for sid in topological_order if not children[sid]
    ]

    return DependencyGraph(
        nodes=nodes,
        edges=edges,
        root_nodes=root_nodes,
        leaf_nodes=leaf_nodes,
        topological_order=topological_order,
    )


def _find_cycle_path(
    index: dict[str, TraceStep],
    children: dict[str, list[str]],
    topological_order: list[str],
) -> list[str]:
    """在未完成拓扑排序的节点中定位一条实际的循环路径。

    说明：Kahn 算法结束后，剩余节点必然每个都有至少一个"仍在剩余集合中"的
    父节点（否则其入度会降到 0 而被处理）。因此从任一剩余节点出发沿父节点
    回溯，必然在有限步内重复，即可得到环路。

    Returns:
        形如 ["s2", "s3", "s1", "s2"] 的路径，相邻两项构成一条真实的有向边
        （父 -> 子），首尾相同。
    """
    remaining: set[str] = set(index) - set(topological_order)
    current: str = next(iter(remaining))

    visited: list[str] = []
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        visited.append(current)
        parent = next(p for p in index[current].parent_step_ids if p in remaining)
        current = parent

    start: int = visited.index(current)
    # visited[start:] = [v0, v1, ..., vk]，其中 v(i+1) 是 v(i) 的父节点
    # （边方向 v(i+1) -> v(i)），且 vk 的父节点为 v0（边 v0 -> vk）。
    # 按边方向重建环路：v0 -> vk -> v(k-1) -> ... -> v1 -> v0
    return [visited[start]] + list(reversed(visited[start + 1:])) + [current]
