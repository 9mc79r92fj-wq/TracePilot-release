"""TracePilot 基础本地工具集（第 13 步）。

提供三个真实本地工具，供 LangGraph Agent 通过 DeepSeek Function Calling 调用：

1. calculator    安全计算数学表达式（AST 白名单求值，禁止 eval/exec）；
2. sqlite_query  项目测试数据库只读 SELECT 查询（只读连接 + authorizer 白名单
                 + 参数化查询 + 行数限制，禁止访问其他数据库）；
3. text_analyzer 文本统计（字符数 / 词数 / 行数等），返回结构化结果。

安全设计：
- 不执行任何系统命令；
- Calculator 仅允许数字常量、四则运算、幂、取模等白名单 AST 节点，
  并限制表达式长度与幂指数，防止资源耗尽；
- SQLite 以只读模式打开（file:...?mode=ro）+ authorizer 只允许
  SELECT/READ/FUNCTION + 单条语句 + fetchmany 行数上限；
- 所有工具异常均转为带明确信息的 ToolError，由 Agent 侧捕获后
  以结构化错误返回模型，绝不让异常穿透。
"""

from __future__ import annotations

import ast
import math
import operator
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

__all__ = [
    "ToolError",
    "calculator",
    "sqlite_query",
    "text_analyzer",
    "BASIC_TOOLS",
    "get_tool_registry",
    "build_function_schemas",
    "get_tool_schemas",
]

# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ToolError(Exception):
    """工具执行错误（信息面向模型 / 用户，可安全展示）。"""


# ---------------------------------------------------------------------------
# 工具 1：Calculator（安全数学计算）
# ---------------------------------------------------------------------------

_MAX_EXPR_LENGTH = 200        # 表达式最大字符数
_MAX_EXPONENT = 100           # 幂指数绝对值上限，防止 9**9**9 类资源耗尽
_MAX_MAGNITUDE = 1e100        # 中间 / 最终结果绝对值上限

_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _eval_safe(node: ast.AST) -> int | float:
    """仅对白名单 AST 节点求值；任何函数调用、名称、属性访问都会被拒绝。"""
    if isinstance(node, ast.Expression):
        return _eval_safe(node.body)

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value

    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_safe(node.left)
        right = _eval_safe(node.right)
        if isinstance(node.op, ast.Pow):
            if isinstance(right, float) or abs(right) > _MAX_EXPONENT:
                raise ToolError(
                    f"幂指数非法或过大: {right!r}（限制 |指数| <= {_MAX_EXPONENT}）"
                )
        try:
            result = _BIN_OPS[type(node.op)](left, right)
        except (ZeroDivisionError, OverflowError, ValueError) as exc:
            raise ToolError(f"运算错误: {exc}") from exc
        if not math.isfinite(result) or abs(result) > _MAX_MAGNITUDE:
            raise ToolError("计算结果超出允许范围")
        return result

    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_safe(node.operand))

    raise ToolError(
        f"不支持的表达式元素: {type(node).__name__}"
        f"（仅允许数字、括号与 + - * / // % ** 运算符）"
    )


@tool
def calculator(expression: str) -> dict[str, Any]:
    """安全计算数学表达式（支持 + - * / // % ** 与括号），返回数值结果。"""
    if not expression or not expression.strip():
        raise ToolError("表达式不能为空")
    expr = expression.strip()
    if len(expr) > _MAX_EXPR_LENGTH:
        raise ToolError(f"表达式过长（最多 {_MAX_EXPR_LENGTH} 字符）")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"表达式语法错误: {exc}") from exc
    value = _eval_safe(tree)
    return {"expression": expr, "result": value}


# ---------------------------------------------------------------------------
# 工具 2：SQLite Query（项目测试数据库只读查询）
# ---------------------------------------------------------------------------

_ENV_DB_PATH = "TRACEPILOT_DB_PATH"
_MAX_ROWS_DEFAULT = 100
_MAX_ROWS_LIMIT = 1000


def _resolve_db_path() -> Path:
    """解析项目测试数据库路径（仅允许项目内路径，禁止访问其他数据库）。"""
    raw = os.environ.get(_ENV_DB_PATH)
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path
    return PROJECT_ROOT / "backend" / "data" / "tracepilot.db"


def _readonly_authorizer(action, arg1, arg2, db_name, source) -> int:
    """SQLite authorizer：仅放行读取类操作，禁止任何写入 / ATTACH。"""
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


@tool
def sqlite_query(
    query: str,
    params: list[Any] | None = None,
    limit: int = _MAX_ROWS_DEFAULT,
) -> dict[str, Any]:
    """对项目测试数据库执行只读 SELECT 查询（可用 ? 占位符，配合 params 参数化传值）。"""
    if not query or not query.strip():
        raise ToolError("查询语句不能为空")
    sql = query.strip()
    if not re.match(r"^\s*SELECT\b", sql, re.IGNORECASE):
        raise ToolError("仅允许只读 SELECT 查询")
    if not 1 <= limit <= _MAX_ROWS_LIMIT:
        raise ToolError(f"limit 必须在 1-{_MAX_ROWS_LIMIT} 之间")

    db_path = _resolve_db_path()
    if not db_path.exists():
        raise ToolError(
            f"测试数据库不存在: {db_path}（请先初始化项目测试数据，"
            f"或通过环境变量 {_ENV_DB_PATH} 指定路径）"
        )

    try:
        conn = sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5.0
        )
    except sqlite3.Error as exc:
        raise ToolError(f"无法以只读方式打开数据库: {exc}") from exc

    try:
        conn.set_authorizer(_readonly_authorizer)
        cursor = conn.cursor()
        cursor.execute(sql, list(params) if params else [])  # 参数化查询，禁止拼接
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(limit + 1)
    except sqlite3.Error as exc:
        raise ToolError(f"SQL 执行失败: {exc}") from exc
    finally:
        conn.close()

    truncated = len(rows) > limit
    rows = rows[:limit]
    return {
        "columns": columns,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# 工具 3：Text Analyzer（文本统计）
# ---------------------------------------------------------------------------

_MAX_TEXT_LENGTH = 100_000
_WORD_RE = re.compile(r"\S+")


@tool
def text_analyzer(text: str) -> dict[str, Any]:
    """统计文本的字符数、词数、行数等结构化信息（词按空白分隔近似统计）。"""
    if text is None:
        raise ToolError("text 不能为空")
    if len(text) > _MAX_TEXT_LENGTH:
        raise ToolError(f"文本过长（最多 {_MAX_TEXT_LENGTH} 字符），请截断后重试")

    words = _WORD_RE.findall(text)
    top_words = Counter(words).most_common(5)
    return {
        "char_count": len(text),
        "char_count_no_whitespace": len(re.sub(r"\s+", "", text)),
        "word_count": len(words),
        "line_count": len(text.splitlines()),
        "unique_word_count": len(set(words)),
        "top_words": [{"word": w, "count": c} for w, c in top_words],
    }


# ---------------------------------------------------------------------------
# 工具注册与 Function Calling Schema
# ---------------------------------------------------------------------------

BASIC_TOOLS: list[Any] = [calculator, sqlite_query, text_analyzer]


def get_tool_registry(tools: list[Any] | None = None) -> dict[str, Any]:
    """返回 {工具名: 工具对象} 映射，供 Agent 按模型返回的 name 执行。"""
    return {t.name: t for t in (tools if tools is not None else BASIC_TOOLS)}


def build_function_schemas(tools: list[Any]) -> list[dict[str, Any]]:
    """从 LangChain 工具列表生成 DeepSeek/OpenAI Function Calling 的 tools 参数。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": (t.description or "").strip(),
                "parameters": t.args_schema.model_json_schema(),
            },
        }
        for t in tools
    ]


def get_tool_schemas() -> list[dict[str, Any]]:
    """生成全部基础工具的 Function Calling schema。"""
    return build_function_schemas(BASIC_TOOLS)
