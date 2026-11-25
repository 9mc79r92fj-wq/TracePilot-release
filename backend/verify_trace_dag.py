"""真实 Trace → DAG 验证脚本（第 19 步）。

用法：
    python backend/verify_trace_dag.py                 # 读取 data/traces/ 最新 Trace
    python backend/verify_trace_dag.py <trace.json>    # 指定 Trace 文件

说明：
- 只复用现有 Pydantic Schema（TraceRecord.model_validate_json）与现有 DAG 模块
  （build_dag），不重新实现 DAG 算法；
- 输出节点/边数量、根/叶节点、拓扑排序、循环检测、孤立节点与工具调用关系。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.schemas.trace import TraceRecord  # noqa: E402
from backend.app.services.trace_dag import build_dag  # noqa: E402


def _latest_trace(traces_dir: Path) -> Path | None:
    if not traces_dir.exists():
        return None
    files = sorted(
        traces_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return files[0] if files else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证真实 Trace 能否转换为合法 DAG")
    parser.add_argument("trace_file", nargs="?", help="Trace JSON 路径；默认取 data/traces 最新文件")
    args = parser.parse_args(argv)

    if args.trace_file:
        path = Path(args.trace_file)
    else:
        path = _latest_trace(PROJECT_ROOT / "data" / "traces")

    if path is None or not path.exists():
        print(
            "[错误] 未找到 Trace 文件。请先运行 "
            "`python backend/run_agent.py \"...\" --save-trace` 生成真实 Trace。",
            file=sys.stderr,
        )
        return 1

    record = TraceRecord.model_validate_json(path.read_text(encoding="utf-8"))
    dag = build_dag(record)

    print("Trace ID:", record.trace_id)
    print("节点数量:", dag.node_count)
    print("边数量:", dag.edge_count)
    print("根节点:", dag.root_nodes)
    print("叶节点:", dag.leaf_nodes)
    print("拓扑排序:", dag.topological_order)
    print("DAG 合法(无循环依赖):", len(dag.topological_order) == dag.node_count)

    isolated = [
        n for n in dag.topological_order if not dag.ancestors(n) and not dag.descendants(n)
    ]
    print("孤立节点:", isolated if isolated else "无")

    print("工具调用关系:")
    for s in record.steps:
        if s.step_type.value == "tool_call":
            print(f"  {s.step_id}  tool={s.tool_name}  parent_step_ids={s.parent_step_ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
