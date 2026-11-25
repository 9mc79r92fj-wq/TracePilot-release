"""Minimal Repair Engine 测试（pytest，第 33 步）。确定性模拟，不调用付费 API。"""

import glob
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.schemas.trace import TraceRecord  # noqa: E402
from backend.app.services.minimal_repair import (  # noqa: E402
    MAX_CANDIDATES,
    REPAIR_TYPES,
    RepairError,
    UnsupportedRepairError,
    build_repair_report,
    generate_candidates,
    score_candidate,
    verify_candidate,
)

DATA_TRACES = PROJECT_ROOT / "data" / "traces"
TS = "2026-11-18T12:00:00+00:00"


def _fault_trace(fault_type: str = "tool_timeout") -> TraceRecord:
    for p in sorted(glob.glob(str(DATA_TRACES / "*.json"))):
        r = TraceRecord.model_validate_json(Path(p).read_text(encoding="utf-8"))
        for s in r.steps:
            fi = s.metadata.get("fault_injection")
            if isinstance(fi, dict) and fi.get("injected") is True and fi.get("type") == fault_type:
                return r
    pytest.skip(f"缺少 {fault_type} 故障 Trace")


def _synth_trace(fault_type: str, tool: str = "calculator") -> TraceRecord:
    def step(sid, stype, parents, **kw):
        s = {
            "step_id": sid, "trace_id": "t-syn", "parent_step_ids": parents,
            "step_type": stype, "timestamp": TS, "status": "succeeded", "metadata": {},
        }
        s.update(kw)
        return s

    steps = [
        step("s1", "planning", []),
        step(
            "s2", "tool_call", ["s1"], tool_name=tool, status="failed",
            tool_result={"error": "x"},
            error={"error_type": fault_type, "error_message": "injected", "recoverable": True},
            metadata={"fault_injection": {"type": fault_type, "injected": True, "message": "injected"}},
        ),
        step("s3", "model_call", ["s2"]),
    ]
    return TraceRecord.model_validate({
        "trace_id": "t-syn", "task_id": "task-syn", "run_id": "run-syn",
        "started_at": TS, "ended_at": TS, "status": "failed",
        "steps": steps, "metadata": {"fault_injections": [{"fault_type": fault_type}]},
    })


# 1. 超时故障生成候选
def test_timeout_fault_generates_candidates():
    cands = generate_candidates(_fault_trace("tool_timeout"))
    types = {c.repair_type for c in cands}
    assert "INCREASE_TIMEOUT" in types
    assert "LIMITED_RETRY" in types
    assert all(c.repair_id.startswith("repair-") for c in cands)
    for c in cands:
        assert c.modification and c.expected_effect and c.risks and c.applicable_conditions


# 2. 参数错误生成候选
def test_argument_fault_generates_candidates():
    cands = generate_candidates(_synth_trace("invalid_arguments"))
    types = {c.repair_type for c in cands}
    assert "ARGUMENT_CORRECTION" in types
    arg_c = next(c for c in cands if c.repair_type == "ARGUMENT_CORRECTION")
    assert arg_c.modification["arguments"]["expression"] in {"1+1", "1 + 1", "(12+8)*5"}


# 3. 不支持的故障/修复类型正确处理
def test_unsupported_handling():
    # 无故障 -> 无候选（不伪造）
    clean = TraceRecord.model_validate({
        "trace_id": "t-clean", "task_id": "task", "run_id": "run",
        "started_at": TS, "ended_at": TS, "status": "succeeded",
        "steps": [{
            "step_id": "s1", "trace_id": "t-clean", "parent_step_ids": [],
            "step_type": "planning", "timestamp": TS, "status": "succeeded",
        }],
        "metadata": {},
    })
    assert generate_candidates(clean) == []

    # 越界候选数 -> 拒绝
    with pytest.raises(RepairError):
        generate_candidates(_fault_trace(), max_candidates=0)
    with pytest.raises(RepairError):
        generate_candidates(_fault_trace(), max_candidates=MAX_CANDIDATES + 1)


# 4. 候选数量受限
def test_candidate_limit():
    for limit in (1, 2, MAX_CANDIDATES):
        cands = generate_candidates(_fault_trace(), max_candidates=limit)
        assert len(cands) <= limit


# 5. 修复实验不覆盖原始 Trace
def test_original_trace_not_overwritten():
    trace = _fault_trace("tool_timeout")
    before = trace.model_dump_json()
    report = build_repair_report(trace)
    assert trace.model_dump_json() == before
    assert report["original_trace_modified"] is False


# 6. 有效修复能被识别
def test_valid_repair_detected():
    report = build_repair_report(_fault_trace("tool_timeout"))
    verified = [c for c in report["candidates"] if c["verification_status"] == "verified"]
    assert verified, "至少应有一个候选通过受控验证"
    assert verified[0]["repair_trace_id"] is not None
    assert verified[0]["experiment_id"] is not None
    assert report["recommended"] is not None
    assert report["recommended"]["repair_id"] in {c["repair_id"] for c in verified}


# 7. 无效修复不能标记成功
def test_invalid_repair_not_marked_verified():
    # tool_exception 场景下 INCREASE_TIMEOUT 无法消除异常故障
    trace = _synth_trace("tool_exception")
    cands = generate_candidates(trace)
    timeout_c = [c for c in cands if c.repair_type == "INCREASE_TIMEOUT"]
    if timeout_c:
        c = verify_candidate(trace, timeout_c[0])
        # 超时修复对异常故障应无效 -> failed 或 not_tested，绝不能是 verified
        assert c.verification_status in {"failed", "not_tested"}


# 8. 未执行修复标记 not_tested
def test_not_tested_marking():
    report = build_repair_report(_fault_trace("tool_timeout"), verify=False)
    assert all(c["verification_status"] == "not_tested" for c in report["candidates"])
    assert report["recommended"] is None  # 未验证不得推荐
    assert report["unverified"]


# 9. 评分可解释
def test_score_explainable():
    report = build_repair_report(_fault_trace("tool_timeout"))
    for c in report["candidates"]:
        assert c["score"] is not None
        bd = c["score_breakdown"]
        for key in ("verified", "scope", "latency", "token", "tool_calls", "side_effect", "weights", "disclaimer"):
            assert key in bd, f"缺少评分依据 {key}"
        assert "不代表真实成功概率" in bd["disclaimer"]
    # 已验证应排在 not_tested 之前
    statuses = [c["verification_status"] for c in report["candidates"]]
    if "verified" in statuses and "not_tested" in statuses:
        assert statuses.index("verified") < statuses.index("not_tested")


# 9b. 缺失指标不填 0
def test_missing_metrics_not_zero():
    report = build_repair_report(_fault_trace("tool_timeout"))
    for c in report["candidates"]:
        cd = c["cost_delta"]
        if cd and cd.get("tool_calls_delta") is None:
            assert "tool_calls" in cd.get("missing_metrics", [])


# 10. 不执行危险操作
def test_no_dangerous_operations():
    report = build_repair_report(_fault_trace("tool_timeout"))
    assert set(REPAIR_TYPES) == {
        "INCREASE_TIMEOUT", "LIMITED_RETRY", "FALLBACK_RESULT",
        "ARGUMENT_CORRECTION", "SKIP_OPTIONAL_STEP", "SAFE_TOOL_ALTERNATIVE",
    }
    for c in report["candidates"]:
        assert c["repair_type"] in REPAIR_TYPES
        # 不得出现任意命令/代码执行类修复
        assert "shell" not in str(c["modification"]).lower()
        assert "exec" not in str(c["modification"]).lower()


# 11. 不泄露 API Key
def test_no_sensitive_leak():
    report = build_repair_report(_fault_trace("tool_timeout"))
    text = str(report)
    assert "DEEPSEEK_API_KEY" not in text
    assert "sk-proj-" not in text


# 12. 相同受控条件下可复现
def test_reproducible():
    trace = _fault_trace("tool_timeout")
    a = build_repair_report(trace)
    b = build_repair_report(trace)
    sa = [(c["repair_type"], c["verification_status"]) for c in a["candidates"]]
    sb = [(c["repair_type"], c["verification_status"]) for c in b["candidates"]]
    assert sa == sb, "相同受控条件的验证结果应一致"


# 13. 自定义权重生效且被记录
def test_configurable_weights():
    report = build_repair_report(
        _fault_trace("tool_timeout"),
        weights={"verified": 1.0, "scope": 0.0, "latency": 0.0, "token": 0.0, "tool_calls": 0.0, "side_effect": 0.0},
    )
    assert report["weights_used"]["verified"] == 1.0
    for c in report["candidates"]:
        assert c["score_breakdown"]["weights"]["verified"] == 1.0
