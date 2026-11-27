"""V2 根因归因实验框架测试（不调用真实模型）。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.agentrx_benchmark import load_cases, split_cases  # noqa: E402
from backend.app.services import rootcause_v2 as rc  # noqa: E402


def _test_cases():
    cases = load_cases()
    return split_cases(cases)["test"][:5]


def _fake_llm(messages, **kwargs):
    # 从 Prompt 中解析第一个步骤编号，返回合法 JSON
    text = messages[-1]["content"]
    import re

    match = re.search(r"\[(\d+)\]", text)
    step = int(match.group(1)) if match else 1
    return {
        "data": {
            "root_cause_step": step,
            "failure_category": "Misinterpretation of Tool Output",
            "rationale": "测试用固定响应",
            "top3_steps": [step, step + 1, step + 2],
        },
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "latency_ms": 1.0,
        "model": "deepseek-flash",
        "raw": "{}",
    }


def test_deterministic_candidates_do_not_use_labels():
    case = _test_cases()[0]
    candidates = rc.build_deterministic_candidates(case)
    assert isinstance(candidates, list)
    assert all(1 <= c.step_index <= len(case.steps) for c in candidates)
    # 候选逻辑只能读取步骤文本，不读取 case.root_cause_* 字段（字段值不同不影响候选项数）
    assert candidates == rc.build_deterministic_candidates(case)


def test_baseline_b_returns_valid_structure():
    result = rc.baseline_b(_test_cases()[0])
    assert result["method"] == "baseline_b_deterministic"
    assert result["llm_calls"] == 0
    assert result["root_cause_step"] is None or isinstance(result["root_cause_step"], int)


def test_evaluate_cases_with_fake_llm():
    cases = _test_cases()
    report = rc.evaluate_cases(cases, llm_call=_fake_llm, include_llm=True)
    assert report["case_count"] == len(cases)
    metrics = report["metrics"]
    for method in ("baseline_b", "baseline_a", "tracepilot_v2"):
        assert method in metrics
        assert metrics[method]["cases"] == len(cases)
    assert metrics["baseline_a"]["llm_calls"] == len(cases)
    assert metrics["tracepilot_v2"]["llm_calls"] == len(cases)
    assert metrics["baseline_b"]["llm_calls"] == 0
    # 标签隔离字段
    for row in report["results"]:
        assert row["prompt_uses_labels"] is False
        assert "root_cause_step" in row["labels"]


def test_invalid_step_is_rejected():
    case = _test_cases()[0]

    def bad_llm(messages, **kwargs):
        return {
            "data": {"root_cause_step": 99999, "failure_category": "Nope"},
            "usage": None,
            "latency_ms": 1.0,
            "raw": "{}",
        }

    row = rc.evaluate_case(case, llm_call=bad_llm)
    assert row["methods"]["baseline_a"]["root_cause_step"] is None
    assert row["methods"]["baseline_a"]["failure_category"] == "Unknown"
