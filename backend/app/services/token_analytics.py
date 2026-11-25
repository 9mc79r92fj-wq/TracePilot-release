"""Token & Cost Analytics（第 53 步）：Token 用量采集、价格配置与成本计算。

数据来源（全部为本地真实数据，不调用任何模型/付费 API）：
- Trace 文件：`data/traces/`（Agent 运行）与 `data/evaluation/traces/`（评测运行）；
- Trace step：`token_usage`（Schema 内）+ `metadata.usage_meta`（第 53 步新增扩展：
  provider / model / usage_source / raw_usage / recorded_at，不改变 Trace Schema）；
- 评测运行：`evaluation_store`（内存，含 elapsed_ms）；
- 流水线运行：`data/pipelines/reports/` 快照 + 内存记录；
- 价格配置：`config/model_pricing.yaml`（可版本管理）。

核心口径（诚实性）：
1. Token 用量来源四分：`PROVIDER_REPORTED / ESTIMATED / SIMULATED / UNKNOWN`；
   Provider 未返回用量时 token 字段为 `null`（**绝不填 0**）。
2. 成本状态五分：`ACTUAL_USAGE_PRICED / ESTIMATED / SIMULATED / PRICE_UNKNOWN / UNKNOWN`；
   其中 `ACTUAL_USAGE_PRICED` 仅表示「用量由 Provider 报告 + 价格来自本地价格表」，
   **金额是按价格表计算的估算，不等于 Provider 实际账单**。
3. 缺少可信价格 → `PRICE_UNKNOWN`，金额为 `null`；不允许编造官方价格。
4. 缓存 Token 分桶（如 prompt_cache_hit_tokens）只有在配置了缓存价格时才计费，
   否则整次调用返回 `PRICE_UNKNOWN`（不把所有 Token 按同一价格计算）。
5. 只对真实 model_call 步骤计费一次；run 级 `token_usage` 仅用于交叉校验，
   不与 step 级求和重复累加（避免嵌套/重复计费）。
6. 延迟：使用 Trace step duration / 运行 elapsed_ms / 时间戳差值；无法可靠计算为
   `null` 并计入 `unknown_fields`（不把 UNKNOWN 当 0）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

__all__ = [
    "CostAnalyticsError",
    "PricingConfigError",
    "ENGINE_VERSION",
    "PRICING_PATH",
    "USAGE_SOURCES",
    "COST_STATUSES",
    "load_pricing",
    "list_all_traces",
    "analyze_trace",
    "analyze_agent_run",
    "analyze_evaluation_run",
    "analyze_pipeline_run",
    "analyze_overview",
    "model_distribution",
    "version_cost_comparison",
    "cost_attribution",
    "unknown_data_report",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PRICING_PATH = PROJECT_ROOT / "config" / "model_pricing.yaml"
TRACE_DIRS = (
    PROJECT_ROOT / "data" / "traces",
    PROJECT_ROOT / "data" / "evaluation" / "traces",
)
ENGINE_VERSION = "1.0.0"

USAGE_SOURCES = ("PROVIDER_REPORTED", "ESTIMATED", "SIMULATED", "UNKNOWN")
COST_STATUSES = (
    "ACTUAL_USAGE_PRICED",
    "ESTIMATED",
    "SIMULATED",
    "PRICE_UNKNOWN",
    "UNKNOWN",
)

# 会真实调用模型/产生 Token 的步骤类型（planning/reflection 在本项目中也是模型调用）
MODEL_CALL_STEP_TYPES = {"model_call", "planning", "reflection"}
CACHE_TOKEN_KEYS = (
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
)
TRACE_CACHE_TTL_SECONDS = 30.0
BILLING_DISCLAIMER = (
    "金额为「Token 用量 × 本地价格表」计算的成本估算，不是 Provider 实际账单/扣费金额"
)


class CostAnalyticsError(ValueError):
    """成本分析请求非法 / 数据不可读。"""


class PricingConfigError(CostAnalyticsError):
    """价格配置非法（字段缺失、类型/范围错误、文件不可读）。"""


# ---------------------------------------------------------------------------
# 价格配置
# ---------------------------------------------------------------------------

_REQUIRED_ENTRY_FIELDS = (
    "provider",
    "model_name",
    "currency",
    "input_price_per_million_tokens",
    "output_price_per_million_tokens",
    "effective_date",
    "pricing_version",
    "pricing_source",
)
_OPTIONAL_PRICE_FIELDS = (
    "cache_read_price_per_million_tokens",
    "cache_write_price_per_million_tokens",
)
_DEFAULT_META = {
    "pricing_version": "unknown",
    "effective_date": None,
    "currency": "CNY",
    "pricing_source": "NOT_CONFIGURED",
    "note": "未提供价格表 meta",
}
_PRICING_CACHE: dict[str, Any] = {"key": None, "value": None}
_PRICING_LOCK = threading.Lock()


def _validate_date(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PricingConfigError(f"{field} 必须是 YYYY-MM-DD 字符串或 null")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise PricingConfigError(f"{field} 不是合法日期（YYYY-MM-DD）: {value!r}") from exc
    return value


def _validate_price(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PricingConfigError(f"{field} 必须是非负数值或 null")
    if value < 0:
        raise PricingConfigError(f"{field} 必须 >= 0")
    return float(value)


def _validate_pricing_entry(raw: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PricingConfigError("providers 列表中的每个条目必须是映射")
    entry = dict(meta)
    entry.update(raw)
    for field in _REQUIRED_ENTRY_FIELDS:
        if field not in entry:
            raise PricingConfigError(f"价格条目缺少字段: {field}")
    if not isinstance(entry["provider"], str) or not entry["provider"].strip():
        raise PricingConfigError("provider 必须是非空字符串")
    if not isinstance(entry["model_name"], str) or not entry["model_name"].strip():
        raise PricingConfigError("model_name 必须是非空字符串（支持 * 通配）")
    if not isinstance(entry["currency"], str) or len(entry["currency"]) != 3:
        raise PricingConfigError(f"currency 必须是 3 位 ISO 货币代码: {entry['currency']!r}")
    if not isinstance(entry["pricing_version"], str) or not entry["pricing_version"]:
        raise PricingConfigError("pricing_version 必须是非空字符串")
    if not isinstance(entry["pricing_source"], str) or not entry["pricing_source"]:
        raise PricingConfigError("pricing_source 必须是非空字符串")
    entry["input_price_per_million_tokens"] = _validate_price(
        entry["input_price_per_million_tokens"], "input_price_per_million_tokens"
    )
    entry["output_price_per_million_tokens"] = _validate_price(
        entry["output_price_per_million_tokens"], "output_price_per_million_tokens"
    )
    for opt in _OPTIONAL_PRICE_FIELDS:
        entry[opt] = _validate_price(entry.get(opt), opt)
    entry["effective_date"] = _validate_date(entry["effective_date"], "effective_date")
    if not isinstance(entry.get("billing_note", ""), str):
        raise PricingConfigError("billing_note 必须是字符串")
    return entry


def load_pricing(path: str | Path | None = None) -> dict[str, Any]:
    """加载并校验价格配置；配置非法抛 PricingConfigError。"""
    pricing_path = Path(path) if path is not None else PRICING_PATH
    cache_key = str(pricing_path.resolve())
    try:
        mtime = pricing_path.stat().st_mtime
    except OSError as exc:
        raise PricingConfigError(f"价格配置不存在或不可读: {pricing_path}") from exc
    with _PRICING_LOCK:
        cached = _PRICING_CACHE.get("value")
        if (
            _PRICING_CACHE.get("key") == cache_key
            and cached is not None
            and cached.get("mtime") == mtime
        ):
            return cached["data"]

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise PricingConfigError("读取价格配置需要 PyYAML（见 requirements.txt）") from exc
    try:
        raw = yaml.safe_load(pricing_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PricingConfigError(f"价格配置不可读: {pricing_path}（{exc}）") from exc
    except Exception as exc:
        raise PricingConfigError(f"价格配置 YAML 解析失败: {pricing_path}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise PricingConfigError("价格配置顶层必须是映射（meta / providers）")
    meta = dict(_DEFAULT_META)
    if raw.get("meta") is not None:
        if not isinstance(raw["meta"], dict):
            raise PricingConfigError("meta 必须是映射")
        meta.update(raw["meta"])
    meta["effective_date"] = _validate_date(meta.get("effective_date"), "meta.effective_date")
    providers = raw.get("providers")
    if not isinstance(providers, list) or not providers:
        raise PricingConfigError("providers 必须是非空列表")
    entries = [_validate_pricing_entry(item, meta) for item in providers]

    data = {
        "pricing_version": str(meta.get("pricing_version")),
        "effective_date": meta.get("effective_date"),
        "currency": str(meta.get("currency")),
        "pricing_source": str(meta.get("pricing_source")),
        "note": str(meta.get("note") or ""),
        "entries": entries,
        "path": str(pricing_path),
        "loaded_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer": BILLING_DISCLAIMER,
        "missing_price_behavior": "PRICE_UNKNOWN（不编造价格、不按 0 计费）",
    }
    with _PRICING_LOCK:
        _PRICING_CACHE["key"] = cache_key
        _PRICING_CACHE["value"] = {"mtime": mtime, "data": data}
    return data


def _select_price_entry(
    pricing: dict[str, Any], provider: str | None, model_name: str | None
) -> dict[str, Any] | None:
    """按 provider/model 精确匹配，其次 * 通配；多条同日取最新生效日期。"""
    if not provider or not model_name:
        return None
    candidates: list[dict[str, Any]] = []
    wildcard: list[dict[str, Any]] = []
    for entry in pricing.get("entries") or []:
        if entry["provider"].lower() != provider.lower():
            continue
        if entry["model_name"].lower() == model_name.lower():
            candidates.append(entry)
        elif entry["model_name"] == "*":
            wildcard.append(entry)
    pool = candidates or wildcard
    if not pool:
        return None
    pool = sorted(pool, key=lambda item: item.get("effective_date") or "", reverse=True)
    return pool[0]


def _decimal(value: int | float | str | Decimal) -> Decimal:
    return Decimal(str(value))


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def _round_amount(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.00000001")))


def _token_count(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return max(0, value)


# ---------------------------------------------------------------------------
# Trace 索引（本地文件；带目录签名缓存）
# ---------------------------------------------------------------------------

_TRACE_CACHE: dict[str, Any] = {
    "signature": None,
    "signature_checked_at": 0.0,
    "traces": None,
    "paths": [],
    "file_keys": {},
    "records_by_file": {},
    "analyses": {},
    "by_file": {},
    "built_at": 0.0,
}
_TRACE_LOCK = threading.Lock()
# 单飞锁：多个请求同时发现缓存失效时只允许一个线程重算，其余等待后复用结果，
# 避免 CPU 密集分析被并发重复执行（本地 9.7k Trace 冷启动约 30s）。
_ANALYSIS_LOCK = threading.Lock()


def _compute_signature() -> tuple[tuple[int, int], ...]:
    """目录签名：文件名数量 + 目录 mtime（scandir 计数，避免逐文件 stat 开销）。

    说明：Trace 写入后不会原地覆盖（新 Trace = 新文件 → 目录 mtime/数量变化），
    因此该签名足以发现新增 Trace；缓存 TTL 之外强制重算。
    """
    signature: list[tuple[int, int]] = []
    for directory in TRACE_DIRS:
        if not directory.exists():
            signature.append((0, 0))
            continue
        try:
            dir_mtime = directory.stat().st_mtime_ns
            count = sum(1 for _ in os.scandir(directory))
        except OSError:
            signature.append((0, 0))
            continue
        signature.append((count, dir_mtime))
    return tuple(signature)


def _signature_cached(*, force: bool = False) -> tuple[tuple[int, int], ...]:
    now = time.time()
    if (
        not force
        and _TRACE_CACHE["signature"] is not None
        and now - _TRACE_CACHE["signature_checked_at"] < TRACE_CACHE_TTL_SECONDS
    ):
        return _TRACE_CACHE["signature"]
    signature = _compute_signature()
    with _TRACE_LOCK:
        _TRACE_CACHE["signature"] = signature
        _TRACE_CACHE["signature_checked_at"] = now
    return signature


def clear_trace_cache() -> None:
    """清空 Trace / 分析缓存（测试或需要强制刷新时使用）。"""
    with _TRACE_LOCK:
        _TRACE_CACHE["signature"] = None
        _TRACE_CACHE["signature_checked_at"] = 0.0
        _TRACE_CACHE["traces"] = None
        _TRACE_CACHE["paths"] = []
        _TRACE_CACHE["file_keys"] = {}
        _TRACE_CACHE["records_by_file"] = {}
        _TRACE_CACHE["analyses"] = {}
        _TRACE_CACHE["by_file"] = {}
        _TRACE_CACHE["built_at"] = 0.0


def list_all_traces(*, force: bool = False) -> list[Any]:
    """读取两个 Trace 目录的全部可解析 Trace（缓存 30s / 目录签名变化时刷新）。

    增量刷新：文件 mtime/size 未变化的 Trace 复用已解析对象，新增 Trace 只解析新文件；
    这样每次评测/流水线新增少量 Trace 时无需重新反序列化全部历史数据。
    """
    from backend.app.schemas.trace import TraceRecord

    now = time.time()
    signature = None if force else _signature_cached()
    with _TRACE_LOCK:
        if (
            not force
            and _TRACE_CACHE["signature"] == signature
            and _TRACE_CACHE["traces"] is not None
            and now - _TRACE_CACHE["built_at"] < TRACE_CACHE_TTL_SECONDS
        ):
            return list(_TRACE_CACHE["traces"])
        prev_records = dict(_TRACE_CACHE.get("records_by_file") or {})
    traces: list[Any] = []
    paths: list[str] = []
    file_keys: dict[str, tuple[int, int]] = {}
    records_by_file: dict[str, tuple[tuple[int, int], Any]] = {}
    seen: set[str] = set()
    for directory in TRACE_DIRS:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            path_key = str(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            stat_key = (stat.st_mtime_ns, stat.st_size)
            entry = prev_records.get(path_key)
            if entry is not None and entry[0] == stat_key:
                record = entry[1]
            else:
                try:
                    record = TraceRecord.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                except Exception:
                    continue
            records_by_file[path_key] = (stat_key, record)
            if record.trace_id in seen:
                continue
            seen.add(record.trace_id)
            traces.append(record)
            paths.append(path_key)
            file_keys[path_key] = stat_key
    with _TRACE_LOCK:
        if signature is not None:
            _TRACE_CACHE["signature"] = signature
        _TRACE_CACHE["signature_checked_at"] = now
        _TRACE_CACHE["traces"] = list(traces)
        _TRACE_CACHE["paths"] = paths
        _TRACE_CACHE["file_keys"] = file_keys
        _TRACE_CACHE["records_by_file"] = records_by_file
        _TRACE_CACHE["analyses"] = {}
        _TRACE_CACHE["by_file"] = {}
        _TRACE_CACHE["built_at"] = now
    return traces


def _get_trace(trace_id: str, *, allow_full_reload: bool = True) -> Any:
    """按 trace_id 读取 Trace。

    allow_full_reload=False 时不做全量强制重载：流水线等历史引用中常见「Trace 已被
    清理/隔离」的失效 ID，若每个缺失都触发一次全库重解析，归因查询会从秒级恶化到
    分钟级（第 55-A 步实测 >5 分钟）。
    """
    from backend.app.services.trace_store import TraceNotFoundError, load_trace

    try:
        return load_trace(trace_id)
    except (TraceNotFoundError, Exception):
        if not allow_full_reload:
            raise CostAnalyticsError(f"Trace 不存在或不可读: {trace_id}")
        # 索引缓存可能尚未包含刚写入的 Trace：按 ID 查找失败时强制刷新一次
        for trace in list_all_traces(force=True):
            if trace.trace_id == trace_id:
                return trace
    raise CostAnalyticsError(f"Trace 不存在或不可读: {trace_id}")


def _infer_provider(trace: Any, model_name: str | None) -> str:
    meta = trace.metadata or {}
    provider = meta.get("model_provider")
    if provider:
        return str(provider)
    if meta.get("simulated") or meta.get("simulated_model"):
        return "local-simulation"
    if meta.get("data_source") == "real_api":
        return "deepseek" if (model_name or "").lower().startswith("deepseek") else "UNKNOWN"
    model = (model_name or "").lower()
    if model.startswith("deepseek"):
        return "deepseek"
    if "simulated" in model or "mock" in model:
        return "local-simulation"
    return "UNKNOWN"


def _trace_model_info(trace: Any) -> tuple[str, str]:
    meta = trace.metadata or {}
    model_name = meta.get("model")
    if not model_name:
        for step in trace.steps:
            usage_meta = (step.metadata or {}).get("usage_meta") or {}
            if usage_meta.get("model"):
                model_name = usage_meta["model"]
                break
    if not model_name:
        model_name = "simulated-model" if (meta.get("simulated") or meta.get("simulated_model")) else "UNKNOWN"
    provider = _infer_provider(trace, model_name)
    return provider, str(model_name)


# ---------------------------------------------------------------------------
# 单次 Trace / 调用级用量与成本
# ---------------------------------------------------------------------------

def _step_usage_meta(step: Any) -> dict[str, Any]:
    meta = step.metadata or {}
    usage_meta = meta.get("usage_meta")
    return usage_meta if isinstance(usage_meta, dict) else {}


def _is_model_call_step(step: Any) -> bool:
    step_type = step.step_type.value if hasattr(step.step_type, "value") else str(step.step_type)
    return step_type in MODEL_CALL_STEP_TYPES or step.token_usage is not None


def _usage_source_of(step: Any, trace: Any) -> str:
    usage_meta = _step_usage_meta(step)
    source = usage_meta.get("usage_source")
    if source in USAGE_SOURCES:
        return str(source)
    meta = trace.metadata or {}
    if meta.get("simulated") or meta.get("simulated_model"):
        return "SIMULATED"
    if step.token_usage is not None and meta.get("data_source") == "real_api":
        return "PROVIDER_REPORTED"
    if step.token_usage is not None:
        return "UNKNOWN"
    return "UNKNOWN"


def _estimate_tokens(step: Any) -> dict[str, int]:
    """确定性估算（仅在显式开启时使用）：字符数 / 4，四舍五入，至少 1。"""
    try:
        input_text = json.dumps(step.input, ensure_ascii=False, default=str)
    except Exception:
        input_text = str(step.input)
    try:
        output_text = json.dumps(step.output, ensure_ascii=False, default=str)
    except Exception:
        output_text = str(step.output)
    prompt = max(1, round(len(input_text) / 4))
    completion = max(1, round(len(output_text) / 4))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "method": "chars_div_4",
        "note": "本地字符数估算（非 Provider 报告），仅用于显式开启估算时的趋势分析",
    }


def _price_call(
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    usage_source: str,
    entry: dict[str, Any] | None,
    raw_usage: dict[str, Any] | None,
) -> dict[str, Any]:
    """单次模型调用成本：金额为按本地价格表计算的估算（非实际账单）。"""
    base = {
        "amount": None,
        "currency": (entry or {}).get("currency"),
        "status": "UNKNOWN",
        "pricing_version": (entry or {}).get("pricing_version"),
        "pricing_source": (entry or {}).get("pricing_source"),
        "effective_date": (entry or {}).get("effective_date"),
        "note": None,
    }
    if usage_source == "SIMULATED":
        base.update(
            {
                "amount": 0.0,
                "currency": (entry or {}).get("currency") or "CNY",
                "status": "SIMULATED",
                "note": "模拟执行器不调用付费 API；0 表示未产生调用，不是真实计费",
            }
        )
        return base
    if prompt_tokens is None or completion_tokens is None:
        base["status"] = "UNKNOWN"
        base["note"] = "Token 用量未知（Provider 未报告且未开启估算），不按 0 计费"
        return base
    if entry is None:
        base["status"] = "PRICE_UNKNOWN"
        base["note"] = "缺少该 provider/model 的价格配置，成本未知（不编造价格）"
        return base
    input_price = entry.get("input_price_per_million_tokens")
    output_price = entry.get("output_price_per_million_tokens")
    if input_price is None or output_price is None:
        base["status"] = "PRICE_UNKNOWN"
        base["note"] = "价格配置为 null（无可信价格），成本未知"
        return base

    raw_usage = raw_usage or {}
    cache_fields_present = any(key in raw_usage for key in CACHE_TOKEN_KEYS)
    amount = Decimal(0)
    amount += _decimal(prompt_tokens) / Decimal(1_000_000) * _decimal(input_price)
    amount += _decimal(completion_tokens) / Decimal(1_000_000) * _decimal(output_price)

    if cache_fields_present:
        cache_read_price = entry.get("cache_read_price_per_million_tokens")
        cache_write_price = entry.get("cache_write_price_per_million_tokens")
        hit_tokens = _token_count(
            raw_usage.get("prompt_cache_hit_tokens")
            if raw_usage.get("prompt_cache_hit_tokens") is not None
            else raw_usage.get("cache_read_input_tokens")
        )
        if hit_tokens is not None and cache_read_price is None:
            base["status"] = "PRICE_UNKNOWN"
            base["note"] = (
                "Provider 返回缓存命中 Token 分桶，但未配置缓存读取价格；"
                "不得把缓存 Token 与普通输入 Token 按同一价格计算"
            )
            return base
        if cache_read_price is not None and hit_tokens is not None:
            # 缓存命中部分按缓存价，其余输入按普通输入价（prompt_tokens 含命中部分）
            normal_input = max(0, prompt_tokens - hit_tokens)
            amount -= _decimal(hit_tokens) / Decimal(1_000_000) * _decimal(input_price)
            amount += _decimal(normal_input) / Decimal(1_000_000) * _decimal(input_price)
            amount += _decimal(hit_tokens) / Decimal(1_000_000) * _decimal(cache_read_price)
        write_tokens = _token_count(raw_usage.get("cache_write_input_tokens"))
        if write_tokens is not None:
            if cache_write_price is None:
                base["status"] = "PRICE_UNKNOWN"
                base["note"] = "Provider 返回缓存写入 Token 分桶，但未配置缓存写入价格"
                return base
            amount += _decimal(write_tokens) / Decimal(1_000_000) * _decimal(cache_write_price)

    base.update(
        {
            "amount": _round_amount(amount),
            "status": "ESTIMATED" if usage_source == "ESTIMATED" else "ACTUAL_USAGE_PRICED",
            "note": (
                "按本地价格表计算的估算（Token 用量 Provider 报告 + 价格本地配置），非实际账单"
                if usage_source == "PROVIDER_REPORTED"
                else "按本地价格表计算的估算（Token 用量为本地估算），非实际账单"
            ),
        }
    )
    return base


def _aggregate_cost(calls: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = [c["cost"]["status"] for c in calls]
    amounts = [c["cost"]["amount"] for c in calls if c["cost"]["amount"] is not None]
    currencies = sorted({c["cost"]["currency"] for c in calls if c["cost"]["currency"]})
    if "PRICE_UNKNOWN" in statuses:
        status = "PRICE_UNKNOWN"
    elif "UNKNOWN" in statuses:
        status = "UNKNOWN"
    elif "ESTIMATED" in statuses:
        status = "ESTIMATED"
    elif statuses and all(s == "SIMULATED" for s in statuses):
        status = "SIMULATED"
    elif statuses:
        status = "ACTUAL_USAGE_PRICED"
    else:
        status = "UNKNOWN"
    priced = sum(1 for s in statuses if s == "ACTUAL_USAGE_PRICED")
    estimated = sum(1 for s in statuses if s == "ESTIMATED")
    simulated = sum(1 for s in statuses if s == "SIMULATED")
    price_unknown = sum(1 for s in statuses if s == "PRICE_UNKNOWN")
    unknown = sum(1 for s in statuses if s == "UNKNOWN")
    return {
        "amount": _round_amount(sum(Decimal(str(a)) for a in amounts)) if amounts else None,
        "currency": currencies[0] if len(currencies) == 1 else ("MIXED" if currencies else None),
        "currencies": currencies,
        "status": status,
        "complete": unknown == 0 and price_unknown == 0,
        "partial": unknown + price_unknown > 0,
        "priced_calls": priced,
        "estimated_calls": estimated,
        "simulated_calls": simulated,
        "price_unknown_calls": price_unknown,
        "unknown_calls": unknown,
        "pricing_versions": sorted(
            {c["cost"]["pricing_version"] for c in calls if c["cost"]["pricing_version"]}
        ),
        "pricing_sources": sorted(
            {c["cost"]["pricing_source"] for c in calls if c["cost"]["pricing_source"]}
        ),
        "disclaimer": BILLING_DISCLAIMER,
    }


def _trace_latency(trace: Any, calls: list[dict[str, Any]]) -> dict[str, Any]:
    model_values = [
        c["duration_ms"]
        for c in calls
        if c.get("duration_ms") is not None
    ]
    tool_values = [
        step.duration_ms
        for step in trace.steps
        if step.step_type.value == "tool_call" and step.duration_ms is not None
    ]
    total: int | None = None
    unknown_fields: list[str] = []
    if trace.started_at and trace.ended_at:
        total = int((trace.ended_at - trace.started_at).total_seconds() * 1000)
    else:
        unknown_fields.append("total_run_latency_ms")
    model_latency = int(sum(model_values)) if model_values else None
    if model_latency is None:
        unknown_fields.append("model_latency_ms")
    tool_latency = int(sum(tool_values)) if tool_values else None
    if tool_latency is None:
        unknown_fields.append("tool_latency_ms")
    return {
        "model_latency_ms": model_latency,
        "tool_latency_ms": tool_latency,
        "total_run_latency_ms": total,
        "unknown_fields": unknown_fields,
    }


def _analyze_trace_record(
    trace: Any,
    *,
    pricing: dict[str, Any],
    estimate_missing: bool,
) -> dict[str, Any]:
    provider, model_name = _trace_model_info(trace)
    entry = _select_price_entry(pricing, provider, model_name)
    calls: list[dict[str, Any]] = []
    for step in trace.steps:
        if not _is_model_call_step(step):
            continue
        usage_source = _usage_source_of(step, trace)
        usage_meta = _step_usage_meta(step)
        raw_usage = usage_meta.get("raw_usage")
        if not isinstance(raw_usage, dict):
            raw_usage = None
        step_provider = str(usage_meta.get("provider") or provider)
        step_model = str(usage_meta.get("model") or model_name)
        step_entry = _select_price_entry(pricing, step_provider, step_model)
        if step_entry is None and (
            step_provider.lower(), step_model.lower()
        ) == (provider.lower(), model_name.lower()):
            # 仅当 step 与 Trace 级 provider/model 一致时，才允许回退到 Trace 级价格；
            # 显式声明的 provider/model 无价格 → PRICE_UNKNOWN，绝不套用其他模型价格。
            step_entry = entry
        prompt = completion = total = None
        usage_confirmed = True
        if step.token_usage is not None:
            prompt = int(step.token_usage.input_tokens)
            completion = int(step.token_usage.output_tokens)
            total = int(step.token_usage.total_tokens)
        else:
            usage_confirmed = False
            if estimate_missing and usage_source != "SIMULATED":
                estimate = _estimate_tokens(step)
                prompt = estimate["prompt_tokens"]
                completion = estimate["completion_tokens"]
                total = estimate["total_tokens"]
                usage_source = "ESTIMATED"
                usage_meta = {
                    **usage_meta,
                    "usage_source": "ESTIMATED",
                    "estimation_method": estimate["method"],
                    "estimation_note": estimate["note"],
                }
        cost = _price_call(
            prompt_tokens=prompt,
            completion_tokens=completion,
            usage_source=usage_source,
            entry=step_entry,
            raw_usage=raw_usage,
        )
        calls.append(
            {
                "model_call_id": step.step_id,
                "trace_id": trace.trace_id,
                "run_id": trace.run_id,
                "step_type": step.step_type.value,
                "step_parent_ids": list(step.parent_step_ids or []),
                "provider": step_provider,
                "model_name": step_model,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                "usage_source": usage_source,
                "usage_confirmed": usage_confirmed,
                "usage_meta_source": usage_meta.get("usage_source") or "INFERRED",
                "raw_usage": raw_usage,
                "recorded_at": step.timestamp.isoformat() if step.timestamp else None,
                "duration_ms": step.duration_ms,
                "cost": cost,
            }
        )

    prompt_values = [c["prompt_tokens"] for c in calls if c["prompt_tokens"] is not None]
    completion_values = [c["completion_tokens"] for c in calls if c["completion_tokens"] is not None]
    total_values = [c["total_tokens"] for c in calls if c["total_tokens"] is not None]
    known_prompt = sum(prompt_values) if prompt_values else None
    known_completion = sum(completion_values) if completion_values else None
    known_total = sum(total_values) if total_values else None
    unknown_calls = [c for c in calls if c["total_tokens"] is None]
    meta = trace.metadata or {}
    from backend.app.services.data_governance import classify_metadata

    source_class = classify_metadata(meta)["source_class"]
    return {
        "trace_id": trace.trace_id,
        "run_id": trace.run_id,
        "task_id": trace.task_id,
        "status": trace.status.value,
        "data_source": meta.get("data_source"),
        "source_class": source_class,
        "simulated": bool(meta.get("simulated") or meta.get("simulated_model")),
        "agent_version": meta.get("agent_version"),
        "agent_version_note": meta.get("agent_version_note"),
        "provider": provider,
        "model_name": model_name,
        "started_at": trace.started_at.isoformat() if trace.started_at else None,
        "ended_at": trace.ended_at.isoformat() if trace.ended_at else None,
        "tokens": {
            "prompt_tokens": known_prompt,
            "completion_tokens": known_completion,
            "total_tokens": known_total,
            "known_model_calls": len(calls) - len(unknown_calls),
            "unknown_model_calls": len(unknown_calls),
            "complete": len(unknown_calls) == 0 and bool(calls),
            "usage_sources": {
                source: sum(1 for c in calls if c["usage_source"] == source)
                for source in USAGE_SOURCES
            },
        },
        "cost": _aggregate_cost(calls),
        "latency": _trace_latency(trace, calls),
        "model_calls": calls,
        "billing_disclaimer": BILLING_DISCLAIMER,
        "paid_api_called": False,
        "data_sources": {
            "trace_dirs": [_display_path(d) for d in TRACE_DIRS],
            "usage_sources": list(USAGE_SOURCES),
        },
        "limitations": [
            "成本为本地价格表估算，不是 Provider 实际账单",
            "缺失 Token 用量或价格时金额为 null（UNKNOWN / PRICE_UNKNOWN），不按 0 计费",
            "agent_version 缺失时成本对比按模型/执行模式分组，并明确标注",
        ],
    }


def analyze_trace(
    trace_id: str,
    *,
    pricing: dict[str, Any] | None = None,
    estimate_missing: bool = False,
) -> dict[str, Any]:
    """单条 Trace 的 Token / 成本 / 延迟分析。"""
    price_data = pricing or load_pricing()
    trace = _get_trace(trace_id)
    return _analyze_trace_record(trace, pricing=price_data, estimate_missing=estimate_missing)


def _pricing_fingerprint(pricing: dict[str, Any]) -> str:
    payload = json.dumps(pricing, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _cached_analyses(
    pricing: dict[str, Any], *, force: bool = False
) -> list[dict[str, Any]]:
    """全部 Trace 的成本分析缓存（按文件 + 价格指纹增量复用，单飞重算）。"""
    fingerprint = _pricing_fingerprint(pricing)

    def cache_hit(now: float) -> tuple[bool, list[dict[str, Any]]]:
        signature = None if force else _signature_cached()
        with _TRACE_LOCK:
            cached = _TRACE_CACHE.get("analyses")
            hit = bool(
                not force
                and _TRACE_CACHE["signature"] == signature
                and cached
                and now - _TRACE_CACHE["built_at"] < TRACE_CACHE_TTL_SECONDS
            )
            return hit, list((cached or {}).get("all") or [])

    hit, analyses = cache_hit(time.time())
    if hit:
        return analyses
    with _ANALYSIS_LOCK:
        hit, analyses = cache_hit(time.time())
        if hit:
            return analyses
        with _TRACE_LOCK:
            # 必须在 list_all_traces 重建缓存前取旧分析，才能按文件增量复用
            prev = dict(_TRACE_CACHE.get("by_file") or {})
        traces = list_all_traces(force=force)
        with _TRACE_LOCK:
            paths = list(_TRACE_CACHE.get("paths") or [])
            file_keys = dict(_TRACE_CACHE.get("file_keys") or {})
        by_file: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        analyses = []
        for index, trace in enumerate(traces):
            path_key = paths[index] if index < len(paths) else None
            stat_key = file_keys.get(path_key) if path_key else None
            key = (stat_key, fingerprint)
            entry = prev.get(path_key) if path_key else None
            if entry is not None and entry[0] == key:
                analysis = entry[1]
            else:
                analysis = _analyze_trace_record(
                    trace, pricing=pricing, estimate_missing=False
                )
            if path_key:
                by_file[path_key] = (key, analysis)
            analyses.append(analysis)
        with _TRACE_LOCK:
            _TRACE_CACHE["analyses"] = {"all": analyses}
            _TRACE_CACHE["by_file"] = by_file
        return list(analyses)


# ---------------------------------------------------------------------------
# 运行级 / 评测级 / 流水线级
# ---------------------------------------------------------------------------

def _aggregate_from_analyses(
    analyses: list[dict[str, Any]],
) -> dict[str, Any]:
    calls = [call for analysis in analyses for call in analysis["model_calls"]]
    tokens = {
        "prompt_tokens": sum(
            call["prompt_tokens"] for call in calls if call["prompt_tokens"] is not None
        ),
        "completion_tokens": sum(
            call["completion_tokens"] for call in calls if call["completion_tokens"] is not None
        ),
        "total_tokens": sum(
            call["total_tokens"] for call in calls if call["total_tokens"] is not None
        ),
        "known_model_calls": sum(1 for call in calls if call["total_tokens"] is not None),
        "unknown_model_calls": sum(1 for call in calls if call["total_tokens"] is None),
        "usage_sources": {
            source: sum(1 for call in calls if call["usage_source"] == source)
            for source in USAGE_SOURCES
        },
    }
    tokens["complete"] = tokens["unknown_model_calls"] == 0
    model_latencies = [
        a["latency"]["model_latency_ms"]
        for a in analyses
        if a["latency"]["model_latency_ms"] is not None
    ]
    tool_latencies = [
        a["latency"]["tool_latency_ms"]
        for a in analyses
        if a["latency"]["tool_latency_ms"] is not None
    ]
    total_latencies = [
        a["latency"]["total_run_latency_ms"]
        for a in analyses
        if a["latency"]["total_run_latency_ms"] is not None
    ]
    latency = {
        "model_latency_ms": int(sum(model_latencies)) if model_latencies else None,
        "tool_latency_ms": int(sum(tool_latencies)) if tool_latencies else None,
        "total_run_latency_ms": int(sum(total_latencies)) if total_latencies else None,
        "model_latency_avg_ms": round(sum(model_latencies) / len(model_latencies), 2)
        if model_latencies
        else None,
        "unknown_fields": [],
    }
    if latency["model_latency_ms"] is None:
        latency["unknown_fields"].append("model_latency_ms")
    if latency["tool_latency_ms"] is None:
        latency["unknown_fields"].append("tool_latency_ms")
    if latency["total_run_latency_ms"] is None:
        latency["unknown_fields"].append("total_run_latency_ms")
    return {
        "trace_count": len(analyses),
        "model_call_count": len(calls),
        "tokens": tokens,
        "cost": _aggregate_cost(calls),
        "latency": latency,
    }


def analyze_agent_run(
    run_id: str,
    *,
    pricing: dict[str, Any] | None = None,
    estimate_missing: bool = False,
) -> dict[str, Any]:
    """单次 Agent Run（`run-*`）成本：按 Trace 关联聚合，不重复累加 run 级汇总。"""
    price_data = pricing or load_pricing()
    traces = [t for t in list_all_traces() if (t.metadata or {}).get("run_id") == run_id]
    if not traces:
        traces = [t for t in list_all_traces() if t.run_id == run_id]
    if not traces:
        raise CostAnalyticsError(f"未找到 Agent Run 关联 Trace: {run_id}")
    analyses = [
        _analyze_trace_record(t, pricing=price_data, estimate_missing=estimate_missing)
        for t in traces
    ]
    aggregate = _aggregate_from_analyses(analyses)
    run_meta = traces[0].metadata or {}
    return {
        "run_id": run_id,
        "agent_version": run_meta.get("agent_version"),
        "agent_version_note": run_meta.get("agent_version_note"),
        "execution_mode": run_meta.get("data_source"),
        "model_name": analyses[0]["model_name"] if analyses else None,
        "provider": analyses[0]["provider"] if analyses else None,
        "trace_ids": [t.trace_id for t in traces],
        "statuses": sorted({a["status"] for a in analyses}),
        "aggregate": aggregate,
        "traces": analyses,
        "billing_disclaimer": BILLING_DISCLAIMER,
        "paid_api_called": False,
        "limitations": [
            "Run 级成本由 Trace step 汇总而来（step 级为唯一计费来源），未额外累加 run 汇总字段",
            "金额为本地价格表估算，非 Provider 实际账单",
        ],
    }


def analyze_evaluation_run(
    evaluation_run_id: str,
    *,
    pricing: dict[str, Any] | None = None,
    estimate_missing: bool = False,
) -> dict[str, Any]:
    """单次 Evaluation Run 成本（确定性评测的模拟模型为 SIMULATED 0 成本）。"""
    from backend.app.services.evaluation_store import (
        EvaluationNotFoundError,
        get_evaluation_store,
    )

    price_data = pricing or load_pricing()
    try:
        record = get_evaluation_store().get(evaluation_run_id)
    except EvaluationNotFoundError as exc:
        raise CostAnalyticsError(
            f"评测运行不存在（运行记录仅内存保留，可能已过期）: {evaluation_run_id}"
        ) from exc
    report = record.get("report") or {}
    trace_ids = list(report.get("trace_ids") or [])
    analyses = []
    for trace_id in trace_ids:
        try:
            # 历史流水线引用的 Trace 可能已被清理/隔离：缺失即跳过并计入
            # missing_trace_ids，绝不触发全库强制重载（否则归因会退化为分钟级）。
            trace = _get_trace(trace_id, allow_full_reload=False)
        except CostAnalyticsError:
            continue
        analyses.append(
            _analyze_trace_record(
                trace, pricing=price_data, estimate_missing=estimate_missing
            )
        )
    aggregate = _aggregate_from_analyses(analyses)
    elapsed_ms = record.get("elapsed_ms")
    evaluation_latency = int(elapsed_ms) if isinstance(elapsed_ms, int) else None
    return {
        "evaluation_run_id": evaluation_run_id,
        "dataset_id": record.get("dataset_id"),
        "dataset_version": report.get("dataset_version"),
        "judge_mode": record.get("judge_mode"),
        "status": record.get("status"),
        "total_cases": report.get("total_cases"),
        "executed_cases": report.get("executed_cases"),
        "bad_case_count": len(report.get("bad_cases") or []),
        "trace_ids": trace_ids,
        "missing_trace_ids": [tid for tid in trace_ids if tid not in {a["trace_id"] for a in analyses}],
        "aggregate": aggregate,
        "latency": {
            **aggregate["latency"],
            "evaluation_latency_ms": evaluation_latency,
            "unknown_fields": aggregate["latency"]["unknown_fields"]
            + ([] if evaluation_latency is not None else ["evaluation_latency_ms"]),
        },
        "traces": analyses,
        "billing_disclaimer": BILLING_DISCLAIMER,
        "paid_api_called": False,
        "limitations": [
            "评测使用受控确定性模拟模型，Token 用量为 SIMULATED（0 真实消耗）",
            "evaluation_latency_ms 来自运行记录 elapsed_ms；缺失时为 null 而非 0",
        ],
    }


def analyze_pipeline_run(
    pipeline_run_id: str,
    *,
    pricing: dict[str, Any] | None = None,
    estimate_missing: bool = False,
) -> dict[str, Any]:
    """单次 Pipeline Run 成本：按流水线记录的真实引用聚合（不新增重复计费）。"""
    from backend.app.services.evaluation_pipeline import get_pipeline

    price_data = pricing or load_pricing()
    try:
        record = get_pipeline(pipeline_run_id)
    except Exception as exc:  # noqa: BLE001 - 流水线不存在
        raise CostAnalyticsError(f"流水线不存在: {pipeline_run_id}") from exc

    refs = record.get("refs") or {}
    trace_ids = list(refs.get("trace_ids") or [])
    analyses = []
    for trace_id in trace_ids:
        try:
            # 历史流水线引用的 Trace 可能已被清理/隔离：缺失即跳过并计入
            # missing_trace_ids，绝不触发全库强制重载（否则归因会退化为分钟级）。
            trace = _get_trace(trace_id, allow_full_reload=False)
        except CostAnalyticsError:
            continue
        analyses.append(
            _analyze_trace_record(
                trace, pricing=price_data, estimate_missing=estimate_missing
            )
        )
    aggregate = _aggregate_from_analyses(analyses)
    stage_latency: dict[str, Any] = {"evaluation_latency_ms": None}
    for stage in record.get("stages") or []:
        if stage.get("stage_id") == "deterministic_evaluation":
            duration = stage.get("duration_ms")
            stage_latency["evaluation_latency_ms"] = int(duration) if isinstance(duration, int) else None
            break
    return {
        "pipeline_run_id": pipeline_run_id,
        "status": record.get("status"),
        "dataset_id": (record.get("config") or {}).get("dataset_id"),
        "execution_mode": (record.get("config") or {}).get("execution_mode"),
        "evaluation_run_id": refs.get("evaluation_run_id"),
        "agent_run_ids": refs.get("agent_run_ids") or [],
        "trace_ids": trace_ids,
        "missing_trace_ids": [tid for tid in trace_ids if tid not in {a["trace_id"] for a in analyses}],
        "aggregate": aggregate,
        "latency": {
            **aggregate["latency"],
            "evaluation_latency_ms": stage_latency["evaluation_latency_ms"],
            "pipeline_latency_ms": record.get("duration_ms"),
            "unknown_fields": aggregate["latency"]["unknown_fields"]
            + ([] if stage_latency["evaluation_latency_ms"] is not None else ["evaluation_latency_ms"]),
        },
        "stage_allocation_available": False,
        "stage_allocation_note": (
            "流水线阶段未记录独立 Token 计数；只有 step 级关联（trace→step）可靠，"
            "因此不把总成本分摊到各阶段，避免伪造分摊"
        ),
        "traces": analyses,
        "billing_disclaimer": BILLING_DISCLAIMER,
        "paid_api_called": False,
        "limitations": [
            "Pipeline 成本由 evaluation_run 的 Trace 汇总，agent_run_ids 仅用于关联，不重复累加",
            "阶段级成本分摊不可靠 → 不做伪造分摊",
        ],
    }


# ---------------------------------------------------------------------------
# 总览 / 分布 / 版本对比 / 归因 / UNKNOWN 报告
# ---------------------------------------------------------------------------

def _sum_tokens(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    prompts = [
        a["tokens"]["prompt_tokens"] for a in analyses if a["tokens"]["prompt_tokens"] is not None
    ]
    completions = [
        a["tokens"]["completion_tokens"]
        for a in analyses
        if a["tokens"]["completion_tokens"] is not None
    ]
    totals = [
        a["tokens"]["total_tokens"] for a in analyses if a["tokens"]["total_tokens"] is not None
    ]
    return {
        "prompt_tokens": sum(prompts) if prompts else None,
        "completion_tokens": sum(completions) if completions else None,
        "total_tokens": sum(totals) if totals else None,
        "traces_with_known_tokens": len(totals),
    }


def _model_distribution_rows(analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for analysis in analyses:
        for call in analysis["model_calls"]:
            key = (call["provider"], call["model_name"])
            groups.setdefault(key, []).append(call)
    rows = []
    for (provider, model_name), calls in groups.items():
        rows.append(
            {
                "provider": provider,
                "model_name": model_name,
                "model_calls": len(calls),
                "tokens": {
                    "prompt_tokens": sum(
                        c["prompt_tokens"] for c in calls if c["prompt_tokens"] is not None
                    ),
                    "completion_tokens": sum(
                        c["completion_tokens"] for c in calls if c["completion_tokens"] is not None
                    ),
                    "total_tokens": sum(
                        c["total_tokens"] for c in calls if c["total_tokens"] is not None
                    ),
                },
                "cost": _aggregate_cost(calls),
                "usage_sources": {
                    source: sum(1 for c in calls if c["usage_source"] == source)
                    for source in USAGE_SOURCES
                },
            }
        )
    rows.sort(key=lambda row: -(row["tokens"]["total_tokens"] or 0))
    return rows


def model_distribution(*, pricing: dict[str, Any] | None = None) -> dict[str, Any]:
    price_data = pricing or load_pricing()
    analyses = _cached_analyses(price_data)
    rows = _model_distribution_rows(analyses)
    return {
        "models": rows,
        "model_count": len(rows),
        "pricing": {
            "pricing_version": price_data["pricing_version"],
            "effective_date": price_data["effective_date"],
            "pricing_source": price_data["pricing_source"],
        },
        "disclaimer": BILLING_DISCLAIMER,
    }


def version_cost_comparison(
    *,
    pricing: dict[str, Any] | None = None,
    analyses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """成本版本对比：优先 agent_version；缺失时按模型/执行模式分组并明确标注。"""
    price_data = pricing or load_pricing()
    if analyses is None:
        analyses = _cached_analyses(price_data)
    versions = {a["agent_version"] for a in analyses}
    field_available = any(v is not None for v in versions)

    def _group_rows(key_fn) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for analysis in analyses:
            groups.setdefault(str(key_fn(analysis)), []).append(analysis)
        rows = []
        for key, items in groups.items():
            aggregate = _aggregate_from_analyses(items)
            tokens = aggregate["tokens"]
            rows.append(
                {
                    "group": key,
                    "trace_count": len(items),
                    "total_tokens": tokens["total_tokens"],
                    "cost": aggregate["cost"],
                    "latency_avg_model_ms": aggregate["latency"]["model_latency_avg_ms"],
                }
            )
        rows.sort(key=lambda row: -(row["total_tokens"] or 0))
        return rows

    return {
        "agent_version_field_available": field_available,
        "agent_version_note": (
            "受控执行器未记录独立 agent_version；以下按模型/执行模式分组作为替代口径，"
            "不能当作真实 Agent 版本对比"
            if not field_available
            else "按 Trace metadata.agent_version 分组"
        ),
        "by_agent_version": _group_rows(
            lambda a: a["agent_version"] or "UNKNOWN"
        ),
        "by_model": _group_rows(lambda a: f"{a['provider']}/{a['model_name']}"),
        "by_execution_mode": _group_rows(lambda a: a["data_source"] or "UNKNOWN"),
        "generated_from_traces": len(analyses),
        "disclaimer": BILLING_DISCLAIMER,
    }


def analyze_overview(
    *,
    recent: int = 20,
    pricing: dict[str, Any] | None = None,
    estimate_missing: bool = False,
    force_refresh: bool = False,
    source_class: str | None = None,
) -> dict[str, Any]:
    """成本总览：核心指标、模型分布、延迟、近期趋势、UNKNOWN 统计、价格版本。

    source_class 只收窄统计范围（REAL_API / MOCK / SIMULATED / REPLAY / UNKNOWN），
    不改变任何金额计算口径；全库分类计数始终随响应返回，避免把筛选结果误当全库。
    """
    from backend.app.services.data_governance import SOURCE_CLASSES

    wanted = (source_class or "").strip().upper() or None
    if wanted is not None and wanted not in SOURCE_CLASSES:
        raise CostAnalyticsError(
            "source_class 必须是 " + " / ".join(SOURCE_CLASSES) + " 之一"
        )
    price_data = pricing or load_pricing()
    analyses = _cached_analyses(price_data, force=force_refresh)
    if estimate_missing:
        analyses = [
            _analyze_trace_record(t, pricing=price_data, estimate_missing=True)
            for t in list_all_traces()
        ]
    available_by_source = {name: 0 for name in SOURCE_CLASSES}
    for analysis in analyses:
        cls = analysis.get("source_class", "UNKNOWN")
        available_by_source[cls] = available_by_source.get(cls, 0) + 1
    total_trace_count = len(analyses)
    if wanted is not None:
        analyses = [a for a in analyses if a.get("source_class") == wanted]
    calls = [call for analysis in analyses for call in analysis["model_calls"]]
    aggregate = _aggregate_from_analyses(analyses)
    totals = _sum_tokens(analyses)
    status_counts: dict[str, int] = {}
    usage_counts: dict[str, int] = {}
    failed = sim_success = priced_success = 0
    failed_cost_amounts: list[float] = []
    successful_cost_amounts: list[float] = []
    known_cost_statuses = {"ACTUAL_USAGE_PRICED", "ESTIMATED", "SIMULATED"}
    for analysis in analyses:
        status_counts[analysis["cost"]["status"]] = (
            status_counts.get(analysis["cost"]["status"], 0) + 1
        )
        for call in analysis["model_calls"]:
            usage_counts[call["usage_source"]] = usage_counts.get(call["usage_source"], 0) + 1
        amount = analysis["cost"]["amount"]
        if analysis["status"] == "failed":
            failed += 1
            if amount is not None:
                failed_cost_amounts.append(amount)
        if analysis["status"] == "succeeded":
            sim_success += 1
            if analysis["cost"]["status"] in known_cost_statuses and amount is not None:
                priced_success += 1
                successful_cost_amounts.append(amount)

    recent_items = sorted(
        analyses,
        key=lambda a: a.get("started_at") or "",
        reverse=True,
    )[: max(1, recent)]
    recent_rows = [
        {
            "trace_id": a["trace_id"],
            "run_id": a["run_id"],
            "started_at": a["started_at"],
            "provider": a["provider"],
            "model_name": a["model_name"],
            "total_tokens": a["tokens"]["total_tokens"],
            "cost_amount": a["cost"]["amount"],
            "cost_status": a["cost"]["status"],
            "usage_source_summary": a["tokens"]["usage_sources"],
            "model_latency_ms": a["latency"]["model_latency_ms"],
            "trace_status": a["status"],
        }
        for a in recent_items
    ]

    unknown_calls = [c for c in calls if c["total_tokens"] is None]
    price_unknown_calls = [c for c in calls if c["cost"]["status"] == "PRICE_UNKNOWN"]
    unknown_examples = [
        {
            "trace_id": c["trace_id"],
            "model_call_id": c["model_call_id"],
            "provider": c["provider"],
            "model_name": c["model_name"],
            "usage_source": c["usage_source"],
            "cost_status": c["cost"]["status"],
            "reason": c["cost"]["note"],
        }
        for c in (unknown_calls + price_unknown_calls)[:50]
    ]

    latency_values = [
        a["latency"]["model_latency_ms"] for a in analyses if a["latency"]["model_latency_ms"] is not None
    ]
    tool_values = [
        a["latency"]["tool_latency_ms"] for a in analyses if a["latency"]["tool_latency_ms"] is not None
    ]
    total_values = [
        a["latency"]["total_run_latency_ms"]
        for a in analyses
        if a["latency"]["total_run_latency_ms"] is not None
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "paid_api_called": False,
        "source_scope": {
            "selected": wanted,
            "total_trace_count": total_trace_count,
            "filtered_trace_count": len(analyses),
            "available_by_source": available_by_source,
            "source_classes": list(SOURCE_CLASSES),
            "note": (
                "全库口径 = 主 Trace 目录（Agent 运行）+ 评测 Trace 目录；"
                "来源筛选只收窄统计范围，不改变金额计算口径"
            ),
        },
        "data_sources": {
            "trace_dirs": [_display_path(d) for d in TRACE_DIRS],
            "usage_sources": list(USAGE_SOURCES),
            "pricing_path": str(PRICING_PATH.relative_to(PROJECT_ROOT)),
            "trace_cache_ttl_seconds": TRACE_CACHE_TTL_SECONDS,
        },
        "trace_count": len(analyses),
        "model_call_count": len(calls),
        "tokens": {
            **totals,
            "usage_sources": usage_counts,
            "unknown_model_calls": len(unknown_calls),
        },
        "cost": {
            **aggregate["cost"],
            "status_counts": status_counts,
            "traces_with_known_cost": sum(
                1 for a in analyses if a["cost"]["status"] in known_cost_statuses
            ),
        },
        "latency": {
            "model_latency_avg_ms": round(sum(latency_values) / len(latency_values), 2)
            if latency_values
            else None,
            "tool_latency_avg_ms": round(sum(tool_values) / len(tool_values), 2)
            if tool_values
            else None,
            "total_run_latency_avg_ms": round(sum(total_values) / len(total_values), 2)
            if total_values
            else None,
            "traces_with_known_latency": len(total_values),
            "unknown_fields": [
                field
                for field, values in (
                    ("model_latency_ms", latency_values),
                    ("tool_latency_ms", tool_values),
                    ("total_run_latency_ms", total_values),
                )
                if not values
            ],
        },
        "task_outcomes": {
            "succeeded_traces": sim_success,
            "failed_traces": failed,
            "priced_successful_traces": priced_success,
            "avg_cost_per_task": round(sum(successful_cost_amounts) / priced_success, 8)
            if priced_success
            else None,
            "avg_cost_per_successful_task": round(sum(successful_cost_amounts) / priced_success, 8)
            if priced_success
            else None,
            "failed_task_cost_total": _round_amount(
                sum(Decimal(str(a)) for a in failed_cost_amounts)
            )
            if failed_cost_amounts
            else None,
            "note": "平均成本只对「成本状态已知」的任务计算；失败任务成本单独统计",
        },
        "models": _model_distribution_rows(analyses),
        "recent": recent_rows,
        "unknown": {
            "unknown_usage_calls": len(unknown_calls),
            "price_unknown_calls": len(price_unknown_calls),
            "examples": unknown_examples,
            "note": "UNKNOWN/PRICE_UNKNOWN 不计为 0，也不计入平均成本",
        },
        "pricing": {
            "pricing_version": price_data["pricing_version"],
            "effective_date": price_data["effective_date"],
            "pricing_source": price_data["pricing_source"],
            "currency": price_data["currency"],
            "path": price_data["path"],
            "disclaimer": BILLING_DISCLAIMER,
            "entries": price_data["entries"],
        },
        "versions": version_cost_comparison(pricing=price_data, analyses=analyses),
        "limitations": [
            "成本为本地价格表估算，不是 Provider 实际账单；禁止称为已扣费金额",
            "Provider 未返回 Token 用量或缺少可信价格时金额为 null（UNKNOWN / PRICE_UNKNOWN）",
            "agent_version 缺失：版本成本对比按模型/执行模式分组并明确标注",
            "缓存 Token 只有在配置了缓存价格时才计费，否则 PRICE_UNKNOWN",
        ],
    }


def cost_attribution(
    *,
    level: str = "model_call",
    limit: int = 50,
    usage_source: str | None = None,
    source_class: str | None = None,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cost Attribution Explorer：按调用 / Trace / Agent Run / 评测 / 流水线归因。

    usage_source 只过滤「用量来源」，不改变金额计算口径；MIXED 只可能出现在
    多来源聚合维度（agent_run / pipeline）。source_class 只收窄 Trace 来源范围
    （REAL_API / MOCK / SIMULATED / REPLAY / UNKNOWN），对 evaluation / pipeline
    维度不生效（它们自身就是评测/流水线对象）。过滤后仍按时间倒序返回。
    """
    from backend.app.services.data_governance import SOURCE_CLASSES

    if level not in {"model_call", "trace", "agent_run", "evaluation", "pipeline"}:
        raise CostAnalyticsError(
            "level 必须是 model_call / trace / agent_run / evaluation / pipeline 之一"
        )
    if usage_source is not None and usage_source not in (*USAGE_SOURCES, "MIXED"):
        raise CostAnalyticsError(
            "usage_source 必须是 "
            + " / ".join((*USAGE_SOURCES, "MIXED"))
            + " 之一"
        )
    wanted_source = (source_class or "").strip().upper() or None
    if wanted_source is not None and wanted_source not in SOURCE_CLASSES:
        raise CostAnalyticsError(
            "source_class 必须是 " + " / ".join(SOURCE_CLASSES) + " 之一"
        )
    limit = max(1, min(int(limit), 200))
    price_data = pricing or load_pricing()
    analyses = _cached_analyses(price_data)
    if wanted_source is not None:
        analyses = [a for a in analyses if a.get("source_class") == wanted_source]

    def match(source: str) -> bool:
        return usage_source is None or usage_source == source

    rows: list[dict[str, Any]] = []
    if level == "model_call":
        calls = [
            call
            for analysis in analyses
            for call in analysis["model_calls"]
            if match(call["usage_source"])
        ]
        calls.sort(key=lambda c: c.get("recorded_at") or "", reverse=True)
        for call in calls[:limit]:
            rows.append(
                {
                    "level": "model_call",
                    "id": call["model_call_id"],
                    "parent_refs": {"trace_id": call["trace_id"], "run_id": call["run_id"]},
                    "provider": call["provider"],
                    "model_name": call["model_name"],
                    "prompt_tokens": call["prompt_tokens"],
                    "completion_tokens": call["completion_tokens"],
                    "total_tokens": call["total_tokens"],
                    "usage_source": call["usage_source"],
                    "cost": call["cost"],
                    "latency_ms": call["duration_ms"],
                    "allocation_basis": "step_id（model_call 唯一计费单元，不重复累加）",
                }
            )
    elif level == "trace":
        analyses_sorted = sorted(
            analyses, key=lambda a: a.get("started_at") or "", reverse=True
        )
        for analysis in analyses_sorted:
            source = _dominant_usage_source(analysis)
            if not match(source):
                continue
            rows.append(
                {
                    "level": "trace",
                    "id": analysis["trace_id"],
                    "parent_refs": {"run_id": analysis["run_id"], "task_id": analysis["task_id"]},
                    "provider": analysis["provider"],
                    "model_name": analysis["model_name"],
                    "prompt_tokens": analysis["tokens"]["prompt_tokens"],
                    "completion_tokens": analysis["tokens"]["completion_tokens"],
                    "total_tokens": analysis["tokens"]["total_tokens"],
                    "usage_source": source,
                    "cost": analysis["cost"],
                    "latency_ms": analysis["latency"]["total_run_latency_ms"],
                    "allocation_basis": "trace 内 step 级 model_call 汇总",
                }
            )
            if len(rows) >= limit:
                break
    elif level == "agent_run":
        groups: dict[str, list[dict[str, Any]]] = {}
        for analysis in analyses:
            groups.setdefault(analysis["run_id"], []).append(analysis)
        sorted_groups = sorted(
            groups.items(),
            key=lambda item: max((a.get("started_at") or "") for a in item[1]),
            reverse=True,
        )
        for run_id, items in sorted_groups:
            aggregate = _aggregate_from_analyses(items)
            source = _merged_usage_source(aggregate["tokens"]["usage_sources"])
            if not match(source):
                continue
            rows.append(
                {
                    "level": "agent_run",
                    "id": run_id,
                    "parent_refs": {"trace_ids": [a["trace_id"] for a in items]},
                    "provider": items[0]["provider"],
                    "model_name": items[0]["model_name"],
                    "prompt_tokens": aggregate["tokens"]["prompt_tokens"],
                    "completion_tokens": aggregate["tokens"]["completion_tokens"],
                    "total_tokens": aggregate["tokens"]["total_tokens"],
                    "usage_source": source,
                    "cost": aggregate["cost"],
                    "latency_ms": aggregate["latency"]["total_run_latency_ms"],
                    "allocation_basis": "同一 run_id 的 Trace 汇总（run 级不重复累加）",
                }
            )
            if len(rows) >= limit:
                break
    elif level == "evaluation":
        from backend.app.services.evaluation_store import get_evaluation_store

        for item in get_evaluation_store().list_all():
            try:
                detail = analyze_evaluation_run(
                    item["run_id"], pricing=price_data
                )
            except CostAnalyticsError:
                continue
            aggregate = detail["aggregate"]
            source = _merged_usage_source(aggregate["tokens"]["usage_sources"])
            if not match(source):
                continue
            rows.append(
                {
                    "level": "evaluation",
                    "id": item["run_id"],
                    "parent_refs": {"trace_ids": detail["trace_ids"][:20]},
                    "provider": (detail["traces"][0]["provider"] if detail["traces"] else None),
                    "model_name": (detail["traces"][0]["model_name"] if detail["traces"] else None),
                    "prompt_tokens": aggregate["tokens"]["prompt_tokens"],
                    "completion_tokens": aggregate["tokens"]["completion_tokens"],
                    "total_tokens": aggregate["tokens"]["total_tokens"],
                    "usage_source": source,
                    "cost": aggregate["cost"],
                    "latency_ms": detail["latency"]["evaluation_latency_ms"],
                    "allocation_basis": "评测报告 trace_ids 逐条汇总（不重复计费）",
                }
            )
            if len(rows) >= limit:
                break
    else:  # pipeline
        from backend.app.services.evaluation_pipeline import list_pipelines

        for item in list_pipelines(limit=200):
            try:
                detail = analyze_pipeline_run(
                    item["pipeline_run_id"], pricing=price_data
                )
            except Exception:  # noqa: BLE001 - 单条流水线分析失败不阻断整体归因
                continue
            aggregate = detail["aggregate"]
            source = _merged_usage_source(aggregate["tokens"]["usage_sources"])
            if not match(source):
                continue
            rows.append(
                {
                    "level": "pipeline",
                    "id": item["pipeline_run_id"],
                    "parent_refs": {
                        "evaluation_run_id": detail["evaluation_run_id"],
                        "trace_ids": detail["trace_ids"][:20],
                    },
                    "provider": None,
                    "model_name": None,
                    "prompt_tokens": aggregate["tokens"]["prompt_tokens"],
                    "completion_tokens": aggregate["tokens"]["completion_tokens"],
                    "total_tokens": aggregate["tokens"]["total_tokens"],
                    "usage_source": source,
                    "cost": aggregate["cost"],
                    "latency_ms": detail["latency"]["pipeline_latency_ms"],
                    "allocation_basis": "Pipeline refs.trace_ids 汇总；无阶段级分摊（避免伪造）",
                }
            )
            if len(rows) >= limit:
                break
    return {
        "level": level,
        "usage_source_filter": usage_source,
        "source_class_filter": wanted_source,
        "count": len(rows),
        "rows": rows,
        "pricing_version": price_data["pricing_version"],
        "disclaimer": BILLING_DISCLAIMER,
        "paid_api_called": False,
        "limitations": [
            "归因只使用可靠的 ID 关联（step→trace→run→evaluation→pipeline）",
            "缺少 step 级关联时不把成本分摊到执行阶段，标记为不可用",
            "金额为本地价格表估算，不是实际账单",
            "用量来源过滤只筛选用量来源，不改变金额计算口径",
            "来源筛选只对 model_call / trace / agent_run 维度生效",
        ],
    }


def _merged_usage_source(counts: dict[str, int]) -> str:
    present = [source for source in USAGE_SOURCES if counts.get(source)]
    if not present:
        return "UNKNOWN"
    if len(present) == 1:
        return present[0]
    return "MIXED"


def _dominant_usage_source(analysis: dict[str, Any]) -> str:
    counts = analysis["tokens"]["usage_sources"]
    if not counts:
        return "UNKNOWN"
    best = max(counts.items(), key=lambda item: item[1])
    return best[0] if best[1] else "UNKNOWN"


def unknown_data_report(*, pricing: dict[str, Any] | None = None) -> dict[str, Any]:
    """列出 UNKNOWN / PRICE_UNKNOWN / 缺失延迟 的数据（不把未知当 0）。"""
    price_data = pricing or load_pricing()
    analyses = _cached_analyses(price_data)
    unknown_usage = [
        {
            "trace_id": call["trace_id"],
            "model_call_id": call["model_call_id"],
            "provider": call["provider"],
            "model_name": call["model_name"],
            "usage_source": call["usage_source"],
            "reason": "Provider 未报告 Token 用量",
        }
        for analysis in analyses
        for call in analysis["model_calls"]
        if call["total_tokens"] is None
    ]
    price_unknown = [
        {
            "trace_id": call["trace_id"],
            "model_call_id": call["model_call_id"],
            "provider": call["provider"],
            "model_name": call["model_name"],
            "pricing_version": call["cost"]["pricing_version"],
            "reason": call["cost"]["note"],
        }
        for analysis in analyses
        for call in analysis["model_calls"]
        if call["cost"]["status"] == "PRICE_UNKNOWN"
    ]
    unknown_latency = [
        {
            "trace_id": analysis["trace_id"],
            "unknown_fields": analysis["latency"]["unknown_fields"],
        }
        for analysis in analyses
        if analysis["latency"]["unknown_fields"]
    ]
    return {
        "trace_count": len(analyses),
        "unknown_usage_calls": len(unknown_usage),
        "price_unknown_calls": len(price_unknown),
        "traces_with_unknown_latency": len(unknown_latency),
        "unknown_usage_examples": unknown_usage[:50],
        "price_unknown_examples": price_unknown[:50],
        "unknown_latency_examples": unknown_latency[:50],
        "pricing": {
            "pricing_version": price_data["pricing_version"],
            "pricing_source": price_data["pricing_source"],
            "effective_date": price_data["effective_date"],
            "currency": price_data["currency"],
        },
        "note": "UNKNOWN / PRICE_UNKNOWN 不会被计为 0，也不会计入平均成本；需要补齐用量或价格配置",
    }
