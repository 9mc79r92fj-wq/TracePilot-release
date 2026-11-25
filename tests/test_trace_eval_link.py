"""Trace→评测 关联与工具边界回归（Mock 模式，零付费调用）。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.agents.basic_agent import safe_tool_names  # noqa: E402
from backend.app.services import agent_runner  # noqa: E402
from backend.app.services.evaluation_engine import evaluate_trace  # noqa: E402
from backend.app.services.trace_store import load_trace  # noqa: E402


def test_mock_trace_can_be_evaluated_by_engine_and_trace_ids_link():
    run = agent_runner.run_agent_task("计算125乘以48", mode="mock")
    trace = load_trace(run.trace_id)
    case = {
        "case_id": "link-test-calc",
        "task_description": "计算125乘以48",
        "scenario_type": "numeric_calculation",
        "data_source": "mock",
        "expected_outcome": {
            "answer_match": {"type": "numeric", "expected": 6000, "tolerance": 0.0001},
            "tool_should_succeed": True,
        },
        "expected_tool_behavior": {
            "required_tools": ["calculator"],
            "forbidden_tools": [],
            "expects_tool_error": False,
        },
        "evaluation_rules": {
            "min_steps": 2,
            "max_steps": 12,
            "allow_tool_errors": False,
            "expects_recovery": False,
            "max_duration_ms": 120000,
        },
    }
    try:
        result = evaluate_trace(trace, case)
        assert result["trace_id"] == run.trace_id
        assert result["case_id"] == "link-test-calc"
        assert set(result["metrics"]) == {
            "task_success",
            "tool_correctness",
            "process_quality",
            "recovery",
            "efficiency",
            "safety",
        }
        assert result["metrics"]["task_success"]["status"] == "PASS"
    finally:
        Path(run.trace_path).unlink(missing_ok=True)


def test_agent_safe_tools_exclude_system_and_code_execution():
    names = set(safe_tool_names())
    assert {"calculator", "text_analyzer", "sqlite_query"}.issubset(names)
    # 能力工具属于本地无副作用实现，允许出现在白名单
    assert "knowledge_search" in names
    for forbidden in (
        "subprocess",
        "shell",
        "bash",
        "exec",
        "eval",
        "python",
        "file_write",
        "http_request",
    ):
        assert forbidden not in names


def test_agent_run_limits_keep_retry_zero_and_positive_budgets():
    limits = agent_runner.RunLimits()
    assert limits.api_max_retries == 0
    assert limits.max_total_tokens > 0
    assert limits.max_tokens_per_call > 0
    assert limits.max_tool_calls >= 0
    assert limits.max_wall_seconds > 0
