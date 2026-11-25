"""基础工具集自动化测试（pytest，第 13 步）。

仅本地测试，不调用真实 DeepSeek API。
"""

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.tools.basic_tools import (  # noqa: E402
    ToolError,
    calculator,
    get_tool_schemas,
    sqlite_query,
    text_analyzer,
)


@pytest.fixture
def sales_db(tmp_path, monkeypatch):
    """创建测试用销售数据库并注入环境变量。"""
    db_path = tmp_path / "sales_test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sales (id INTEGER PRIMARY KEY, name TEXT, amount REAL)")
    conn.executemany(
        "INSERT INTO sales (name, amount) VALUES (?, ?)",
        [("手机", 100.0), ("电脑", 200.0), ("耳机", 50.0), ("键盘", 80.0), ("鼠标", 30.0)],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("TRACEPILOT_DB_PATH", str(db_path))
    return db_path


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------

def test_calculator_basic_arithmetic():
    result = calculator.invoke({"expression": "2 + 3 * 4"})
    assert result == {"expression": "2 + 3 * 4", "result": 14}


def test_calculator_parentheses_and_power():
    result = calculator.invoke({"expression": "(2 + 3) ** 2"})
    assert result["result"] == 25.0


def test_calculator_rejects_code_injection():
    with pytest.raises(ToolError):
        calculator.invoke({"expression": "__import__('os').system('dir')"})
    with pytest.raises(ToolError):
        calculator.invoke({"expression": "open('secret.txt')"})


def test_calculator_division_by_zero():
    with pytest.raises(ToolError):
        calculator.invoke({"expression": "1 / 0"})


def test_calculator_huge_exponent_rejected():
    with pytest.raises(ToolError):
        calculator.invoke({"expression": "9 ** 9 ** 9"})


# ---------------------------------------------------------------------------
# SQLite Query
# ---------------------------------------------------------------------------

def test_sqlite_select_returns_structured_rows(sales_db):
    result = sqlite_query.invoke({"query": "SELECT id, name, amount FROM sales ORDER BY id"})
    assert result["columns"] == ["id", "name", "amount"]
    assert result["row_count"] == 5
    assert result["truncated"] is False
    assert result["rows"][0][1] == "手机"


def test_sqlite_parameterized_query(sales_db):
    result = sqlite_query.invoke(
        {"query": "SELECT name, amount FROM sales WHERE amount > ?", "params": [90]}
    )
    assert result["row_count"] == 2
    assert {r[0] for r in result["rows"]} == {"手机", "电脑"}


def test_sqlite_rejects_write_statements(sales_db):
    with pytest.raises(ToolError):
        sqlite_query.invoke({"query": "DELETE FROM sales"})
    with pytest.raises(ToolError):
        sqlite_query.invoke({"query": "INSERT INTO sales (name, amount) VALUES ('x', 1)"})


def test_sqlite_missing_db_reports_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACEPILOT_DB_PATH", str(tmp_path / "nope.db"))
    with pytest.raises(ToolError):
        sqlite_query.invoke({"query": "SELECT 1"})


def test_sqlite_row_limit_truncation(sales_db):
    result = sqlite_query.invoke({"query": "SELECT id FROM sales ORDER BY id", "limit": 3})
    assert result["row_count"] == 3
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# Text Analyzer
# ---------------------------------------------------------------------------

def test_text_analyzer_counts():
    result = text_analyzer.invoke({"text": "hello world hello\nfoo"})
    assert result["char_count"] == 21
    assert result["char_count_no_whitespace"] == 18
    assert result["word_count"] == 4
    assert result["line_count"] == 2
    assert result["unique_word_count"] == 3
    assert result["top_words"][0] == {"word": "hello", "count": 2}


def test_text_analyzer_empty_text():
    result = text_analyzer.invoke({"text": ""})
    assert result["char_count"] == 0
    assert result["word_count"] == 0
    assert result["line_count"] == 0


# ---------------------------------------------------------------------------
# Function Calling Schema 与 Agent 集成（离线）
# ---------------------------------------------------------------------------

def test_function_schemas_for_deepseek():
    schemas = get_tool_schemas()
    assert len(schemas) == 3
    by_name = {s["function"]["name"]: s["function"] for s in schemas}
    assert set(by_name) == {"calculator", "sqlite_query", "text_analyzer"}
    for s in schemas:
        assert s["type"] == "function"
        assert "parameters" in s["function"]
    assert "expression" in by_name["calculator"]["parameters"]["required"]
    assert "query" in by_name["sqlite_query"]["parameters"]["required"]
    assert "text" in by_name["text_analyzer"]["parameters"]["required"]


def test_agent_routing_offline():
    """验证 Agent 工具循环路由逻辑（不调用真实 API）。"""
    from langgraph.graph import END

    from backend.app.agents.basic_agent import _route_after_agent

    with_tool_call = {
        "messages": [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}
        ]
    }
    assert _route_after_agent(with_tool_call) == "tools"

    final_answer = {"messages": [{"role": "assistant", "content": "最终答案"}]}
    assert _route_after_agent(final_answer) == END

    assert _route_after_agent({"messages": []}) == END


def test_agent_module_imports_with_tools():
    """验证 basic_agent 与工具集集成后模块可正常加载、图可编译（离线）。"""
    import backend.app.agents.basic_agent as ba

    assert ba._GRAPH is not None
    assert len(ba.BASIC_TOOLS) == 3
