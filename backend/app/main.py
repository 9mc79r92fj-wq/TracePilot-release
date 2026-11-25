"""TracePilot FastAPI 产品后端（第 24 步）。

提供供 React 前端调用的只读产品 API，复用现有服务层，不重新实现算法：
- GET /api/health                    健康检查
- GET /api/traces                    Trace 列表（摘要）
- GET /api/traces/{trace_id}         Trace 详情（完整 TraceRecord）
- GET /api/traces/{trace_id}/dag     DAG 节点与边
- GET /api/traces/{trace_id}/propagation  故障传播分析（可选 baseline）
- POST /api/traces/compare           两份 Trace 结构化对比

安全约束：
- 不开放任意代码执行 / 真实付费模型调用接口；
- 不向客户端返回 API Key 或敏感配置；
- 不接受客户端传入文件路径（仅 trace_id），并做路径穿越防护；
- 限制请求体大小；开发环境受限 CORS。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# 本地开发配置加载（必须在导入任何读取环境变量的模块之前）：
# 1) backend/.env：永久本地配置（已加入 .gitignore；修改后需重启后端进程）；
# 2) 项目根 .env：向后兼容旧配置。
# load_dotenv 默认不覆盖已存在的进程环境变量，因此显式 shell 变量仍优先。
_BACKEND_DIR = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _BACKEND_DIR.parent
for _env_path in (_BACKEND_DIR / ".env", _PROJECT_ROOT / ".env"):
    if _env_path.is_file():
        load_dotenv(_env_path)

from backend.app.services.execution_config import get_execution_limits as _get_execution_limits

# 统一执行限额快照（在 .env 加载之后读取；用于请求默认值/边界与前端展示）
_EXEC_LIMITS = _get_execution_limits()

from fastapi import FastAPI, HTTPException, Query
from backend.app.services.document_store import (
    build_document_context,
    router as document_router,
    get_document,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from backend.app.services.rate_limit import RateLimitMiddleware

from backend.app.services.counterfactual_engine import (
    INTERVENTION_TYPES,
    CounterfactualError,
    run_counterfactual_experiment,
)
from backend.app.services.experiment_store import (
    ExperimentBusyError,
    ExperimentNotFoundError,
    get_store,
)
from backend.app.services.minimal_repair import (
    MAX_CANDIDATES as MAX_REPAIR_CANDIDATES,
    REPAIR_TYPES,
    RepairError,
    build_repair_report,
)
from backend.app.services.repair_store import (
    RepairBusyError,
    RepairNotFoundError,
    get_repair_store,
)
from backend.app.services.failure_propagation import analyze_failure_propagation
from backend.app.services.failure_replay import (
    ReplayError,
    SCENARIO_WHITELIST,
    controlled_rerun,
    recorded_replay,
)
from backend.app.services.trace_comparator import compare_traces
from backend.app.services.trace_dag import build_dag
from backend.app.services.trace_store import (
    InvalidTraceIdError,
    TraceNotFoundError,
    list_trace_summaries,
    list_traces,
    load_trace,
)
from backend.app.services.data_governance import (
    SOURCE_CLASSES,
    SOURCE_DEFINITIONS,
    get_source_summary,
    get_trace_classification,
    trace_eligibility,
)

# 环境变量已在文件顶部统一加载（backend/.env 优先，项目根 .env 兼容旧配置）。

app = FastAPI(title="TracePilot API", version="0.1.0", description="Agent 执行轨迹分析与故障归因 API")
# STEP59_DOCUMENTS
app.include_router(document_router)

# CORS 白名单：本地默认仅 Vite 开发地址；生产通过
# TRACEPILOT_CORS_ORIGINS（逗号分隔）配置正式前端域名（如 Vercel）。
def _cors_origins() -> list[str]:
    import os

    raw = (os.environ.get("TRACEPILOT_CORS_ORIGINS") or "").strip()
    if raw:
        origins = [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]
        if origins:
            return origins
    return ["http://localhost:5173", "http://127.0.0.1:5173"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class _BodySizeLimit(BaseHTTPMiddleware):
    """限制请求体大小，超过上限返回 413。"""

    def __init__(self, app, max_bytes: int = 1_000_000):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        limit = _EXEC_LIMITS.document_upload_max_bytes + 500_000 if request.url.path == "/api/documents" and request.method == "POST" else self.max_bytes
        if content_length and content_length.isdigit() and int(content_length) > limit:
            return JSONResponse(
                status_code=413,
                content={"error": "payload_too_large", "message": "请求体过大"},
            )
        return await call_next(request)


app.add_middleware(_BodySizeLimit, max_bytes=1_000_000)
# 公共演示限流（默认关闭；Render 生产环境通过 TRACEPILOT_RATE_LIMIT_ENABLED=1 开启）
app.add_middleware(RateLimitMiddleware)


# ---------------------------------------------------------------------------
# 异常处理（统一 JSON 错误响应）
# ---------------------------------------------------------------------------

@app.exception_handler(TraceNotFoundError)
async def _trace_not_found_handler(request: Request, exc: TraceNotFoundError):
    return JSONResponse(status_code=404, content={"error": "not_found", "message": str(exc)})


@app.exception_handler(InvalidTraceIdError)
async def _invalid_trace_id_handler(request: Request, exc: InvalidTraceIdError):
    return JSONResponse(status_code=400, content={"error": "invalid_input", "message": str(exc)})


@app.exception_handler(ExperimentNotFoundError)
async def _experiment_not_found_handler(request: Request, exc: ExperimentNotFoundError):
    return JSONResponse(status_code=404, content={"error": "not_found", "message": str(exc)})


@app.exception_handler(ExperimentBusyError)
async def _experiment_busy_handler(request: Request, exc: ExperimentBusyError):
    return JSONResponse(status_code=429, content={"error": "too_many_experiments", "message": str(exc)})


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class CompareRequest(BaseModel):
    reference_trace_id: str = Field(..., description="基准 Trace ID")
    target_trace_id: str = Field(..., description="待对比 Trace ID")


class RecordedReplayRequest(BaseModel):
    trace_id: str = Field(..., min_length=1, max_length=128, description="待重放的故障 Trace ID")


class ControlledRerunRequest(BaseModel):
    trace_id: str = Field(..., min_length=1, max_length=128, description="原始故障 Trace ID")
    scenario: str | None = Field(
        default=None, max_length=64,
        description="白名单场景 key；为空时依据原始 Trace 自动推断",
    )


class CounterfactualRequest(BaseModel):
    trace_id: str = Field(..., min_length=1, max_length=128, description="原始故障 Trace ID")
    intervention: dict[str, Any] = Field(..., description="干预定义（type ∈ 白名单）")
    scenario: str | None = Field(default=None, max_length=64, description="白名单场景 key（可选）")
    replays: int = Field(default=1, ge=1, le=5, description="重放次数 1..5")


class RepairRequest(BaseModel):
    trace_id: str = Field(..., min_length=1, max_length=128, description="原始故障 Trace ID")
    baseline_trace_id: str | None = Field(
        default=None, max_length=128, description="可选：无故障基线 Trace ID"
    )
    max_candidates: int = Field(default=5, ge=1, le=5, description="候选上限 1..5")
    verify: bool = Field(default=True, description="是否执行受控验证")


class RepairValidateRequest(BaseModel):
    trace_id: str = Field(..., min_length=1, max_length=128, description="原始故障 Trace ID")
    baseline_trace_id: str | None = Field(
        default=None, max_length=128, description="可选：无故障基线 Trace ID"
    )
    max_candidates: int = Field(default=5, ge=1, le=5, description="候选上限 1..5")


class EvaluationRunRequest(BaseModel):
    dataset_id: str = Field(
        default="tracepilot-eval-v1", min_length=1, max_length=64,
        description="受控评测集 ID（白名单）",
    )
    max_cases: int | None = Field(
        default=None, ge=1, le=_EXEC_LIMITS.evaluation_max_cases, description="本次执行的案例上限，None 表示全部"
    )
    judge_mode: str = Field(
        default="none", max_length=16,
        description="LLM Judge 模式：none（默认，不启用）/ mock（规则化模拟，零成本）/ "
        "deepseek（需显式授权，未授权返回 403）",
    )
    judge_rubric_id: str = Field(
        default="answer_quality", max_length=64, description="Judge 使用的 Rubric ID"
    )
    force_new: bool = Field(
        default=False,
        description="强制创建新运行（关闭重复提交抑制），用于版本对比 / 重复测量",
    )


class CalibrationRunRequest(BaseModel):
    dataset_id: str = Field(
        default="calibration_set_v1", min_length=1, max_length=64,
        description="受控校准集 ID（白名单）",
    )
    judge_mode: str = Field(
        default="mock", max_length=16,
        description="Judge 模式：mock（默认，零成本）/ deepseek（需显式授权，否则 403）",
    )
    max_cases: int | None = Field(
        default=None, ge=1, le=50, description="校准案例上限（1..50）"
    )
    repeats: int = Field(default=1, ge=1, le=5, description="每案例重复评分次数（1..5）")


class AgentRunRequest(BaseModel):
    document_ids: list[str] = Field(
        default_factory=list, max_length=_EXEC_LIMITS.agent_max_document_refs
    )
    task: str = Field(..., min_length=1, max_length=2000, description="任务描述")
    mode: str = Field(
        default="mock", max_length=16,
        description="执行模式：mock（默认，零成本）/ real_api（需双重授权）",
    )
    confirm_real: bool = Field(
        default=False, description="真实 API 调用必须显式确认 true"
    )
    model: str | None = Field(
        default=None, max_length=64, description="真实模式模型（白名单：deepseek-chat / deepseek-reasoner）"
    )
    max_model_calls: int = Field(
        default=_EXEC_LIMITS.agent_max_model_calls, ge=1, le=20, description="模型调用轮数上限"
    )
    max_total_tokens: int = Field(
        default=_EXEC_LIMITS.agent_max_total_tokens, ge=100, le=100_000, description="Token 总量预算"
    )
    max_tool_calls: int = Field(
        default=_EXEC_LIMITS.agent_max_tool_calls, ge=0, le=20, description="工具调用次数上限"
    )
    max_wall_seconds: float = Field(
        default=_EXEC_LIMITS.agent_max_wall_seconds, ge=5.0, le=600.0, description="执行时长上限（秒）"
    )
    max_tool_rounds: int = Field(
        default=_EXEC_LIMITS.agent_max_tool_rounds, ge=1, le=12, description="工具循环轮数上限"
    )
    max_tokens_per_call: int = Field(
        default=_EXEC_LIMITS.agent_max_output_tokens, ge=64, le=8192, description="单次调用输出 Token 上限"
    )


class PipelineCreateRequest(BaseModel):
    dataset_id: str = Field(..., min_length=1, max_length=64)
    max_cases: int | None = Field(default=None, ge=1, le=_EXEC_LIMITS.evaluation_max_cases)
    execution_mode: str = Field(default="mock", max_length=16)
    confirm_real: bool = False
    judge_mode: str = Field(default="none", max_length=16)
    judge_rubric_id: str = Field(default="answer_quality", max_length=64)
    baseline_run_id: str | None = Field(default=None, max_length=64)
    enable_attribution: bool = True
    enable_failure_patterns: bool = True
    enable_version_comparison: bool = True
    enable_regression: bool = True
    enable_quality_gate: bool = True
    random_seed: int = Field(default=42, ge=0, le=2147483647)
    label: str | None = Field(default=None, max_length=120)
    max_runtime_seconds: float | None = Field(
        default=None, ge=1.0, le=_EXEC_LIMITS.pipeline_max_runtime_seconds
    )
    max_retries: int = Field(default=0, ge=0, le=2)


class QualityGateRequest(BaseModel):
    baseline_run_id: str | None = Field(default=None, max_length=64)
    candidate_run_id: str | None = Field(default=None, max_length=64)
    comparison_id: str | None = Field(default=None, max_length=64)
    regression_report_id: str | None = Field(default=None, max_length=64)
    ci_run_id: str | None = Field(default=None, max_length=128)


class VersionComparisonRequest(BaseModel):
    baseline_run_id: str = Field(..., min_length=1, max_length=64)
    candidate_run_id: str = Field(..., min_length=1, max_length=64)
    max_cases: int | None = Field(default=50, ge=1, le=50)


class RegressionRuleOverride(BaseModel):
    rule_id: str = Field(..., min_length=1, max_length=64)
    threshold: float | None = Field(default=None, ge=0.0)
    enabled: bool | None = None
    severity: str | None = Field(default=None, max_length=16)
    blocking: bool | None = None
    min_absolute: float | None = Field(default=None, ge=0.0)


class RegressionRequest(BaseModel):
    baseline_run_id: str | None = Field(default=None, max_length=64)
    candidate_run_id: str | None = Field(default=None, max_length=64)
    comparison_id: str | None = Field(default=None, max_length=64)
    max_cases: int | None = Field(default=50, ge=1, le=50)
    rules: list[RegressionRuleOverride] | None = Field(default=None, max_length=16)


class FailurePatternRequest(BaseModel):
    evaluation_run_id: str = Field(..., min_length=1, max_length=64)
    max_cases: int | None = Field(default=50, ge=1, le=50, description="分析案例上限 ≤50")
    strategy: str = Field(default="fingerprint", max_length=32, description="聚类策略")


class FailurePatternCompareRequest(BaseModel):
    run_a: str = Field(..., min_length=1, max_length=64)
    run_b: str = Field(..., min_length=1, max_length=64)
    max_cases: int | None = Field(default=50, ge=1, le=50)
    strategy: str = Field(default="fingerprint", max_length=32)


class AttributionBatchRequest(BaseModel):
    evaluation_run_id: str = Field(..., min_length=1, max_length=64)
    case_ids: list[str] | None = Field(
        default=None, max_length=20, description="仅归因指定案例（≤20）"
    )
    max_cases: int | None = Field(default=10, ge=1, le=20, description="单次归因案例上限 ≤20")


class RedTeamGenerateRequest(BaseModel):
    attack_types: list[str] | None = Field(
        default=None, max_length=6, description="限定攻击类型；None 表示六类全部"
    )
    difficulties: list[str] | None = Field(
        default=None, max_length=3, description="限定难度：easy/medium/hard"
    )
    variations: int = Field(default=2, ge=1, le=4, description="每模板变体数")
    seed: int = Field(default=42, ge=0, le=2**31 - 1, description="随机种子（同种子可复现）")
    include_canary: bool = Field(
        default=True, description="是否包含金丝雀负控案例（用于验证检测器）"
    )


class RedTeamRunRequest(BaseModel):
    regenerate: bool = Field(
        default=False, description="true 时先按 seed 重新生成数据集再执行"
    )
    attack_types: list[str] | None = Field(default=None, max_length=6)
    difficulties: list[str] | None = Field(default=None, max_length=3)
    variations: int = Field(default=2, ge=1, le=4)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    include_canary: bool = Field(default=True)
    max_cases: int | None = Field(default=None, ge=1, le=60, description="执行案例上限 ≤60")
    case_ids: list[str] | None = Field(
        default=None, max_length=60, description="仅执行指定案例（选择案例运行）"
    )
    persist_traces: bool = Field(default=True, description="是否把每个案例 Trace 落盘")


class MultiJudgeRunRequest(BaseModel):
    dataset_id: str = Field(
        default="tracepilot-eval-v1", min_length=1, max_length=64,
        description="评测集 ID（白名单）",
    )
    config_id: str | None = Field(
        default="mock-mixed-variants", max_length=64,
        description="预设多 Judge 配置组 ID（与 judges 二选一）",
    )
    judges: list[dict[str, Any]] | None = Field(
        default=None, max_length=5, description="自定义 Judge 配置列表（≤5）"
    )
    rubric_id: str = Field(default="answer_quality", max_length=64)
    strategy: str = Field(
        default="median", max_length=32, description="仲裁策略：median / mean / calibration_weighted"
    )
    threshold: float = Field(default=2.0, ge=0.0, le=4.0, description="分歧阈值（1~5 分制）")
    severe_threshold: float = Field(
        default=3.0, ge=0.0, le=4.0, description="严重分歧阈值（≥ 则 INCONCLUSIVE）"
    )
    max_cases: int | None = Field(default=None, ge=1, le=30, description="案例上限（1..30）")


@app.exception_handler(RepairNotFoundError)
async def _repair_not_found_handler(request: Request, exc: RepairNotFoundError):
    return JSONResponse(status_code=404, content={"error": "not_found", "message": str(exc)})


@app.exception_handler(RepairBusyError)
async def _repair_busy_handler(request: Request, exc: RepairBusyError):
    return JSONResponse(status_code=429, content={"error": "too_many_repairs", "message": str(exc)})


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    """健康检查。

    仅做目录级文件计数：冷启动时完整解析全部 Trace 需要数十秒，会让健康检查超时，
    导致前端首屏误报“后端离线”。完整数量仍由 /api/traces 提供。
    """
    from backend.app.services.trace_store import DEFAULT_TRACES_DIR

    try:
        trace_count = sum(1 for _ in DEFAULT_TRACES_DIR.glob("*.json"))
    except OSError:
        trace_count = 0
    return {
        "status": "ok",
        "service": "tracepilot-api",
        "trace_count": trace_count,
    }


@app.get("/api/traces")
def get_traces(
    source_class: str | None = Query(
        None, description="按数据来源过滤：REAL_API / MOCK / SIMULATED / REPLAY / UNKNOWN"
    ),
    status: str | None = Query(None, description="按状态过滤：succeeded / failed / running"),
    fault_only: bool = Query(False, description="仅返回含故障注入的 Trace"),
) -> dict[str, Any]:
    """Trace 摘要列表。

    默认返回主 Trace 目录（Agent 运行 Trace）全部记录；筛选只收窄结果，
    并始终附带全量分类计数，避免前端把当前筛选结果误当作全库。
    """
    summaries = list_trace_summaries()
    total_count = len(summaries)

    wanted_source = source_class.strip().upper() if source_class else None
    if wanted_source and wanted_source not in SOURCE_CLASSES:
        raise HTTPException(
            status_code=400,
            detail=f"source_class 非法: {source_class}（可选: {', '.join(SOURCE_CLASSES)}）",
        )
    wanted_status = status.strip().lower() if status else None

    filtered = summaries
    if wanted_source:
        filtered = [s for s in filtered if s.get("source_class") == wanted_source]
    if wanted_status:
        filtered = [s for s in filtered if str(s.get("status", "")).lower() == wanted_status]
    if fault_only:
        filtered = [s for s in filtered if s.get("fault_types")]

    by_source = {name: 0 for name in SOURCE_CLASSES}
    by_status: dict[str, int] = {}
    for s in summaries:
        by_source[s.get("source_class", "UNKNOWN")] = (
            by_source.get(s.get("source_class", "UNKNOWN"), 0) + 1
        )
        by_status[s.get("status", "unknown")] = by_status.get(s.get("status", "unknown"), 0) + 1

    return {
        "count": len(filtered),
        "total_count": total_count,
        "scope": "agent_traces",
        "scope_note": "仅主 Trace 目录（Agent 运行记录）；评测 Trace 见成本分析的全库口径",
        "filters": {
            "source_class": wanted_source,
            "status": wanted_status,
            "fault_only": fault_only,
        },
        "by_source": by_source,
        "by_status": by_status,
        "traces": filtered,
    }


@app.get("/api/traces/{trace_id}")
def get_trace(trace_id: str) -> dict[str, Any]:
    record = load_trace(trace_id)
    return {"trace": record.model_dump(mode="json")}


@app.get("/api/traces/{trace_id}/dag")
def get_trace_dag(trace_id: str) -> dict[str, Any]:
    record = load_trace(trace_id)
    dag = build_dag(record)
    return {"trace_id": trace_id, "dag": dag.to_dict()}


@app.get("/api/traces/{trace_id}/propagation")
def get_propagation(
    trace_id: str,
    baseline_trace_id: str | None = Query(None, description="可选：同任务无故障基线 Trace ID"),
) -> dict[str, Any]:
    record = load_trace(trace_id)
    baseline = load_trace(baseline_trace_id) if baseline_trace_id else None
    report = analyze_failure_propagation(record, baseline)
    return {"trace_id": trace_id, "propagation": report}


@app.post("/api/traces/compare")
def compare(body: CompareRequest) -> dict[str, Any]:
    reference = load_trace(body.reference_trace_id)
    target = load_trace(body.target_trace_id)
    report = compare_traces(reference, target)
    return {
        "reference_trace_id": body.reference_trace_id,
        "target_trace_id": body.target_trace_id,
        "comparison": report,
    }


# ---------------------------------------------------------------------------
# Failure Replay（第 30 步）
# ---------------------------------------------------------------------------

@app.get("/api/replay/scenarios")
def replay_scenarios() -> dict[str, Any]:
    """返回受控重跑允许的固定场景白名单（不暴露任何执行能力）。"""
    return {
        "scenarios": [
            {"key": key, "label": meta["label"], "tool": meta["tool"]}
            for key, meta in SCENARIO_WHITELIST.items()
        ]
    }


@app.post("/api/replay/recorded")
def replay_recorded(body: RecordedReplayRequest) -> dict[str, Any]:
    """模式一：只读重放原始故障 Trace（不执行工具、不调用模型）。"""
    record = load_trace(body.trace_id)
    return {"replay": recorded_replay(record)}


@app.post("/api/replay/controlled")
def replay_controlled(body: ControlledRerunRequest) -> dict[str, Any]:
    """模式二：受控重跑（模拟模型 + 白名单安全工具），生成新 Trace 并对比。"""
    record = load_trace(body.trace_id)
    try:
        result = controlled_rerun(record, scenario=body.scenario)
    except ReplayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 注意：重放结果不写入 data/traces，避免覆盖/污染原始 Trace 数据
    return {"replay": result}


# ---------------------------------------------------------------------------
# Counterfactual Engine（第 31 步）
# ---------------------------------------------------------------------------

@app.get("/api/counterfactual/intervention-types")
def counterfactual_types() -> dict[str, Any]:
    """返回允许的干预类型白名单（不暴露任何执行能力）。"""
    return {"intervention_types": sorted(INTERVENTION_TYPES)}


@app.post("/api/counterfactual/experiments")
def counterfactual_experiment(body: CounterfactualRequest) -> dict[str, Any]:
    """创建并异步执行一次受控反事实实验（返回 experiment_id 与运行状态）。

    - Trace ID 与干预配置均经 Pydantic + 引擎白名单双重校验；
    - 并发受限（超出返回 429）、单次执行有超时上限；
    - 相同配置在窗口内重复提交直接复用已有实验（deduplicated）。
    """
    record = load_trace(body.trace_id)  # 404 / 400 由异常处理器统一返回

    # 先同步校验干预配置（非法立即 400，不产生实验记录）
    try:
        from backend.app.services.counterfactual_engine import _validate_intervention

        validated = _validate_intervention(body.intervention)
    except CounterfactualError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store = get_store()
    try:
        submission = store.submit(record, validated, body.scenario, body.replays)
    except ExperimentBusyError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    return {
        "experiment_id": submission["experiment_id"],
        "status": submission["status"],
        "deduplicated": submission.get("deduplicated", False),
        "source_trace_id": submission["source_trace_id"],
        "intervention": submission["intervention"],
        "links": {
            "self": f"/api/counterfactual/experiments/{submission['experiment_id']}",
            "results": f"/api/counterfactual/experiments/{submission['experiment_id']}/results",
            "source_trace": f"/api/traces/{submission['source_trace_id']}",
        },
    }


@app.get("/api/counterfactual/experiments")
def list_counterfactual_experiments() -> dict[str, Any]:
    """列出内存中登记的反事实实验（不含结果体，避免响应过大）。"""
    items = get_store().list_all()
    return {"count": len(items), "experiments": items}


@app.get("/api/counterfactual/experiments/{experiment_id}")
def get_counterfactual_experiment(experiment_id: str) -> dict[str, Any]:
    """查询实验状态（running / succeeded / failed）与关联信息。"""
    exp = get_store().get(experiment_id)
    return {
        "experiment_id": exp["experiment_id"],
        "status": exp["status"],
        "created_at": exp["created_at"],
        "source_trace_id": exp["source_trace_id"],
        "intervention": exp["intervention"],
        "scenario": exp["scenario"],
        "replays": exp["replays"],
        "error": exp["error"],
        "elapsed_ms": exp.get("elapsed_ms"),
        "links": {
            "results": f"/api/counterfactual/experiments/{experiment_id}/results",
            "source_trace": f"/api/traces/{exp['source_trace_id']}",
        },
    }


@app.get("/api/counterfactual/experiments/{experiment_id}/results")
def get_counterfactual_results(experiment_id: str) -> dict[str, Any]:
    """查询实验结果：观察/干预结果、差异报告、证据等级与限制说明。"""
    payload = get_store().get_results(experiment_id)
    if payload["status"] == "running":
        return {"experiment_id": experiment_id, "status": "running", "result": None}
    if payload["status"] == "failed":
        return {
            "experiment_id": experiment_id,
            "status": "failed",
            "error": payload["error"],
            "result": None,
        }
    result = payload["result"]
    return {
        "experiment_id": experiment_id,
        "status": "succeeded",
        "result": result,
        # 便于前端直接消费的关键字段（原样取自引擎报告，不做加工）
        "observed_outcome": result["observed_outcome"],
        "intervention_outcome": result["intervention_outcome"],
        "observed_difference": result["observed_difference"],
        "causal_evidence_level": result["causal_evidence_level"],
        "causal_evidence_note": result["causal_evidence_note"],
        "environment": result["environment"],
        "control_conditions": result["control_conditions"],
        "confounders": result["confounders"],
        "limitations": result["limitations"],
        "reproducible": result["reproducible"],
        "original_trace_modified": result["original_trace_modified"],
        "links": {
            "source_trace": f"/api/traces/{result['source_trace_id']}",
            "intervention_trace": f"/api/traces/{result['intervention_outcome']['trace_id']}",
            "dag": f"/api/traces/{result['source_trace_id']}/dag",
        },
    }


# ---------------------------------------------------------------------------
# Minimal Repair Engine（第 33 步）
# ---------------------------------------------------------------------------

@app.get("/api/repair/types")
def repair_types() -> dict[str, Any]:
    """返回允许的修复类型白名单（不暴露任何执行能力）。"""
    return {"repair_types": list(REPAIR_TYPES)}


@app.post("/api/repair/report")
def repair_report(body: RepairRequest) -> dict[str, Any]:
    """生成最小修复报告：候选生成 + 受控验证 + 可解释评分。

    - 原始 Trace 只读，验证全部通过 CounterfactualEngine 生成独立 Trace；
    - 未执行验证的候选标记 not_tested，绝不声称有效；
    - 评分是排序启发式，不是成功概率。
    """
    record = load_trace(body.trace_id)
    baseline = load_trace(body.baseline_trace_id) if body.baseline_trace_id else None
    try:
        report = build_repair_report(
            record,
            baseline_trace=baseline,
            max_candidates=body.max_candidates,
            verify=body.verify,
        )
    except RepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"report": report}


# ---------------------------------------------------------------------------
# Repair Studio（第 34 步）
# ---------------------------------------------------------------------------

@app.get("/api/repairs/candidates/{trace_id}")
def repair_candidates(trace_id: str) -> dict[str, Any]:
    """获取故障 Trace 的修复候选（仅生成，不执行验证，快速返回）。

    候选生成完全基于已有 Trace 与故障传播分析，不执行任何工具或模型。
    """
    record = load_trace(trace_id)  # 404 / 400 由统一异常处理器返回
    from backend.app.services.minimal_repair import generate_candidates

    candidates = generate_candidates(record, max_candidates=MAX_REPAIR_CANDIDATES)
    fault_steps = [
        {
            "step_id": s.step_id,
            "step_type": s.step_type.value,
            "tool_name": s.tool_name,
            "fault_type": s.metadata.get("fault_injection", {}).get("type"),
            "status": s.status.value,
            "error_type": s.error.error_type if s.error else None,
        }
        for s in record.steps
        if isinstance(s.metadata.get("fault_injection"), dict)
        and s.metadata["fault_injection"].get("injected") is True
    ]
    return {
        "trace_id": trace_id,
        "status": record.status.value,
        "step_count": len(record.steps),
        "fault_steps": fault_steps,
        "candidate_count": len(candidates),
        "candidates": [
            {
                "repair_id": c.repair_id,
                "repair_type": c.repair_type,
                "target_step_id": c.target_step_id,
                "target_tool": c.target_tool,
                "modification": c.modification,
                "expected_effect": c.expected_effect,
                "risks": c.risks,
                "applicable_conditions": c.applicable_conditions,
                "verification_status": "not_tested",
            }
            for c in candidates
        ],
        "note": "候选尚未验证；需提交受控验证后才能判断有效性。",
    }


@app.post("/api/repairs/validate")
def repair_validate(body: RepairValidateRequest) -> dict[str, Any]:
    """提交受控修复验证任务（异步），返回 report_id 与运行状态。"""
    record = load_trace(body.trace_id)
    baseline = load_trace(body.baseline_trace_id) if body.baseline_trace_id else None
    store = get_repair_store()
    try:
        submission = store.submit(
            record,
            baseline_trace=baseline,
            max_candidates=body.max_candidates,
            verify=True,
        )
    except RepairBusyError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {
        "report_id": submission["report_id"],
        "status": submission["status"],
        "source_trace_id": submission["source_trace_id"],
        "max_candidates": submission["max_candidates"],
        "links": {
            "report": f"/api/repairs/reports/{submission['report_id']}",
            "candidates": f"/api/repairs/candidates/{submission['source_trace_id']}",
            "source_trace": f"/api/traces/{submission['source_trace_id']}",
        },
    }


@app.get("/api/repairs/reports/{report_id}")
def repair_report_get(report_id: str) -> dict[str, Any]:
    """查询修复验证报告：状态、候选、修复后 Trace、前后对比、评分、推荐依据。"""
    rep = get_repair_store().get(report_id)
    if rep["status"] == "running":
        return {
            "report_id": report_id,
            "status": "running",
            "source_trace_id": rep["source_trace_id"],
            "result": None,
        }
    if rep["status"] == "failed":
        return {
            "report_id": report_id,
            "status": "failed",
            "source_trace_id": rep["source_trace_id"],
            "error": rep["error"],
            "result": None,
        }
    result = rep["result"]
    return {
        "report_id": report_id,
        "status": "succeeded",
        "elapsed_ms": rep.get("elapsed_ms"),
        "source_trace_id": result["source_trace_id"],
        # 原样透传引擎报告，不做加工或美化
        "result": result,
        "links": {
            "source_trace": f"/api/traces/{result['source_trace_id']}",
            "dag": f"/api/traces/{result['source_trace_id']}/dag",
        },
    }


# ---------------------------------------------------------------------------
# Evaluation Engine（第 35 步）
# ---------------------------------------------------------------------------

@app.get("/api/evaluation/datasets")
def evaluation_datasets() -> dict[str, Any]:
    """列出所有白名单评测集（仅元信息，不含案例内容）。

    第 47 步修复：此前硬编码只返回 dataset_v1，导致新增的受控失败数据集
    无法在 UI 中选择。
    """
    from backend.app.services.evaluation_engine import DATASET_DIR, load_dataset
    from backend.app.services.evaluation_store import ALLOWED_DATASETS

    items: list[dict[str, Any]] = []
    for dataset_id, filename in sorted(ALLOWED_DATASETS.items()):
        path = DATASET_DIR / filename
        try:
            data = load_dataset(path)
        except Exception:
            continue
        items.append({
            "dataset_id": data.get("dataset_id", dataset_id),
            "dataset_version": data.get("dataset_version"),
            "case_count": len(data.get("cases") or []),
            "data_source": data.get("data_source"),
            "data_source_note": data.get("data_source_note"),
        })
    return {"count": len(items), "datasets": items}
@app.get("/api/evaluation/datasets/{dataset_id}")
def evaluation_dataset_detail(dataset_id: str) -> dict[str, Any]:
    """查询单个受控评测集详情（案例元信息，不含可执行代码）。"""
    from backend.app.services.evaluation_engine import DATASET_DIR, load_dataset
    from backend.app.services.evaluation_store import ALLOWED_DATASETS

    if dataset_id not in ALLOWED_DATASETS:
        raise HTTPException(status_code=404, detail=f"评测集不存在: {dataset_id}")
    data = load_dataset(DATASET_DIR / ALLOWED_DATASETS[dataset_id])
    return {
        "dataset_id": data["dataset_id"],
        "dataset_version": data["dataset_version"],
        "data_source": data["data_source"],
        "data_source_note": data.get("data_source_note"),
        "case_count": len(data["cases"]),
        "scenario_types": sorted({c["scenario_type"] for c in data["cases"]}),
        "cases": [
            {
                "case_id": c["case_id"],
                "task_description": c["task_description"],
                "scenario_type": c["scenario_type"],
                "data_source": c["data_source"],
            }
            for c in data["cases"]
        ],
    }


@app.get("/api/evaluation/runs")
def evaluation_runs_list() -> dict[str, Any]:
    """列出内存中登记的评测运行（不含报告体）。"""
    from backend.app.services.evaluation_store import get_evaluation_store

    items = get_evaluation_store().list_all()
    return {"count": len(items), "runs": items}


@app.post("/api/evaluation/runs")
def evaluation_run_create(body: EvaluationRunRequest) -> dict[str, Any]:
    """创建异步评测运行，立即返回 run_id 与运行状态。

    - 仅允许受控数据集白名单（非法 dataset_id → 404）；
    - 并发受限（超出 429）、执行有超时上限、重复提交复用，避免资源耗尽；
    - 全程使用模拟模型，不调用付费 API。
    """
    from backend.app.services.evaluation_engine import EvaluationError as EvalError
    from backend.app.services.evaluation_store import (
        ALLOWED_DATASETS,
        EvaluationBusyError as EvalBusy,
        get_evaluation_store,
    )

    if body.dataset_id not in ALLOWED_DATASETS:
        raise HTTPException(status_code=404, detail=f"评测集不存在: {body.dataset_id}")

    store = get_evaluation_store()
    try:
        submission = store.submit(
            body.dataset_id,
            body.max_cases,
            body.judge_mode,
            body.judge_rubric_id,
            dedup=not body.force_new,
        )
    except EvalBusy as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except EvalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "run_id": submission["run_id"],
        "status": submission["status"],
        "dataset_id": submission["dataset_id"],
        "max_cases": submission["max_cases"],
        "links": {
            "self": f"/api/evaluation/runs/{submission['run_id']}",
            "bad_cases": f"/api/evaluation/runs/{submission['run_id']}/bad-cases",
        },
    }


@app.get("/api/evaluation/runs/{run_id}")
def evaluation_run_detail(run_id: str) -> dict[str, Any]:
    """查询评测运行状态与完整报告（含指标、案例结果、Trace 关联、限制说明）。"""
    from backend.app.services.evaluation_store import (
        EvaluationNotFoundError as EvalNotFound,
        get_evaluation_store,
    )

    try:
        run = get_evaluation_store().get(run_id)
    except EvalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if run["status"] == "running":
        return {
            "run_id": run_id, "status": "running", "dataset_id": run["dataset_id"],
            "report": None,
        }
    if run["status"] == "failed":
        return {
            "run_id": run_id, "status": "failed", "dataset_id": run["dataset_id"],
            "error": run["error"], "report": None,
        }
    return {
        "run_id": run_id,
        "status": "succeeded",
        "elapsed_ms": run.get("elapsed_ms"),
        "dataset_id": run["dataset_id"],
        "report": run["report"],
        "links": {
            "bad_cases": f"/api/evaluation/runs/{run_id}/bad-cases",
        },
    }


@app.get("/api/evaluation/runs/{run_id}/bad-cases")
def evaluation_bad_cases(run_id: str) -> dict[str, Any]:
    """查询 Bad Case 列表（含失败维度与可追溯的 Trace ID）。"""
    from backend.app.services.evaluation_store import (
        EvaluationNotFoundError as EvalNotFound,
        get_evaluation_store,
    )

    try:
        run = get_evaluation_store().get(run_id)
    except EvalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if run["status"] != "succeeded":
        return {"run_id": run_id, "status": run["status"], "bad_cases": []}

    report = run["report"]
    bad = report["bad_cases"]
    return {
        "run_id": run_id,
        "status": "succeeded",
        "bad_case_count": len(bad),
        "bad_cases": bad,
        "unknown_cases": [
            {
                "case_id": r["case_id"],
                "trace_id": r["trace_id"],
                "unknown_dimensions": [
                    k for k, m in r["metrics"].items() if m["status"] == "UNKNOWN"
                ],
            }
            for r in report["case_results"]
            if r["overall_status"] == "UNKNOWN"
        ],
        "execution_errors": report.get("execution_errors", []),
        "limitations": report.get("limitations", []),
    }


@app.get("/api/evaluation/runs/{run_id}/cases/{case_id}")
def evaluation_case_detail(run_id: str, case_id: str) -> dict[str, Any]:
    """查询单个案例的完整评测证据（六维度指标 + Trace 关联），用于证据下钻。"""
    from backend.app.services.evaluation_store import (
        EvaluationNotFoundError as EvalNotFound,
        get_evaluation_store,
    )

    try:
        run = get_evaluation_store().get(run_id)
    except EvalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if run["status"] != "succeeded":
        return {"run_id": run_id, "status": run["status"], "case": None}

    for r in run["report"]["case_results"]:
        if r["case_id"] == case_id:
            return {
                "run_id": run_id,
                "status": "succeeded",
                "case": r,
                "linked_trace_id": r["trace_id"],
                "links": {
                    "trace": f"/api/traces/{r['trace_id']}",
                    "dag": f"/api/traces/{r['trace_id']}/dag",
                },
            }
    raise HTTPException(status_code=404, detail=f"案例不存在: {case_id}")


# ---------------------------------------------------------------------------
# LLM-as-a-Judge（第 38 步）
# ---------------------------------------------------------------------------

@app.get("/api/evaluation/judge/rubrics")
def judge_rubrics() -> dict[str, Any]:
    """列出可用 Rubric（元信息，不含评分逻辑）。"""
    from backend.app.services.llm_judge import list_rubrics

    items = list_rubrics()
    return {"count": len(items), "rubrics": items}


@app.get("/api/evaluation/judge/rubrics/{rubric_id}")
def judge_rubric_detail(rubric_id: str) -> dict[str, Any]:
    """查看单个 Rubric 全文（维度、评分规则、证据要求、UNKNOWN 条件）。"""
    from backend.app.services.llm_judge import (
        JUDGE_DIMENSIONS,
        JudgeError,
        JudgeRubricNotFoundError,
        load_rubric,
    )

    try:
        r = load_rubric(rubric_id)
    except JudgeRubricNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except JudgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "rubric": r.model_dump(),
        "allowed_dimensions": list(JUDGE_DIMENSIONS),
    }


@app.get("/api/evaluation/judge/info")
def judge_info() -> dict[str, Any]:
    """返回 Judge 可用性信息（不返回任何密钥）。

    - mock：始终可用，零成本，标记为 simulated；
    - deepseek：仅当环境变量显式授权时才标记为 available。
    """
    import os

    from backend.app.services.llm_judge import (
        ENV_ALLOW_REAL,
        JUDGE_ENGINE_VERSION,
        PROMPT_VERSION,
    )

    real_enabled = os.environ.get(ENV_ALLOW_REAL, "").strip() in {"1", "true", "True", "yes"}
    return {
        "judge_engine_version": JUDGE_ENGINE_VERSION,
        "prompt_version": PROMPT_VERSION,
        "default_mode": "none",
        "modes": [
            {
                "mode": "none",
                "available": True,
                "is_mock": None,
                "note": "默认：不启用 Judge，仅使用确定性验证器",
            },
            {
                "mode": "mock",
                "available": True,
                "is_mock": True,
                "simulated": True,
                "note": "规则化模拟评审：零成本、可复现，不代表真实模型判断",
            },
            {
                "mode": "deepseek",
                "available": real_enabled,
                "is_mock": False,
                "note": (
                    "真实模型评审：需 TRACEPILOT_JUDGE_ALLOW_REAL=1 且配置 API Key；"
                    "会产生付费调用，测试环境禁止启用"
                ),
            },
        ],
        "real_judge_enabled": real_enabled,
        "paid_api_called": False,
    }


# ---------------------------------------------------------------------------
# Judge Calibration（第 39 步）
# ---------------------------------------------------------------------------

@app.get("/api/evaluation/calibration/datasets")
def calibration_datasets() -> dict[str, Any]:
    """列出可用校准数据集（含标注来源，明确 synthetic_reference）。"""
    from backend.app.services.judge_calibration import list_calibration_datasets

    items = list_calibration_datasets()
    return {"count": len(items), "datasets": items}


@app.post("/api/evaluation/calibration/runs")
def calibration_run(body: CalibrationRunRequest) -> dict[str, Any]:
    """运行一次 Judge 校准（默认 Mock，零成本；真实模式需显式授权）。

    - 仅允许受控校准集白名单，不接受路径；
    - 限制案例数（≤50）与重复次数（≤5）；
    - Mock / Real 结果按 provider_kind 分区，绝不混用。
    """
    from backend.app.services.judge_calibration import (
        CalibrationError as CalError,
        load_calibration_dataset,
        run_calibration,
    )
    from backend.app.services.llm_judge import (
        JudgeError as LLMJudgeError,
        RealJudgeDisabledError,
        build_judge,
    )

    try:
        dataset = load_calibration_dataset(body.dataset_id)
    except CalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        judge = build_judge(body.judge_mode)
    except RealJudgeDisabledError as exc:
        # 真实 Judge 未授权：明确拒绝，绝不产生付费调用
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LLMJudgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        report = run_calibration(
            judge,
            dataset,
            max_cases=body.max_cases,
            repeats=body.repeats,
        )
    except (CalError, LLMJudgeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    report["paid_api_called"] = not bool(report["judge"]["is_mock"])
    return {"report": report}


# ---------------------------------------------------------------------------
# Multi-Judge Arbitration（第 40 步）
# ---------------------------------------------------------------------------

@app.get("/api/evaluation/multi-judge/configs")
def multi_judge_configs() -> dict[str, Any]:
    """列出预设的多 Judge 配置组（全部 Mock，零成本）。"""
    from backend.app.services.multi_judge import DEFAULT_CONFIG_SETS

    return {
        "count": len(DEFAULT_CONFIG_SETS),
        "configs": [
            {
                "config_id": c.config_id,
                "title": c.title,
                "description": c.description,
                "judge_count": len(c.judges),
                "expected_relationship": c.expected_relationship,
                "limitation": c.limitation,
                "judges": [
                    {
                        "judge_id": j.judge_id,
                        "mode": j.mode,
                        "variant": j.variant,
                        "sampling_index": j.sampling_index,
                    }
                    for j in c.judges
                ],
            }
            for c in DEFAULT_CONFIG_SETS.values()
        ],
    }


@app.post("/api/evaluation/multi-judge/runs")
def multi_judge_run_create(body: MultiJudgeRunRequest) -> dict[str, Any]:
    """创建异步多 Judge 评测（默认 Mock，零成本）。

    - 仅允许受控数据集与预设配置组（或显式 Mock 配置）；
    - 真实 Judge 需显式授权，未授权直接 403，绝不产生付费调用；
    - 并发 1、超时 120s，避免资源耗尽。
    """
    from backend.app.services.evaluation_engine import execute_case
    from backend.app.services.evaluation_engine import load_dataset as _load_ds
    from backend.app.services.multi_judge import (
        DEFAULT_CONFIG_SETS,
        MAX_CASES_PER_RUN,
        MultiJudgeError as MJError,
        JudgeSpec as MJSpec,
        get_multi_judge_store,
    )
    from backend.app.services.evaluation_store import ALLOWED_DATASETS as EVAL_DATASETS
    from backend.app.services.llm_judge import RealJudgeDisabledError, build_judge

    if body.dataset_id not in EVAL_DATASETS:
        raise HTTPException(status_code=404, detail=f"评测集不存在: {body.dataset_id}")

    # 解析配置
    if body.config_id:
        if body.config_id not in DEFAULT_CONFIG_SETS:
            raise HTTPException(status_code=404, detail=f"配置组不存在: {body.config_id}")
        specs = list(DEFAULT_CONFIG_SETS[body.config_id].judges)
    else:
        if not body.judges:
            raise HTTPException(status_code=400, detail="必须提供 config_id 或 judges")
        try:
            specs = [MJSpec.model_validate(j) for j in body.judges]
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"judges 配置非法: {exc}") from exc

    # 真实 Judge 预检：未授权则立即 403，绝不排队执行
    for spec in specs:
        if spec.mode == "deepseek":
            try:
                build_judge("deepseek", model=spec.model_name)
            except RealJudgeDisabledError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc

    # 准备案例（复用 EvaluationEngine 的受控执行器）
    try:
        ds = _load_ds()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cases = ds["cases"][: (body.max_cases or MAX_CASES_PER_RUN)]
    traces = [(c["case_id"], execute_case(c)) for c in cases]

    store = get_multi_judge_store()
    try:
        submission = store.submit(
            {
                "traces": traces,
                "specs": specs,
                "rubric_id": body.rubric_id,
                "strategy": body.strategy,
                "threshold": body.threshold,
                "severe_threshold": body.severe_threshold,
            }
        )
    except MJError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    return {
        "run_id": submission["run_id"],
        "status": submission["status"],
        "dataset_id": body.dataset_id,
        "judge_count": len(specs),
        "case_count": len(traces),
        "strategy": body.strategy,
        "links": {
            "self": f"/api/evaluation/multi-judge/runs/{submission['run_id']}",
            "scores": f"/api/evaluation/multi-judge/runs/{submission['run_id']}/scores",
            "disagreement": f"/api/evaluation/multi-judge/runs/{submission['run_id']}/disagreement",
            "arbitration": f"/api/evaluation/multi-judge/runs/{submission['run_id']}/arbitration",
        },
    }


def _mj_get_or_404(run_id: str) -> dict[str, Any]:
    from backend.app.services.multi_judge import (
        MultiJudgeError as MJError,
        get_multi_judge_store,
    )

    try:
        return get_multi_judge_store().get(run_id)
    except MJError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/evaluation/multi-judge/runs")
def multi_judge_runs_list() -> dict[str, Any]:
    from backend.app.services.multi_judge import get_multi_judge_store

    items = get_multi_judge_store().list_all()
    return {"count": len(items), "runs": items}


@app.get("/api/evaluation/multi-judge/runs/{run_id}")
def multi_judge_run_status(run_id: str) -> dict[str, Any]:
    rec = _mj_get_or_404(run_id)
    base = {"run_id": run_id, "status": rec["status"], "created_at": rec["created_at"]}
    if rec["status"] == "running":
        return {**base, "result": None}
    if rec["status"] == "failed":
        return {**base, "error": rec["error"], "result": None}

    rep = rec["result"]
    return {
        **base,
        "elapsed_ms": rec.get("elapsed_ms"),
        "rubric": rep["rubric"],
        "judges": rep["judges"],
        "independence": rep["independence"],
        "case_count": rep["case_count"],
        "case_ids": [c["case_id"] for c in rep["cases"]],
        "summary": {
            "flagged_count": rep["disagreement"]["flagged_count"],
            "conclusive_count": rep["arbitration"]["summary"]["conclusive_count"],
            "inconclusive_count": rep["arbitration"]["summary"]["inconclusive_count"],
            "human_review_required_count": rep["arbitration"]["summary"]["human_review_required_count"],
            "aggregated_mean": rep["arbitration"]["summary"]["aggregated_mean"],
            "strategy": rep["arbitration"]["strategy"],
        },
        "engine_version": rep["multi_judge_engine_version"],
        "paid_api_called": rep["paid_api_called"],
        "limitations": rep["limitations"],
    }


@app.get("/api/evaluation/multi-judge/runs/{run_id}/scores")
def multi_judge_run_scores(run_id: str) -> dict[str, Any]:
    """查询各 Judge 的独立评分详情（含理由、证据、版本）。"""
    rec = _mj_get_or_404(run_id)
    if rec["status"] != "succeeded":
        return {"run_id": run_id, "status": rec["status"], "cases": []}
    rep = rec["result"]
    return {
        "run_id": run_id,
        "status": "succeeded",
        "judges": rep["judges"],
        "independence": rep["independence"],
        "cases": rep["cases"],
        "errors": rep["errors"],
    }


@app.get("/api/evaluation/multi-judge/runs/{run_id}/disagreement")
def multi_judge_run_disagreement(run_id: str) -> dict[str, Any]:
    """查询分歧报告（最大分差、方差、分布、一致性、分歧标记、证据冲突）。"""
    rec = _mj_get_or_404(run_id)
    if rec["status"] != "succeeded":
        return {"run_id": run_id, "status": rec["status"], "disagreement": None}
    return {
        "run_id": run_id,
        "status": "succeeded",
        "disagreement": rec["result"]["disagreement"],
        "independence": rec["result"]["independence"],
    }


@app.get("/api/evaluation/multi-judge/runs/{run_id}/arbitration")
def multi_judge_run_arbitration(run_id: str) -> dict[str, Any]:
    """查询仲裁结果（聚合分数、策略、INCONCLUSIVE、人工复核建议）。"""
    rec = _mj_get_or_404(run_id)
    if rec["status"] != "succeeded":
        return {"run_id": run_id, "status": rec["status"], "arbitration": None}
    return {
        "run_id": run_id,
        "status": "succeeded",
        "arbitration": rec["result"]["arbitration"],
        "limitations": rec["result"]["limitations"],
    }


# ---------------------------------------------------------------------------
# 真实/模拟 Agent 执行（第 41 步）
# ---------------------------------------------------------------------------

@app.get("/api/agent/status")
def agent_status() -> dict[str, Any]:
    """返回 Agent 执行能力状态（含真实 API 可用性诊断；不返回任何密钥）。"""
    import os

    from backend.app.agents.basic_agent import safe_tool_names
    from backend.app.services.agent_runner import (
        ALLOWED_MODELS,
        DEFAULT_REAL_MODEL,
        REAL_API_ENV_FLAG,
        real_api_readiness,
    )

    env_model = (os.environ.get("DEEPSEEK_MODEL") or "").strip()
    readiness = real_api_readiness()
    limits = _get_execution_limits()
    public_limits = limits.to_public_dict()
    # 前端只展示执行相关的安全限额（不含内部路径/密钥）
    frontend_limits = {
        key: public_limits[key]
        for key in (
            "profile",
            "agent_max_total_tokens",
            "agent_max_output_tokens",
            "agent_max_model_calls",
            "agent_max_tool_calls",
            "agent_max_tool_rounds",
            "agent_max_wall_seconds",
            "agent_max_document_refs",
            "document_context_max_chars",
            "document_per_file_max_chars",
            "frontend_agent_timeout_ms",
        )
    }
    return {
        "modes": [
            {
                "mode": "mock",
                "available": True,
                "simulated": True,
                "note": "确定性脚本化模型 + 真实本地工具，零成本",
            },
            {
                "mode": "real_api",
                "available": readiness["available"],
                "simulated": False,
                "note": f"真实 DeepSeek API：需服务端设置 {REAL_API_ENV_FLAG}=1 且请求确认；会产生费用",
                "blocking_reasons": readiness["blocking_reasons"],
                "warnings": readiness["warnings"],
                "api_key_configured": readiness["api_key_configured"],
            },
        ],
        "allowed_models": list(ALLOWED_MODELS),
        "configured_model": env_model or None,
        "configured_model_valid": readiness["configured_model_valid"],
        "default_real_model": DEFAULT_REAL_MODEL,
        "real_api_ready": readiness["available"],
        "real_api_blocking_reasons": readiness["blocking_reasons"],
        "real_api_warnings": readiness["warnings"],
        "api_key_configured": readiness["api_key_configured"],
        "tool_whitelist": safe_tool_names(),
        "limits": frontend_limits,
        "data_sources": ["real_api", "mock", "simulated"],
        "paid_api_called": False,
    }


@app.post("/api/agent/runs")
def agent_run_create(body: AgentRunRequest) -> dict[str, Any]:
    """创建并同步执行一次 Agent 任务（默认 mock，零成本）。

    真实模式需请求 `confirm_real=true` 且服务端 `TRACEPILOT_ALLOW_REAL_API=1`，
    否则返回 403 且**不发起任何 API 请求**。
    """
    from backend.app.services.agent_runner import (
        AgentRunError as RunError,
        InvalidModelError,
        RealApiNotAuthorized,
        RunLimits,
        run_agent_task,
    )

    limits = RunLimits(
        max_model_calls=body.max_model_calls,
        max_total_tokens=body.max_total_tokens,
        max_tool_calls=body.max_tool_calls,
        max_wall_seconds=body.max_wall_seconds,
        max_tool_rounds=body.max_tool_rounds,
        api_max_retries=0,  # 强制禁止自动重试
        max_tokens_per_call=body.max_tokens_per_call,
    )
    try:
        # STEP59_DOCUMENTS: validate IDs server-side; never trust client text or paths.
        docs = [get_document(doc_id) for doc_id in body.document_ids]
        # 文档正文注入（明确截断与摘要证据），不是只传文件名。
        context_task, provenance = build_document_context(body.task, docs)
        run = run_agent_task(
            context_task,
            mode=body.mode,
            confirm_real=body.confirm_real,
            model=body.model,
            limits=limits,
            document_provenance=provenance,
        )
    except RealApiNotAuthorized as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except InvalidModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RunError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"run": run.to_dict()}


@app.get("/api/agent/runs")
def agent_runs_list() -> dict[str, Any]:
    """列出本次进程内的 Agent 运行。"""
    from backend.app.services.agent_runner import list_runs

    items = list_runs()
    return {"count": len(items), "runs": items}


@app.get("/api/agent/runs/{run_id}")
def agent_run_detail(run_id: str) -> dict[str, Any]:
    """查询单次 Agent 运行结果。"""
    from backend.app.services.agent_runner import AgentRunError as RunError
    from backend.app.services.agent_runner import get_run

    try:
        run = get_run(run_id)
    except RunError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = run.to_dict()
    if run.trace_id:
        payload["links"] = {
            "trace": f"/api/traces/{run.trace_id}",
            "dag": f"/api/traces/{run.trace_id}/dag",
        }
    return {"run": payload}


# ---------------------------------------------------------------------------
# Evidence Lineage（第 44 步，只读证据血缘）
# ---------------------------------------------------------------------------

@app.get("/api/lineage/trace/{trace_id}")
def lineage_trace(trace_id: str) -> dict[str, Any]:
    """从 Trace 追溯：Trace → Agent Run → Step → Tool Call → Tool Result。"""
    from backend.app.services.evidence_lineage import (
        LineageError as LineageErr,
        build_trace_lineage,
    )

    try:
        return build_trace_lineage(trace_id)
    except LineageErr as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/lineage/evaluation/{run_id}/cases/{case_id}")
def lineage_evaluation_case(run_id: str, case_id: str) -> dict[str, Any]:
    """从评测案例追溯：Evaluation Run → Case → Trace → Run → Step → Tool。"""
    from backend.app.services.evidence_lineage import (
        LineageError as LineageErr,
        build_evaluation_case_lineage,
    )

    try:
        return build_evaluation_case_lineage(run_id, case_id)
    except LineageErr as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/lineage/experiment/{experiment_id}")
def lineage_experiment(experiment_id: str) -> dict[str, Any]:
    """从反事实实验追溯：Experiment → 原始 Trace / 变体 Trace。"""
    from backend.app.services.evidence_lineage import (
        LineageError as LineageErr,
        build_experiment_lineage,
    )

    try:
        return build_experiment_lineage(experiment_id)
    except LineageErr as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/lineage/repair/{report_id}")
def lineage_repair(report_id: str) -> dict[str, Any]:
    """从修复报告追溯：Report → Candidate → 修复 Trace → 原始 Trace。"""
    from backend.app.services.evidence_lineage import (
        LineageError as LineageErr,
        build_repair_lineage,
    )

    try:
        return build_repair_lineage(report_id)
    except LineageErr as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/lineage/consistency")
def lineage_consistency(
    limit: int = Query(100, ge=1, le=500, description="最多检查的 Trace 数量"),
) -> dict[str, Any]:
    """只读一致性检查：step/trace ID、DAG 节点、派生来源可解析性。"""
    from backend.app.services.evidence_lineage import consistency_check

    return consistency_check(limit=limit)


# ---------------------------------------------------------------------------
# Red Team Generator（第 45 步，受控安全测试）
# ---------------------------------------------------------------------------

# 第 46 步安全限制
REDTEAM_MAX_EXECUTE_CASES = 60      # 单次执行案例上限
REDTEAM_MAX_GENERATED = 120         # 单次生成上限
_REDTEAM_INFLIGHT = {"busy": False}

_REDTEAM_RUNS: dict[str, Any] = {}
_REDTEAM_ORDER: list[str] = []
REDTEAM_MAX_RUNS = 20


@app.get("/api/redteam/types")
def redteam_types() -> dict[str, Any]:
    """六类红队攻击类型与安全约束清单（只读元信息）。"""
    from backend.app.services.red_team_generator import (
        ATTACK_TYPES,
        DIFFICULTIES,
        SAFETY_CONSTRAINT_IDS,
    )

    labels = {
        "prompt_injection": "提示注入（不可信文本试图改变行为）",
        "tool_misuse": "工具误用（选错工具 / 参数越界）",
        "permission_boundary": "权限边界（越权操作应被拒绝）",
        "invalid_arguments": "非法参数（应优雅失败而非崩溃）",
        "conflicting_instructions": "指令冲突（矛盾要求应显式处理）",
        "fault_recovery": "故障恢复（故障后恢复或诚实失败）",
    }
    return {
        "attack_types": [
            {"key": t, "label": labels.get(t, t)}
            for t in ATTACK_TYPES
        ],
        "difficulties": list(DIFFICULTIES),
        "safety_constraints": list(SAFETY_CONSTRAINT_IDS),
        "execution_environment": {
            "mode": "controlled",
            "tools_whitelist": ["calculator", "text_analyzer", "sqlite_query"],
            "permissions": "mock_permission_system",
            "network": "disabled",
            "real_credentials": "not_used",
            "paid_api_called": False,
        },
        "metrics_note": "安全通过率与任务成功率是两个独立指标，不得互相替代",
    }


@app.post("/api/redteam/generate")
def redteam_generate(body: RedTeamGenerateRequest) -> dict[str, Any]:
    """按参数生成红队数据集（确定性、可复现），并保存为结构化数据。"""
    from backend.app.services.red_team_generator import (
        RedTeamError as RTError,
        UnsafeCaseError,
        generate_dataset,
    )

    try:
        ds = generate_dataset(
            attack_types=body.attack_types,
            variations=body.variations,
            seed=body.seed,
            include_canary=body.include_canary,
            save=True,
        )
    except UnsafeCaseError as exc:
        raise HTTPException(status_code=400, detail=f"生成结果未通过安全校验: {exc}") from exc
    except RTError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cases = ds["cases"]
    summary = {
        "dataset_id": ds["dataset_id"],
        "dataset_version": ds["dataset_version"],
        "saved_path": ds.get("saved_path"),
        "generation": ds["generation"],
        "total_cases": len(cases),
        "canary_count": sum(1 for c in cases if c.get("is_canary")),
        "by_attack_type": {
            t: sum(1 for c in cases if c["attack_type"] == t)
            for t in {c["attack_type"] for c in cases}
        },
        "by_difficulty": {
            d: sum(1 for c in cases if c["difficulty"] == d)
            for d in {c["difficulty"] for c in cases}
        },
        "data_source": ds["data_source"],
    }
    return {"dataset": summary, "cases": cases}


@app.get("/api/redteam/dataset")
def redteam_dataset() -> dict[str, Any]:
    """读取已保存的红队数据集（不存在时按默认参数生成）。"""
    from backend.app.services.red_team_generator import (
        RedTeamError as RTError,
        load_redteam_dataset,
    )

    try:
        ds = load_redteam_dataset()
    except RTError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "dataset": {
            "dataset_id": ds["dataset_id"],
            "dataset_version": ds["dataset_version"],
            "data_source": ds["data_source"],
            "generation": ds.get("generation"),
            "total_cases": len(ds["cases"]),
        },
        "cases": ds["cases"],
    }


@app.post("/api/redteam/runs")
def redteam_run_create(body: RedTeamRunRequest) -> dict[str, Any]:
    """执行红队评测（受控环境、确定性判定，不调用付费模型）。

    - 未传 dataset_path 时读取已保存的红队数据集（缺失则按默认参数生成）；
    - 安全判定与任务成功分别统计，不合并成单一分数。
    """
    import uuid as _uuid
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from backend.app.services.red_team_generator import (
        RedTeamError as RTError,
        generate_dataset,
        run_red_team,
    )

    try:
        if body.regenerate:
            dataset = generate_dataset(
                attack_types=body.attack_types,
                variations=body.variations,
                seed=body.seed,
                include_canary=body.include_canary,
                save=True,
            )
        else:
            dataset = None
        report = run_red_team(
            dataset,
            max_cases=body.max_cases,
            persist_traces=body.persist_traces,
        )
    except RTError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    run_id = f"rt-run-{_uuid.uuid4().hex}"
    report_rows = report.get("cases") or []
    executed_rows = [r for r in report_rows if r.get("status") == "EXECUTED"]
    attack_rows = [r for r in executed_rows if not r.get("is_canary")]
    canary_rows = [r for r in executed_rows if r.get("is_canary")]
    record = {
        "run_id": run_id,
        "status": "succeeded",
        "created_at": _dt.now(_tz.utc).isoformat(),
        "dataset_id": report["dataset"]["dataset_id"],
        # 口径：已执行案例总数（含金丝雀负控）；攻击案例数单列
        "case_count": len(executed_rows),
        "attack_case_count": len(attack_rows),
        "canary_case_count": len(canary_rows),
        "metrics": report["metrics"],
        "report": report,
        "links": {
            "self": f"/api/redteam/runs/{run_id}",
            "dataset": "/api/redteam/dataset",
            "types": "/api/redteam/types",
        },
        "paid_api_called": False,
    }
    _REDTEAM_RUNS[run_id] = record
    _REDTEAM_ORDER.append(run_id)
    while len(_REDTEAM_ORDER) > REDTEAM_MAX_RUNS:
        old = _REDTEAM_ORDER.pop(0)
        _REDTEAM_RUNS.pop(old, None)

    return {
        "run_id": run_id,
        "status": "succeeded",
        "dataset_id": record["dataset_id"],
        "case_count": record["case_count"],
        "metrics": record["metrics"],
        "links": record["links"],
    }


@app.get("/api/redteam/runs")
def redteam_runs_list() -> dict[str, Any]:
    """列出进程内红队评测运行。"""
    items = [
        {
            "run_id": rid,
            "status": _REDTEAM_RUNS[rid].get("status"),
            "created_at": _REDTEAM_RUNS[rid].get("created_at"),
            "dataset_id": _REDTEAM_RUNS[rid].get("dataset_id"),
            "case_count": _REDTEAM_RUNS[rid].get("case_count"),
            "attack_case_count": _REDTEAM_RUNS[rid].get("attack_case_count"),
            "canary_case_count": _REDTEAM_RUNS[rid].get("canary_case_count"),
            "safety_pass_rate": (_REDTEAM_RUNS[rid].get("metrics") or {}).get("safety_pass_rate"),
            "task_success_rate": (_REDTEAM_RUNS[rid].get("metrics") or {}).get("task_success_rate"),
        }
        for rid in _REDTEAM_ORDER
        if rid in _REDTEAM_RUNS
    ]
    return {"count": len(items), "runs": items}


@app.get("/api/redteam/runs/{run_id}")
def redteam_run_detail(run_id: str) -> dict[str, Any]:
    """查询红队评测运行详情（安全与任务指标分开呈现）。"""
    rec = _REDTEAM_RUNS.get(run_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"红队评测运行不存在: {run_id}")
    return rec

# ---------------------------------------------------------------------------
# Adversarial Evaluation Lab（第 46 步，/api/red-team/*）
# 与既有 /api/redteam/* 并存（兼容），本组接口面向交互式实验室。
# ---------------------------------------------------------------------------


def _rt_limits() -> dict[str, Any]:
    from backend.app.services.red_team_generator import (
        MAX_CASES,
        MAX_PAYLOAD_CHARS,
        MAX_VARIANTS,
    )

    return {
        "max_generated_cases": min(MAX_CASES, REDTEAM_MAX_GENERATED),
        "max_execute_cases": REDTEAM_MAX_EXECUTE_CASES,
        "max_variants": MAX_VARIANTS,
        "max_attack_types": 6,
        "max_payload_chars": MAX_PAYLOAD_CHARS,
        "max_concurrent_runs": 1,
        "max_wall_seconds": 120,
        "paid_api_called": False,
        "execution_environment": "controlled_sandbox",
    }


@app.get("/api/red-team/types")
def redteam_lab_types() -> dict[str, Any]:
    """测试类型 / 难度 / 安全约束 / 限制（只读元信息）。"""
    from backend.app.services.red_team_generator import (
        ATTACK_TYPES,
        DIFFICULTIES,
        SAFETY_CONSTRAINT_IDS,
    )

    labels = {
        "prompt_injection": "提示注入",
        "tool_misuse": "工具误用",
        "permission_boundary": "权限边界",
        "invalid_arguments": "非法参数",
        "conflicting_instructions": "指令冲突",
        "fault_recovery": "故障恢复",
    }
    return {
        "attack_types": [{"key": t, "label": labels.get(t, t)} for t in ATTACK_TYPES],
        "difficulties": list(DIFFICULTIES),
        "safety_constraints": list(SAFETY_CONSTRAINT_IDS),
        "limits": _rt_limits(),
        "data_sources": ["synthetic"],
        "metrics_note": "安全通过率与任务成功率是两个独立指标，不得互相替代",
    }


@app.post("/api/red-team/generate")
def redteam_lab_generate(body: RedTeamGenerateRequest) -> dict[str, Any]:
    """生成受控测试案例（确定性、可复现），返回案例清单与统计。"""
    from backend.app.services.red_team_generator import (
        RedTeamError as RTError,
        UnsafeCaseError,
        generate_dataset,
    )

    if body.variations > 4:
        raise HTTPException(status_code=400, detail="variations 超出上限 4")
    try:
        ds = generate_dataset(
            attack_types=body.attack_types,
            difficulties=body.difficulties,
            variations=body.variations,
            seed=body.seed,
            include_canary=body.include_canary,
            save=True,
        )
    except UnsafeCaseError as exc:
        raise HTTPException(status_code=400, detail=f"生成结果未通过安全校验: {exc}") from exc
    except RTError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cases = ds["cases"]
    if len(cases) > REDTEAM_MAX_GENERATED:
        raise HTTPException(status_code=400, detail="生成案例数超出上限")

    return {
        "dataset": {
            "dataset_id": ds["dataset_id"],
            "dataset_version": ds["dataset_version"],
            "data_source": ds["data_source"],
            "saved_path": ds.get("saved_path"),
            "generation": ds["generation"],
            "total_cases": len(cases),
            "canary_count": sum(1 for c in cases if c.get("is_canary")),
            "by_attack_type": {
                t: sum(1 for c in cases if c["attack_type"] == t)
                for t in {c["attack_type"] for c in cases}
            },
            "by_difficulty": {
                d: sum(1 for c in cases if c["difficulty"] == d)
                for d in {c["difficulty"] for c in cases}
            },
        },
        "cases": [
            {
                "case_id": c["case_id"],
                "attack_id": c["attack_id"],
                "attack_type": c["attack_type"],
                "difficulty": c["difficulty"],
                "title": c["title"],
                "template_id": c["template_id"],
                "variant_index": c["variant_index"],
                "mutation_rules": c["mutation_rules"],
                "input_payload": c["input_payload"],
                "expected_safe_behavior": c["expected_safe_behavior"],
                "safety_constraints": c["safety_constraints"],
                "data_source": c["data_source"],
                "is_canary": bool(c.get("is_canary")),
            }
            for c in cases
        ],
        "limits": _rt_limits(),
    }


@app.post("/api/red-team/runs")
def redteam_lab_run(body: RedTeamRunRequest) -> dict[str, Any]:
    """创建并执行对抗评测运行（受控沙盒、确定性判定）。

    - 并发上限 1（其他请求返回 429）；
    - 单次执行案例 ≤60；
    - 不调用任何付费模型。
    """
    import uuid as _uuid
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from backend.app.services.red_team_generator import (
        RedTeamError as RTError,
        generate_dataset,
        load_redteam_dataset,
        run_red_team,
    )

    if _REDTEAM_INFLIGHT["busy"]:
        raise HTTPException(status_code=429, detail="已有对抗评测在运行中，请稍后重试")

    if body.case_ids and len(body.case_ids) > REDTEAM_MAX_EXECUTE_CASES:
        raise HTTPException(status_code=400, detail=f"case_ids 超过单次上限 {REDTEAM_MAX_EXECUTE_CASES}")

    _REDTEAM_INFLIGHT["busy"] = True
    try:
        try:
            if body.regenerate:
                dataset = generate_dataset(
                    attack_types=body.attack_types,
                    difficulties=body.difficulties,
                    variations=body.variations,
                    seed=body.seed,
                    include_canary=body.include_canary,
                    save=True,
                )
            else:
                dataset = load_redteam_dataset()

            limit = body.max_cases or REDTEAM_MAX_EXECUTE_CASES
            report = run_red_team(
                dataset,
                max_cases=min(limit, REDTEAM_MAX_EXECUTE_CASES),
                persist_traces=body.persist_traces,
                case_ids=body.case_ids,
            )
        except RTError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        _REDTEAM_INFLIGHT["busy"] = False

    run_id = f"rt-run-{_uuid.uuid4().hex}"
    report_rows = report.get("cases") or []
    executed_rows = [r for r in report_rows if r.get("status") == "EXECUTED"]
    attack_rows = [r for r in executed_rows if not r.get("is_canary")]
    canary_rows = [r for r in executed_rows if r.get("is_canary")]
    record = {
        "run_id": run_id,
        "status": "succeeded",
        "created_at": _dt.now(_tz.utc).isoformat(),
        "dataset_id": report["dataset"]["dataset_id"],
        # 口径：已执行案例总数（含金丝雀负控）；攻击案例数单列
        "case_count": len(executed_rows),
        "attack_case_count": len(attack_rows),
        "canary_case_count": len(canary_rows),
        "metrics": report["metrics"],
        "coverage": report["coverage"],
        "report": report,
        "limits": _rt_limits(),
        "data_source": "synthetic",
        "paid_api_called": False,
        "links": {
            "self": f"/api/red-team/runs/{run_id}",
            "cases": f"/api/red-team/runs/{run_id}/cases",
            "bad_cases": f"/api/red-team/runs/{run_id}/bad-cases",
            "coverage": f"/api/red-team/runs/{run_id}/coverage",
        },
    }
    _REDTEAM_RUNS[run_id] = record
    _REDTEAM_ORDER.append(run_id)
    while len(_REDTEAM_ORDER) > REDTEAM_MAX_RUNS:
        old = _REDTEAM_ORDER.pop(0)
        _REDTEAM_RUNS.pop(old, None)

    return record


def _rt_get_or_404(run_id: str) -> dict[str, Any]:
    rec = _REDTEAM_RUNS.get(run_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"对抗评测运行不存在: {run_id}")
    return rec


@app.get("/api/red-team/runs")
def redteam_lab_runs() -> dict[str, Any]:
    """列出进程内对抗评测运行。"""
    items = [
        {
            "run_id": rid,
            "status": _REDTEAM_RUNS[rid].get("status"),
            "created_at": _REDTEAM_RUNS[rid].get("created_at"),
            "case_count": _REDTEAM_RUNS[rid].get("case_count"),
            "attack_case_count": _REDTEAM_RUNS[rid].get("attack_case_count"),
            "canary_case_count": _REDTEAM_RUNS[rid].get("canary_case_count"),
            "coverage_status": (_REDTEAM_RUNS[rid].get("coverage") or {}).get("coverage_status"),
            "safety_pass_rate": (_REDTEAM_RUNS[rid].get("metrics") or {}).get("safety_pass_rate"),
            "task_success_rate": (_REDTEAM_RUNS[rid].get("metrics") or {}).get("task_success_rate"),
        }
        for rid in _REDTEAM_ORDER
        if rid in _REDTEAM_RUNS
    ]
    return {"count": len(items), "runs": items}


@app.get("/api/red-team/runs/{run_id}")
def redteam_lab_run_detail(run_id: str) -> dict[str, Any]:
    """运行状态 + 指标 + 覆盖率（安全与任务指标分开）。"""
    rec = _rt_get_or_404(run_id)
    return {
        "run_id": rec["run_id"],
        "status": rec["status"],
        "created_at": rec["created_at"],
        "dataset_id": rec["dataset_id"],
        "case_count": rec["case_count"],
        "attack_case_count": rec.get("attack_case_count"),
        "canary_case_count": rec.get("canary_case_count"),
        "metrics": rec["metrics"],
        "coverage": rec["coverage"],
        "limits": rec["limits"],
        "data_source": rec["data_source"],
        "paid_api_called": rec["paid_api_called"],
        "links": rec["links"],
    }


@app.get("/api/red-team/runs/{run_id}/cases")
def redteam_lab_cases(
    run_id: str,
    attack_type: str | None = Query(None, max_length=64),
    difficulty: str | None = Query(None, max_length=16),
    status: str | None = Query(None, max_length=32, description="EXECUTED / EXECUTION_ERROR"),
    safety: str | None = Query(None, max_length=16, description="SAFE / UNSAFE / INCONCLUSIVE"),
    executed_only: bool = Query(False, description="仅返回已执行案例"),
) -> dict[str, Any]:
    """查询运行内案例（支持按类型/难度/状态/安全判定筛选）。"""
    rec = _rt_get_or_404(run_id)
    rows = list((rec.get("report") or {}).get("cases") or [])

    def keep(r: dict[str, Any]) -> bool:
        if attack_type and r.get("attack_type") != attack_type:
            return False
        if difficulty and r.get("difficulty") != difficulty:
            return False
        if status and r.get("status") != status:
            return False
        if safety and (r.get("safety") or {}).get("verdict") != safety:
            return False
        if executed_only and r.get("status") != "EXECUTED":
            return False
        return True

    filtered = [r for r in rows if keep(r)]
    return {"run_id": run_id, "count": len(filtered), "total": len(rows), "cases": filtered}


@app.get("/api/red-team/runs/{run_id}/bad-cases")
def redteam_lab_bad_cases(run_id: str) -> dict[str, Any]:
    """查询 Bad Case（安全判定 UNSAFE），附失败原因与 Trace 关联。"""
    rec = _rt_get_or_404(run_id)
    rows = (rec.get("report") or {}).get("cases") or []
    bad: list[dict[str, Any]] = []
    for r in rows:
        if r.get("status") != "EXECUTED":
            continue
        safety = r.get("safety") or {}
        if safety.get("verdict") != "UNSAFE":
            continue
        bad.append({
            "case_id": r["case_id"],
            "attack_id": r["attack_id"],
            "attack_type": r["attack_type"],
            "difficulty": r["difficulty"],
            "title": r.get("title"),
            "is_canary": bool(r.get("is_canary")),
            "violations": safety.get("violations") or [],
            "checked": safety.get("checked") or [],
            "task": r.get("task"),
            "trace_id": r.get("trace_id"),
            "links": {
                "trace": f"/api/traces/{r['trace_id']}" if r.get("trace_id") else None,
                "dag": f"/api/traces/{r['trace_id']}/dag" if r.get("trace_id") else None,
                "case": f"/api/red-team/runs/{run_id}/cases?status=EXECUTED",
            },
        })
    return {"run_id": run_id, "count": len(bad), "bad_cases": bad}


@app.get("/api/red-team/runs/{run_id}/coverage")
def redteam_lab_coverage(run_id: str) -> dict[str, Any]:
    """攻击覆盖地图（来自实际执行数据；未执行与 UNKNOWN 不并入通过/失败）。"""
    rec = _rt_get_or_404(run_id)
    return {
        "run_id": run_id,
        "coverage": rec["coverage"],
        "metrics": {
            "safety_pass_rate": rec["metrics"].get("safety_pass_rate"),
            "task_success_rate": rec["metrics"].get("task_success_rate"),
            "canary_detected_count": rec["metrics"].get("canary_detected_count"),
            "canary_count": rec["metrics"].get("canary_count"),
            "execution_error_count": rec["metrics"].get("execution_error_count"),
        },
    }


@app.get("/api/red-team/runs/{run_id}/cases/{case_id}")
def redteam_lab_case_detail(run_id: str, case_id: str) -> dict[str, Any]:
    """单案例详情（安全约束逐条结果 + 任务判定 + Trace 关联）。"""
    rec = _rt_get_or_404(run_id)
    rows = (rec.get("report") or {}).get("cases") or []
    row = next((r for r in rows if r["case_id"] == case_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail=f"案例不存在: {case_id}")
    trace_id = row.get("trace_id")
    return {
        "run_id": run_id,
        "case": row,
        "links": {
            "trace": f"/api/traces/{trace_id}" if trace_id else None,
            "dag": f"/api/traces/{trace_id}/dag" if trace_id else None,
        },
        "data_source": "synthetic",
    }

# ---------------------------------------------------------------------------
# Automated Bad Case Attribution（第 47 步）
# ---------------------------------------------------------------------------

ATTRIBUTION_MAX_BATCH = 20
ATTRIBUTION_MAX_SECONDS = 60.0


@app.get("/api/attribution/types")
def attribution_types() -> dict[str, Any]:
    """归因类型与证据等级词表（只读）。"""
    from backend.app.services.bad_case_attribution import (
        ATTRIBUTION_TYPES,
        EVIDENCE_LEVELS,
        MAX_BATCH_CASES,
    )

    return {
        "attribution_types": list(ATTRIBUTION_TYPES),
        "evidence_levels": list(EVIDENCE_LEVELS),
        "evidence_level_rules": {
            "CONFIRMED_EVENT": "Trace 直接记录的异常（故障注入 / step.error / 工具失败）",
            "OBSERVED_DIFFERENCE": "与基线对比或指标比对观察到的差异",
            "POTENTIAL_CAUSE": "由依赖关系或相关性推断（DAG 可达性不构成因果）",
            "INTERVENTION_SUPPORTED": "有真实反事实实验引用，且注明控制条件",
            "UNKNOWN": "证据不足，不伪造原因",
        },
        "limits": {
            "max_batch_cases": min(MAX_BATCH_CASES, ATTRIBUTION_MAX_BATCH),
            "max_wall_seconds": ATTRIBUTION_MAX_SECONDS,
            "confidence_probability": "not_emitted（不生成无来源的置信概率）",
            "paid_api_called": False,
        },
        "available_datasets": {
            "tracepilot-fault-v1": "受控失败数据集（synthetic），用于产生可归因的 Bad Case",
        },
    }


@app.post("/api/attribution/runs")
def attribution_batch(body: AttributionBatchRequest) -> dict[str, Any]:
    """批量归因：读取评测运行中的 Bad Case → 生成并持久化归因报告。"""
    from backend.app.services.bad_case_attribution import (
        AttributionError as AttrError,
        attribute_evaluation_run,
    )

    if body.case_ids and len(body.case_ids) > ATTRIBUTION_MAX_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"case_ids 超过单次上限 {ATTRIBUTION_MAX_BATCH}",
        )
    try:
        result = attribute_evaluation_run(
            body.evaluation_run_id,
            case_ids=body.case_ids,
            max_cases=min(body.max_cases or 10, ATTRIBUTION_MAX_BATCH),
        )
    except AttrError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.post("/api/attribution/evaluations/{run_id}/cases/{case_id}")
def attribution_single_case(run_id: str, case_id: str) -> dict[str, Any]:
    """单案例归因（从评测运行中取该案例）。"""
    from backend.app.services.bad_case_attribution import (
        AttributionError as AttrError,
        attribute_evaluation_run,
    )
    from backend.app.services.evaluation_store import (
        EvaluationNotFoundError,
        get_evaluation_store,
    )

    try:
        run = get_evaluation_store().get(run_id)
    except EvaluationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if run.get("status") != "succeeded":
        raise HTTPException(status_code=400, detail=f"评测运行状态为 {run.get('status')!r}")

    report = run.get("result") or run.get("report") or {}
    exists = any(c.get("case_id") == case_id for c in report.get("case_results") or [])
    if not exists:
        raise HTTPException(status_code=404, detail=f"案例不存在: {case_id}")

    try:
        result = attribute_evaluation_run(run_id, case_ids=[case_id], max_cases=1)
    except AttrError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result["reports"]:
        raise HTTPException(
            status_code=409,
            detail="该案例不是 Bad Case（无失败指标），未生成归因报告",
        )
    return {"run_id": run_id, "report": result["reports"][0]}


@app.post("/api/attribution/traces/{trace_id}")
def attribution_trace(trace_id: str) -> dict[str, Any]:
    """直接对单个 Trace 归因（无评测上下文时使用）。"""
    from backend.app.services.bad_case_attribution import (
        AttributionError as AttrError,
        attribute_trace,
    )

    try:
        return {"report": attribute_trace(trace_id)}
    except AttrError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/attribution/reports")
def attribution_reports(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """列出已持久化的归因报告（按生成时间倒序）。"""
    from backend.app.services.bad_case_attribution import list_reports

    items = list_reports(limit=limit)
    return {"count": len(items), "reports": items}


@app.get("/api/attribution/reports/{attribution_id}")
def attribution_report(attribution_id: str) -> dict[str, Any]:
    """查询归因报告（持久化）。"""
    from backend.app.services.bad_case_attribution import (
        AttributionError as AttrError,
        get_report,
    )

    try:
        return {"report": get_report(attribution_id)}
    except AttrError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/attribution/reports/{attribution_id}/evidence")
def attribution_evidence(attribution_id: str) -> dict[str, Any]:
    """查询归因证据详情（按证据等级分组 + 逐条引用）。"""
    from backend.app.services.bad_case_attribution import (
        AttributionError as AttrError,
        get_report,
    )

    try:
        rep = get_report(attribution_id)
    except AttrError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    by_level: dict[str, list[dict[str, Any]]] = {}
    for cand in rep.get("candidate_causes") or []:
        by_level.setdefault(cand["evidence_level"], []).append({
            "cause_type": cand["cause_type"],
            "summary": cand["summary"],
            "confidence_basis": cand["confidence_basis"],
            "uncertainty": cand["uncertainty"],
            "is_conclusive": cand["is_conclusive"],
            "evidence": cand["evidence"],
            "affected_steps": cand["affected_steps"],
        })
    return {
        "attribution_id": attribution_id,
        "case_id": rep.get("case_id"),
        "trace_id": rep.get("trace_id"),
        "evidence_levels": rep.get("evidence_levels"),
        "candidate_by_level": by_level,
        "flat_refs": rep.get("evidence_refs"),
        "observed_events": rep.get("observed_events"),
        "ambiguity_notes": rep.get("ambiguity_notes"),
        "limitations": rep.get("limitations"),
    }


@app.get("/api/attribution/reports/{attribution_id}/trace")
def attribution_trace_link(attribution_id: str) -> dict[str, Any]:
    """查询归因报告关联的 Trace（含跳转链接与可读性）。"""
    from backend.app.services.bad_case_attribution import (
        AttributionError as AttrError,
        get_report,
    )
    from backend.app.services.trace_store import load_trace

    try:
        rep = get_report(attribution_id)
    except AttrError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    trace_id = rep.get("trace_id")
    if not trace_id:
        return {
            "attribution_id": attribution_id,
            "trace_id": None,
            "status": "NOT_AVAILABLE",
            "note": "该归因报告没有关联 Trace",
        }
    try:
        trace = load_trace(trace_id)
    except Exception:
        return {
            "attribution_id": attribution_id,
            "trace_id": trace_id,
            "status": "NOT_AVAILABLE",
            "note": "Trace 文件不可读取",
        }
    return {
        "attribution_id": attribution_id,
        "trace_id": trace_id,
        "status": "AVAILABLE",
        "step_count": len(trace.steps),
        "data_source": (trace.metadata or {}).get("data_source"),
        "affected_steps": rep.get("affected_steps"),
        "links": {
            "trace": f"/api/traces/{trace_id}",
            "dag": f"/api/traces/{trace_id}/dag",
            "lineage": f"/api/lineage/trace/{trace_id}",
        },
    }


@app.get("/api/attribution/reports/{attribution_id}/experiments")
def attribution_experiments(attribution_id: str) -> dict[str, Any]:
    """查询归因报告引用的反事实实验（真实 experiment_id）。"""
    from backend.app.services.bad_case_attribution import (
        AttributionError as AttrError,
        get_report,
    )
    from backend.app.services.experiment_store import (
        ExperimentNotFoundError,
        get_store,
    )

    try:
        rep = get_report(attribution_id)
    except AttrError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    refs = rep.get("experiment_refs") or []
    resolved: list[dict[str, Any]] = []
    for ref in refs:
        exp_id = ref.get("experiment_id")
        item = {**ref, "resolved": False}
        if exp_id:
            try:
                exp = get_store().get(exp_id)
                item["resolved"] = True
                item["status"] = exp.get("status")
                item["source_trace_id"] = exp.get("source_trace_id")
                item["links"] = {
                    "experiment": f"/api/counterfactual/experiments/{exp_id}",
                    "results": f"/api/counterfactual/experiments/{exp_id}/results",
                }
            except ExperimentNotFoundError:
                item["note"] = "实验记录不在当前进程登记表中（历史实验），引用 ID 保留但不可查询"
        resolved.append(item)

    return {
        "attribution_id": attribution_id,
        "count": len(resolved),
        "experiments": resolved,
        "note": "仅引用真实存在的 experiment_id；未找到实验时不会捏造引用",
    }

# ---------------------------------------------------------------------------
# Failure Pattern Mining（第 48 步）
# ---------------------------------------------------------------------------

FAILURE_PATTERN_MAX_CASES = 50


@app.get("/api/failure-patterns/types")
def failure_pattern_types() -> dict[str, Any]:
    """指纹字段 / 失败类型 / 恢复状态 / 聚类策略（只读元信息）。"""
    from backend.app.services.failure_pattern_mining import (
        CLUSTER_STRATEGIES,
        FAILURE_TYPES,
        FINGERPRINT_FIELDS,
        RECOVERY_STATES,
    )

    return {
        "fingerprint_fields": list(FINGERPRINT_FIELDS),
        "fingerprint_field_notes": {
            "failure_type": "归因主失败类型（确定性优先级选择）",
            "tool_name": "Trace 中失败工具名（多个用 + 连接）；无失败工具时为 null",
            "failed_metric": "确定性验证器判定 FAIL 的指标名（多个用 + 连接）",
            "recovery_status": "恢复状态（证据来自 Trace 步骤顺序与 metadata）",
            "scenario_type": "评测案例声明的场景类型",
        },
        "failure_types": list(FAILURE_TYPES),
        "recovery_states": list(RECOVERY_STATES),
        "cluster_strategies": sorted(CLUSTER_STRATEGIES),
        "missing_field_rule": "缺失字段为 null，指纹中以 '-' 表示，不填充猜测值",
        "limits": {
            "max_cases": FAILURE_PATTERN_MAX_CASES,
            "paid_api_called": False,
            "semantic_clustering": "not_implemented（已预留 register_cluster_strategy 接口）",
        },
        "notes": [
            "相同指纹只表示失败特征相似，不代表已证明的相同根因",
            "统计分母见报告的 denominators；未执行案例不计入任何模式",
            "UNKNOWN 案例单独计数，不并入任何失败类型",
        ],
    }


@app.post("/api/failure-patterns/runs")
def failure_pattern_create(body: FailurePatternRequest) -> dict[str, Any]:
    """对一次评测运行创建失败模式分析（读取归因结果 → 指纹分组 → 统计）。"""
    from backend.app.services.failure_pattern_mining import (
        FailurePatternError as FPError,
        analyze_run,
    )

    if (body.max_cases or 0) > FAILURE_PATTERN_MAX_CASES:
        raise HTTPException(status_code=400, detail=f"max_cases 上限 {FAILURE_PATTERN_MAX_CASES}")
    try:
        result = analyze_run(
            body.evaluation_run_id,
            max_cases=body.max_cases,
            strategy=body.strategy,
        )
    except FPError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.get("/api/failure-patterns/reports")
def failure_pattern_reports(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """列出已持久化的失败模式报告。"""
    from backend.app.services.failure_pattern_mining import list_pattern_reports

    items = list_pattern_reports(limit=limit)
    return {"count": len(items), "reports": items}


@app.get("/api/failure-patterns/reports/{report_id}")
def failure_pattern_report(report_id: str) -> dict[str, Any]:
    """查询失败模式报告（模式统计 + 分母 + 限制）。"""
    from backend.app.services.failure_pattern_mining import (
        FailurePatternError as FPError,
        get_pattern_report,
    )

    try:
        return {"report": get_pattern_report(report_id)}
    except FPError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _fp_report_or_404(report_id: str) -> dict[str, Any]:
    from backend.app.services.failure_pattern_mining import (
        FailurePatternError as FPError,
        get_pattern_report,
    )

    try:
        return get_pattern_report(report_id)
    except FPError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _fp_pattern_or_404(report: dict[str, Any], pattern_id: str) -> dict[str, Any]:
    pat = next((p for p in report.get("patterns") or [] if p["pattern_id"] == pattern_id), None)
    if pat is None:
        raise HTTPException(status_code=404, detail=f"失败模式不存在: {pattern_id}")
    return pat


@app.get("/api/failure-patterns/reports/{report_id}/patterns/{pattern_id}")
def failure_pattern_detail(report_id: str, pattern_id: str) -> dict[str, Any]:
    """查询单个失败模式详情（含该类全部案例的指纹与证据等级）。"""
    report = _fp_report_or_404(report_id)
    pat = _fp_pattern_or_404(report, pattern_id)
    members = [c for c in report.get("cases") or [] if c["fingerprint_id"] == pattern_id]
    return {
        "pattern_report_id": report_id,
        "evaluation_run_id": report.get("evaluation_run_id"),
        "pattern": pat,
        "cases": members,
        "denominators": report.get("denominators"),
        "limitations": report.get("limitations"),
    }


@app.get("/api/failure-patterns/reports/{report_id}/patterns/{pattern_id}/cases")
def failure_pattern_cases(report_id: str, pattern_id: str) -> dict[str, Any]:
    """查询该失败模式关联的 Bad Case 列表（附 Trace / 归因链接）。"""
    report = _fp_report_or_404(report_id)
    pat = _fp_pattern_or_404(report, pattern_id)
    members = [c for c in report.get("cases") or [] if c["fingerprint_id"] == pattern_id]
    return {
        "pattern_report_id": report_id,
        "pattern_id": pattern_id,
        "count": len(members),
        "cases": [
            {
                **c,
                "links": {
                    "trace": f"/api/traces/{c['trace_id']}" if c.get("trace_id") else None,
                    "dag": f"/api/traces/{c['trace_id']}/dag" if c.get("trace_id") else None,
                    "attribution": (
                        f"/api/attribution/reports/{c['attribution_id']}"
                        if c.get("attribution_id") else None
                    ),
                },
            }
            for c in members
        ],
    }


@app.get("/api/failure-patterns/reports/{report_id}/patterns/{pattern_id}/trace")
def failure_pattern_trace(report_id: str, pattern_id: str) -> dict[str, Any]:
    """查询该模式的代表性与全部关联 Trace（可查性逐条校验）。"""
    from backend.app.services.trace_store import load_trace

    report = _fp_report_or_404(report_id)
    pat = _fp_pattern_or_404(report, pattern_id)

    rows: list[dict[str, Any]] = []
    for tid in pat.get("trace_ids") or []:
        try:
            trace = load_trace(tid)
            rows.append({
                "trace_id": tid,
                "status": "AVAILABLE",
                "step_count": len(trace.steps),
                "data_source": (trace.metadata or {}).get("data_source"),
                "links": {
                    "trace": f"/api/traces/{tid}",
                    "dag": f"/api/traces/{tid}/dag",
                    "lineage": f"/api/lineage/trace/{tid}",
                },
            })
        except Exception:
            rows.append({
                "trace_id": tid,
                "status": "NOT_AVAILABLE",
                "note": "Trace 文件不可读取",
            })
    return {
        "pattern_report_id": report_id,
        "pattern_id": pattern_id,
        "representative_case": pat.get("representative_case"),
        "count": len(rows),
        "traces": rows,
        "note": "Trace 可查性逐条校验；不可读取时标记 NOT_AVAILABLE，不伪造",
    }


@app.post("/api/failure-patterns/compare")
def failure_pattern_compare(body: FailurePatternCompareRequest) -> dict[str, Any]:
    """对比两次评测运行的失败模式变化（按 fingerprint_id 对齐）。"""
    from backend.app.services.failure_pattern_mining import (
        FailurePatternError as FPError,
        compare_runs,
    )

    if body.run_a == body.run_b:
        raise HTTPException(status_code=400, detail="两次运行不能相同")
    try:
        return compare_runs(
            body.run_a,
            body.run_b,
            max_cases=body.max_cases,
            strategy=body.strategy,
        )
    except FPError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

# ---------------------------------------------------------------------------
# Evaluation Version Comparison（第 49 步）
# ---------------------------------------------------------------------------

@app.get("/api/version-comparison/runs")
def version_comparison_runs(limit: int = Query(30, ge=1, le=100)) -> dict[str, Any]:
    """列出可比较的评测运行及其版本元数据（全部来自真实运行记录）。"""
    from backend.app.services.evaluation_store import get_evaluation_store
    from backend.app.services.evaluation_version_comparison import (
        VersionComparisonError,
        build_version_descriptor,
    )

    store = get_evaluation_store()
    runs = store.list_all() if hasattr(store, "list_all") else []
    out: list[dict[str, Any]] = []
    for r in list(runs)[:limit]:
        if r.get("status") != "succeeded":
            continue
        try:
            desc = build_version_descriptor(r["run_id"])
        except VersionComparisonError:
            continue
        out.append(desc)
    return {"count": len(out), "runs": out}


@app.post("/api/version-comparison/runs")
def version_comparison_create(body: VersionComparisonRequest) -> dict[str, Any]:
    """创建版本对比（含可比性检查、指标差异、Case-level Diff、失败模式变化）。"""
    from backend.app.services.evaluation_version_comparison import (
        VersionComparisonError as VCError,
        compare_runs,
    )

    try:
        return compare_runs(
            body.baseline_run_id, body.candidate_run_id, max_cases=body.max_cases
        )
    except VCError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/version-comparison/reports")
def version_comparison_reports(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    from backend.app.services.evaluation_version_comparison import list_comparisons

    items = list_comparisons(limit=limit)
    return {"count": len(items), "reports": items}


def _comparison_or_404(comparison_id: str) -> dict[str, Any]:
    from backend.app.services.evaluation_version_comparison import (
        VersionComparisonError as VCError,
        get_comparison,
    )

    try:
        return get_comparison(comparison_id)
    except VCError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/version-comparison/reports/{comparison_id}")
def version_comparison_report(comparison_id: str) -> dict[str, Any]:
    """查询版本对比报告。"""
    return {"report": _comparison_or_404(comparison_id)}


@app.get("/api/version-comparison/reports/{comparison_id}/metrics")
def version_comparison_metrics(comparison_id: str) -> dict[str, Any]:
    """查询指标差异（含分子分母、pp 与相对变化）。"""
    rep = _comparison_or_404(comparison_id)
    return {
        "comparison_id": comparison_id,
        "comparability": rep.get("comparability"),
        "metric_deltas": rep.get("metric_deltas"),
        "dimension_deltas": rep.get("dimension_deltas"),
        "bad_case_diff": rep.get("bad_case_diff"),
        "limitations": rep.get("limitations"),
    }


@app.get("/api/version-comparison/reports/{comparison_id}/cases")
def version_comparison_cases(
    comparison_id: str,
    transition: str | None = Query(
        None, max_length=32,
        description="FAIL_TO_PASS / PASS_TO_FAIL / PASS_TO_PASS / FAIL_TO_FAIL / UNKNOWN_INVOLVED",
    ),
) -> dict[str, Any]:
    """查询 Case-level Diff（可按状态迁移筛选）。"""
    rep = _comparison_or_404(comparison_id)
    cases = rep.get("cases") or {}
    rows = list(cases.get("rows") or [])
    if transition:
        rows = [r for r in rows if r.get("transition") == transition]
    return {
        "comparison_id": comparison_id,
        "counts": cases.get("counts"),
        "count": len(rows),
        "rows": rows,
        "regressed_cases": cases.get("regressed_cases"),
        "improved_cases": cases.get("improved_cases"),
        "missing_cases": cases.get("missing_cases"),
        "note": cases.get("note"),
    }


@app.get("/api/version-comparison/reports/{comparison_id}/failure-patterns")
def version_comparison_patterns(comparison_id: str) -> dict[str, Any]:
    """查询失败模式分布变化。"""
    rep = _comparison_or_404(comparison_id)
    return {
        "comparison_id": comparison_id,
        "failure_pattern_diff": rep.get("failure_pattern_diff"),
    }


@app.get("/api/version-comparison/reports/{comparison_id}/traces")
def version_comparison_traces(comparison_id: str) -> dict[str, Any]:
    """查询关联 Trace（基线/候选两侧，逐条校验可读性）。"""
    from backend.app.services.trace_store import load_trace

    rep = _comparison_or_404(comparison_id)
    rows = (rep.get("cases") or {}).get("rows") or []
    out: list[dict[str, Any]] = []
    for r in rows:
        for side in ("baseline", "candidate"):
            tid = r.get(f"trace_id_{side}")
            if not tid:
                continue
            try:
                trace = load_trace(tid)
                out.append({
                    "case_id": r["case_id"], "side": side, "trace_id": tid,
                    "status": "AVAILABLE", "step_count": len(trace.steps),
                    "links": {"trace": f"/api/traces/{tid}", "dag": f"/api/traces/{tid}/dag"},
                })
            except Exception:
                out.append({"case_id": r["case_id"], "side": side, "trace_id": tid,
                            "status": "NOT_AVAILABLE"})
    return {"comparison_id": comparison_id, "count": len(out), "traces": out}


# ---------------------------------------------------------------------------
# Regression Detection（第 50 步）
# ---------------------------------------------------------------------------

@app.get("/api/regression/rules")
def regression_rules() -> dict[str, Any]:
    """列出回归检测规则与默认阈值（明确标注为项目默认值）。"""
    from backend.app.services.regression_detector import (
        DEFAULTS_LABEL,
        DEFAULT_RULES,
        GATE_STATUSES,
        RISK_LEVELS,
        RULE_DIRECTIONS,
    )

    return {
        "rules": [r.to_dict() for r in DEFAULT_RULES],
        "rule_count": len(DEFAULT_RULES),
        "risk_levels": list(RISK_LEVELS),
        "gate_statuses": list(GATE_STATUSES),
        "directions": list(RULE_DIRECTIONS),
        "rule_statuses": [
            "TRIGGERED", "NOT_TRIGGERED", "NOT_EVALUABLE", "NOT_APPLICABLE", "DISABLED",
        ],
        "threshold_source": DEFAULTS_LABEL,
        "threshold_note": (
            "阈值为本项目默认演示配置，可按需覆盖；不代表行业统一标准"
        ),
        "gate_note": "INCONCLUSIVE 表示无法判定，不等同于 PASS",
    }


@app.post("/api/regression/runs")
def regression_create(body: RegressionRequest) -> dict[str, Any]:
    """创建回归检测（给定对比报告 ID，或两个评测运行）。"""
    from backend.app.services.regression_detector import (
        RegressionError as RError,
        detect_regressions,
    )
    from backend.app.services.evaluation_version_comparison import (
        VersionComparisonError as VCError,
        get_comparison,
    )

    comparison: dict[str, Any] | None = None
    if body.comparison_id:
        try:
            comparison = get_comparison(body.comparison_id)
        except VCError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    elif not (body.baseline_run_id and body.candidate_run_id):
        raise HTTPException(
            status_code=400,
            detail="必须提供 comparison_id 或 (baseline_run_id, candidate_run_id)",
        )

    rules = [r.model_dump(exclude_none=True) for r in body.rules] if body.rules else None
    try:
        return detect_regressions(
            comparison=comparison,
            baseline_run_id=body.baseline_run_id,
            candidate_run_id=body.candidate_run_id,
            max_cases=body.max_cases,
            rules=rules,
        )
    except RError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/regression/reports")
def regression_reports(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    from backend.app.services.regression_detector import list_regression_reports

    items = list_regression_reports(limit=limit)
    return {"count": len(items), "reports": items}


def _regression_or_404(report_id: str) -> dict[str, Any]:
    from backend.app.services.regression_detector import (
        RegressionError as RError,
        get_regression_report,
    )

    try:
        return get_regression_report(report_id)
    except RError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/regression/reports/{report_id}")
def regression_report(report_id: str) -> dict[str, Any]:
    """查询回归报告（风险等级 + 门禁 + 触发规则）。"""
    return {"report": _regression_or_404(report_id)}


@app.get("/api/regression/reports/{report_id}/rules")
def regression_report_rules(report_id: str) -> dict[str, Any]:
    """查询触发规则与全部规则求值事件。"""
    rep = _regression_or_404(report_id)
    return {
        "regression_report_id": report_id,
        "triggered_rules": rep.get("triggered_rules"),
        "rule_events": rep.get("rule_events"),
        "not_evaluable_rules": rep.get("not_evaluable_rules"),
        "not_applicable_rules": rep.get("not_applicable_rules"),
        "disabled_rules": rep.get("disabled_rules"),
        "rules": rep.get("rules"),
        "threshold_source": rep.get("threshold_source"),
    }


@app.get("/api/regression/reports/{report_id}/cases")
def regression_report_cases(report_id: str) -> dict[str, Any]:
    """查询回归案例（PASS→FAIL）与关联 Trace。"""
    rep = _regression_or_404(report_id)
    return {
        "regression_report_id": report_id,
        "count": len(rep.get("regressed_cases") or []),
        "regressed_cases": rep.get("regressed_cases"),
        "metric_deltas": rep.get("metric_deltas"),
    }


@app.get("/api/regression/reports/{report_id}/risk")
def regression_report_risk(report_id: str) -> dict[str, Any]:
    """查询风险等级（只来自规则命中，不含概率）。"""
    rep = _regression_or_404(report_id)
    return {
        "regression_report_id": report_id,
        "risk_level": rep.get("risk_level"),
        "risk_reason": rep.get("risk_reason"),
        "triggered_rules": [e["rule_id"] for e in rep.get("triggered_rules") or []],
        "comparability": (rep.get("comparability") or {}).get("verdict"),
    }


@app.get("/api/regression/reports/{report_id}/gate")
def regression_report_gate(report_id: str) -> dict[str, Any]:
    """查询质量门禁预判。"""
    rep = _regression_or_404(report_id)
    return {
        "regression_report_id": report_id,
        "gate_status": rep.get("gate_status"),
        "gate_reason": rep.get("gate_reason"),
        "note": "INCONCLUSIVE 不等同于 PASS",
    }


@app.get("/api/regression/reports/{report_id}/trace")
def regression_report_traces(report_id: str) -> dict[str, Any]:
    """查询回归案例关联的 Trace（两侧逐条校验）。"""
    from backend.app.services.trace_store import load_trace

    rep = _regression_or_404(report_id)
    out: list[dict[str, Any]] = []
    for c in rep.get("regressed_cases") or []:
        for side in ("baseline", "candidate"):
            tid = c.get(f"trace_id_{side}")
            if not tid:
                continue
            try:
                trace = load_trace(tid)
                out.append({"case_id": c["case_id"], "side": side, "trace_id": tid,
                            "status": "AVAILABLE", "step_count": len(trace.steps),
                            "links": {"trace": f"/api/traces/{tid}",
                                      "dag": f"/api/traces/{tid}/dag"}})
            except Exception:
                out.append({"case_id": c["case_id"], "side": side, "trace_id": tid,
                            "status": "NOT_AVAILABLE"})
    return {"regression_report_id": report_id, "count": len(out), "traces": out}


@app.get("/api/regression/reports/{report_id}/evidence")
def regression_report_evidence(report_id: str) -> dict[str, Any]:
    """查询证据详情（规则 → 观测值 → 证据引用）。"""
    rep = _regression_or_404(report_id)
    return {
        "regression_report_id": report_id,
        "comparison_id": rep.get("comparison_id"),
        "evidence_refs": rep.get("evidence_refs"),
        "rule_evidence": [
            {
                "rule_id": e["rule_id"],
                "status": e["status"],
                "severity": e["severity"],
                "observed": e.get("observed"),
                "reason": e.get("reason"),
                "evidence_refs": e.get("evidence_refs"),
            }
            for e in rep.get("rule_events") or []
        ],
        "limitations": rep.get("limitations"),
    }


# ---------------------------------------------------------------------------
# CI Quality Gate（第 51 步）
# ---------------------------------------------------------------------------

@app.get("/api/quality-gate/config")
def quality_gate_config() -> dict[str, Any]:
    """查询当前生效的质量门禁配置（阈值、单位、来源与退出码语义）。"""
    from backend.app.services.quality_gate import (
        DEFAULT_CONFIG_PATH,
        DEFAULTS_LABEL,
        GATE_STATUSES,
        QualityGateConfigError,
        load_gate_config,
    )

    try:
        loaded = load_gate_config()
    except QualityGateConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cfg = loaded["config"]
    return {
        "config": cfg,
        "config_source": loaded["config_source"],
        "config_path": (
            "config/quality_gate.yaml"
            if loaded["config_source"] == "file"
            else None
        ),
        "config_fingerprint": loaded["config_fingerprint"],
        "label": loaded["config_label"],
        "defaults_label": DEFAULTS_LABEL,
        "default_config_path": "config/quality_gate.yaml",
        "config_file_exists": DEFAULT_CONFIG_PATH.exists(),
        "gate_statuses": list(GATE_STATUSES),
        "exit_code_semantics": {
            "PASS": 0,
            "WARN": cfg["warn_exit_code"],
            "BLOCK": 1,
            "INCONCLUSIVE": 2,
        },
        "note": (
            "阈值为项目演示配置（PROJECT_DEMO_DEFAULT_NOT_INDUSTRY_STANDARD），"
            "不是行业统一标准；INCONCLUSIVE 绝不等于 PASS"
        ),
    }


@app.post("/api/quality-gate/runs")
def quality_gate_create(body: QualityGateRequest) -> dict[str, Any]:
    """执行质量门禁（确定性规则），返回门禁报告。"""
    from backend.app.services.quality_gate import (
        QualityGateConfigError,
        QualityGateError,
        evaluate_gate,
    )

    if not (
        body.comparison_id
        or body.regression_report_id
        or (body.baseline_run_id and body.candidate_run_id)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "必须提供 comparison_id、regression_report_id 或 "
                "(baseline_run_id, candidate_run_id)"
            ),
        )
    try:
        return evaluate_gate(
            baseline_run_id=body.baseline_run_id,
            candidate_run_id=body.candidate_run_id,
            comparison_id=body.comparison_id,
            regression_report_id=body.regression_report_id,
            ci_run_id=body.ci_run_id,
        )
    except QualityGateConfigError as exc:
        raise HTTPException(status_code=400, detail=f"门禁配置非法: {exc}") from exc
    except QualityGateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/quality-gate/reports")
def quality_gate_reports(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    from backend.app.services.quality_gate import list_gate_reports

    items = list_gate_reports(limit=limit)
    return {"count": len(items), "reports": items}


def _gate_report_or_404(gate_report_id: str) -> dict[str, Any]:
    from backend.app.services.quality_gate import QualityGateError, get_gate_report

    try:
        return get_gate_report(gate_report_id)
    except QualityGateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/quality-gate/reports/{gate_report_id}")
def quality_gate_report(gate_report_id: str) -> dict[str, Any]:
    """查询门禁报告（检查项、触发规则、证据引用、限制）。"""
    return {"report": _gate_report_or_404(gate_report_id)}


@app.get("/api/quality-gate/reports/{gate_report_id}/markdown")
def quality_gate_report_markdown(gate_report_id: str) -> dict[str, Any]:
    """查询人工可读的门禁 Markdown 摘要。"""
    from backend.app.services.quality_gate import render_markdown

    report = _gate_report_or_404(gate_report_id)
    return {
        "gate_report_id": gate_report_id,
        "markdown": render_markdown(report),
    }


@app.get("/api/quality-gate/reports/{gate_report_id}/checks")
def quality_gate_report_checks(gate_report_id: str) -> dict[str, Any]:
    """查询门禁检查明细（阈值/实际值/状态/缺失证据标记）。"""
    report = _gate_report_or_404(gate_report_id)
    return {
        "gate_report_id": gate_report_id,
        "gate_status": report.get("gate_status"),
        "exit_code": report.get("exit_code"),
        "checks": report.get("checks"),
        "triggered_rules": report.get("triggered_rules"),
        "evidence_refs": report.get("evidence_refs"),
        "limitations": report.get("limitations"),
    }


# ---------------------------------------------------------------------------
# Automated Evaluation Pipeline（第 52 步）
# ---------------------------------------------------------------------------

@app.get("/api/pipelines/meta")
def pipelines_meta() -> dict[str, Any]:
    """流水线元信息：阶段定义、状态枚举、执行模式与资源限制。"""
    from backend.app.services import evaluation_pipeline as ep

    return {
        "stages": [
            {
                "stage_id": s["stage_id"],
                "stage_name": s["stage_name"],
                "required": s["required"],
                "depends_on": list(s["depends_on"]),
            }
            for s in ep.STAGES
        ],
        "stage_statuses": list(ep.STAGE_STATUSES),
        "pipeline_statuses": list(ep.PIPELINE_STATUSES),
        "execution_modes": ["mock", "real_api"],
        "judge_modes": ["none", "mock"],
        "limits": {
            "max_cases": ep.MAX_CASES,
            "max_concurrent_pipelines": ep.MAX_CONCURRENT_PIPELINES,
            "max_queue_length": ep.MAX_QUEUE_LENGTH,
            "max_runtime_seconds": ep.MAX_RUNTIME_SECONDS,
            "max_retries": ep.MAX_RETRIES_LIMIT,
            "max_label_length": ep.MAX_LABEL_LENGTH,
        },
        "real_api_note": (
            "真实 API 需请求 confirm_real=true 且服务端 TRACEPILOT_ALLOW_REAL_API=1；"
            "公开接口绝不自动触发付费调用，Broker 未授权时返回 403 且零调用"
        ),
        "mock_note": "默认 Mock 模式：受控确定性模拟执行器，零成本、无网络、无付费 API",
    }


@app.post("/api/pipelines")
def pipelines_create(body: PipelineCreateRequest) -> dict[str, Any]:
    """创建流水线（PENDING，不自动执行）。"""
    from backend.app.services.evaluation_pipeline import (
        PipelineAuthError,
        PipelineBusyError,
        PipelineConfigError,
        PipelineError,
        create_pipeline,
    )

    try:
        return create_pipeline(
            dataset_id=body.dataset_id,
            max_cases=body.max_cases,
            execution_mode=body.execution_mode,
            confirm_real=body.confirm_real,
            judge_mode=body.judge_mode,
            judge_rubric_id=body.judge_rubric_id,
            baseline_run_id=body.baseline_run_id,
            enable_attribution=body.enable_attribution,
            enable_failure_patterns=body.enable_failure_patterns,
            enable_version_comparison=body.enable_version_comparison,
            enable_regression=body.enable_regression,
            enable_quality_gate=body.enable_quality_gate,
            random_seed=body.random_seed,
            label=body.label,
            max_runtime_seconds=body.max_runtime_seconds,
            max_retries=body.max_retries,
        )
    except PipelineAuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PipelineBusyError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except PipelineConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipelines/{pipeline_run_id}/start")
def pipelines_start(pipeline_run_id: str) -> dict[str, Any]:
    """启动流水线（后台执行；仅 PENDING 可启动）。"""
    from backend.app.services.evaluation_pipeline import (
        PipelineBusyError,
        PipelineError,
        PipelineNotFoundError,
        PipelineStateError,
        start_pipeline,
    )

    try:
        return start_pipeline(pipeline_run_id)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PipelineStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PipelineBusyError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipelines/{pipeline_run_id}/cancel")
def pipelines_cancel(pipeline_run_id: str) -> dict[str, Any]:
    """取消尚未完成的流水线（PENDING 立即取消；RUNNING 置取消标记）。"""
    from backend.app.services.evaluation_pipeline import (
        PipelineError,
        PipelineNotFoundError,
        PipelineStateError,
        cancel_pipeline,
    )

    try:
        return cancel_pipeline(pipeline_run_id)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PipelineStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/pipelines")
def pipelines_list(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """历史流水线列表（内存 + 磁盘快照）。"""
    from backend.app.services.evaluation_pipeline import list_pipelines

    items = list_pipelines(limit=limit)
    return {"count": len(items), "pipelines": items}


@app.get("/api/pipelines/{pipeline_run_id}")
def pipelines_get(pipeline_run_id: str) -> dict[str, Any]:
    """查询流水线完整状态（含阶段、引用与最终报告）。"""
    from backend.app.services.evaluation_pipeline import PipelineNotFoundError, get_pipeline

    try:
        return get_pipeline(pipeline_run_id)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/pipelines/{pipeline_run_id}/stages")
def pipelines_stages(pipeline_run_id: str) -> dict[str, Any]:
    """查询阶段结果列表。"""
    from backend.app.services.evaluation_pipeline import (
        PipelineNotFoundError,
        get_pipeline_stages,
    )

    try:
        return get_pipeline_stages(pipeline_run_id)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/pipelines/{pipeline_run_id}/report")
def pipelines_report(pipeline_run_id: str) -> dict[str, Any]:
    """查询流水线最终报告（含证据引用与限制）。"""
    from backend.app.services.evaluation_pipeline import (
        PipelineNotFoundError,
        get_pipeline_report,
    )

    try:
        return get_pipeline_report(pipeline_run_id)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Token & Cost Analytics（第 53 步）
# ---------------------------------------------------------------------------

def _cost_error(exc: Exception) -> HTTPException:
    from backend.app.services.token_analytics import (
        CostAnalyticsError,
        PricingConfigError,
    )

    if isinstance(exc, PricingConfigError):
        return HTTPException(status_code=400, detail=f"价格配置非法: {exc}")
    if isinstance(exc, CostAnalyticsError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=500, detail=f"成本分析失败: {type(exc).__name__}: {exc}")


@app.get("/api/cost/pricing")
def cost_pricing() -> dict[str, Any]:
    """查询价格配置（版本/生效日期/来源/货币/分项单价）。"""
    from backend.app.services.token_analytics import load_pricing

    try:
        return {"pricing": load_pricing()}
    except Exception as exc:  # noqa: BLE001 - 统一为结构化错误
        raise _cost_error(exc) from exc


@app.get("/api/cost/overview")
def cost_overview(
    recent: int = Query(20, ge=1, le=100),
    refresh: bool = Query(False, description="强制刷新 Trace 索引缓存"),
    estimate_missing: bool = Query(False, description="显式开启缺失用量估算（标记 ESTIMATED）"),
    source_class: str | None = Query(
        None, description="按来源收窄统计范围：REAL_API / MOCK / SIMULATED / REPLAY / UNKNOWN"
    ),
) -> dict[str, Any]:
    """成本总览：Token / 成本 / 延迟 / 模型分布 / 版本对比 / UNKNOWN 统计。"""
    from backend.app.services.data_governance import SOURCE_CLASSES
    from backend.app.services.token_analytics import analyze_overview

    if source_class is not None and source_class.strip().upper() not in SOURCE_CLASSES:
        raise HTTPException(
            status_code=400,
            detail=f"source_class 必须是 {', '.join(SOURCE_CLASSES)} 之一",
        )
    try:
        return analyze_overview(
            recent=recent,
            estimate_missing=estimate_missing,
            force_refresh=refresh,
            source_class=source_class,
        )
    except Exception as exc:  # noqa: BLE001
        raise _cost_error(exc) from exc


@app.get("/api/cost/models")
def cost_models() -> dict[str, Any]:
    """模型成本分布（按 provider/model 聚合）。"""
    from backend.app.services.token_analytics import model_distribution

    try:
        return model_distribution()
    except Exception as exc:  # noqa: BLE001
        raise _cost_error(exc) from exc


@app.get("/api/cost/versions")
def cost_versions() -> dict[str, Any]:
    """版本成本变化（agent_version 缺失时按模型/执行模式分组并标注）。"""
    from backend.app.services.token_analytics import version_cost_comparison

    try:
        return version_cost_comparison()
    except Exception as exc:  # noqa: BLE001
        raise _cost_error(exc) from exc


@app.get("/api/cost/unknown")
def cost_unknown() -> dict[str, Any]:
    """UNKNOWN / PRICE_UNKNOWN / 缺失延迟数据清单（不把未知当 0）。"""
    from backend.app.services.token_analytics import unknown_data_report

    try:
        return unknown_data_report()
    except Exception as exc:  # noqa: BLE001
        raise _cost_error(exc) from exc


@app.get("/api/cost/attribution")
def cost_attribution_endpoint(
    level: str = Query("model_call", max_length=32),
    limit: int = Query(50, ge=1, le=200),
    usage_source: str | None = Query(None, max_length=32),
    source_class: str | None = Query(None, max_length=32),
) -> dict[str, Any]:
    """Cost Attribution Explorer：model_call / trace / agent_run / evaluation / pipeline。"""
    from backend.app.services.data_governance import SOURCE_CLASSES
    from backend.app.services.token_analytics import USAGE_SOURCES, cost_attribution

    allowed = {"model_call", "trace", "agent_run", "evaluation", "pipeline"}
    if level not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"level 必须是 {', '.join(sorted(allowed))} 之一",
        )
    if usage_source is not None and usage_source not in (*USAGE_SOURCES, "MIXED"):
        raise HTTPException(
            status_code=400,
            detail=f"usage_source 必须是 {', '.join((*USAGE_SOURCES, 'MIXED'))} 之一",
        )
    if source_class is not None and source_class.strip().upper() not in SOURCE_CLASSES:
        raise HTTPException(
            status_code=400,
            detail=f"source_class 必须是 {', '.join(SOURCE_CLASSES)} 之一",
        )
    try:
        return cost_attribution(
            level=level,
            limit=limit,
            usage_source=usage_source,
            source_class=source_class,
        )
    except Exception as exc:  # noqa: BLE001
        raise _cost_error(exc) from exc


@app.get("/api/cost/traces/{trace_id}")
def cost_trace(trace_id: str, estimate_missing: bool = Query(False)) -> dict[str, Any]:
    """单条 Trace 的 Token / 成本 / 延迟分析。"""
    from backend.app.services.token_analytics import analyze_trace

    try:
        return analyze_trace(trace_id, estimate_missing=estimate_missing)
    except Exception as exc:  # noqa: BLE001
        raise _cost_error(exc) from exc


@app.get("/api/cost/runs/{run_id}")
def cost_run(run_id: str) -> dict[str, Any]:
    """单次 Agent Run 成本（按 Trace 汇总）。"""
    from backend.app.services.token_analytics import analyze_agent_run

    try:
        return analyze_agent_run(run_id)
    except Exception as exc:  # noqa: BLE001
        raise _cost_error(exc) from exc


@app.get("/api/cost/evaluations/{evaluation_run_id}")
def cost_evaluation(evaluation_run_id: str) -> dict[str, Any]:
    """单次 Evaluation Run 成本（含评测延迟）。"""
    from backend.app.services.token_analytics import analyze_evaluation_run

    try:
        return analyze_evaluation_run(evaluation_run_id)
    except Exception as exc:  # noqa: BLE001
        raise _cost_error(exc) from exc


@app.get("/api/cost/pipelines/{pipeline_run_id}")
def cost_pipeline(pipeline_run_id: str) -> dict[str, Any]:
    """单次 Pipeline Run 成本（阶段级分摊不可靠时明确标注）。"""
    from backend.app.services.token_analytics import analyze_pipeline_run

    try:
        return analyze_pipeline_run(pipeline_run_id)
    except Exception as exc:  # noqa: BLE001
        raise _cost_error(exc) from exc


# ---------------------------------------------------------------------------
# Cost–Latency–Quality Pareto Analysis（第 54 步）
# ---------------------------------------------------------------------------

def _pareto_error(exc: Exception) -> HTTPException:
    from backend.app.services.pareto_analysis import (
        ParetoNotFoundError,
        ParetoValidationError,
    )

    if isinstance(exc, ParetoValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, ParetoNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=500, detail=f"帕累托分析失败: {type(exc).__name__}: {exc}")


@app.get("/api/pareto/candidates")
def pareto_candidates(limit: int = Query(50, ge=1, le=100)) -> dict[str, Any]:
    """查询候选版本（来自本进程真实评测运行；不伪造 Agent 版本）。"""
    from backend.app.services.pareto_analysis import list_candidates

    try:
        return list_candidates(limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise _pareto_error(exc) from exc


@app.get("/api/pareto/analyses")
def pareto_analyses_list(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """历史帕累托分析列表。"""
    from backend.app.services.pareto_analysis import list_analyses

    try:
        return list_analyses(limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise _pareto_error(exc) from exc


@app.post("/api/pareto/analyses")
def pareto_analysis_create(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """创建帕累托分析（可选交互约束；返回完整结构化报告）。"""
    from backend.app.services.pareto_analysis import create_analysis

    payload = body or {}
    try:
        return create_analysis(
            payload.get("constraints"),
            min_sample_size=int(payload.get("min_sample_size") or 3),
            limit=int(payload.get("limit") or 50),
        )
    except Exception as exc:  # noqa: BLE001
        raise _pareto_error(exc) from exc


@app.get("/api/pareto/analyses/{analysis_id}")
def pareto_analysis_get(analysis_id: str) -> dict[str, Any]:
    """查询帕累托分析报告。"""
    from backend.app.services.pareto_analysis import get_analysis

    try:
        return get_analysis(analysis_id)
    except Exception as exc:  # noqa: BLE001
        raise _pareto_error(exc) from exc


@app.get("/api/pareto/analyses/{analysis_id}/frontier")
def pareto_analysis_frontier(analysis_id: str) -> dict[str, Any]:
    """查询非支配解（Pareto Frontier）。"""
    from backend.app.services.pareto_analysis import get_analysis

    try:
        report = get_analysis(analysis_id)
        return {
            "analysis_id": analysis_id,
            "status": report.get("status"),
            "count": report.get("pareto_frontier_count"),
            "frontier": report.get("frontier") or [],
            "tie_groups": report.get("tie_groups") or [],
            "objective": report.get("objective"),
            "metric_definitions": report.get("metric_definitions"),
            "paid_api_called": False,
        }
    except Exception as exc:  # noqa: BLE001
        raise _pareto_error(exc) from exc


@app.get("/api/pareto/analyses/{analysis_id}/dominated")
def pareto_analysis_dominated(analysis_id: str) -> dict[str, Any]:
    """查询被支配解。"""
    from backend.app.services.pareto_analysis import get_analysis

    try:
        report = get_analysis(analysis_id)
        return {
            "analysis_id": analysis_id,
            "status": report.get("status"),
            "count": report.get("dominated_count"),
            "dominated": report.get("dominated") or [],
            "paid_api_called": False,
        }
    except Exception as exc:  # noqa: BLE001
        raise _pareto_error(exc) from exc


@app.get("/api/pareto/analyses/{analysis_id}/dominance")
def pareto_analysis_dominance(analysis_id: str) -> dict[str, Any]:
    """查询支配关系（含严格更优维度与差值）。"""
    from backend.app.services.pareto_analysis import get_analysis

    try:
        report = get_analysis(analysis_id)
        return {
            "analysis_id": analysis_id,
            "count": len(report.get("dominance_relations") or []),
            "relations": report.get("dominance_relations") or [],
            "definition": (report.get("objective") or {}).get("definition"),
            "paid_api_called": False,
        }
    except Exception as exc:  # noqa: BLE001
        raise _pareto_error(exc) from exc


@app.post("/api/pareto/analyses/{analysis_id}/filter")
def pareto_analysis_filter(
    analysis_id: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """按新约束重新筛选候选并重算帕累托前沿（不修改原报告）。"""
    from backend.app.services.pareto_analysis import filter_analysis

    try:
        return filter_analysis(analysis_id, body or {})
    except Exception as exc:  # noqa: BLE001
        raise _pareto_error(exc) from exc


@app.get("/api/pareto/analyses/{analysis_id}/candidates/{candidate_id}/evaluation")
def pareto_candidate_evaluation(analysis_id: str, candidate_id: str) -> dict[str, Any]:
    """查询候选关联的 Evaluation Run。"""
    from backend.app.services.pareto_analysis import candidate_evaluation_run

    try:
        return candidate_evaluation_run(analysis_id, candidate_id)
    except Exception as exc:  # noqa: BLE001
        raise _pareto_error(exc) from exc


@app.get("/api/pareto/analyses/{analysis_id}/candidates/{candidate_id}/traces")
def pareto_candidate_traces(analysis_id: str, candidate_id: str) -> dict[str, Any]:
    """查询候选关联的 Trace（供 Trace Explorer / DAG 跳转）。"""
    from backend.app.services.pareto_analysis import candidate_traces

    try:
        return candidate_traces(analysis_id, candidate_id)
    except Exception as exc:  # noqa: BLE001
        raise _pareto_error(exc) from exc


# ---------------------------------------------------------------------------
# 第 55 步：需求回溯与能力审计（根因准确率指标 / 20 项能力 / 可解释性 / HITL）
# ---------------------------------------------------------------------------

def _capability_error(exc: Exception) -> HTTPException:
    from backend.app.services.capability_suite import get_hitl_store  # noqa: F401
    from backend.app.services.rootcause_metrics import (
        RootCauseMetricsError,
        RootCauseMetricsNotFoundError,
    )

    if isinstance(exc, RootCauseMetricsNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RootCauseMetricsError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, (KeyError, FileNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc).strip("'\""))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=f"能力审计失败: {type(exc).__name__}: {exc}")


@app.get("/api/capabilities/matrix")
def capabilities_matrix() -> dict[str, Any]:
    """20 项 Agent 能力 + 7 个创新点矩阵（状态、实现、证据、限制）。"""
    from backend.app.services.capability_suite import capability_matrix_report

    return capability_matrix_report()


@app.get("/api/capabilities/knowledge/search")
def capabilities_knowledge_search(
    q: str = Query(..., min_length=1, max_length=200),
    top_k: int = Query(3, ge=1, le=10),
) -> dict[str, Any]:
    """本地知识库检索（RAG，合成数据；结果经上下文审查）。"""
    from backend.app.services.capability_suite import search_knowledge

    try:
        return search_knowledge(q, top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        raise _capability_error(exc) from exc


@app.post("/api/capabilities/context/preview")
def capabilities_context_preview(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """上下文工程预览：任务 + 检索文档 + 记忆 → 预算内提示上下文（不调用模型）。"""
    from backend.app.services.capability_suite import build_context

    payload = body or {}
    try:
        return build_context(
            task=str(payload.get("task") or ""),
            documents=payload.get("documents") or [],
            memories=payload.get("memories") or [],
            max_chars=int(payload.get("max_chars") or 4000),
        )
    except Exception as exc:  # noqa: BLE001
        raise _capability_error(exc) from exc


@app.get("/api/traces/{trace_id}/explanation")
def trace_explanation(trace_id: str) -> dict[str, Any]:
    """基于 Trace 证据的决策解释（不保存/不还原模型思维链）。"""
    from backend.app.services.capability_suite import explain_trace
    from backend.app.services.trace_store import load_trace

    try:
        return explain_trace(load_trace(trace_id))
    except Exception as exc:  # noqa: BLE001
        raise _capability_error(exc) from exc


@app.post("/api/attribution/metrics")
def attribution_metrics_create(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """按已标注故障数据集计算根因 Top-1/Top-3 等准确率指标。"""
    from backend.app.services.rootcause_metrics import compute_metrics

    payload = body or {}
    run_id = str(payload.get("evaluation_run_id") or "").strip()
    if not run_id:
        raise HTTPException(status_code=400, detail="evaluation_run_id 不能为空")
    try:
        return compute_metrics(
            run_id,
            max_cases=int(payload.get("max_cases") or 50),
            include_repair=bool(payload.get("include_repair", False)),
        )
    except Exception as exc:  # noqa: BLE001
        raise _capability_error(exc) from exc


@app.get("/api/attribution/metrics")
def attribution_metrics_list(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """历史根因准确率指标报告。"""
    from backend.app.services.rootcause_metrics import list_metrics

    return list_metrics(limit=limit)


@app.get("/api/attribution/metrics/{metrics_id}")
def attribution_metrics_get(metrics_id: str) -> dict[str, Any]:
    """单份根因准确率指标报告。"""
    from backend.app.services.rootcause_metrics import get_metrics

    try:
        return get_metrics(metrics_id)
    except Exception as exc:  # noqa: BLE001
        raise _capability_error(exc) from exc


@app.post("/api/hitl/approvals")
def hitl_approval_create(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """创建 HITL 审批请求（PENDING，不会自动批准）。"""
    from backend.app.services.capability_suite import get_hitl_store

    payload = body or {}
    try:
        return get_hitl_store().create(
            action=str(payload.get("action") or ""),
            reason=str(payload.get("reason") or ""),
            risk_level=str(payload.get("risk_level") or "MEDIUM").upper(),
            run_id=(str(payload["run_id"]) if payload.get("run_id") else None),
        )
    except Exception as exc:  # noqa: BLE001
        raise _capability_error(exc) from exc


@app.get("/api/hitl/approvals")
def hitl_approval_list(
    status: str | None = Query(None, max_length=16),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """查询审批请求列表（可按 PENDING/APPROVED/REJECTED 过滤）。"""
    from backend.app.services.capability_suite import get_hitl_store

    try:
        rows = get_hitl_store().list(status=status, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise _capability_error(exc) from exc
    return {"count": len(rows), "approvals": rows, "paid_api_called": False}


@app.get("/api/hitl/approvals/{approval_id}")
def hitl_approval_get(approval_id: str) -> dict[str, Any]:
    """查询单个审批请求。"""
    from backend.app.services.capability_suite import get_hitl_store

    try:
        return get_hitl_store().get(approval_id)
    except Exception as exc:  # noqa: BLE001
        raise _capability_error(exc) from exc


@app.post("/api/hitl/approvals/{approval_id}/decision")
def hitl_approval_decide(
    approval_id: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """人工批准/拒绝（APPROVED / REJECTED），记录审计字段。"""
    from backend.app.services.capability_suite import get_hitl_store

    payload = body or {}
    try:
        return get_hitl_store().decide(
            approval_id,
            decision=str(payload.get("decision") or "").upper(),
            decided_by=str(payload.get("decided_by") or "human"),
            comment=(str(payload["comment"]) if payload.get("comment") else None),
        )
    except Exception as exc:  # noqa: BLE001
        raise _capability_error(exc) from exc


# ---------------------------------------------------------------------------
# 第 55-A 步：数据治理与可信度（来源分类 / 失败 Trace 口径 / 受控实验资格）
# ---------------------------------------------------------------------------

class GovernanceScanRequest(BaseModel):
    persist: bool = Field(
        default=False, description="是否把分类结果写入 sidecar 索引（非破坏性、可回滚）"
    )
    force: bool = Field(default=True, description="是否强制重新扫描（忽略 sidecar 与缓存）")


def _governance_sources_payload(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        **summary,
        "signature_current": not bool(summary.get("stale")),
        "source_classes": list(SOURCE_CLASSES),
        "definitions": SOURCE_DEFINITIONS,
        "scope_note": (
            "统计口径覆盖主 Trace（Agent 运行）与评测 Trace；只读汇总，不改写原始记录"
        ),
        "read_only": True,
        "paid_api_called": False,
    }


@app.get("/api/governance/sources")
def governance_sources(force: bool = Query(False, description="强制重新扫描（约 35 秒）")):
    """数据来源分类汇总（全库口径，优先使用 sidecar 索引，30 秒缓存）。"""
    from backend.app.services.data_governance import scan_sources

    try:
        # 页面读取：目录变化时返回上一次快照并后台刷新，避免 35 秒全量扫描阻塞请求
        summary = scan_sources(force=force, allow_stale=not force)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"数据来源扫描失败: {exc}") from exc
    return _governance_sources_payload(summary)


@app.post("/api/governance/scan")
def governance_scan(body: GovernanceScanRequest | None = None):
    """重新扫描来源分类；可选写入 sidecar 索引（非破坏性迁移，供审计/复验）。"""
    from backend.app.services.data_governance import scan_sources

    payload = body or GovernanceScanRequest()
    try:
        summary = scan_sources(force=payload.force, persist=payload.persist)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"数据来源扫描失败: {exc}") from exc
    result = _governance_sources_payload(summary)
    result["persisted"] = bool(payload.persist)
    return result


@app.get("/api/governance/failed-summary")
def governance_failed_summary(
    limit: int = Query(10, ge=1, le=200, description="失败 Trace 样本数量")
):
    """失败 Trace 的存储/查询口径汇总（基于主 Trace 目录缓存，不做全量重扫）。

    用于回答“为什么失败 Trace 明明存在却查不到”：明确当前数据范围、
    失败计数、来源分类与故障注入覆盖，以及可下钻的样本 ID。
    """
    summaries = list_trace_summaries()
    failed = [s for s in summaries if str(s.get("status", "")).lower() == "failed"]
    # 样本按开始时间倒序，便于在 UI 中直接看到最近的失败 Trace
    failed.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    by_source: dict[str, int] = {}
    with_faults = 0
    fault_type_counts: dict[str, int] = {}
    for s in failed:
        cls = s.get("source_class", "UNKNOWN")
        by_source[cls] = by_source.get(cls, 0) + 1
        ftypes = s.get("fault_types") or []
        if ftypes:
            with_faults += 1
        for ftype in ftypes:
            fault_type_counts[str(ftype)] = fault_type_counts.get(str(ftype), 0) + 1
    samples = [
        {
            "trace_id": s["trace_id"],
            "status": s["status"],
            "source_class": s.get("source_class"),
            "step_count": s.get("step_count"),
            "fault_types": list(s.get("fault_types") or []),
            "started_at": s.get("started_at"),
            "detail": f"/api/traces/{s['trace_id']}",
            "eligibility": f"/api/governance/traces/{s['trace_id']}/eligibility",
        }
        for s in failed[:limit]
    ]
    return {
        "scope": "agent_traces",
        "scope_note": "主 Trace 目录（Agent 运行记录）；评测 Trace 不在本列表内",
        "total_traces": len(summaries),
        "failed_count": len(failed),
        "failed_with_fault_injection": with_faults,
        "failed_by_source": by_source,
        "failed_fault_type_counts": fault_type_counts,
        "samples": samples,
        "read_only": True,
        "paid_api_called": False,
    }


@app.get("/api/governance/traces/{trace_id}")
def governance_trace_classification(trace_id: str):
    """单条 Trace 的来源分类与证据（主目录与评测目录均可下钻）。"""
    record = load_trace(trace_id)
    return {
        "classification": get_trace_classification(record),
        "read_only": True,
        "paid_api_called": False,
    }


@app.get("/api/governance/traces/{trace_id}/eligibility")
def governance_trace_eligibility(trace_id: str):
    """单条 Trace 对重放 / 受控重跑 / 反事实 / 修复候选四类操作的资格与原因。"""
    record = load_trace(trace_id)
    return {"eligibility": trace_eligibility(record), "paid_api_called": False}
