"""TraceDAG 构建与查询的自动化测试（pytest，第 11 步）。

覆盖验收项：
1. 串行依赖；2. 并行分支；3. 多父节点汇合；4. 循环依赖拒绝；
5. 不存在的父节点拒绝；6. 拓扑排序正确；7. 祖先与后代查询正确。
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.schemas.trace import TraceRecord, TraceStep  # noqa: E402
from backend.app.services.trace_dag import (  # noqa: E402
    TraceDAG,
    build_dag,
    build_dag_from_steps,
)
from backend.app.services.trace_dependency import (  # noqa: E402
    CycleError,
    MissingParentError,
)

SAMPLE_PATH = PROJECT_ROOT / "examples" / "sample_trace.json"


def make_step(sid: str, parents: list[str]) -> TraceStep:
    """构造最小合法步骤。"""
    return TraceStep.model_validate(
        {
            "step_id": sid,
            "trace_id": "trace-test",
            "parent_step_ids": parents,
            "step_type": "planning",
            "timestamp": "2026-11-18T09:00:00Z",
            "status": "succeeded",
        }
    )


def build(steps: list[TraceStep]) -> TraceDAG:
    """从步骤列表构建 DAG。"""
    return build_dag_from_steps(steps)


# ---------------------------------------------------------------------------
# 1. 串行依赖
# ---------------------------------------------------------------------------

def test_serial_chain():
    """a -> b -> c 串行链。"""
    dag = build([make_step("a", []), make_step("b", ["a"]), make_step("c", ["b"])])

    assert dag.topological_order == ["a", "b", "c"]
    assert dag.root_nodes == ["a"]
    assert dag.leaf_nodes == ["c"]
    assert dag.node_count == 3
    assert dag.edge_count == 2
    assert dag.to_dict()["edges"] == [
        {"parent_id": "a", "child_id": "b"},
        {"parent_id": "b", "child_id": "c"},
    ]


# ---------------------------------------------------------------------------
# 2. 并行分支
# ---------------------------------------------------------------------------

def test_parallel_branches():
    """a 分出两个并行子分支 b、c。"""
    dag = build([make_step("a", []), make_step("b", ["a"]), make_step("c", ["a"])])

    assert dag.root_nodes == ["a"]
    assert dag.leaf_nodes == ["b", "c"]
    assert set(dag.descendants("a")) == {"b", "c"}
    assert dag.has_path("a", "b") is True
    assert dag.has_path("a", "c") is True
    assert dag.has_path("b", "c") is False  # 兄弟节点之间无路径
    assert dag.ancestors("b") == ["a"]


# ---------------------------------------------------------------------------
# 3. 多父节点汇合
# ---------------------------------------------------------------------------

def test_multi_parent_convergence():
    """a、b 两条分支汇合到 c（c 有两个父节点）。"""
    dag = build([make_step("a", []), make_step("b", []), make_step("c", ["a", "b"])])

    assert dag.root_nodes == ["a", "b"]
    assert dag.leaf_nodes == ["c"]
    assert set(dag.ancestors("c")) == {"a", "b"}
    assert set(dag.descendants("a")) == {"c"}
    assert dag.topological_order == ["a", "b", "c"]
    # 父节点必须排在子节点之前
    assert dag.topological_order.index("a") < dag.topological_order.index("c")
    assert dag.topological_order.index("b") < dag.topological_order.index("c")


# ---------------------------------------------------------------------------
# 4. 循环依赖拒绝
# ---------------------------------------------------------------------------

def test_cycle_rejected():
    """a -> b -> c -> a 循环必须被拒绝。"""
    with pytest.raises(CycleError):
        build(
            [make_step("a", ["c"]), make_step("b", ["a"]), make_step("c", ["b"])]
        )


# ---------------------------------------------------------------------------
# 5. 不存在的父节点拒绝
# ---------------------------------------------------------------------------

def test_missing_parent_rejected():
    """引用不存在的父节点必须被拒绝。"""
    with pytest.raises(MissingParentError):
        build([make_step("a", ["ghost"])])


# ---------------------------------------------------------------------------
# 6. 拓扑排序正确
# ---------------------------------------------------------------------------

def test_topological_order_correct():
    """菱形图 a->(b,c)->d：每条边的父节点都排在子节点之前。"""
    dag = build(
        [
            make_step("a", []),
            make_step("b", ["a"]),
            make_step("c", ["a"]),
            make_step("d", ["b", "c"]),
        ]
    )

    assert dag.topological_order == ["a", "b", "c", "d"]
    for edge in dag.to_dict()["edges"]:
        assert dag.topological_order.index(edge["parent_id"]) < dag.topological_order.index(
            edge["child_id"]
        )


# ---------------------------------------------------------------------------
# 7. 祖先与后代查询正确
# ---------------------------------------------------------------------------

def test_ancestors_descendants_queries():
    """菱形图：祖先 / 后代 / 依赖路径查询。"""
    dag = build(
        [
            make_step("a", []),
            make_step("b", ["a"]),
            make_step("c", ["a"]),
            make_step("d", ["b", "c"]),
        ]
    )

    assert set(dag.ancestors("d")) == {"a", "b", "c"}
    assert dag.ancestors("a") == []
    assert dag.ancestors("b") == ["a"]

    assert set(dag.descendants("a")) == {"b", "c", "d"}
    assert dag.descendants("d") == []
    assert dag.descendants("b") == ["d"]

    assert dag.has_path("a", "d") is True
    assert dag.has_path("d", "a") is False
    assert dag.has_path("b", "c") is False
    assert dag.has_path("a", "a") is False


# ---------------------------------------------------------------------------
# 补充：未知节点与标准 JSON 输出
# ---------------------------------------------------------------------------

def test_unknown_node_raises():
    """查询不存在的节点应抛出 KeyError 且信息清晰。"""
    dag = build([make_step("a", [])])

    with pytest.raises(KeyError):
        dag.ancestors("ghost")
    with pytest.raises(KeyError):
        dag.descendants("ghost")
    with pytest.raises(KeyError):
        dag.has_path("ghost", "a")


def test_json_output_structure():
    """to_dict() 输出必须包含标准五键且可 JSON 序列化。"""
    dag = build([make_step("a", []), make_step("b", ["a"])])
    data = dag.to_dict()

    assert set(data.keys()) == {
        "nodes",
        "edges",
        "topological_order",
        "root_nodes",
        "leaf_nodes",
    }
    assert data["nodes"][0]["step_id"] == "a"
    assert data["edges"] == [{"parent_id": "a", "child_id": "b"}]
    assert data["root_nodes"] == ["a"]
    assert data["leaf_nodes"] == ["b"]
    assert json.loads(json.dumps(data)) == data  # JSON 往返无损


def test_sample_trace_dag():
    """基于 examples/sample_trace.json 构建 DAG 并验证结构。"""
    sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    record = TraceRecord.model_validate(sample)
    dag = build_dag(record)

    assert dag.topological_order == ["step-1", "step-2", "step-3"]
    assert dag.root_nodes == ["step-1"]
    assert dag.leaf_nodes == ["step-3"]
    assert dag.edge_count == 2
    assert dag.has_path("step-1", "step-3") is True
    assert dag.ancestors("step-3") == ["step-1", "step-2"]
    assert dag.descendants("step-1") == ["step-2", "step-3"]
