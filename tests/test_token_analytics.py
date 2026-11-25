"""第 53 步：Token & Cost Analytics 测试。

覆盖需求第十部分 19 项：
Provider 用量 / 未返回用量 / 模拟用量 / 估算用量 / UNKNOWN 处理 / 价格版本 /
输入输出价格不同 / 计算精度 / 多次调用聚合 / 避免重复计费 / 失败任务成本 /
延迟计算 / 价格缺失 / Trace 关联 / API / 前端构建（独立构建验证）/ 后端测试 /
不调用付费 API / 不泄露密钥。

诚实性约束测试：
- Provider 未返回用量时 token 为 null（不是 0）；
- 缺少可信价格时金额为 null 且状态 PRICE_UNKNOWN（不编造价格）；
- 金额带「非实际账单」声明；UNKNOWN 不计入平均成本。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.schemas.trace import (  # noqa: E402
    StepStatus,
    StepType,
    TokenUsage,
    TraceRecord,
    TraceStatus,
)
from backend.app.services import token_analytics as ta  # noqa: E402

client = TestClient(app)
REAL_TRACE_ID = "trace-4d3d82dc7a67496dbc2842cd3b31aa47"


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

def _write_pricing(tmp_path: Path, *, input_price: float | None, output_price: float | None,
                   version: str = "v-test", extra: dict | None = None) -> Path:
    entry = {
        "provider": "deepseek",
        "model_name": "deepseek-chat",
        "currency": "CNY",
        "input_price_per_million_tokens": input_price,
        "output_price_per_million_tokens": output_price,
        "effective_date": "2026-11-01",
        "pricing_version": version,
        "pricing_source": "TEST_FIXTURE_NOT_OFFICIAL_PRICE",
        "billing_note": "测试价格，仅用于验证计算逻辑，不是官方价格",
    }
    if extra:
        entry.update(extra)
    config = {
        "meta": {
            "pricing_version": version,
            "effective_date": "2026-11-01",
            "currency": "CNY",
            "pricing_source": "TEST_FIXTURE_NOT_OFFICIAL_PRICE",
            "note": "测试用价格配置（非官方价格）",
        },
        "providers": [entry],
    }
    path = tmp_path / f"pricing-{version}.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _make_step(
    trace_id: str,
    seq: int,
    *,
    step_type: str = "model_call",
    usage: tuple[int, int] | None = None,
    duration_ms: int | None = 100,
    metadata: dict | None = None,
    parents: tuple[str, ...] = (),
) -> dict:
    token_usage = TokenUsage(input_tokens=usage[0], output_tokens=usage[1]) if usage else None
    return {
        "step_id": f"{trace_id}-s{seq:03d}",
        "trace_id": trace_id,
        "parent_step_ids": list(parents),
        "step_type": step_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "input": {"messages": [{"role": "user", "content": "test task"}]},
        "output": "test answer",
        "status": StepStatus.SUCCEEDED.value,
        "duration_ms": duration_ms,
        "token_usage": token_usage.model_dump() if token_usage else None,
        "metadata": metadata or {},
    }


def _write_trace(
    directory: Path,
    *,
    trace_id: str = "trace-test-0001",
    run_id: str = "run-test-0001",
    steps: list[dict] | None = None,
    end_offset_ms: int | None = 500,
    status: str = "succeeded",
    metadata: dict | None = None,
) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    ended = started + timedelta(milliseconds=end_offset_ms) if end_offset_ms is not None else None
    trace = TraceRecord(
        trace_id=trace_id,
        task_id="task-test",
        run_id=run_id,
        started_at=started,
        ended_at=ended,
        status=TraceStatus(status),
        steps=steps or [],
        metadata=metadata or {},
    )
    (directory / f"{trace_id}.json").write_text(
        trace.model_dump_json(indent=2), encoding="utf-8"
    )
    return trace_id


def _usage_meta(provider: str, model: str, source: str, raw: dict | None = None) -> dict:
    return {
        "usage_meta": {
            "provider": provider,
            "model": model,
            "usage_source": source,
            "raw_usage": raw,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
    }


@pytest.fixture
def trace_env(tmp_path, monkeypatch):
    """把分析器指向临时 Trace 目录，避免依赖历史数据。"""
    traces_dir = tmp_path / "traces"
    eval_dir = tmp_path / "eval-traces"
    traces_dir.mkdir()
    eval_dir.mkdir()
    monkeypatch.setattr(ta, "TRACE_DIRS", (traces_dir, eval_dir))
    ta.clear_trace_cache()
    yield {"traces": traces_dir, "eval": eval_dir, "tmp": tmp_path}
    ta.clear_trace_cache()


# ---------------------------------------------------------------------------
# 1-5：用量来源
# ---------------------------------------------------------------------------

def test_provider_reported_usage_from_real_api_trace():
    """真实 API Trace（历史已验证 1 次）：Provider 报告用量 1852 tokens。"""
    analysis = ta.analyze_trace(REAL_TRACE_ID)
    assert analysis["tokens"]["total_tokens"] == 1852
    assert analysis["tokens"]["prompt_tokens"] == 1631
    assert analysis["tokens"]["completion_tokens"] == 221
    assert analysis["tokens"]["usage_sources"]["PROVIDER_REPORTED"] == 4
    assert analysis["tokens"]["unknown_model_calls"] == 0
    assert analysis["provider"] == "deepseek"
    assert analysis["model_name"] == "deepseek-chat"
    # 仓库默认价格未配置 → PRICE_UNKNOWN（不编造官方价格）
    assert analysis["cost"]["status"] == "PRICE_UNKNOWN"
    assert analysis["cost"]["amount"] is None
    assert analysis["latency"]["model_latency_ms"] == 3170
    assert analysis["latency"]["total_run_latency_ms"] == 3336
    assert analysis["paid_api_called"] is False
    assert "不是 Provider 实际账单" in analysis["billing_disclaimer"]


def test_provider_missing_usage_is_unknown_not_zero(trace_env):
    trace_id = _write_trace(
        trace_env["traces"],
        trace_id="trace-no-usage",
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=[
            _make_step("trace-no-usage", 1, usage=None,
                       metadata=_usage_meta("deepseek", "deepseek-chat", "UNKNOWN")),
        ],
    )
    analysis = ta.analyze_trace(trace_id)
    assert analysis["tokens"]["total_tokens"] is None
    assert analysis["tokens"]["prompt_tokens"] is None
    assert analysis["tokens"]["unknown_model_calls"] == 1
    assert analysis["tokens"]["complete"] is False
    assert analysis["cost"]["status"] == "UNKNOWN"
    assert analysis["cost"]["amount"] is None
    assert analysis["model_calls"][0]["usage_source"] == "UNKNOWN"


def test_simulated_usage_is_simulated_zero(trace_env):
    trace_id = _write_trace(
        trace_env["traces"],
        trace_id="trace-sim",
        metadata={
            "data_source": "simulated",
            "simulated": True,
            "simulated_model": True,
            "model_provider": "local-simulation",
            "model": "simulated-model",
        },
        steps=[
            _make_step("trace-sim", 1, usage=(0, 0),
                       metadata=_usage_meta("local-simulation", "simulated-model", "SIMULATED")),
            _make_step("trace-sim", 2, step_type="tool_call", usage=None, metadata={}),
            _make_step("trace-sim", 3, usage=(0, 0),
                       metadata=_usage_meta("local-simulation", "simulated-model", "SIMULATED")),
        ],
    )
    analysis = ta.analyze_trace(trace_id)
    assert analysis["tokens"]["usage_sources"]["SIMULATED"] == 2
    assert analysis["tokens"]["total_tokens"] == 0
    assert analysis["cost"]["status"] == "SIMULATED"
    assert analysis["cost"]["amount"] == 0.0
    assert analysis["cost"]["simulated_calls"] == 2


def test_estimated_usage_marked_estimated(trace_env):
    pricing = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=3.0, output_price=6.0))
    trace_id = _write_trace(
        trace_env["traces"],
        trace_id="trace-est",
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=[_make_step("trace-est", 1, usage=None,
                          metadata=_usage_meta("deepseek", "deepseek-chat", "UNKNOWN"))],
    )
    estimated = ta.analyze_trace(trace_id, pricing=pricing, estimate_missing=True)
    call = estimated["model_calls"][0]
    assert call["usage_source"] == "ESTIMATED"
    assert call["prompt_tokens"] is not None and call["prompt_tokens"] > 0
    assert estimated["cost"]["status"] == "ESTIMATED"
    assert estimated["cost"]["amount"] is not None and estimated["cost"]["amount"] > 0


def test_unknown_usage_not_counted_as_zero_in_overview(trace_env):
    pricing = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=1.0, output_price=2.0))
    _write_trace(
        trace_env["traces"],
        trace_id="trace-ok",
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=[_make_step("trace-ok", 1, usage=(1_000_000, 0),
                          metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
    )
    _write_trace(
        trace_env["traces"],
        trace_id="trace-unknown",
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=[_make_step("trace-unknown", 1, usage=None,
                          metadata=_usage_meta("deepseek", "deepseek-chat", "UNKNOWN"))],
    )
    ta.clear_trace_cache()
    overview = ta.analyze_overview(pricing=pricing)
    assert overview["trace_count"] == 2
    assert overview["tokens"]["total_tokens"] == 1_000_000  # 只累加已知用量
    assert overview["tokens"]["unknown_model_calls"] == 1
    assert overview["cost"]["priced_calls"] == 1
    assert overview["cost"]["unknown_calls"] == 1
    assert overview["cost"]["complete"] is False
    assert overview["unknown"]["unknown_usage_calls"] == 1
    # 未知调用没有被计为 0 元成本，也没有进入平均成本的分母
    assert overview["cost"]["amount"] == 1.0


# ---------------------------------------------------------------------------
# 6-10：价格版本、输入输出差异、精度、聚合、防重复
# ---------------------------------------------------------------------------

def test_pricing_versions_change_amount(trace_env):
    pricing_v1 = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=1.0, output_price=2.0, version="v1"))
    pricing_v2 = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=3.0, output_price=4.0, version="v2"))
    trace_id = _write_trace(
        trace_env["traces"],
        trace_id="trace-price",
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=[_make_step("trace-price", 1, usage=(1_000_000, 1_000_000),
                          metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
    )
    first = ta.analyze_trace(trace_id, pricing=pricing_v1)
    second = ta.analyze_trace(trace_id, pricing=pricing_v2)
    assert first["cost"]["amount"] == 3.0
    assert first["cost"]["pricing_versions"] == ["v1"]
    assert second["cost"]["amount"] == 7.0
    assert second["cost"]["pricing_versions"] == ["v2"]
    assert first["cost"]["amount"] != second["cost"]["amount"]


def test_input_output_prices_are_separate(trace_env):
    pricing = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=3.0, output_price=6.0))
    trace_id = _write_trace(
        trace_env["traces"],
        trace_id="trace-io",
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=[_make_step("trace-io", 1, usage=(1_000_000, 0),
                          metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED")),
               _make_step("trace-io", 2, usage=(0, 1_000_000),
                          metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
    )
    analysis = ta.analyze_trace(trace_id, pricing=pricing)
    amounts = [call["cost"]["amount"] for call in analysis["model_calls"]]
    assert amounts == [3.0, 6.0]  # 输入/输出单价不同，不能按同一价格计算
    assert analysis["cost"]["amount"] == 9.0


def test_cost_precision_decimal(trace_env):
    pricing = ta.load_pricing(
        _write_pricing(trace_env["tmp"], input_price=1.5, output_price=2.5, version="v-precision")
    )
    trace_id = _write_trace(
        trace_env["traces"],
        trace_id="trace-precision",
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=[_make_step("trace-precision", 1, usage=(333_333, 0),
                          metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
    )
    analysis = ta.analyze_trace(trace_id, pricing=pricing)
    # 333_333 * 1.5 / 1e6 = 0.4999995（Decimal 精确计算，8 位小数）
    assert analysis["cost"]["amount"] == pytest.approx(0.4999995, abs=1e-9)


def test_multiple_calls_aggregation(trace_env):
    pricing = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=1.0, output_price=2.0))
    steps = [
        _make_step("trace-multi", i, usage=(1000, 500),
                   metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))
        for i in range(1, 4)
    ]
    trace_id = _write_trace(
        trace_env["traces"],
        trace_id="trace-multi",
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=steps,
    )
    analysis = ta.analyze_trace(trace_id, pricing=pricing)
    assert analysis["tokens"]["prompt_tokens"] == 3000
    assert analysis["tokens"]["completion_tokens"] == 1500
    assert analysis["tokens"]["total_tokens"] == 4500
    assert analysis["model_calls"].__len__() == 3
    assert analysis["cost"]["amount"] == pytest.approx(3000 * 1.0 / 1e6 + 1500 * 2.0 / 1e6)


def test_no_double_counting_from_run_summary(trace_env):
    pricing = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=1.0, output_price=1.0))
    trace_id = _write_trace(
        trace_env["traces"],
        trace_id="trace-nodup",
        metadata={
            "data_source": "real_api",
            "model_provider": "deepseek",
            "model": "deepseek-chat",
            # run 级汇总故意写成更大值：分析器不得与 step 级重复累加
            "token_usage": {"input_tokens": 999_999, "output_tokens": 999_999, "total_tokens": 1_999_998},
        },
        steps=[_make_step("trace-nodup", 1, usage=(100, 50),
                          metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
    )
    analysis = ta.analyze_trace(trace_id, pricing=pricing)
    assert analysis["tokens"]["total_tokens"] == 150
    assert analysis["cost"]["amount"] == pytest.approx(0.00015)


# ---------------------------------------------------------------------------
# 11-14：失败任务 / 延迟 / 价格缺失 / Trace 关联
# ---------------------------------------------------------------------------

def test_failed_task_cost_and_averages(trace_env):
    pricing = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=1.0, output_price=1.0))
    for trace_id, status, tokens in (
        ("trace-success-1", "succeeded", 1_000_000),
        ("trace-failed-1", "failed", 2_000_000),
    ):
        _write_trace(
            trace_env["traces"],
            trace_id=trace_id,
            status=status,
            metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
            steps=[_make_step(trace_id, 1, usage=(tokens, 0),
                              metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
        )
    ta.clear_trace_cache()
    overview = ta.analyze_overview(pricing=pricing)
    outcomes = overview["task_outcomes"]
    assert outcomes["failed_traces"] == 1
    assert outcomes["succeeded_traces"] == 1
    assert outcomes["failed_task_cost_total"] == 2.0
    assert outcomes["avg_cost_per_task"] == 0.0 or outcomes["avg_cost_per_task"] == pytest.approx(1.0)


def test_latency_calculation_and_unknown(trace_env):
    trace_id = _write_trace(
        trace_env["traces"],
        trace_id="trace-latency",
        end_offset_ms=1000,
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=[
            _make_step("trace-latency", 1, duration_ms=300, usage=(10, 5),
                       metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED")),
            _make_step("trace-latency", 2, step_type="tool_call", duration_ms=200),
        ],
    )
    analysis = ta.analyze_trace(trace_id)
    assert analysis["latency"]["model_latency_ms"] == 300
    assert analysis["latency"]["tool_latency_ms"] == 200
    assert analysis["latency"]["total_run_latency_ms"] == 1000
    assert analysis["latency"]["unknown_fields"] == []

    unknown_id = _write_trace(
        trace_env["traces"],
        trace_id="trace-latency-unknown",
        end_offset_ms=None,
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=[_make_step("trace-latency-unknown", 1, duration_ms=None, usage=(10, 5),
                          metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
    )
    unknown = ta.analyze_trace(unknown_id)
    assert unknown["latency"]["total_run_latency_ms"] is None
    assert "total_run_latency_ms" in unknown["latency"]["unknown_fields"]
    assert unknown["latency"]["model_latency_ms"] is None  # UNKNOWN 不当 0


def test_price_missing_returns_price_unknown(trace_env):
    pricing = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=None, output_price=None))
    trace_id = _write_trace(
        trace_env["traces"],
        trace_id="trace-no-price",
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=[_make_step("trace-no-price", 1, usage=(100, 50),
                          metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
    )
    analysis = ta.analyze_trace(trace_id, pricing=pricing)
    assert analysis["cost"]["status"] == "PRICE_UNKNOWN"
    assert analysis["cost"]["amount"] is None
    assert analysis["cost"]["price_unknown_calls"] == 1

    # 完全不认识的价格条目
    other = ta.load_pricing(_write_pricing(
        trace_env["tmp"], input_price=1.0, output_price=1.0, version="v-other",
        extra={"provider": "other-provider", "model_name": "other-model"},
    ))
    analysis_other = ta.analyze_trace(trace_id, pricing=other)
    assert analysis_other["cost"]["status"] == "PRICE_UNKNOWN"


def test_trace_linkage_fields(trace_env):
    pricing = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=1.0, output_price=1.0))
    trace_id = _write_trace(
        trace_env["traces"],
        trace_id="trace-link",
        run_id="run-link-001",
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=[_make_step("trace-link", 1, usage=(100, 50),
                          metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
    )
    analysis = ta.analyze_trace(trace_id, pricing=pricing)
    call = analysis["model_calls"][0]
    assert call["trace_id"] == "trace-link"
    assert call["run_id"] == "run-link-001"
    assert call["model_call_id"] == "trace-link-s001"
    assert call["recorded_at"]
    # 归因可追溯到 model_call → trace → run
    attribution = ta.cost_attribution(level="model_call", pricing=pricing, limit=10)
    row = next(r for r in attribution["rows"] if r["id"] == "trace-link-s001")
    assert row["parent_refs"]["trace_id"] == "trace-link"
    assert row["parent_refs"]["run_id"] == "run-link-001"


def test_attribution_usage_source_filter(trace_env):
    """归因可按用量来源过滤：真实 API 与模拟调用互不混淆，金额口径不变。"""
    pricing = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=1.0, output_price=2.0))
    _write_trace(
        trace_env["traces"],
        trace_id="trace-filter-real",
        metadata={"data_source": "real_api", "model_provider": "deepseek",
                  "model": "deepseek-chat"},
        steps=[_make_step("trace-filter-real", 1, usage=(10, 5),
                          metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
    )
    _write_trace(
        trace_env["traces"],
        trace_id="trace-filter-sim",
        metadata={"data_source": "simulated", "model_provider": "local-simulation",
                  "model": "mock-scripted-model"},
        steps=[_make_step("trace-filter-sim", 1, usage=(0, 0),
                          metadata=_usage_meta("local-simulation", "mock-scripted-model",
                                               "SIMULATED"))],
    )
    ta.clear_trace_cache()
    all_rows = ta.cost_attribution(level="model_call", pricing=pricing, limit=10)
    assert {r["usage_source"] for r in all_rows["rows"]} == {"PROVIDER_REPORTED", "SIMULATED"}

    real = ta.cost_attribution(
        level="model_call", usage_source="PROVIDER_REPORTED", pricing=pricing, limit=10
    )
    assert real["usage_source_filter"] == "PROVIDER_REPORTED"
    assert [r["id"] for r in real["rows"]] == ["trace-filter-real-s001"]
    assert real["rows"][0]["cost"]["status"] == "ACTUAL_USAGE_PRICED"
    assert real["paid_api_called"] is False

    simulated = ta.cost_attribution(
        level="trace", usage_source="SIMULATED", pricing=pricing, limit=10
    )
    assert [r["id"] for r in simulated["rows"]] == ["trace-filter-sim"]

    with pytest.raises(ta.CostAnalyticsError):
        ta.cost_attribution(level="model_call", usage_source="BOGUS", pricing=pricing)


def test_step_provider_without_price_does_not_borrow_trace_price(trace_env):
    """step 显式声明了其他 provider/model 且无价格时，绝不能回退套用 Trace 级 0 价。"""
    config = {
        "meta": {
            "pricing_version": "v-sim-zero",
            "effective_date": "2026-11-01",
            "currency": "CNY",
            "pricing_source": "TEST_FIXTURE_NOT_OFFICIAL_PRICE",
        },
        "providers": [
            {
                "provider": "local-simulation",
                "model_name": "*",
                "currency": "CNY",
                "input_price_per_million_tokens": 0.0,
                "output_price_per_million_tokens": 0.0,
                "effective_date": "2026-11-01",
                "pricing_version": "v-sim-zero",
                "pricing_source": "TEST_FIXTURE_NOT_OFFICIAL_PRICE",
            }
        ],
    }
    path = trace_env["tmp"] / "pricing-sim-zero.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    pricing = ta.load_pricing(path)
    trace_id = _write_trace(
        trace_env["traces"],
        trace_id="trace-mismatch",
        metadata={
            "data_source": "simulated",
            "simulated": True,
            "model_provider": "local-simulation",
            "model": "simulated-model",
        },
        steps=[_make_step("trace-mismatch", 1, usage=(100, 50),
                          metadata=_usage_meta("deepseek", "deepseek-chat",
                                               "PROVIDER_REPORTED"))],
    )
    analysis = ta.analyze_trace(trace_id, pricing=pricing)
    call = analysis["model_calls"][0]
    assert call["provider"] == "deepseek"
    assert call["model_name"] == "deepseek-chat"
    # deepseek/deepseek-chat 在测试价格表中无条目 → PRICE_UNKNOWN，不能借用 0 价
    assert call["cost"]["status"] == "PRICE_UNKNOWN"
    assert call["cost"]["amount"] is None


# ---------------------------------------------------------------------------
# 价格配置校验 / 版本对比 / UNKNOWN 报告
# ---------------------------------------------------------------------------

def test_pricing_config_validation_errors(trace_env):
    bad_cases = [
        ({"input_price_per_million_tokens": -1.0}, "必须 >= 0"),
        ({"effective_date": "not-a-date"}, "合法日期"),
        ({"currency": "RMBX"}, "3 位"),
        ({"model_name": ""}, "model_name"),
    ]
    for index, (override, expected) in enumerate(bad_cases):
        path = trace_env["tmp"] / f"bad-{index}.yaml"
        entry = {
            "provider": "deepseek",
            "model_name": "deepseek-chat",
            "currency": "CNY",
            "input_price_per_million_tokens": 1.0,
            "output_price_per_million_tokens": 2.0,
            "effective_date": "2026-11-01",
            "pricing_version": "v-bad",
            "pricing_source": "TEST",
        }
        entry.update(override)
        path.write_text(
            yaml.safe_dump(
                {"meta": {"pricing_version": "v-bad"}, "providers": [entry]},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        with pytest.raises(ta.PricingConfigError) as excinfo:
            ta.load_pricing(path)
        assert expected in str(excinfo.value)

    missing = trace_env["tmp"] / "missing.yaml"
    with pytest.raises(ta.PricingConfigError):
        ta.load_pricing(missing)


def test_agent_version_comparison(trace_env):
    pricing = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=1.0, output_price=1.0))
    for version, tokens in (("agent-v1", 1_000_000), ("agent-v2", 2_000_000)):
        _write_trace(
            trace_env["traces"],
            trace_id=f"trace-{version}",
            metadata={
                "data_source": "real_api",
                "model_provider": "deepseek",
                "model": "deepseek-chat",
                "agent_version": version,
            },
            steps=[_make_step(f"trace-{version}", 1, usage=(tokens, 0),
                              metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
        )
    ta.clear_trace_cache()
    versions = ta.version_cost_comparison(pricing=pricing)
    assert versions["agent_version_field_available"] is True
    groups = {row["group"]: row for row in versions["by_agent_version"]}
    assert groups["agent-v1"]["cost"]["amount"] == 1.0
    assert groups["agent-v2"]["cost"]["amount"] == 2.0


def test_overview_agent_version_unavailable_note():
    overview = ta.analyze_overview(recent=1)
    versions = overview["versions"]
    assert versions["agent_version_field_available"] is False
    assert "不能当作真实 Agent 版本对比" in versions["agent_version_note"]
    assert versions["by_model"]


def test_unknown_data_report_not_zero():
    report = ta.unknown_data_report()
    assert report["price_unknown_calls"] >= 4  # 真实 deepseek Trace 的 4 次调用无价格
    assert report["note"]
    assert report["pricing"]["pricing_version"]
    examples = report["price_unknown_examples"]
    assert examples and examples[0]["reason"]


# ---------------------------------------------------------------------------
# Evaluation / Pipeline / Agent Run 级成本
# ---------------------------------------------------------------------------

def test_evaluation_run_cost_simulated():
    from backend.app.services.evaluation_store import get_evaluation_store

    store = get_evaluation_store()
    record = store.submit("tracepilot-eval-v1", 2, judge_mode="none", dedup=False)
    deadline = time.time() + 120
    while time.time() < deadline:
        current = store.get(record["run_id"])
        if current["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert current["status"] == "succeeded"
    cost = ta.analyze_evaluation_run(record["run_id"])
    assert cost["evaluation_run_id"] == record["run_id"]
    assert cost["status"] == "succeeded"
    assert cost["aggregate"]["tokens"]["total_tokens"] == 0
    assert cost["aggregate"]["cost"]["status"] == "SIMULATED"
    assert cost["aggregate"]["cost"]["amount"] == 0.0
    assert cost["latency"]["evaluation_latency_ms"] is not None
    assert len(cost["trace_ids"]) == 2
    assert cost["paid_api_called"] is False

    with pytest.raises(ta.CostAnalyticsError):
        ta.analyze_evaluation_run("eval-not-exist")


def test_pipeline_run_cost():
    from backend.app.services import evaluation_pipeline as ep

    record = ep.create_pipeline(
        dataset_id="tracepilot-eval-v1", max_cases=2, label="cost-analytics-test"
    )
    ep.start_pipeline(record["pipeline_run_id"])
    deadline = time.time() + 120
    while time.time() < deadline:
        current = ep.get_pipeline(record["pipeline_run_id"])
        if current["status"] in {"SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"}:
            break
        time.sleep(0.05)
    assert current["status"] == "SUCCEEDED", current.get("error")
    cost = ta.analyze_pipeline_run(record["pipeline_run_id"])
    assert cost["pipeline_run_id"] == record["pipeline_run_id"]
    assert cost["evaluation_run_id"] == current["refs"]["evaluation_run_id"]
    assert len(cost["trace_ids"]) == 2
    assert cost["aggregate"]["cost"]["status"] == "SIMULATED"
    assert cost["aggregate"]["cost"]["amount"] == 0.0
    assert cost["latency"]["pipeline_latency_ms"] is not None
    assert cost["latency"]["evaluation_latency_ms"] is not None
    # 阶段级分摊不可靠 → 明确标注不可用（不伪造分摊）
    assert cost["stage_allocation_available"] is False
    assert "不伪造" in cost["stage_allocation_note"] or "避免伪造" in cost["stage_allocation_note"]


def test_agent_run_cost_from_traces(trace_env):
    pricing = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=1.0, output_price=1.0))
    for index in (1, 2):
        _write_trace(
            trace_env["traces"],
            trace_id=f"trace-run-{index}",
            run_id="run-multi-trace",
            metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat",
                      "run_id": "run-multi-trace"},
            steps=[_make_step(f"trace-run-{index}", 1, usage=(1000, 0),
                              metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
        )
    ta.clear_trace_cache()
    run_cost = ta.analyze_agent_run("run-multi-trace", pricing=pricing)
    assert run_cost["run_id"] == "run-multi-trace"
    assert run_cost["aggregate"]["tokens"]["total_tokens"] == 2000
    assert run_cost["aggregate"]["cost"]["amount"] == pytest.approx(0.002)
    assert len(run_cost["trace_ids"]) == 2
    with pytest.raises(ta.CostAnalyticsError):
        ta.analyze_agent_run("run-does-not-exist", pricing=pricing)


# ---------------------------------------------------------------------------
# API + 安全
# ---------------------------------------------------------------------------

def test_api_cost_endpoints():
    r = client.get("/api/cost/pricing")
    assert r.status_code == 200
    pricing = r.json()["pricing"]
    assert pricing["pricing_version"]
    assert pricing["missing_price_behavior"].startswith("PRICE_UNKNOWN")
    assert pricing["disclaimer"]

    overview = client.get("/api/cost/overview", params={"recent": 5})
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["trace_count"] >= 1
    assert body["paid_api_called"] is False
    assert body["pricing"]["pricing_version"]
    assert "tokens" in body and "cost" in body and "latency" in body
    assert body["recent"]

    trace = client.get(f"/api/cost/traces/{REAL_TRACE_ID}")
    assert trace.status_code == 200
    assert trace.json()["tokens"]["total_tokens"] == 1852

    models = client.get("/api/cost/models")
    assert models.status_code == 200
    assert models.json()["model_count"] >= 1

    versions = client.get("/api/cost/versions")
    assert versions.status_code == 200
    assert "agent_version_field_available" in versions.json()

    unknown = client.get("/api/cost/unknown")
    assert unknown.status_code == 200
    assert unknown.json()["price_unknown_calls"] >= 1

    attribution = client.get("/api/cost/attribution",
                             params={"level": "model_call", "limit": 5})
    assert attribution.status_code == 200
    assert attribution.json()["count"] <= 5
    assert attribution.json()["paid_api_called"] is False

    filtered = client.get(
        "/api/cost/attribution",
        params={"level": "model_call", "usage_source": "PROVIDER_REPORTED", "limit": 20},
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["usage_source_filter"] == "PROVIDER_REPORTED"
    assert filtered.json()["rows"]
    assert all(r["usage_source"] == "PROVIDER_REPORTED" for r in filtered.json()["rows"])
    assert client.get(
        "/api/cost/attribution", params={"usage_source": "BOGUS"}
    ).status_code == 400

    # 错误路径
    assert client.get("/api/cost/attribution", params={"level": "bad"}).status_code == 400
    assert client.get("/api/cost/traces/trace-does-not-exist").status_code == 404
    assert client.get("/api/cost/evaluations/eval-does-not-exist").status_code == 404
    assert client.get("/api/cost/pipelines/pipe-does-not-exist").status_code == 404


def test_api_evaluation_cost_endpoint():
    from backend.app.services.evaluation_store import get_evaluation_store

    store = get_evaluation_store()
    record = store.submit("tracepilot-eval-v1", 1, judge_mode="none", dedup=False)
    deadline = time.time() + 120
    while time.time() < deadline:
        current = store.get(record["run_id"])
        if current["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    response = client.get(f"/api/cost/evaluations/{record['run_id']}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["aggregate"]["cost"]["status"] == "SIMULATED"
    assert payload["latency"]["evaluation_latency_ms"] is not None


def test_no_paid_api_and_no_secret_leakage(monkeypatch):
    secret = "sk-cost-test-secret-should-not-leak-0123456789"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    overview = ta.analyze_overview(recent=2)
    payload = json.dumps(overview, ensure_ascii=False)
    assert secret not in payload
    for token in ("DEEPSEEK_API_KEY", "sk-", "Authorization", "Bearer "):
        assert token not in payload
    assert overview["paid_api_called"] is False
    assert "不是 Provider 实际账单" in overview["cost"]["disclaimer"]


def test_overview_refresh_picks_up_new_traces(trace_env):
    pricing = ta.load_pricing(_write_pricing(trace_env["tmp"], input_price=1.0, output_price=1.0))
    _write_trace(
        trace_env["traces"],
        trace_id="trace-refresh-1",
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=[_make_step("trace-refresh-1", 1, usage=(1000, 0),
                          metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
    )
    before = ta.analyze_overview(pricing=pricing)
    assert before["trace_count"] == 1
    _write_trace(
        trace_env["traces"],
        trace_id="trace-refresh-2",
        metadata={"data_source": "real_api", "model_provider": "deepseek", "model": "deepseek-chat"},
        steps=[_make_step("trace-refresh-2", 1, usage=(1000, 0),
                          metadata=_usage_meta("deepseek", "deepseek-chat", "PROVIDER_REPORTED"))],
    )
    refreshed = ta.analyze_overview(pricing=pricing, force_refresh=True)
    assert refreshed["trace_count"] == 2
