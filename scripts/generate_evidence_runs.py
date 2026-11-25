# -*- coding: utf-8 -*-
"""TracePilot 真实性整改：用项目自身 Agent 引擎生成可追溯的执行证据。

原则：
1. 真实执行：每个任务都经过 LangGraph Agent 引擎（理解 → 计划 → 工具循环 → 结论）、
   真实本地工具调用与真实 Trace 采集；失败任务保留真实错误。
2. 不冒充付费模型：模型响应来自本地脚本化服务（scripts/mock_llm_server.py），
   Trace metadata 统一标注 data_source=mock / simulated=true / model_provider=local-scripted-llm，
   不写入 real_api_verified，不生成伪造 Token 费用。
3. 只调用免费本地能力：工具为本地白名单实现（calculator / sqlite_query / text_analyzer /
   知识检索 / 销售汇总 / 上下文审查 / 记忆 / HITL），不发起任何付费 API 请求。
4. 下游链路（评测、归因、失败模式、根因指标、流水线）由对应引擎真实计算并落盘。

运行前提：先完成备份；本脚本会清空并重建 data/ 下的执行记录与派生产物（配置/数据集保留）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"C:\Users\yyxxy\Desktop\TracePilot")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

os.environ.update({
    "TRACEPILOT_ENV": "production",
    "TRACEPILOT_PUBLIC_DEMO": "1",
    "TRACEPILOT_DB_PATH": str(ROOT / "tracepilot.db"),
    "TRACEPILOT_PERSIST_AGENT_RUNS": "1",
    "TRACEPILOT_PERSIST_EVAL_RUNS": "1",
    "DEEPSEEK_API_KEY": "local-scripted-mock-key",
    "DEEPSEEK_BASE_URL": "http://127.0.0.1:9/unused",
    "PYTHONIOENCODING": "utf-8",
})
sys.stdout.reconfigure(encoding="utf-8")

DATA = ROOT / "data"
MANIFEST = DATA / "evidence_manifest.json"
GENERATION_REPORT = DATA / "evidence_generation_report.json"

# 需要重建的执行记录/派生产物目录（保留数据集、rubrics、知识库、校准集、红队输入）
RESET_DIRS = [
    "traces",
    "agent_runs",
    "attribution",
    "failure_patterns",
    "pipelines",
    "quality_gate",
    "regression",
    "version_comparison",
    "pareto",
    "rootcause_metrics",
    "governance",
    "evaluation/traces",
    "evaluation/runs",
]

DOC_FILES: dict[str, tuple[str, bytes]] = {
    "evidence_orders.csv": (
        "evidence_orders.csv",
        "order_id,status,latency_ms,error\n"
        "R-101,success,120,\n"
        "R-102,success,180,\n"
        "R-103,failed,2400,HTTP 503\n"
        "R-104,success,210,\n"
        "R-105,failed,1800,HTTP 500\n"
        "R-106,success,150,\n"
        "R-107,failed,3200,HTTP 503\n"
        "R-108,success,190,\n".encode("utf-8"),
    ),
    "evidence_incident.json": (
        "evidence_incident.json",
        json.dumps({
            "incident_id": "INC-2026-011",
            "severity": "P2",
            "scope": "支付链路",
            "started_at": "2026-11-21T02:10:00Z",
            "detected_by": "订单失败率告警",
            "summary": "支付网关在 02:10 出现 503 集中告警，订单 R-103/R-107 重试三次后失败。",
        }, ensure_ascii=False, indent=2).encode("utf-8"),
    ),
    "evidence_runbook.md": (
        "evidence_runbook.md",
        "# 支付链路变更手册\n\n"
        "1. 变更前先确认监控指标正常。\n"
        "2. 停止写入流量，执行数据库迁移脚本。\n"
        "3. 迁移失败立即回滚，并恢复写入流量。\n"
        "4. 变更完成后观察 30 分钟错误率与延迟。\n".encode("utf-8"),
    ),
    "evidence_log.txt": (
        "evidence_log.txt",
        "2026-11-21T02:10:01Z WARN retry=1 order=R-103 status=503\n"
        "2026-11-21T02:10:03Z WARN retry=2 order=R-103 status=503\n"
        "2026-11-21T02:10:05Z ERROR payment gateway timeout order=R-103\n"
        "2026-11-21T02:10:08Z INFO circuit breaker opened for payment-gateway\n".encode("utf-8"),
    ),
}

TS = lambda: datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def reset_generated_data() -> None:
    """把旧执行记录/派生产物整体移入 backups（秒级、可回滚），再重建空目录。"""
    legacy = ROOT / "backups" / "step74_legacy_moved"
    moved = 0
    for rel in RESET_DIRS:
        target = DATA / rel
        if target.exists():
            archive = legacy / rel
            if archive.exists():
                # 保留历史归档：重命名为带时间戳目录，避免逐文件删除拖慢生成
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                os.replace(archive, archive.with_name(f"{archive.name}_{stamp}"))
            archive.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, archive)
            moved += 1
        target.mkdir(parents=True, exist_ok=True)
    (DATA / "hitl").mkdir(parents=True, exist_ok=True)
    hitl = DATA / "hitl" / "approvals.json"
    if hitl.exists():
        hitl.unlink()
    docs = ROOT / "backend" / "data" / "documents"
    if docs.exists():
        for path in docs.glob("*.json"):
            path.unlink()
    log(f"旧执行记录/派生产物已整体移入 backups/step74_legacy_moved（{moved} 个目录）")


def upload_documents() -> dict[str, dict]:
    """通过真实文件上传/解析 API 创建证据用资料。"""
    from fastapi.testclient import TestClient
    from backend.app.main import app

    documents: dict[str, dict] = {}
    with TestClient(app) as client:
        for filename, raw in [(v[0], v[1]) for v in DOC_FILES.values()]:
            resp = client.post("/api/documents", files={"file": (filename, raw, "application/octet-stream")})
            if resp.status_code != 200:
                raise RuntimeError(f"上传 {filename} 失败: {resp.status_code} {resp.text[:200]}")
            doc = resp.json()["document"]
            documents[filename] = doc
            log(f"文件已上传并解析: {filename} id={doc['id']} size={doc['size']}")
    return documents


def build_agent_failed_trace(task: str, task_id: str, error_type: str, error_message: str):
    from backend.app.schemas.trace import TraceStatus
    from backend.app.services.trace_collector import TraceCollector

    collector = TraceCollector(task_id=task_id)
    now = TS()
    raw_step = {
        "step_id": f"{collector.trace_id}-s001",
        "trace_id": collector.trace_id,
        "parent_step_ids": [],
        "step_type": "model_call",
        "timestamp": now,
        "status": "failed",
        "input": {"task": task},
        "output": "",
        "error": {"error_type": error_type, "error_message": error_message, "recoverable": False},
        "duration_ms": 0,
    }
    trace = collector.build(
        [raw_step],
        status=TraceStatus.FAILED,
        metadata={"error_type": error_type, "error_message": error_message},
    )
    return collector, trace, task_id


def finalize_trace(trace, *, run_id: str, task_def: dict, provenance: list[dict] | None):
    meta = dict(trace.metadata or {})
    meta.update({
        "data_source": "mock",
        "simulated": True,
        "simulated_model": True,
        "model_provider": "local-scripted-llm",
        "model": "mock-scripted-model",
        "execution_origin": "local_agent_engine",
        "evidence_task_id": task_def["id"],
        "evidence_category": task_def["category"],
        "evidence_note": (
            "由 TracePilot LangGraph Agent 引擎在本地真实执行；模型响应由本地脚本化服务生成，"
            "工具为真实本地实现；未调用任何付费模型 API，不冒充真实 Provider 计量。"
        ),
        "usage_note": (
            "本地脚本化模型不返回 Provider usage，Token 为引擎估算值，仅用于预算控制；"
            "不据此计算真实费用。"
        ),
        "run_id": run_id,
    })
    meta.pop("real_api_verified", None)
    meta.pop("real_api_verification", None)
    trace = trace.model_copy(update={"metadata": meta, "run_id": run_id})
    if provenance and trace.steps:
        step_meta = dict(trace.steps[0].metadata or {})
        step_meta["referenced_documents"] = provenance
        trace.steps[0] = trace.steps[0].model_copy(update={"metadata": step_meta})
    return trace


def save_agent_run(run_dict: dict) -> None:
    path = DATA / "agent_runs" / f"{run_dict['run_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run_dict, ensure_ascii=False, indent=2), encoding="utf-8")


async def run_all_tasks(documents: dict[str, dict]) -> list[dict]:
    from backend.app.agents.basic_agent import AgentError, run_agent
    from backend.app.services.document_store import build_document_context
    from backend.app.services.execution_config import get_execution_limits
    from backend.app.services.fault_injector import FaultInjector, FaultRule, FaultType
    from backend.app.services.trace_collector import TraceCollector
    from backend.app.services.trace_store import DEFAULT_TRACES_DIR
    from mock_llm_server import MockLLMServer, load_scripts

    task_defs = load_scripts(ROOT / "scripts" / "evidence_tasks.json")
    server = MockLLMServer(task_defs).start()
    log(f"本地脚本化模型已启动: {server.base_url}")
    limits = get_execution_limits()
    manifest: list[dict] = []
    try:
        for task_def in task_defs:
            task = task_def["task"]
            task_id = f"evidence-{task_def['id']}"
            explicit = task_def.get("limits") or {}
            max_tool_calls = int(explicit.get("max_tool_calls", 4))
            max_total_tokens = int(explicit.get("max_total_tokens", 4000))
            overall = float(explicit.get("overall_timeout_seconds", 30))
            fault = None
            if task_def.get("fault"):
                cfg = task_def["fault"]
                fault = FaultInjector([FaultRule(
                    FaultType(cfg["type"]),
                    target_tool=cfg.get("target_tool"),
                    count=int(cfg.get("count", 1)),
                )])
            extra_context = None
            provenance: list[dict] = []
            if task_def.get("documents"):
                docs = [documents[name] for name in task_def["documents"] if name in documents]
                # build_document_context 只依赖文档元数据；正文从磁盘记录读取
                full_docs = []
                for doc in docs:
                    record_path = ROOT / "backend" / "data" / "documents" / f"{doc['id']}.json"
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    full_docs.append(record)
                context_task, provenance = build_document_context(task, full_docs)
                extra_context = context_task

            started = time.monotonic()
            result = None
            error_type = error_message = None
            trace = None
            try:
                result = await run_agent(
                    task,
                    api_key="local-scripted-mock-key",
                    model="mock-scripted-model",
                    base_url=server.base_url,
                    save_trace=False,
                    collect_trace=True,
                    max_tool_rounds=6,
                    max_tool_calls=max_tool_calls,
                    max_total_tokens=max_total_tokens,
                    overall_timeout_seconds=overall,
                    extra_context=extra_context,
                    fault_injector=fault,
                    enable_reflection=False,
                    tool_timeout_seconds=15.0,
                    task_id=task_id,
                )
                trace = result.trace
            except AgentError as exc:
                error_type = type(exc).__name__
                error_message = str(exc)
            except Exception as exc:  # noqa: BLE001
                error_type = type(exc).__name__
                error_message = str(exc)
            elapsed_ms = int((time.monotonic() - started) * 1000)

            run_id = f"run-{uuid.uuid4().hex}"
            if trace is None:
                collector, trace, _ = build_agent_failed_trace(
                    task, task_id, error_type or "AgentError", (error_message or "")[:900]
                )
                trace = trace.model_copy(update={"started_at": datetime.now(timezone.utc)})
                trace = finalize_trace(trace, run_id=run_id, task_def=task_def, provenance=None)
                status = "failed"
                answer = None
                plan: list[str] = []
                tool_calls: list[dict] = []
                limitations = [
                    "本地受控失败记录：模型调用阶段发生真实异常，未产生任何付费 API 请求",
                    task_def.get("provider_error_phase") and "错误来源：本地脚本化模型服务（用于验证失败链路）" or "",
                ]
                tool_calls_count = 0
                step_count = len(trace.steps)
            else:
                trace = finalize_trace(trace, run_id=run_id, task_def=task_def, provenance=provenance or None)
                status_map = {
                    "succeeded": "succeeded",
                    "partial": "limit_exceeded",
                    "failed": "failed",
                }
                status = status_map.get(trace.status.value, "succeeded")
                if getattr(result, "tool_calls_limit_reached", False) or getattr(result, "budget_exceeded", False):
                    status = "limit_exceeded"
                answer = result.result
                plan = list(result.plan)
                tool_calls = list(result.tool_executions)
                limitations = [
                    "模型响应由本地脚本化服务生成（非真实大模型 API），工具调用为真实本地执行",
                    "Token 用量未由 Provider 上报，界面标记为估算/未知，不据此生成真实费用",
                ]
                if getattr(result, "budget_exceeded", False):
                    limitations.append(f"Token 预算限制：{getattr(result, 'budget_reason', '预算不足')}")
                if getattr(result, "tool_calls_limit_reached", False):
                    limitations.append(f"工具调用达到上限 {max_tool_calls} 次，已停止继续调用")
                tool_calls_count = result.tool_call_count
                step_count = len(trace.steps)

            collector = TraceCollector(
                task_id=task_id,
                trace_id=trace.trace_id,
                run_id=run_id,
                started_at=trace.started_at,
                traces_dir=DEFAULT_TRACES_DIR,
            )
            trace_path = collector.save(trace)
            limits_dict = {
                "max_model_calls": limit_snapshot(limits, "agent_max_model_calls"),
                "max_total_tokens": max_total_tokens,
                "max_tool_calls": max_tool_calls,
                "max_wall_seconds": overall,
                "max_tool_rounds": 6,
                "max_tokens_per_call": limit_snapshot(limits, "agent_max_output_tokens"),
            }
            run_dict = {
                "run_id": run_id,
                "task": task,
                "mode": "mock",
                "data_source": "mock",
                "simulated": True,
                "status": status,
                "created_at": trace.started_at.isoformat() if hasattr(trace.started_at, "isoformat") else TS(),
                "limits": limits_dict,
                "model": "mock-scripted-model",
                "trace_id": trace.trace_id,
                "trace_path": str(trace_path),
                "final_answer": answer,
                "plan": plan,
                "tool_calls": tool_calls,
                "token_usage": None,
                "elapsed_ms": elapsed_ms,
                "error": None if status in ("succeeded",) else (error_message or f"执行状态 {status}"),
                "limit_violations": [],
                "limitations": [x for x in limitations if x],
            }
            save_agent_run(run_dict)
            entry = {
                "task_id": task_def["id"],
                "category": task_def["category"],
                "task": task,
                "run_id": run_id,
                "trace_id": trace.trace_id,
                "status": status,
                "trace_status": trace.status.value,
                "step_count": step_count,
                "tool_call_count": tool_calls_count,
                "documents": task_def.get("documents") or [],
                "error_type": error_type,
                "error_message": (error_message or "")[:300],
                "elapsed_ms": elapsed_ms,
            }
            manifest.append(entry)
            log(f"{task_def['id']} [{task_def['category']}] -> {status} steps={step_count} tools={tool_calls_count} {elapsed_ms}ms")
    finally:
        server.stop()
    return manifest


def limit_snapshot(limits, name: str):
    return getattr(limits, name, None)


def run_evaluation_chain() -> dict:
    """真实运行评测引擎，并生成归因/失败模式/根因指标/流水线等派生产物。"""
    from backend.app.services.evaluation_store import get_evaluation_store

    store = get_evaluation_store()
    results: dict = {"evaluations": [], "attribution": [], "failure_patterns": [], "rootcause_metrics": [], "pipeline": None}

    def submit_and_wait(dataset_id: str, judge_mode: str, max_cases=None, timeout=180):
        rec = store.submit(dataset_id, max_cases, judge_mode)
        run_id = rec["run_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = store.get(run_id)
            if cur.get("status") != "running":
                log(f"评测完成: {run_id} status={cur.get('status')} dataset={dataset_id}")
                return cur
            time.sleep(0.5)
        raise TimeoutError(f"评测超时: {run_id}")

    eval_main = submit_and_wait("tracepilot-eval-v1", "mock")
    eval_fault = submit_and_wait("tracepilot-fault-v1", "none")
    results["evaluations"] = [
        {"run_id": eval_main["run_id"], "dataset_id": "tracepilot-eval-v1", "status": eval_main["status"]},
        {"run_id": eval_fault["run_id"], "dataset_id": "tracepilot-fault-v1", "status": eval_fault["status"]},
    ]

    from backend.app.services.bad_case_attribution import attribute_evaluation_run
    from backend.app.services.failure_pattern_mining import analyze_run
    from backend.app.services.rootcause_metrics import compute_metrics

    for eval_run in (eval_main, eval_fault):
        try:
            out = attribute_evaluation_run(eval_run["run_id"], max_cases=10)
            results["attribution"].append({
                "evaluation_run_id": eval_run["run_id"],
                "attributed_count": out["attributed_count"],
                "report_ids": [r["attribution_id"] for r in out["reports"]],
            })
            log(f"归因完成: {eval_run['run_id']} reports={out['attributed_count']}")
        except Exception as exc:  # noqa: BLE001
            results["attribution"].append({"evaluation_run_id": eval_run["run_id"], "error": f"{type(exc).__name__}: {exc}"})
            log(f"归因失败: {eval_run['run_id']} {exc}")
        try:
            fp = analyze_run(eval_run["run_id"])
            results["failure_patterns"].append({
                "evaluation_run_id": eval_run["run_id"],
                "report_id": fp["pattern_report_id"],
                "pattern_count": fp.get("pattern_count"),
            })
            log(f"失败模式完成: {eval_run['run_id']} patterns={fp.get('pattern_count')}")
        except Exception as exc:  # noqa: BLE001
            results["failure_patterns"].append({"evaluation_run_id": eval_run["run_id"], "error": f"{type(exc).__name__}: {exc}"})
            log(f"失败模式失败: {eval_run['run_id']} {exc}")
    try:
        metrics = compute_metrics(eval_fault["run_id"], max_cases=10)
        results["rootcause_metrics"].append({
            "metrics_id": metrics["metrics_id"],
            "evaluation_run_id": eval_fault["run_id"],
            "bad_case_count": metrics.get("bad_case_count"),
            "attributed_cases": metrics.get("attributed_cases"),
        })
        log(f"根因指标完成: {metrics['metrics_id']}")
    except Exception as exc:  # noqa: BLE001
        results["rootcause_metrics"].append({"error": f"{type(exc).__name__}: {exc}"})
        log(f"根因指标失败: {exc}")

    # 流水线：以主评测为 baseline，真实执行完整链路（含版本对比/回归/质量门禁）
    try:
        from backend.app.services.evaluation_pipeline import (
            create_pipeline, get_pipeline, start_pipeline,
        )

        pipeline = create_pipeline(
            dataset_id="tracepilot-eval-v1",
            max_cases=6,
            execution_mode="mock",
            judge_mode="mock",
            baseline_run_id=eval_main["run_id"],
            enable_attribution=True,
            enable_failure_patterns=True,
            enable_version_comparison=True,
            enable_regression=True,
            enable_quality_gate=True,
            label="evidence-pipeline",
        )
        run_id = pipeline["pipeline_run_id"]
        start_pipeline(run_id)
        deadline = time.time() + 300
        while time.time() < deadline:
            cur = get_pipeline(run_id)
            if cur.get("status") not in ("PENDING", "RUNNING"):
                log(f"流水线完成: {run_id} status={cur.get('status')}")
                results["pipeline"] = {
                    "pipeline_run_id": run_id,
                    "status": cur.get("status"),
                    "refs": cur.get("refs"),
                    "error": cur.get("error"),
                    "stages": [
                        {"stage_id": s.get("stage_id"), "status": s.get("status"), "skip_reason": s.get("skip_reason")}
                        for s in cur.get("stages") or []
                    ],
                }
                break
            time.sleep(1.0)
        else:
            results["pipeline"] = {"pipeline_run_id": run_id, "error": "流水线执行超时"}
    except Exception as exc:  # noqa: BLE001
        results["pipeline"] = {"error": f"{type(exc).__name__}: {exc}"}
        log(f"流水线失败: {exc}")

    # 治理扫描（真实重扫并按需持久化索引）
    try:
        from backend.app.services.data_governance import scan_sources
        summary = scan_sources(force=True, persist=True)
        results["governance"] = {"total": summary.get("total") or summary.get("total_count"), "persisted": True}
        log("治理扫描完成并持久化")
    except Exception as exc:  # noqa: BLE001
        results["governance"] = {"error": f"{type(exc).__name__}: {exc}"}
        log(f"治理扫描失败: {exc}")

    return results


def main() -> int:
    reset_generated_data()
    documents = upload_documents()
    manifest = asyncio.run(run_all_tasks(documents))
    chain = run_evaluation_chain()

    (DATA / "evidence_manifest.json").write_text(
        json.dumps({"version": "1.0.0", "generated_at": TS(), "runs": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report = {
        "generated_at": TS(),
        "task_count": len(manifest),
        "status_counts": {s: sum(1 for m in manifest if m["status"] == s) for s in sorted({m["status"] for m in manifest})},
        "trace_status_counts": {s: sum(1 for m in manifest if m["trace_status"] == s) for s in sorted({m["trace_status"] for m in manifest})},
        "documents": list(documents),
        "downstream": chain,
        "environment": {
            "agent_engine": "backend.app.agents.basic_agent (LangGraph)",
            "scripted_model": "scripts/mock_llm_server.py",
            "paid_api_called": False,
            "profile": os.environ.get("TRACEPILOT_ENV"),
        },
    }
    (DATA / "evidence_generation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log("=" * 60)
    log(f"任务 {report['task_count']} 条；状态 {report['status_counts']}")
    log(f"Trace 状态 {report['trace_status_counts']}")
    log(f"生成报告: {GENERATION_REPORT}")

    # 清理后台线程池，避免脚本退出被阻塞
    try:
        from backend.app.services.evaluation_store import get_evaluation_store
        get_evaluation_store()._executor.shutdown(wait=False)
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.app.services import evaluation_pipeline as ep
        ep._EXECUTOR.shutdown(wait=False)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"FATAL {type(exc).__name__}: {exc}")
        raise
