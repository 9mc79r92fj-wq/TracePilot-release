"""Agent 能力工具集（第 55 步需求回溯修复）。

为 basic_agent 默认工具集补充 6 个真实能力工具（RAG / Memory / Skill / Context Review / HITL）：

- knowledge_search        本地知识库检索（RAG；结果自动经上下文审查）
- remember_fact           写入本次运行内的短期记忆
- recall_facts            读取本次运行内的短期记忆
- composite_sales_report  复合技能：SQLite 只读查询 + 汇总统计（Skill）
- review_context_tool     上下文质量/注入审查
- request_human_approval  创建人工审批请求（HITL 工作流，不自动批准）

对应 Trace step_type 通过 `CAPABILITY_STEP_TYPES` 映射，便于前端区分能力步骤。
所有工具均为本地确定性执行，不调用任何外部 API。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from backend.app.services.capability_suite import (
    MemoryStore,
    get_hitl_store,
    review_context,
    search_knowledge,
)
from backend.app.tools.basic_tools import ToolError

__all__ = [
    "CAPABILITY_STEP_TYPES",
    "CAPABILITY_TOOL_NAMES",
    "CAPABILITY_TOOLS",
    "build_capability_tools",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]

CAPABILITY_STEP_TYPES = {
    "knowledge_search": "retrieval",
    "remember_fact": "memory_write",
    "recall_facts": "memory_read",
    "composite_sales_report": "skill",
    "review_context_tool": "context_review",
    "request_human_approval": "human_confirmation",
}

CAPABILITY_TOOL_NAMES = tuple(CAPABILITY_STEP_TYPES)


@tool
def knowledge_search(query: str, top_k: int = 3) -> dict[str, Any]:
    """从本地知识库检索与 query 相关的文档（RAG，本地确定性检索，非真实业务知识库）。"""
    try:
        return search_knowledge(query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 - 统一转 ToolError
        raise ToolError(f"知识检索失败: {exc}") from exc


@tool
def composite_sales_report(limit: int = 100) -> dict[str, Any]:
    """复合技能：只读查询本地 SQLite sales 表，按产品汇总金额并给出结论。"""
    limit = max(1, min(int(limit), 1000))
    db_path = Path(
        __import__("os").environ.get("TRACEPILOT_DB_PATH", str(PROJECT_ROOT / "tracepilot.db"))
    )
    if not db_path.exists():
        raise ToolError(f"sales 数据库不存在: {db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            rows = conn.execute(
                "SELECT product, amount FROM sales ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ToolError(f"SQLite 只读查询失败: {exc}") from exc
    if not rows:
        return {
            "skill": "sales_summary",
            "row_count": 0,
            "note": "没有可汇总的销售数据（不是 0 金额的业务结论）",
            "data_source": "synthetic_local_sqlite",
        }
    by_product: dict[str, dict[str, float | int]] = {}
    total = 0.0
    for product, amount in rows:
        item = by_product.setdefault(str(product), {"count": 0, "amount": 0.0})
        item["count"] = int(item["count"]) + 1
        item["amount"] = round(float(item["amount"]) + float(amount), 2)
        total += float(amount)
    amounts = [float(row[1]) for row in rows]
    return {
        "skill": "sales_summary",
        "steps": ["sqlite_query(sales)", "aggregate_by_product", "summarize"],
        "row_count": len(rows),
        "total_amount": round(total, 2),
        "average_amount": round(total / len(rows), 2),
        "min_amount": round(min(amounts), 2),
        "max_amount": round(max(amounts), 2),
        "by_product": by_product,
        "conclusion": (
            f"共 {len(rows)} 条销售记录，总计 {round(total, 2)}；"
            f"产品数 {len(by_product)}。"
        ),
        "data_source": "synthetic_local_sqlite",
        "note": "本地演示 sales 表为合成数据，不代表真实业务数据",
    }


@tool
def review_context_tool(text: str) -> dict[str, Any]:
    """审查一段上下文是否含提示注入、重复、超长或敏感串（不调用模型）。"""
    return review_context(text)


def build_capability_tools(
    memory: MemoryStore | None = None,
) -> list[Any]:
    """构造包含本次运行短期记忆的能力工具列表（每次 Agent 运行独立实例）。"""
    memory = memory or MemoryStore()

    @tool
    def remember_fact(key: str, value: str) -> dict[str, Any]:
        """把一条事实写入本次运行内的短期记忆（不跨进程持久化）。"""
        try:
            return memory.remember(key, value)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @tool
    def recall_facts(query: str = "", limit: int = 5) -> dict[str, Any]:
        """从本次运行的短期记忆中检索事实（query 为空时返回最近记忆）。"""
        entries = memory.recall(query, limit=limit)
        return {
            "query": query,
            "entry_count": len(entries),
            "entries": entries,
            "note": "仅本次运行会话的短期记忆",
        }

    @tool
    def request_human_approval(
        action: str, reason: str, risk_level: str = "MEDIUM"
    ) -> dict[str, Any]:
        """为高风险动作创建人工审批请求（PENDING；不会自动批准，也不阻塞运行）。"""
        try:
            approval = get_hitl_store().create(
                action=action, reason=reason, risk_level=risk_level
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "approval_id": approval["approval_id"],
            "status": approval["status"],
            "risk_level": approval["risk_level"],
            "note": "审批请求已记录，需外部人工批准；本工具不自动放行",
        }

    return [
        knowledge_search,
        composite_sales_report,
        review_context_tool,
        remember_fact,
        recall_facts,
        request_human_approval,
    ]


CAPABILITY_TOOLS = build_capability_tools()
