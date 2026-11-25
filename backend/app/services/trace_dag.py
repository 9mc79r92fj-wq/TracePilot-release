"""Trace DAG 构建与分析（第 11 步）。

将第 10 步的依赖解析结果（DependencyGraph）转换为可分析的 DAG：

- 有向图双向邻接（父 -> 子 / 子 -> 父）；
- 拓扑排序（Kahn 算法，同时完成循环依赖检测）；
- 祖先 / 后代查询；
- 两节点间依赖路径查询；
- 可 JSON 序列化的标准输出（nodes / edges / topological_order / root_nodes / leaf_nodes）。

说明：
- 仅使用 Python 标准库实现（heapq / deque），不引入 networkx 等额外依赖；
- 循环依赖与非法父节点引用的主检测位于第 10 步（trace_dependency），
  本模块在构建时通过拓扑排序再次校验（防御性检查），保证任何入边都构成 DAG；
- 拓扑排序采用最小堆，输出与步骤在 JSON 数组中的排列顺序无关。
"""

from __future__ import annotations

import heapq
from collections import deque

from backend.app.schemas.trace import TraceRecord, TraceStep
from backend.app.services.trace_dependency import (
    CycleError,
    DependencyGraph,
    resolve_dependencies,
    resolve_from_steps,
)

__all__ = [
    "TraceDAG",
    "build_dag",
    "build_dag_from_steps",
]


class TraceDAG:
    """基于依赖解析结果构建的有向无环图。"""

    def __init__(self, graph: DependencyGraph) -> None:
        self._graph: DependencyGraph = graph
        self._nodes = {n.step_id: n for n in graph.nodes}
        self._children: dict[str, set[str]] = {sid: set() for sid in self._nodes}
        self._parents: dict[str, set[str]] = {sid: set() for sid in self._nodes}

        for edge in graph.edges:
            self._children[edge.parent_id].add(edge.child_id)
            self._parents[edge.child_id].add(edge.parent_id)

        # 拓扑排序（Kahn）。若无法覆盖全部节点，说明存在循环依赖
        # （防御性检查；正常情况第 10 步已拦截）。
        self.topological_order: list[str] = self._topological_sort()

        self.root_nodes: list[str] = [
            sid for sid in self.topological_order if not self._parents[sid]
        ]
        self.leaf_nodes: list[str] = [
            sid for sid in self.topological_order if not self._children[sid]
        ]

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------

    @property
    def node_count(self) -> int:
        """节点数量。"""
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        """有向边数量。"""
        return len(self._graph.edges)

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def _require_node(self, node_id: str) -> None:
        if node_id not in self._nodes:
            raise KeyError(f"节点不存在: {node_id!r}（当前 DAG 中没有该 step_id）")

    def ancestors(self, node_id: str) -> list[str]:
        """查询 node_id 的所有祖先节点（不含自身），按字典序返回。

        沿父节点方向广度优先遍历，支持多父节点汇合。
        """
        self._require_node(node_id)
        result: set[str] = set()
        queue: deque[str] = deque(self._parents[node_id])
        while queue:
            current = queue.popleft()
            if current in result:
                continue
            result.add(current)
            queue.extend(self._parents[current])
        return sorted(result)

    def descendants(self, node_id: str) -> list[str]:
        """查询 node_id 的所有后代节点（不含自身），按字典序返回。

        沿子节点方向广度优先遍历，支持并行分支。
        """
        self._require_node(node_id)
        result: set[str] = set()
        queue: deque[str] = deque(self._children[node_id])
        while queue:
            current = queue.popleft()
            if current in result:
                continue
            result.add(current)
            queue.extend(self._children[current])
        return sorted(result)

    def has_path(self, source_id: str, target_id: str) -> bool:
        """判断 source 到 target 之间是否存在依赖路径。

        路径需至少经过一条有向边；同一节点返回 False。
        """
        self._require_node(source_id)
        self._require_node(target_id)
        if source_id == target_id:
            return False
        queue: deque[str] = deque([source_id])
        seen: set[str] = {source_id}
        while queue:
            current = queue.popleft()
            for child in self._children[current]:
                if child == target_id:
                    return True
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
        return False

    # ------------------------------------------------------------------
    # 数据输出
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """返回可 JSON 序列化的标准 DAG 数据。

        结构：nodes / edges / topological_order / root_nodes / leaf_nodes。
        """
        return {
            "nodes": [self._nodes[sid].to_dict() for sid in self.topological_order],
            "edges": [
                {"parent_id": e.parent_id, "child_id": e.child_id}
                for e in self._graph.edges
            ],
            "topological_order": list(self.topological_order),
            "root_nodes": list(self.root_nodes),
            "leaf_nodes": list(self.leaf_nodes),
        }

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _topological_sort(self) -> list[str]:
        """Kahn 拓扑排序（最小堆保证确定性顺序），同时检测循环依赖。"""
        in_degree: dict[str, int] = {sid: len(self._parents[sid]) for sid in self._nodes}
        heap: list[str] = [sid for sid, degree in in_degree.items() if degree == 0]
        heapq.heapify(heap)

        order: list[str] = []
        while heap:
            sid = heapq.heappop(heap)
            order.append(sid)
            for child in self._children[sid]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    heapq.heappush(heap, child)

        if len(order) != len(self._nodes):
            raise CycleError(
                f"构建 DAG 时检测到循环依赖："
                f"共 {len(self._nodes)} 个节点，仅完成 {len(order)} 个的拓扑排序"
            )
        return order


# ---------------------------------------------------------------------------
# 构建入口
# ---------------------------------------------------------------------------

def build_dag(record: TraceRecord) -> TraceDAG:
    """从 TraceRecord 构建 DAG（内部先执行依赖解析与校验）。"""
    return TraceDAG(resolve_dependencies(record))


def build_dag_from_steps(steps: list[TraceStep]) -> TraceDAG:
    """从步骤列表构建 DAG（内部先执行依赖解析与校验）。"""
    return TraceDAG(resolve_from_steps(steps))
