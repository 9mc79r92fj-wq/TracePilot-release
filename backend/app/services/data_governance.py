"""数据来源分类与数据治理（第 55-A 步）。

统一 Trace 来源分类，解决「真实数据 / 模拟数据混用」与统计口径问题：

- REAL_API：确有真实模型 API 调用且**有独立可验证标记**（`real_api_verified=true`）；
- MOCK：本地 Mock Agent / 脚本化模型实际执行产生的轨迹；
- SIMULATED：评测批量合成 / 离线构造 / 模拟器输出；
- REPLAY：基于历史 Trace 的受控重放或反事实/修复验证派生记录；
- UNKNOWN：来源无法可靠确认（包括历史 `data_source=real_api` 但缺少调用证据的数据）。

严格规则（防止“升级”来源）：
- 仅凭 model 名称、Token 字段、`simulated=false` 或 `data_source=real_api` **不能**判 REAL_API；
- 缺 `real_api_verified` 标记的旧数据 → UNKNOWN，并在 evidence 中说明原因；
- 分类是**只读派生**，不修改原始 Trace 文件（可回滚）；迁移仅写 `data/governance/source_index.json`。

统计口径：
- `agent`：`data/traces/`（Agent 执行轨迹）；
- `evaluation`：`data/evaluation/traces/`（评测批量生成的轨迹）；
- 两个目录合计是 Cost Analytics 的数据范围；Overview 真实 KPI 只使用 REAL_API。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "SOURCE_CLASSES",
    "SOURCE_DEFINITIONS",
    "classify_metadata",
    "classify_trace_record",
    "scan_sources",
    "get_source_summary",
    "write_source_index",
    "get_trace_classification",
    "trace_eligibility",
    "SOURCE_INDEX_PATH",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TRACE_DIRS = (
    PROJECT_ROOT / "data" / "traces",
    PROJECT_ROOT / "data" / "evaluation" / "traces",
)
GOVERNANCE_DIR = PROJECT_ROOT / "data" / "governance"
SOURCE_INDEX_PATH = GOVERNANCE_DIR / "source_index.json"
CACHE_TTL_SECONDS = 30.0

SOURCE_CLASSES = ("REAL_API", "MOCK", "SIMULATED", "REPLAY", "UNKNOWN")
SOURCE_DEFINITIONS = {
    "REAL_API": "确有真实模型 API 调用且存在独立可验证标记（real_api_verified=true）",
    "MOCK": "本地 Mock Agent / 脚本化模型实际执行产生的轨迹",
    "SIMULATED": "评测批量合成、离线构造或模拟器输出",
    "REPLAY": "基于历史 Trace 的受控重放 / 反事实 / 修复验证派生记录",
    "UNKNOWN": "来源无法可靠确认（不得自动归入 REAL_API）",
}

_CACHE: dict[str, Any] = {"signature": None, "ts": 0.0, "summary": None}
_CACHE_LOCK = threading.Lock()
# 全库扫描单飞锁：并发请求在目录变化时不得重复执行 35 秒级扫描
_SCAN_LOCK = threading.Lock()
_REFRESH_STATE: dict[str, Any] = {"thread": None}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def classify_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """仅基于 Trace metadata 做保守分类（返回 source_class + 证据 + 理由）。"""
    meta = metadata or {}
    data_source = str(meta.get("data_source") or "").strip().lower()
    simulated = meta.get("simulated") is True
    simulated_model = meta.get("simulated_model") is True
    derivation = str(meta.get("derivation") or "")
    derived = bool(
        meta.get("derived_from")
        or derivation in {"counterfactual", "repair_validation", "replay"}
        or meta.get("counterfactual") is True
        or meta.get("replay") is True
    )
    real_verified = meta.get("real_api_verified") is True
    evidence: list[str] = []
    reasons: list[str] = []

    if data_source == "real_api":
        evidence.append("data_source=real_api")
    if simulated:
        evidence.append("simulated=true")
    if simulated_model:
        evidence.append("simulated_model=true")
    if derivation:
        evidence.append(f"derivation={derivation}")
    if meta.get("evaluation"):
        evidence.append("evaluation=true")
    if real_verified:
        evidence.append("real_api_verified=true")

    if real_verified:
        if data_source != "real_api" or simulated or simulated_model:
            reasons.append("real_api_verified 与 data_source/simulated 标记矛盾，按 UNKNOWN 处理")
            return {
                "source_class": "UNKNOWN",
                "evidence": evidence,
                "reasons": reasons,
            }
        return {
            "source_class": "REAL_API",
            "evidence": evidence,
            "reasons": ["存在独立 real_api_verified 标记"],
        }

    if data_source == "real_api":
        reasons.append(
            "历史数据声明 data_source=real_api，但缺少 real_api_verified 独立调用证据；"
            "按保守规则归为 UNKNOWN，不自动升级"
        )
        return {"source_class": "UNKNOWN", "evidence": evidence, "reasons": reasons}

    if derived:
        reasons.append("存在 derivation / derived_from / counterfactual / replay 派生标记")
        return {"source_class": "REPLAY", "evidence": evidence, "reasons": reasons}

    if data_source == "mock":
        reasons.append("data_source=mock（本地 Mock Agent 实际执行）")
        return {"source_class": "MOCK", "evidence": evidence, "reasons": reasons}

    if data_source == "simulated":
        reasons.append("data_source=simulated")
        return {"source_class": "SIMULATED", "evidence": evidence, "reasons": reasons}

    if simulated or simulated_model or meta.get("evaluation") or meta.get("case_id"):
        reasons.append("simulated / simulated_model / evaluation / case_id 标记表明为模拟或批量评测数据")
        return {"source_class": "SIMULATED", "evidence": evidence, "reasons": reasons}

    if data_source:
        reasons.append(f"未知 data_source={data_source!r}，无足够证据确认来源")
    else:
        reasons.append("缺少 data_source / simulated / derivation 等来源字段")
    return {"source_class": "UNKNOWN", "evidence": evidence, "reasons": reasons}


def classify_trace_record(record: Any) -> dict[str, Any]:
    meta = getattr(record, "metadata", None) or {}
    return classify_metadata(meta)


def _dir_signature() -> tuple[tuple[int, int], ...]:
    signature: list[tuple[int, int]] = []
    for directory in TRACE_DIRS:
        if not directory.exists():
            signature.append((0, 0))
            continue
        try:
            count = 0
            latest = 0
            with os.scandir(directory) as it:
                for entry in it:
                    if not entry.name.endswith(".json"):
                        continue
                    count += 1
                    try:
                        latest = max(latest, entry.stat().st_mtime_ns)
                    except OSError:
                        continue
            signature.append((count, latest))
        except OSError:
            signature.append((0, 0))
    return tuple(signature)


def _scan_uncached() -> dict[str, Any]:
    by_source: Counter[str] = Counter()
    by_dir: dict[str, Counter[str]] = {}
    status_by_source: dict[str, Counter[str]] = {}
    fault_by_source: Counter[str] = Counter()
    unknown_examples: list[dict[str, Any]] = []
    legacy_real_claims: list[dict[str, Any]] = []
    duplicate_ids: dict[str, list[str]] = {}
    seen_ids: dict[str, str] = {}
    malformed = 0
    total = 0
    per_source_evidence: Counter[str] = Counter()
    entries: dict[str, dict[str, Any]] = {}

    for index, directory in enumerate(TRACE_DIRS):
        dir_name = "agent" if index == 0 else "evaluation"
        by_dir.setdefault(dir_name, Counter())
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            total += 1
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                malformed += 1
                continue
            trace_id = str(data.get("trace_id") or path.stem)
            meta = data.get("metadata") or {}
            classified = classify_metadata(meta)
            source = classified["source_class"]
            by_source[source] += 1
            by_dir[dir_name][source] += 1
            status = str(data.get("status") or "unknown")
            status_by_source.setdefault(source, Counter())[status] += 1
            fault_count = len(meta.get("fault_injections") or [])
            if fault_count:
                fault_by_source[source] += 1
            if classified["evidence"]:
                per_source_evidence[source] += 1
            if trace_id in seen_ids:
                duplicate_ids.setdefault(trace_id, [seen_ids[trace_id]]).append(
                    _display_path(path)
                )
            else:
                seen_ids[trace_id] = _display_path(path)
            entries[trace_id] = {
                "source_class": source,
                "evidence": classified["evidence"],
                "reasons": classified["reasons"],
                "path": _display_path(path),
                "scope": dir_name,
                "status": status,
                "fault_count": fault_count,
            }
            if source == "UNKNOWN" and len(unknown_examples) < 10:
                unknown_examples.append(
                    {
                        "trace_id": trace_id,
                        "path": _display_path(path),
                        "claimed_source": meta.get("data_source"),
                        "reasons": classified["reasons"],
                    }
                )
            if meta.get("data_source") == "real_api" and source != "REAL_API":
                legacy_real_claims.append(
                    {
                        "trace_id": trace_id,
                        "path": _display_path(path),
                        "classified_as": source,
                        "reasons": classified["reasons"],
                    }
                )

    evaluation_count = by_dir.get("evaluation", Counter())
    agent_count = by_dir.get("agent", Counter())
    summary = {
        "generated_at": _now_iso(),
        "governance_version": "1.0.0",
        "scope": {
            "agent_dir": _display_path(TRACE_DIRS[0]),
            "evaluation_dir": _display_path(TRACE_DIRS[1]),
            "note": "Cost Analytics 使用两个目录合计；Overview 真实 KPI 仅使用 REAL_API",
        },
        "total_traces": total,
        "agent_traces": sum(agent_count.values()),
        "evaluation_traces": sum(evaluation_count.values()),
        "by_source": {source: by_source.get(source, 0) for source in SOURCE_CLASSES},
        "by_scope_source": {
            scope: {source: counts.get(source, 0) for source in SOURCE_CLASSES}
            for scope, counts in by_dir.items()
        },
        "status_by_source": {
            source: dict(status_by_source.get(source, {})) for source in SOURCE_CLASSES
        },
        "fault_traces_by_source": {
            source: fault_by_source.get(source, 0) for source in SOURCE_CLASSES
        },
        "source_definitions": SOURCE_DEFINITIONS,
        "unknown_count": by_source.get("UNKNOWN", 0),
        "unknown_examples": unknown_examples,
        "legacy_real_api_claims": legacy_real_claims,
        "duplicate_trace_ids": {
            trace_id: paths for trace_id, paths in duplicate_ids.items()
        },
        "malformed_files": malformed,
        "notes": [
            "来源分类为只读扫描，不修改原始 Trace；扫描结果仅写入来源索引",
            "历史 data_source=real_api 但缺少 real_api_verified 标记的数据归为 UNKNOWN，不自动升级",
            "模拟/评测数据不进入真实运行 KPI，可在独立筛选中查看",
        ],
    }
    return {"summary": summary, "entries": entries, "signature": _dir_signature()}


def _load_sidecar() -> dict[str, Any] | None:
    if not SOURCE_INDEX_PATH.exists():
        return None
    try:
        data = json.loads(SOURCE_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 坏 sidecar 不影响实时扫描
        return None
    return data if isinstance(data, dict) else None


def _scan_locked() -> dict[str, Any]:
    """单飞执行全量扫描（同一时刻只允许一个线程解析 16k Trace）。"""
    with _SCAN_LOCK:
        return _scan_uncached()


def _sidecar_summary() -> dict[str, Any] | None:
    sidecar = _load_sidecar()
    if not sidecar:
        return None
    return {
        key: value
        for key, value in sidecar.items()
        if key not in {"entries", "signature", "entry_count"}
    }


def _start_background_refresh(signature: tuple[tuple[int, int], ...]) -> bool:
    """目录签名失效时后台重扫并回写 sidecar（stale-while-revalidate）。"""
    with _CACHE_LOCK:
        thread = _REFRESH_STATE.get("thread")
        if thread is not None and thread.is_alive():
            return False

        def worker() -> None:
            try:
                scanned = _scan_locked()
                summary = scanned["summary"]
                with _CACHE_LOCK:
                    _CACHE.update(
                        signature=scanned["signature"], ts=time.time(), summary=summary
                    )
                write_source_index(scanned["entries"], summary)
            except Exception:  # noqa: BLE001 - 后台刷新失败不影响返回旧快照
                return

        thread = threading.Thread(target=worker, name="governance-refresh", daemon=True)
        _REFRESH_STATE["thread"] = thread
        thread.start()
        return True


def scan_sources(
    *, force: bool = False, persist: bool = False, allow_stale: bool = False
) -> dict[str, Any]:
    """扫描两个 Trace 目录并按来源分类；优先使用 sidecar，缓存 30 秒。

    - 迁移脚本 `scripts/audit_data_sources.py --apply` 生成 sidecar；API 首次加载
      直接读取 sidecar，避免每次重启都重新解析 16k Trace（实测全量扫描约 35 秒）；
    - allow_stale=True（页面读取用）：目录已变化但 sidecar 存在时，**返回上一次
      扫描结果并标记 stale=True**，同时后台单飞重扫 + 回写 sidecar，避免用户请求
      被 35 秒全量扫描阻塞；审计/脚本仍使用同步扫描（allow_stale=False）保证准确。
    """
    now = time.time()
    signature = _dir_signature()
    with _CACHE_LOCK:
        if (
            not force
            and _CACHE["signature"] == signature
            and _CACHE["summary"] is not None
            and now - _CACHE["ts"] < CACHE_TTL_SECONDS
        ):
            return {**_CACHE["summary"], "stale": False}

    if not force:
        sidecar = _load_sidecar()
        if sidecar and sidecar.get("signature") == [list(item) for item in signature]:
            summary = {
                key: value
                for key, value in sidecar.items()
                if key not in {"entries", "signature", "entry_count"}
            }
            with _CACHE_LOCK:
                _CACHE.update(signature=signature, ts=now, summary=summary)
            return {**summary, "stale": False}

    if allow_stale and not force:
        stale_summary = _sidecar_summary()
        if stale_summary is None:
            with _CACHE_LOCK:
                if _CACHE["summary"] is not None:
                    stale_summary = dict(_CACHE["summary"])
        if stale_summary is not None:
            refreshed = _start_background_refresh(signature)
            return {
                **stale_summary,
                "stale": True,
                "refresh_started": refreshed,
                "stale_note": (
                    "数据目录已变化，后台正在刷新来源索引；当前返回上一次扫描结果，"
                    "不阻塞页面加载"
                ),
            }

    scanned = _scan_locked()
    summary = scanned["summary"]
    with _CACHE_LOCK:
        _CACHE.update(signature=scanned["signature"], ts=now, summary=summary)
    if persist:
        write_source_index(scanned["entries"], summary)
    return {**summary, "stale": False}


def get_source_summary(*, force: bool = False) -> dict[str, Any]:
    return scan_sources(force=force)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)


def write_source_index(
    entries: dict[str, dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把来源分类写入 sidecar 索引（非破坏性、可回滚）。"""
    if summary is None or entries is None:
        scanned = _scan_uncached()
        summary = scanned["summary"]
        entries = scanned["entries"]
    payload = {
        **summary,
        "signature": [list(item) for item in _dir_signature()],
        "entry_count": len(entries),
        "entries": entries,
    }
    _atomic_write(SOURCE_INDEX_PATH, payload)
    return {
        "path": _display_path(SOURCE_INDEX_PATH),
        "entry_count": len(entries),
        "summary": summary,
    }


def get_trace_classification(record: Any) -> dict[str, Any]:
    result = classify_trace_record(record)
    result.update(
        {
            "trace_id": getattr(record, "trace_id", None),
            "source_definitions": SOURCE_DEFINITIONS,
            "definitions_note": "分类只读派生，不修改原始 Trace",
        }
    )
    return result


# ---------------------------------------------------------------------------
# 受控实验资格（只读判断，不执行实验）
# ---------------------------------------------------------------------------

_REPLAY_FAULT_TYPES = {
    "tool_timeout",
    "tool_exception",
    "invalid_tool_result",
    "empty_retrieval",
    "invalid_arguments",
    "permission_denied",
}
_SAFE_TOOLS = {"calculator", "text_analyzer", "sqlite_query"}
_REPAIRABLE_FAULT_TYPES = {
    "tool_timeout",
    "tool_exception",
    "invalid_arguments",
    "permission_denied",
    "invalid_tool_result",
    "empty_retrieval",
}


def _fault_events(record: Any) -> list[dict[str, Any]]:
    events = []
    for step in getattr(record, "steps", []) or []:
        fault = (step.metadata or {}).get("fault_injection")
        if isinstance(fault, dict) and fault.get("injected") is True:
            events.append(
                {
                    "step_id": step.step_id,
                    "tool_name": step.tool_name,
                    "fault_type": fault.get("type"),
                }
            )
    return events


def trace_eligibility(record: Any) -> dict[str, Any]:
    """返回四类操作的资格与具体原因（不修改任何业务规则）。"""
    trace_id = getattr(record, "trace_id", None)
    steps = list(getattr(record, "steps", []) or [])
    faults = _fault_events(record)
    tool_steps = [s for s in steps if getattr(s, "tool_name", None)]

    def cap(eligible: bool, reasons: list[str], requirements: list[str]) -> dict[str, Any]:
        return {"eligible": eligible, "reasons": reasons, "requirements": requirements}

    replay_reasons = [] if steps else ["Trace 没有可重放步骤"]
    controlled_reasons: list[str] = []
    if not faults:
        controlled_reasons.append("未发现注入故障事件（fault_injection.injected=true）")
    for fault in faults:
        if fault["fault_type"] not in _REPLAY_FAULT_TYPES:
            controlled_reasons.append(f"故障类型不在受控重跑白名单: {fault['fault_type']!r}")
        if fault["tool_name"] and fault["tool_name"] not in _SAFE_TOOLS:
            controlled_reasons.append(f"工具不在受控重跑白名单: {fault['tool_name']!r}")

    counterfactual_reasons: list[str] = []
    if not tool_steps:
        counterfactual_reasons.append("Trace 没有工具调用步骤，无法推断受控场景")
    if not faults:
        counterfactual_reasons.append("不是故障 Trace：无故障注入事件，反事实实验缺少对照对象")
    else:
        unsupported_scenario_tools = sorted(
            {
                f["tool_name"]
                for f in faults
                if f["tool_name"] and f["tool_name"] not in {"calculator", "text_analyzer"}
            }
        )
        if unsupported_scenario_tools:
            counterfactual_reasons.append(
                "反事实场景白名单仅支持 calculator / text_analyzer，当前故障工具: "
                + ", ".join(unsupported_scenario_tools)
            )

    repair_reasons: list[str] = []
    if not faults:
        error_steps = [s for s in steps if getattr(s, "error", None) is not None]
        if not error_steps:
            repair_reasons.append("未发现故障注入或错误步骤，无法生成修复候选")
    repairable = [
        f for f in faults if f["fault_type"] in _REPAIRABLE_FAULT_TYPES
    ]
    if faults and not repairable:
        repair_reasons.append(
            "故障类型不支持修复候选: " + ", ".join(sorted({str(f['fault_type']) for f in faults}))
        )
    if repairable:
        unsafe_tools = sorted(
            {f["tool_name"] for f in repairable if f["tool_name"] and f["tool_name"] not in _SAFE_TOOLS}
        )
        if unsafe_tools:
            repair_reasons.append("修复目标工具不在白名单: " + ", ".join(unsafe_tools))

    return {
        "trace_id": trace_id,
        "status": getattr(getattr(record, "status", None), "value", str(getattr(record, "status", ""))),
        "step_count": len(steps),
        "tool_step_count": len(tool_steps),
        "fault_events": faults,
        "capabilities": {
            "recorded_replay": cap(
                bool(steps),
                replay_reasons,
                ["Trace 可读且有步骤；只读重放不执行工具/模型"],
            ),
            "controlled_rerun": cap(
                bool(faults) and not controlled_reasons,
                controlled_reasons,
                ["存在白名单故障事件；模拟模型 + 本地白名单工具"],
            ),
            "counterfactual_experiment": cap(
                bool(tool_steps) and bool(faults) and not counterfactual_reasons,
                counterfactual_reasons,
                ["故障 Trace + 工具步骤 + 受控场景白名单"],
            ),
            "repair_candidates": cap(
                (bool(faults) or any(getattr(s, "error", None) for s in steps))
                and not repair_reasons,
                repair_reasons,
                ["故障或错误步骤；修复目标在本地白名单工具内"],
            ),
        },
        "source_class": classify_trace_record(record)["source_class"],
        "read_only": True,
        "note": "资格判断只读；不执行实验、不修改原始 Trace",
    }
