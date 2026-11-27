"""第 52 步：Automated Evaluation Pipeline 测试。

覆盖需求第十一部分的验证项：
1. 完整 Mock 流水线；2. 可选 Judge 跳过；3. 无基线时版本对比跳过；
4. 阶段执行顺序；5. 状态转换；6. Trace 关联；7. Bad Case 关联；
8. 失败处理；9. 超时处理；10. 取消逻辑；11. 报告生成；
12. 质量门禁结果；13. API 测试；15. 不调用付费 API；16. 不泄露密钥。

另覆盖第十部分错误处理：数据集缺失 / 配置无效 / Agent 执行失败 / Trace 保存失败 /
Judge 不可用 / 归因失败 / 版本不可比 / 门禁证据不足 / 任务取消 / 刷新后查询历史。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.services import evaluation_pipeline as ep  # noqa: E402
from backend.app.services.trace_store import load_trace  # noqa: E402

client = TestClient(app)
TERMINAL = {"SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _wait(pipeline_run_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = ep.get_pipeline(pipeline_run_id)
        if record["status"] in TERMINAL:
            return record
        time.sleep(0.05)
    raise AssertionError(f"流水线 {pipeline_run_id} 超时未结束")


def _wait_idle(timeout: float = 60.0) -> None:
    """等到没有 RUNNING 记录，且并发计数器也归零（线程清理窗口结束）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with ep._LOCK:
            running = any(r.get("status") == "RUNNING" for r in ep._PIPELINES.values())
            active = ep._RUNNING
        if not running and active == 0:
            return
        time.sleep(0.05)


def _run(*, wait_idle: bool = True, **kwargs) -> dict:
    if wait_idle:
        _wait_idle()
    record = ep.create_pipeline(**kwargs)
    ep.start_pipeline(record["pipeline_run_id"])
    return _wait(record["pipeline_run_id"])


def _stage(record: dict, stage_id: str) -> dict:
    return next(s for s in record["stages"] if s["stage_id"] == stage_id)


def _make_baseline_run() -> str:
    """先跑一条无基线 Mock 流水线，返回其评测运行 ID（供后续对比/门禁用）。"""
    record = _run(dataset_id="tracepilot-eval-v1", label="baseline-for-tests")
    assert record["status"] == "SUCCEEDED", record.get("error")
    return record["refs"]["evaluation_run_id"]


@pytest.fixture(autouse=True)
def _ensure_no_stuck_pipeline():
    # 每个用例开始前对账一次：终态记录对应的线程可能还在 finally 清理窗口内，
    # 并发计数器应回到实际 RUNNING 记录数，否则下一个用例会误报「并发已达上限」。
    ep.reconcile_runtime_state_for_tests()
    yield
    # 若用例失败导致流水线仍在运行，测试结束后等待其结束，避免影响后续用例
    _wait_idle(timeout=60)


# ---------------------------------------------------------------------------
# 1 / 2 / 3 / 4 / 5：完整 Mock 流水线与阶段语义
# ---------------------------------------------------------------------------

def test_full_mock_pipeline_fault_dataset():
    record = _run(dataset_id="tracepilot-fault-v1", label="full-mock-fault")
    assert record["status"] == "SUCCEEDED", record.get("error")
    assert record["paid_api_called"] is False
    assert record["config"]["execution_mode"] == "mock"
    stages = {s["stage_id"]: s for s in record["stages"]}
    # 必需阶段成功
    for sid in (
        "dataset_validation",
        "agent_execution",
        "trace_collection",
        "deterministic_evaluation",
        "bad_case_attribution",
        "failure_pattern_mining",
        "report_generation",
    ):
        assert stages[sid]["status"] == "SUCCEEDED", (sid, stages[sid])
    # 未启用的可选阶段必须 SKIPPED（不是 SUCCEEDED）
    assert stages["judge_evaluation"]["status"] == "SKIPPED"
    assert "未启用 Judge" in stages["judge_evaluation"]["skip_reason"]
    # 无基线 → 对比/回归/门禁 SKIPPED 且原因明确
    for sid in ("version_comparison", "regression_detection", "quality_gate"):
        assert stages[sid]["status"] == "SKIPPED", (sid, stages[sid])
        assert stages[sid]["skip_reason"]
    # 所有阶段都有时间字段或跳过原因（不允许空白）
    for stage in record["stages"]:
        assert stage["status"] in ep.STAGE_STATUSES
        if stage["status"] == "SKIPPED":
            assert stage["skip_reason"]
        else:
            assert stage["started_at"] and stage["finished_at"] is not None
            assert stage["duration_ms"] is not None

    # 报告与证据
    report = ep.get_pipeline_report(record["pipeline_run_id"])
    assert report["report_available"] is True
    final = report["report"]
    assert final["status"] == "SUCCEEDED"
    assert final["stage_summary"]["counts"]["SUCCEEDED"] >= 7
    assert final["stage_summary"]["counts"]["SKIPPED"] == 4
    assert final["paid_api_called"] is False
    kinds = {ref["kind"] for ref in final["evidence_refs"]}
    assert {"pipeline", "evaluation_run", "trace", "bad_case", "attribution_report",
            "failure_pattern_report"} <= kinds
    # 快照 + Markdown 落盘
    paths = record["final_report_paths"]
    assert (PROJECT_ROOT / paths["snapshot"]).exists()
    assert (PROJECT_ROOT / paths["markdown"]).exists()
    markdown = (PROJECT_ROOT / paths["markdown"]).read_text(encoding="utf-8")
    assert "# TracePilot Evaluation Pipeline Report" in markdown
    assert "`SKIPPED`" in markdown or "SKIPPED" in markdown
    assert "不调用真实模型" in markdown or "Mock" in markdown


def test_optional_judge_runs_when_enabled():
    baseline = _make_baseline_run()
    record = _run(
        dataset_id="tracepilot-eval-v1",
        judge_mode="mock",
        baseline_run_id=baseline,
        label="judge-enabled",
    )
    assert record["status"] == "SUCCEEDED", record.get("error")
    stages = {s["stage_id"]: s for s in record["stages"]}
    assert stages["judge_evaluation"]["status"] == "SUCCEEDED"
    report = ep.get_pipeline_report(record["pipeline_run_id"])["report"]
    judge = report["evaluation"]["judge_breakdown"]
    assert judge and judge.get("is_mock") is True
    assert report["reproducibility"]["rubric_version"] == "1.0.0"
    assert report["reproducibility"]["prompt_version"] == judge["prompt_version"]


def test_no_baseline_skips_comparison_and_gate():
    record = _run(dataset_id="tracepilot-eval-v1", label="no-baseline")
    assert record["status"] == "SUCCEEDED"
    stages = {s["stage_id"]: s for s in record["stages"]}
    assert stages["version_comparison"]["status"] == "SKIPPED"
    assert "基线" in stages["version_comparison"]["skip_reason"]
    assert stages["regression_detection"]["status"] == "SKIPPED"
    assert stages["quality_gate"]["status"] == "SKIPPED"
    report = ep.get_pipeline_report(record["pipeline_run_id"])["report"]
    assert report["quality_gate"] is None
    assert report["refs"]["comparison_id"] is None
    assert any("质量门禁未执行" in line for line in report["limitations"])


def test_stage_order_and_status_transitions():
    record = _run(dataset_id="tracepilot-eval-v1", label="order-check")
    stage_ids = [s["stage_id"] for s in record["stages"]]
    assert stage_ids == [meta["stage_id"] for meta in ep.STAGES]
    # 阶段顺序：必需阶段按依赖顺序全部成功
    assert stage_ids.index("dataset_validation") < stage_ids.index("agent_execution")
    assert stage_ids.index("agent_execution") < stage_ids.index("trace_collection")
    assert stage_ids.index("trace_collection") < stage_ids.index("deterministic_evaluation")
    assert stage_ids.index("deterministic_evaluation") < stage_ids.index("quality_gate")
    # 状态合法性：SKIPPED 从未被标记 SUCCEEDED
    for stage in record["stages"]:
        assert stage["status"] in ep.STAGE_STATUSES
        if stage["status"] == "SKIPPED":
            assert stage["skip_reason"]
    # 流水线终态与阶段终态一致
    assert record["status"] in TERMINAL
    assert record["started_at"] and record["finished_at"]
    assert record["duration_ms"] is not None and record["duration_ms"] >= 0


def test_trace_and_bad_case_references():
    record = _run(dataset_id="tracepilot-fault-v1", label="refs-check")
    refs = record["refs"]
    assert refs["evaluation_run_id"].startswith("eval-")
    assert len(refs["trace_ids"]) >= 6
    for trace_id in refs["trace_ids"]:
        trace = load_trace(trace_id)
        assert trace.trace_id == trace_id
    assert refs["bad_case_ids"], "受控失败数据集应产生 Bad Case"
    assert refs["attribution_report_ids"], "Bad Case 应生成归因报告"
    assert refs["failure_pattern_report_id"].startswith("fp-report-")
    report = ep.get_pipeline_report(record["pipeline_run_id"])["report"]
    # 评测摘要中的 case → trace 关联一致
    evaluation = report["evaluation"]
    trace_ids = {c["trace_id"] for c in evaluation["cases"]}
    assert set(refs["trace_ids"]) == trace_ids
    assert evaluation["bad_case_count"] == len(refs["bad_case_ids"])
    # 归因/模式报告可通过既有 API 查询（真实 ID 可追溯）
    attribution_id = refs["attribution_report_ids"][0]
    resp = client.get(f"/api/attribution/reports/{attribution_id}")
    assert resp.status_code == 200
    pattern_resp = client.get(f"/api/failure-patterns/reports/{refs['failure_pattern_report_id']}")
    assert pattern_resp.status_code == 200


# ---------------------------------------------------------------------------
# 12：质量门禁集成
# ---------------------------------------------------------------------------

def test_quality_gate_integration_pass():
    baseline = _make_baseline_run()
    record = _run(
        dataset_id="tracepilot-eval-v1",
        baseline_run_id=baseline,
        label="gate-pass",
    )
    assert record["status"] == "SUCCEEDED", record.get("error")
    stages = {s["stage_id"]: s for s in record["stages"]}
    for sid in ("version_comparison", "regression_detection", "quality_gate"):
        assert stages[sid]["status"] == "SUCCEEDED", (sid, stages[sid])
    gate_summary = record["gate_summary"]
    assert gate_summary["gate_status"] == "PASS"
    assert gate_summary["exit_code"] == 0
    gate_id = record["refs"]["quality_gate_report_id"]
    assert gate_id.startswith("qg-")
    # 门禁报告可独立查询且证据引用一致
    gate_resp = client.get(f"/api/quality-gate/reports/{gate_id}")
    assert gate_resp.status_code == 200
    assert gate_resp.json()["report"]["gate_status"] == "PASS"
    report = ep.get_pipeline_report(record["pipeline_run_id"])["report"]
    assert report["quality_gate"]["gate_report_id"] == gate_id
    assert report["quality_gate"]["checks"]
    assert any(ref["kind"] == "quality_gate_report" for ref in report["evidence_refs"])


def test_quality_gate_incomparable_versions():
    # 基线来自故障集、候选来自主数据集：两者不可比，但候选指标本身达标
    record_baseline = _run(dataset_id="tracepilot-fault-v1", label="incomparable-baseline")
    record = _run(
        dataset_id="tracepilot-eval-v1",
        baseline_run_id=record_baseline["refs"]["evaluation_run_id"],
        label="incomparable-candidate",
    )
    assert record["status"] == "SUCCEEDED", record.get("error")
    stages = {s["stage_id"]: s for s in record["stages"]}
    assert stages["version_comparison"]["status"] == "SUCCEEDED"
    comparison = ep.get_pipeline_report(record["pipeline_run_id"])["report"]["version_comparison"]
    assert comparison["comparability"]["verdict"] == "NOT_COMPARABLE"
    assert stages["quality_gate"]["status"] == "SUCCEEDED"
    gate_summary = record["gate_summary"]
    assert gate_summary["gate_status"] == "INCONCLUSIVE"
    assert gate_summary["exit_code"] == 2
    assert "GATE_NOT_COMPARABLE" in gate_summary["triggered_rules"]


# ---------------------------------------------------------------------------
# 8 / 9 / 10：失败 / 超时 / 取消
# ---------------------------------------------------------------------------

def test_agent_failure_marks_failed_and_blocks_downstream(monkeypatch):
    from backend.app.services import evaluation_engine

    def boom(case):
        raise RuntimeError("模拟 Agent 执行失败")

    monkeypatch.setattr(evaluation_engine, "execute_case", boom)
    record = _run(dataset_id="tracepilot-eval-v1", label="agent-failure")
    assert record["status"] == "FAILED"
    stages = {s["stage_id"]: s for s in record["stages"]}
    assert stages["agent_execution"]["status"] == "FAILED"
    assert "模拟 Agent 执行失败" in stages["agent_execution"]["error_details"]["message"]
    for sid in ("trace_collection", "deterministic_evaluation", "quality_gate"):
        assert stages[sid]["status"] == "BLOCKED", (sid, stages[sid])
        assert stages[sid]["skip_reason"]
    # 即使失败也要生成报告（不允许空白）
    report = ep.get_pipeline_report(record["pipeline_run_id"])
    assert report["report_available"] is True
    assert report["report"]["status"] == "FAILED"
    assert report["report"]["error"]["stage_id"] == "agent_execution"
    assert report["report"]["evidence_refs"]


def test_dataset_missing_failure(monkeypatch, tmp_path):
    from backend.app.services import evaluation_store

    # 指向不存在的目录 → 数据集文件缺失
    monkeypatch.setattr(evaluation_store, "DATASET_DIR", tmp_path)
    record = _run(dataset_id="tracepilot-eval-v1", label="dataset-missing")
    assert record["status"] == "FAILED"
    stage = _stage(record, "dataset_validation")
    assert stage["status"] == "FAILED"
    assert "数据集文件缺失" in stage["error_details"]["message"]
    # 下游全部 BLOCKED，但报告仍生成
    assert _stage(record, "agent_execution")["status"] == "BLOCKED"
    assert ep.get_pipeline_report(record["pipeline_run_id"])["report_available"] is True


def test_trace_save_failure(monkeypatch):
    from backend.app.services import evaluation_engine

    def boom(trace):
        raise OSError("磁盘写入失败（模拟）")

    monkeypatch.setattr(evaluation_engine, "persist_trace", boom)
    record = _run(dataset_id="tracepilot-eval-v1", label="trace-save-failure")
    assert record["status"] == "FAILED"
    stage = _stage(record, "trace_collection")
    assert stage["status"] == "FAILED"
    assert "Trace 保存失败" in stage["error_details"]["message"]


def test_judge_unavailable_failure(monkeypatch):
    from backend.app.services import llm_judge

    def boom(*args, **kwargs):
        raise RuntimeError("Judge 服务不可用（模拟）")

    monkeypatch.setattr(llm_judge, "build_judge", boom)
    record = _run(
        dataset_id="tracepilot-eval-v1",
        judge_mode="mock",
        label="judge-unavailable",
    )
    assert record["status"] == "FAILED"
    stage = _stage(record, "judge_evaluation")
    assert stage["status"] == "FAILED"
    assert "Judge 不可用" in stage["error_details"]["message"]
    # 确定性评测不受影响，报告仍包含评测结果
    report = ep.get_pipeline_report(record["pipeline_run_id"])["report"]
    assert report["evaluation"]["executed_cases"] > 0
    assert _stage(record, "deterministic_evaluation")["status"] == "SUCCEEDED"


def test_attribution_failure(monkeypatch):
    from backend.app.services import bad_case_attribution

    def boom(*args, **kwargs):
        raise RuntimeError("归因引擎异常（模拟）")

    monkeypatch.setattr(bad_case_attribution, "attribute_evaluation_run", boom)
    record = _run(dataset_id="tracepilot-fault-v1", label="attribution-failure")
    assert record["status"] == "FAILED"
    stage = _stage(record, "bad_case_attribution")
    assert stage["status"] == "FAILED"
    assert "归因失败" in stage["error_details"]["message"]
    # 依赖归因的失败模式挖掘 BLOCKED
    assert _stage(record, "failure_pattern_mining")["status"] == "BLOCKED"


def test_timeout_handling(monkeypatch):
    from backend.app.services import evaluation_engine

    def slow(case):
        time.sleep(1.2)
        raise RuntimeError("不会执行到这里")

    monkeypatch.setattr(evaluation_engine, "execute_case", slow)
    record = _run(
        dataset_id="tracepilot-eval-v1",
        max_runtime_seconds=1.0,
        label="timeout",
    )
    assert record["status"] == "FAILED"
    stage = _stage(record, "agent_execution")
    assert stage["status"] == "FAILED"
    assert stage["error_details"]["type"] == "TIMEOUT"
    assert "超出最大运行时间" in stage["error_details"]["message"]
    assert _stage(record, "deterministic_evaluation")["status"] == "BLOCKED"
    assert record["error"]["type"] == "TIMEOUT"


def test_cancel_pending_pipeline():
    _wait_idle()
    record = ep.create_pipeline(dataset_id="tracepilot-eval-v1", label="cancel-pending")
    cancelled = ep.cancel_pipeline(record["pipeline_run_id"])
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["cancel_requested"] is True
    for stage in cancelled["stages"]:
        assert stage["status"] == "CANCELLED"
        assert stage["skip_reason"] == "用户取消流水线"
    # 已结束的流水线不能再次取消
    with pytest.raises(ep.PipelineStateError):
        ep.cancel_pipeline(record["pipeline_run_id"])


def test_cancel_running_pipeline(monkeypatch):
    from backend.app.services import evaluation_engine

    original = evaluation_engine.execute_case

    def slow(case):
        time.sleep(0.25)
        return original(case)

    monkeypatch.setattr(evaluation_engine, "execute_case", slow)
    _wait_idle()
    record = ep.create_pipeline(dataset_id="tracepilot-fault-v1", label="cancel-running")
    pid = record["pipeline_run_id"]
    ep.start_pipeline(pid)
    # 等待进入 agent_execution 阶段后取消
    deadline = time.time() + 10
    while time.time() < deadline:
        current = ep.get_pipeline(pid)
        if _stage(current, "agent_execution")["status"] == "RUNNING":
            break
        if current["status"] in TERMINAL:
            pytest.fail("流水线结束过快，无法测试运行中取消")
        time.sleep(0.05)
    ep.cancel_pipeline(pid)
    final = _wait(pid)
    assert final["status"] == "CANCELLED"
    assert final["cancel_requested"] is True
    cancelled = [s for s in final["stages"] if s["status"] == "CANCELLED"]
    assert cancelled
    assert _stage(final, "agent_execution")["status"] == "CANCELLED"
    # 取消后不得出现"未执行阶段被标为 SUCCEEDED"
    for stage in final["stages"]:
        if stage["stage_id"] in {"trace_collection", "deterministic_evaluation", "quality_gate"}:
            assert stage["status"] in {"CANCELLED", "BLOCKED", "SKIPPED"}
    # 取消也要生成报告
    report = ep.get_pipeline_report(pid)
    assert report["report_available"] is True
    assert report["report"]["status"] == "CANCELLED"


# ---------------------------------------------------------------------------
# 限额 / 重试 / 配置错误 / 历史
# ---------------------------------------------------------------------------

def test_retry_limit(monkeypatch):
    from backend.app.services import evaluation_pipeline as pipeline_module

    original = pipeline_module._stage_dataset_validation
    calls = {"n": 0}

    def flaky(record, ctx, stage):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("首次瞬时失败")
        return original(record, ctx, stage)

    monkeypatch.setattr(pipeline_module, "_stage_dataset_validation", flaky)
    record = _run(dataset_id="tracepilot-eval-v1", max_retries=1, label="retry-ok")
    assert record["status"] == "SUCCEEDED"
    stage = _stage(record, "dataset_validation")
    assert stage["attempts"] == 2
    assert record["retries"] and record["retries"][0]["attempt"] == 1

    monkeypatch.setattr(
        pipeline_module,
        "_stage_dataset_validation",
        lambda record, ctx, stage: (_ for _ in ()).throw(RuntimeError("持续失败")),
    )
    failed = _run(dataset_id="tracepilot-eval-v1", max_retries=1, label="retry-fail")
    assert failed["status"] == "FAILED"
    stage = _stage(failed, "dataset_validation")
    assert stage["attempts"] == 2  # 1 次初始 + 1 次重试，不会无限重试
    assert stage["error_details"]["max_retries"] == 1


@pytest.mark.parametrize(
    "kwargs, error_type",
    [
        ({"dataset_id": "not-a-dataset"}, "PipelineConfigError"),
        ({"dataset_id": "tracepilot-eval-v1", "max_cases": 51}, "PipelineConfigError"),
        ({"dataset_id": "tracepilot-eval-v1", "max_cases": 0}, "PipelineConfigError"),
        ({"dataset_id": "tracepilot-eval-v1", "execution_mode": "cloud"}, "PipelineConfigError"),
        ({"dataset_id": "tracepilot-eval-v1", "judge_mode": "deepseek"}, "PipelineConfigError"),
        ({"dataset_id": "tracepilot-eval-v1", "max_retries": 3}, "PipelineConfigError"),
        ({"dataset_id": "tracepilot-eval-v1", "max_runtime_seconds": 0}, "PipelineConfigError"),
        ({"dataset_id": "tracepilot-eval-v1", "label": "x" * 121}, "PipelineConfigError"),
        ({"dataset_id": "tracepilot-eval-v1", "random_seed": -1}, "PipelineConfigError"),
        ({"dataset_id": "tracepilot-eval-v1", "baseline_run_id": "eval-not-exist"},
         "PipelineConfigError"),
    ],
)
def test_invalid_config_identified(kwargs, error_type):
    with pytest.raises(getattr(ep, error_type)):
        ep.create_pipeline(**kwargs)


def test_real_api_requires_double_authorization(monkeypatch):
    monkeypatch.delenv("TRACEPILOT_ALLOW_REAL_API", raising=False)
    with pytest.raises(ep.PipelineAuthError):
        ep.create_pipeline(
            dataset_id="tracepilot-eval-v1",
            execution_mode="real_api",
            confirm_real=True,
        )
    monkeypatch.setenv("TRACEPILOT_ALLOW_REAL_API", "1")
    with pytest.raises(ep.PipelineAuthError):
        ep.create_pipeline(dataset_id="tracepilot-eval-v1", execution_mode="real_api")
    # 未授权时不会创建任何流水线记录
    monkeypatch.delenv("TRACEPILOT_ALLOW_REAL_API", raising=False)


def test_concurrency_and_queue_limits(monkeypatch):
    from backend.app.services import evaluation_engine

    original = evaluation_engine.execute_case

    def slow(case):
        time.sleep(0.2)
        return original(case)

    monkeypatch.setattr(evaluation_engine, "execute_case", slow)
    _wait_idle()
    first = ep.create_pipeline(dataset_id="tracepilot-fault-v1", label="concurrency-1")
    ep.start_pipeline(first["pipeline_run_id"])
    time.sleep(0.2)
    second = ep.create_pipeline(dataset_id="tracepilot-eval-v1", label="concurrency-2")
    with pytest.raises(ep.PipelineBusyError):
        ep.start_pipeline(second["pipeline_run_id"])
    ep.cancel_pipeline(second["pipeline_run_id"])
    ep.cancel_pipeline(first["pipeline_run_id"])
    _wait(first["pipeline_run_id"])
    # 队列长度限制（不启动，避免并发干扰）
    pending = []
    for index in range(ep.MAX_QUEUE_LENGTH):
        pending.append(
            ep.create_pipeline(
                dataset_id="tracepilot-eval-v1", label=f"queue-{index}"
            )["pipeline_run_id"]
        )
    with pytest.raises(ep.PipelineBusyError):
        ep.create_pipeline(dataset_id="tracepilot-eval-v1", label="queue-overflow")
    for pid in pending:
        ep.cancel_pipeline(pid)


def test_history_and_disk_recovery():
    record = _run(dataset_id="tracepilot-eval-v1", label="history-check")
    pid = record["pipeline_run_id"]
    items = ep.list_pipelines(limit=100)
    assert pid in {item["pipeline_run_id"] for item in items}
    # 模拟页面刷新/进程内存清空：从磁盘快照恢复
    with ep._LOCK:
        ep._PIPELINES.pop(pid, None)
    recovered = ep.get_pipeline(pid)
    assert recovered["pipeline_run_id"] == pid
    assert recovered["status"] == record["status"]
    assert recovered["final_report"]["status"] == record["status"]
    # 从磁盘读取报告不丢证据
    assert recovered["refs"]["evaluation_run_id"] == record["refs"]["evaluation_run_id"]


def test_no_paid_api_and_no_secret_leakage(monkeypatch):
    secret = "sk-pipeline-test-secret-should-not-leak-0123456789"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    monkeypatch.setenv("TRACEPILOT_ALLOW_REAL_API", "0")
    record = _run(dataset_id="tracepilot-eval-v1", label="secret-check")
    payload = json.dumps(record, ensure_ascii=False)
    report = ep.get_pipeline_report(record["pipeline_run_id"])
    report_payload = json.dumps(report, ensure_ascii=False)
    assert secret not in payload
    assert secret not in report_payload
    assert record["paid_api_called"] is False
    assert record["refs"]["agent_run_ids"] == []
    for token in ("DEEPSEEK_API_KEY", "sk-", "Authorization", "Bearer "):
        assert token not in payload
        assert token not in report_payload


# ---------------------------------------------------------------------------
# 13：API 契约
# ---------------------------------------------------------------------------

def test_api_pipeline_flow(monkeypatch):
    meta = client.get("/api/pipelines/meta")
    assert meta.status_code == 200
    body = meta.json()
    assert body["limits"]["max_cases"] == ep.MAX_CASES
    assert len(body["stages"]) == len(ep.STAGES)
    assert "real_api" in body["execution_modes"]

    created = client.post(
        "/api/pipelines",
        json={"dataset_id": "tracepilot-eval-v1", "max_cases": 3, "label": "api-flow"},
    )
    assert created.status_code == 200, created.text
    pipe = created.json()
    pid = pipe["pipeline_run_id"]
    assert pipe["status"] == "PENDING"

    started = client.post(f"/api/pipelines/{pid}/start")
    assert started.status_code == 200
    assert started.json()["status"] in {"RUNNING", "PENDING", "SUCCEEDED"}

    deadline = time.time() + 120
    record = None
    while time.time() < deadline:
        resp = client.get(f"/api/pipelines/{pid}")
        assert resp.status_code == 200
        record = resp.json()
        if record["status"] in TERMINAL:
            break
        time.sleep(0.1)
    assert record is not None and record["status"] == "SUCCEEDED", record.get("error")

    stages = client.get(f"/api/pipelines/{pid}/stages")
    assert stages.status_code == 200
    assert stages.json()["stage_count"] == len(ep.STAGES)
    assert stages.json()["stages"][0]["stage_id"] == "dataset_validation"

    report = client.get(f"/api/pipelines/{pid}/report")
    assert report.status_code == 200
    assert report.json()["report_available"] is True
    assert report.json()["report"]["refs"]["evaluation_run_id"].startswith("eval-")

    listing = client.get("/api/pipelines", params={"limit": 50})
    assert listing.status_code == 200
    assert pid in {item["pipeline_run_id"] for item in listing.json()["pipelines"]}

    # 错误路径
    assert client.get("/api/pipelines/pipe-not-exist").status_code == 404
    assert client.post("/api/pipelines", json={"dataset_id": "bad-dataset"}).status_code == 400
    # 显式清空服务端开关：本地 backend/.env 可能已永久开启真实 API，
    # 未授权断言不能依赖开发机环境是否配置文件。
    monkeypatch.delenv("TRACEPILOT_ALLOW_REAL_API", raising=False)
    unauthorized = client.post(
        "/api/pipelines",
        json={
            "dataset_id": "tracepilot-eval-v1",
            "execution_mode": "real_api",
            "confirm_real": True,
        },
    )
    assert unauthorized.status_code == 403
    assert "未获授权" in unauthorized.text
    # 已结束流水线重复启动 → 409
    assert client.post(f"/api/pipelines/{pid}/start").status_code == 409
    # 已结束流水线取消 → 409
    assert client.post(f"/api/pipelines/{pid}/cancel").status_code == 409
