"""派生 Trace 持久化（第 44 步）。

反事实干预 / 修复验证会产生**派生执行 Trace**（`trace-cf-*`）。此前它们只存在于
内存返回值中，导致：

- `GET /api/traces/{trace-cf-*}` 返回 404，证据链断裂；
- 派生结果缺少统一的数据来源标记。

本模块负责把派生 Trace 落盘，并**强制写入诚实的来源元数据**：

- `data_source = "simulated"`（派生执行是模拟环境重放，不是真实 API）
- `simulated = True`
- `derived_from = <原始 Trace ID>`（保留原始数据来源）
- `derivation = <counterfactual | repair_validation>`（派生方式）
- `engine_version`

绝不把派生结果标记为 `real_api`。原始 Trace 从不被改写。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.app.schemas.trace import TraceRecord
from backend.app.services.trace_collector import TraceCollector
from backend.app.services.trace_store import DEFAULT_TRACES_DIR

__all__ = [
    "DERIVATION_KINDS",
    "save_derived_trace",
    "build_derived_metadata",
]

DERIVATION_KINDS = ("counterfactual", "repair_validation", "failure_replay")

DERIVATION_LABELS = {
    "counterfactual": "反事实干预重放",
    "repair_validation": "修复方案验证重放",
    "failure_replay": "故障重放",
}


def _canonical_origin(metadata: dict[str, Any]) -> str:
    """把原始 metadata 归一为规范来源词表（REAL_API / MOCK / SIMULATED / UNKNOWN）。"""
    try:
        from backend.app.services.evidence_lineage import classify_origin

        return classify_origin(metadata)[0]
    except Exception:  # pragma: no cover - 保守回退
        ds = (metadata or {}).get("data_source")
        if ds in {"real_api", "mock", "simulated"}:
            return {"real_api": "REAL_API", "mock": "MOCK", "simulated": "SIMULATED"}[ds]
        if (metadata or {}).get("simulated") is True:
            return "SIMULATED"
        return "UNKNOWN"


def build_derived_metadata(
    source: TraceRecord,
    *,
    derivation: str,
    engine_version: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造派生 Trace 的元数据（保留原始来源，不伪造真实性）。"""
    if derivation not in DERIVATION_KINDS:
        raise ValueError(f"不支持的派生类型: {derivation!r}")

    src_meta = source.metadata or {}
    return {
        # 派生结果是模拟环境重放
        "data_source": "simulated",
        "simulated": True,
        "simulated_model": True,
        # 血缘：原始来源必须保留（原始 Trace 可能本身是 mock / real_api）
        "derived_from": source.trace_id,
        "derivation": derivation,
        "derivation_label": DERIVATION_LABELS[derivation],
        "source_data_source": _canonical_origin(src_meta),
        "source_data_source_raw": src_meta.get("data_source"),
        "source_task_id": source.task_id,
        "source_run_id": source.run_id,
        "engine_version": engine_version,
        "lineage_note": "派生执行结果；来源为原始 Trace，非真实 API 调用",
        **(extra or {}),
    }


def save_derived_trace(
    record: TraceRecord,
    source: TraceRecord,
    *,
    derivation: str,
    engine_version: str,
    extra: dict[str, Any] | None = None,
    traces_dir: str | Path | None = None,
) -> Path:
    """把派生 Trace 落盘（可被 `GET /api/traces/{id}` 查询）。

    Args:
        record: 派生 Trace。
        source: 原始 Trace（用于记录血缘与来源）。
        derivation: 派生方式（counterfactual / repair_validation / failure_replay）。
        engine_version: 生成该派生 Trace 的引擎版本。

    Returns:
        保存到的文件路径。
    """
    metadata = build_derived_metadata(
        source, derivation=derivation, engine_version=engine_version, extra=extra
    )
    enriched = record.model_copy(update={"metadata": {**(record.metadata or {}), **metadata}})

    collector = TraceCollector(
        task_id=enriched.task_id,
        trace_id=enriched.trace_id,
        run_id=enriched.run_id,
        started_at=enriched.started_at,
        traces_dir=traces_dir or DEFAULT_TRACES_DIR,
    )
    return collector.save(enriched)
