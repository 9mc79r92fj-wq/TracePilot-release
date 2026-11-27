"""Microsoft AgentRx 公开故障 Benchmark 接入（只读）。

本模块把 Microsoft AgentRx 仓库（MIT 许可）公开 ``data/`` 目录中的故障
轨迹子集加载为 TracePilot 统一的 :class:`BenchmarkCase`，并提供只读的
数据划分、摘要统计与 :class:`~backend.app.schemas.trace.TraceRecord` 转换，
为 TracePilot V2 的根因定位 / 故障归因评测提供外部真实故障样本。

数据范围（公开 GitHub 子集，非完整 Hugging Face 发布）：

- ``tau``：tau-bench retail，29 条失败轨迹。任务来自
  ``tau_dataset_failed.json[*].info.task.instruction``；消息来自
  ``[*].traj``；ground truth 的 ``step_number`` 是 1-based 的 ``traj``
  消息下标（轨迹内 ``index`` 字段与之一致）。
- ``magentic``：Magentic-One 多 Agent，44 条失败轨迹。任务取轨迹文件
  第一条 ``human`` 消息；ground truth 的 ``step_number`` 是 1-based 的
  消息下标。

关键约束：

1. 标签只用于评测：:func:`case_without_labels` 与 :func:`to_trace_record`
   的输出**不包含**任何 ``root_cause`` / failure 标签字段；
2. 全部函数只读，不写 ``data/traces``、``data/evaluation`` 或任何生产数据；
3. 不访问网络；Hugging Face 数据集为 gated，本模块只使用已随仓库保存的
   公开 GitHub 数据子集（来源 commit 与许可见 ``manifest.json``）；
4. 轨迹消息原样保存在 ``BenchmarkStep.raw_message``、TraceStep 的
   ``input`` / ``metadata["raw_message"]`` 中，不做改写；
5. 不伪造工具调用：只有原始消息中确实存在 ``tool_calls`` / ``role=tool``
   时才标记为 ``StepType.TOOL_CALL``。

数据来源：
https://github.com/microsoft/AgentRx （commit 见 ``UPSTREAM_COMMIT``）。
论文：AgentRx: Diagnosing AI Agent Failures from Execution Trajectories,
arXiv:2602.02475。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas.trace import (
    SCHEMA_VERSION,
    StepStatus,
    StepType,
    TraceRecord,
    TraceStatus,
    TraceStep,
)

__all__ = [
    "BENCHMARK_NAME",
    "BENCHMARK_DIR",
    "MANIFEST_PATH",
    "DEFAULT_SEED",
    "SOURCES",
    "SPLITS",
    "SPLIT_METHOD",
    "LABEL_FIELDS",
    "UPSTREAM_REPO_URL",
    "UPSTREAM_COMMIT",
    "LICENSE_NAME",
    "ARXIV_ID",
    "AgentRxBenchmarkError",
    "BenchmarkStep",
    "BenchmarkCase",
    "load_manifest",
    "load_cases",
    "split_of",
    "split_cases",
    "case_labels",
    "case_without_labels",
    "to_trace_record",
    "summarize",
]

BENCHMARK_NAME = "agentrx"

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_DIR = PROJECT_ROOT / "data" / "benchmarks" / "agentrx"
MANIFEST_PATH = BENCHMARK_DIR / "manifest.json"
LICENSE_PATH = BENCHMARK_DIR / "LICENSE.txt"
TAU_GROUND_TRUTH_PATH = BENCHMARK_DIR / "ground_truth" / "tau_ground_truth.json"
MAGENTIC_GROUND_TRUTH_PATH = (
    BENCHMARK_DIR / "ground_truth" / "magentic_one_ground_truth.json"
)
TAU_TRAJECTORIES_PATH = BENCHMARK_DIR / "tau_retail" / "tau_dataset_failed.json"
MAGENTIC_TRAJECTORIES_DIR = BENCHMARK_DIR / "magentic_dataset"

SOURCES = ("tau", "magentic")
SPLITS = ("dev", "test")
DOMAIN_BY_SOURCE = {"tau": "tau_retail", "magentic": "magentic_one"}

DEFAULT_SEED = 42
_SUBSET_SIZE_MOD = 2**31
SPLIT_METHOD = (
    "sha256(case_id + '|' + str(seed)) 的前 8 个十六进制字符视为 uint32；"
    "< 2^31 划入 dev，否则划入 test。seed 默认 42，按 case_id 稳定、互斥。"
)

# 评测标签字段：这些字段只在 case_labels / BenchmarkCase 中出现，绝不进入
# case_without_labels 或 TraceRecord。
LABEL_FIELDS = (
    "root_cause_step",
    "root_cause_failure_id",
    "root_cause_category",
    "root_cause_reason",
    "failure_steps",
    "failure_categories",
    "failure_ids",
    "failure_agents",
    "failed_agent",
    "failure_summary",
)

# 上游来源与许可（与 data/benchmarks/agentrx/manifest.json 保持一致）。
UPSTREAM_REPO_URL = "https://github.com/microsoft/AgentRx"
UPSTREAM_COMMIT = "7a18c79708e7671be15124460f4f7296107c2a55"
UPSTREAM_COMMIT_DATE = "2026-09-14T22:18:36Z"
UPSTREAM_LICENSE_URL = f"{UPSTREAM_REPO_URL}/blob/{UPSTREAM_COMMIT}/LICENSE.txt"
LICENSE_NAME = "MIT"
ARXIV_ID = "2602.02475"
ARXIV_URL = f"https://arxiv.org/abs/{ARXIV_ID}"

# 轨迹没有真实逐条时间戳，TraceStep 又要求带时区时间；使用固定基准时间 +
# 1-based 下标作为确定性时间线，并在 metadata 中显式标注为合成时间。
_SYNTHETIC_START = datetime(2025, 1, 1, tzinfo=timezone.utc)


class AgentRxBenchmarkError(ValueError):
    """AgentRx benchmark 数据缺失、格式异常或标签不一致时抛出。"""


class BenchmarkStep(BaseModel):
    """统一的 1-based 轨迹消息步骤。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_index: int = Field(..., ge=1, description="1-based 消息下标（与 ground truth step_number 对齐）")
    role: str = Field(..., description="原始消息 role（不改写）")
    agent: str = Field(..., description="归一化 Agent 名，便于与 ground truth failed_agent 比较")
    content: str | None = Field(default=None, description="消息内容；允许空 assistant 内容")
    raw_message: dict[str, Any] = Field(
        default_factory=dict, description="原始消息（原样保留，供 TraceRecord 使用）"
    )
    tool_name: str | None = Field(default=None, description="仅原始消息存在真实工具调用/结果时填写")
    tool_call_id: str | None = Field(default=None, description="原始 tool_call_id（若存在）")
    tool_arguments: dict[str, Any] | None = Field(
        default=None, description="从原始 tool_calls 参数解析出的 JSON 对象（若存在）"
    )


class BenchmarkCase(BaseModel):
    """一条统一的 AgentRx 故障案例（含评测标签）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(..., description="稳定案例 ID，如 tau-2 / magentic-<uuid>")
    source: str = Field(..., description="来源域：tau / magentic")
    domain: str = Field(..., description="细分领域：tau_retail / magentic_one")
    task: str = Field(..., description="任务文本（tau 取 info.task.instruction；magentic 取第一条 human 消息）")
    steps: list[BenchmarkStep] = Field(..., min_length=1, description="1-based 顺序消息列表")
    root_cause_step: int = Field(..., ge=1, description="根因步骤（1-based 消息下标）")
    root_cause_failure_id: int | str = Field(..., description="根因对应的原始 failure_id（保留原始类型）")
    root_cause_category: str = Field(..., description="根因 failure_category")
    failure_steps: list[int] = Field(default_factory=list, description="全部失败步骤（1-based，升序）")
    failure_categories: list[str] = Field(default_factory=list, description="与 failure_steps 对齐的失败类别")
    failed_agent: str = Field(..., description="根因步骤的 failed_agent（原始大小写）")
    split: str = Field(..., description="数据划分：dev / test")
    root_cause_reason: str | None = Field(default=None, description="ground truth root_cause.reason_for_root_cause")
    failure_summary: str | None = Field(default=None, description="ground truth failure_summary")
    failure_ids: list[int | str] = Field(default_factory=list, description="全部原始 failure_id")
    failure_agents: list[str] = Field(default_factory=list, description="与 failure_steps 对齐的 failed_agent")


# ---------------------------------------------------------------------------
# 只读工具函数
# ---------------------------------------------------------------------------


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def _read_json(path: Path) -> Any:
    """读取 UTF-8 JSON；找不到 / 解析失败时给出可定位的错误。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentRxBenchmarkError(
            f"缺少 AgentRx benchmark 数据文件：{_display_path(path)}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AgentRxBenchmarkError(
            f"AgentRx benchmark 数据文件不是合法 JSON：{_display_path(path)}"
        ) from exc


def _require_list(value: Any, *, where: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise AgentRxBenchmarkError(f"{where} 必须是非空列表")
    return value


def _require_dict(value: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentRxBenchmarkError(f"{where} 必须是 JSON 对象")
    return value


def _normalise_content(value: Any) -> str | None:
    """消息 content 允许空值；非字符串内容做无损 JSON 化兜底。"""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _normalise_step_number(value: Any, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AgentRxBenchmarkError(f"{where} 的 step_number 必须是 >=1 的整数，实际为 {value!r}")
    return value


def _normalise_failure_id(value: Any, *, where: str) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AgentRxBenchmarkError(f"{where} 的 failure_id 必须是 int 或 str，实际为 {value!r}")
    if isinstance(value, str) and not value.strip():
        raise AgentRxBenchmarkError(f"{where} 的 failure_id 不能为空字符串")
    return value


def _parse_tool_arguments(value: Any) -> dict[str, Any] | None:
    """解析 OpenAI function-call 的 arguments；非法 JSON 时返回 None（原始值仍在 raw_message）。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _tau_agent(role: str) -> str:
    return {
        "system": "System",
        "user": "User",
        "assistant": "Assistant",
        "tool": "Tool",
    }.get(role.lower(), role)


def _magentic_agent(role: str) -> str:
    """把 Magentic-One 的 role（含 ``Orchestrator (-> WebSurfer)``）归一化为 Agent 名。"""
    text = role.strip()
    lowered = text.lower()
    if lowered == "human":
        return "User"
    if lowered.startswith("orchestrator"):
        return "Orchestrator"
    if lowered.startswith("websurfer"):
        return "WebSurfer"
    if lowered.startswith("filesurfer"):
        return "FileSurfer"
    if lowered.startswith("computersurfer") or lowered.startswith("computerterminal"):
        return "ComputerTerminal"
    if lowered.startswith("assistant"):
        return "Assistant"
    return text


def split_of(case_id: str, seed: int = DEFAULT_SEED) -> str:
    """按 ``sha256(case_id + '|' + seed)`` 稳定划分到 dev / test。

    该函数是纯函数：同一 ``case_id`` 与 ``seed`` 永远得到同一划分；不同案例
    之间互斥（只会出现在一个 split 中）。公式见 :data:`SPLIT_METHOD`。
    """
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("case_id 必须是非空字符串")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed 必须是整数")
    digest = hashlib.sha256(f"{case_id}|{seed}".encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16)
    return "dev" if bucket < _SUBSET_SIZE_MOD else "test"


def load_manifest() -> dict[str, Any]:
    """读取 ``data/benchmarks/agentrx/manifest.json``（来源 / 许可 / 子集说明）。"""
    manifest = _require_dict(_read_json(MANIFEST_PATH), where=_display_path(MANIFEST_PATH))
    if manifest.get("benchmark") != BENCHMARK_NAME:
        raise AgentRxBenchmarkError(
            f"manifest benchmark 字段异常：{manifest.get('benchmark')!r}"
        )
    return manifest


# ---------------------------------------------------------------------------
# 标签构建
# ---------------------------------------------------------------------------


def _labels_from_ground_truth(
    ground_truth: dict[str, Any],
    *,
    source: str,
    case_id: str,
    step_count: int,
) -> dict[str, Any]:
    """把 AgentRx ground truth 转为统一标签，并校验 step 映射范围。"""
    failures_raw = _require_list(
        ground_truth.get("failures"), where=f"{source}/{case_id}.failures"
    )
    failures: list[dict[str, Any]] = []
    for index, raw in enumerate(failures_raw):
        failure = _require_dict(raw, where=f"{source}/{case_id}.failures[{index}]")
        step_number = _normalise_step_number(
            failure.get("step_number"),
            where=f"{source}/{case_id}.failures[{index}]",
        )
        if step_number > step_count:
            raise AgentRxBenchmarkError(
                f"{source}/{case_id} 的失败步骤 {step_number} 超出轨迹长度 {step_count}"
            )
        category = failure.get("failure_category")
        if not isinstance(category, str) or not category.strip():
            raise AgentRxBenchmarkError(
                f"{source}/{case_id}.failures[{index}] 缺少 failure_category"
            )
        agent = failure.get("failed_agent")
        if not isinstance(agent, str) or not agent.strip():
            raise AgentRxBenchmarkError(
                f"{source}/{case_id}.failures[{index}] 缺少 failed_agent"
            )
        failures.append(
            {
                "failure_id": _normalise_failure_id(
                    failure.get("failure_id"),
                    where=f"{source}/{case_id}.failures[{index}]",
                ),
                "step_number": step_number,
                "failure_category": category,
                "failed_agent": agent,
            }
        )

    # 原始 failure 顺序可能不是 step 顺序；按 (step_number, failure_id) 排序，
    # 保证 failure_steps / failure_categories 严格对齐。
    failures.sort(key=lambda item: (item["step_number"], str(item["failure_id"])))

    root_cause = _require_dict(
        ground_truth.get("root_cause"), where=f"{source}/{case_id}.root_cause"
    )
    root_failure_id = _normalise_failure_id(
        root_cause.get("failure_id"), where=f"{source}/{case_id}.root_cause"
    )
    # 上游存在 failure_id 类型不一致（如 "1" vs 1），按字符串匹配，保留原始值。
    root_failure = next(
        (item for item in failures if str(item["failure_id"]) == str(root_failure_id)),
        None,
    )
    if root_failure is None:
        raise AgentRxBenchmarkError(
            f"{source}/{case_id} 的 root_cause.failure_id={root_failure_id!r} "
            f"不在 failures 中：{[item['failure_id'] for item in failures]}"
        )

    failure_summary = ground_truth.get("failure_summary")
    reason = root_cause.get("reason_for_root_cause")
    return {
        "root_cause_step": int(root_failure["step_number"]),
        "root_cause_failure_id": root_failure_id,
        "root_cause_category": str(root_failure["failure_category"]),
        "failure_steps": [int(item["step_number"]) for item in failures],
        "failure_categories": [str(item["failure_category"]) for item in failures],
        "failed_agent": str(root_failure["failed_agent"]),
        "root_cause_reason": reason if isinstance(reason, str) else None,
        "failure_summary": failure_summary if isinstance(failure_summary, str) else None,
        "failure_ids": [item["failure_id"] for item in failures],
        "failure_agents": [str(item["failed_agent"]) for item in failures],
    }


# ---------------------------------------------------------------------------
# 数据加载（tau / magentic）
# ---------------------------------------------------------------------------


def _build_tau_steps(
    traj: list[Any],
) -> list[BenchmarkStep]:
    """构建 tau 步骤；assistant 空 content 与 tool_calls 原样保留。"""
    arguments_by_call_id: dict[str, tuple[str | None, dict[str, Any] | None]] = {}
    for raw in traj:
        message = _require_dict(raw, where="tau.traj[*]")
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            if not isinstance(function, dict):
                continue
            call_id = call.get("id")
            if call_id:
                arguments_by_call_id[str(call_id)] = (
                    function.get("name") if isinstance(function.get("name"), str) else None,
                    _parse_tool_arguments(function.get("arguments")),
                )

    steps: list[BenchmarkStep] = []
    for index, raw in enumerate(traj, start=1):
        message = _require_dict(raw, where=f"tau.traj[{index - 1}]")
        role = message.get("role")
        if not isinstance(role, str) or not role.strip():
            raise AgentRxBenchmarkError(f"tau.traj[{index - 1}] 缺少 role")
        tool_name: str | None = None
        tool_call_id: str | None = None
        tool_arguments: dict[str, Any] | None = None
        if role.lower() == "tool":
            tool_call_id = (
                str(message["tool_call_id"])
                if message.get("tool_call_id") is not None
                else None
            )
            mapped = arguments_by_call_id.get(tool_call_id) if tool_call_id else None
            if mapped is not None:
                tool_name, tool_arguments = mapped
            if not tool_name and isinstance(message.get("name"), str):
                tool_name = message["name"]
        else:
            tool_calls = message.get("tool_calls") or []
            if isinstance(tool_calls, list) and tool_calls:
                first_call = tool_calls[0]
                if isinstance(first_call, dict):
                    call_id = first_call.get("id")
                    tool_call_id = str(call_id) if call_id else None
                    function = first_call.get("function") or {}
                    if isinstance(function, dict):
                        if isinstance(function.get("name"), str):
                            tool_name = function["name"]
                        tool_arguments = _parse_tool_arguments(function.get("arguments"))
        steps.append(
            BenchmarkStep(
                step_index=index,
                role=role,
                agent=_tau_agent(role),
                content=_normalise_content(message.get("content")),
                raw_message=dict(message),
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_arguments=tool_arguments,
            )
        )
    return steps


def _build_tau_cases(seed: int) -> list[BenchmarkCase]:
    ground_truths = _require_list(
        _read_json(TAU_GROUND_TRUTH_PATH), where=_display_path(TAU_GROUND_TRUTH_PATH)
    )
    dataset = _require_list(
        _read_json(TAU_TRAJECTORIES_PATH), where=_display_path(TAU_TRAJECTORIES_PATH)
    )
    by_task_id: dict[int, dict[str, Any]] = {}
    for index, raw in enumerate(dataset):
        item = _require_dict(raw, where=f"tau_dataset_failed[{index}]")
        task_id = item.get("task_id")
        if isinstance(task_id, bool) or not isinstance(task_id, int):
            raise AgentRxBenchmarkError(f"tau_dataset_failed[{index}].task_id 不是整数")
        if task_id in by_task_id:
            raise AgentRxBenchmarkError(f"tau_dataset_failed 中 task_id={task_id} 重复")
        by_task_id[task_id] = item

    cases: list[BenchmarkCase] = []
    for index, raw in enumerate(ground_truths):
        ground_truth = _require_dict(raw, where=f"tau_ground_truth[{index}]")
        trajectory_id = ground_truth.get("trajectory_id")
        if isinstance(trajectory_id, bool) or not isinstance(trajectory_id, int):
            raise AgentRxBenchmarkError(f"tau_ground_truth[{index}].trajectory_id 不是整数")
        item = by_task_id.get(trajectory_id)
        if item is None:
            raise AgentRxBenchmarkError(
                f"tau_ground_truth.trajectory_id={trajectory_id} 在 tau_dataset_failed 中不存在"
            )
        info = _require_dict(item.get("info"), where=f"tau[{trajectory_id}].info")
        task = _require_dict(info.get("task"), where=f"tau[{trajectory_id}].info.task")
        instruction = task.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise AgentRxBenchmarkError(f"tau[{trajectory_id}] 缺少 info.task.instruction")
        traj = _require_list(item.get("traj"), where=f"tau[{trajectory_id}].traj")
        steps = _build_tau_steps(traj)
        case_id = f"tau-{trajectory_id}"
        labels = _labels_from_ground_truth(
            ground_truth, source="tau", case_id=case_id, step_count=len(steps)
        )
        cases.append(
            BenchmarkCase(
                case_id=case_id,
                source="tau",
                domain=DOMAIN_BY_SOURCE["tau"],
                task=instruction,
                steps=steps,
                split=split_of(case_id, seed),
                **labels,
            )
        )
    return cases


def _build_magentic_cases(seed: int) -> list[BenchmarkCase]:
    ground_truths = _require_list(
        _read_json(MAGENTIC_GROUND_TRUTH_PATH),
        where=_display_path(MAGENTIC_GROUND_TRUTH_PATH),
    )
    cases: list[BenchmarkCase] = []
    for index, raw in enumerate(ground_truths):
        ground_truth = _require_dict(raw, where=f"magentic_one_ground_truth[{index}]")
        trajectory_id = ground_truth.get("trajectory_id")
        if not isinstance(trajectory_id, str) or not trajectory_id.strip():
            raise AgentRxBenchmarkError(
                f"magentic_one_ground_truth[{index}].trajectory_id 不是非空字符串"
            )
        trajectory_path = MAGENTIC_TRAJECTORIES_DIR / f"{trajectory_id}.json"
        messages = _require_list(
            _read_json(trajectory_path), where=_display_path(trajectory_path)
        )
        steps: list[BenchmarkStep] = []
        task: str | None = None
        for step_index, raw_message in enumerate(messages, start=1):
            message = _require_dict(
                raw_message, where=f"magentic/{trajectory_id}[{step_index - 1}]"
            )
            role = message.get("role")
            if not isinstance(role, str) or not role.strip():
                raise AgentRxBenchmarkError(
                    f"magentic/{trajectory_id}[{step_index - 1}] 缺少 role"
                )
            content = _normalise_content(message.get("content"))
            if task is None and role.strip().lower() == "human":
                if content is None or not content.strip():
                    raise AgentRxBenchmarkError(
                        f"magentic/{trajectory_id} 第一条 human 消息为空，无法作为 task"
                    )
                task = content
            # Magentic-One 消息是 Agent 对话文本，不含结构化 tool 消息；
            # 因此只标记 model_call / generic，绝不伪造 tool_call。
            steps.append(
                BenchmarkStep(
                    step_index=step_index,
                    role=role,
                    agent=_magentic_agent(role),
                    content=content,
                    raw_message=dict(message),
                )
            )
        if task is None:
            raise AgentRxBenchmarkError(
                f"magentic/{trajectory_id} 中没有 human 消息，无法确定 task"
            )
        case_id = f"magentic-{trajectory_id}"
        labels = _labels_from_ground_truth(
            ground_truth, source="magentic", case_id=case_id, step_count=len(steps)
        )
        cases.append(
            BenchmarkCase(
                case_id=case_id,
                source="magentic",
                domain=DOMAIN_BY_SOURCE["magentic"],
                task=task,
                steps=steps,
                split=split_of(case_id, seed),
                **labels,
            )
        )
    return cases


def load_cases(
    *, source: str | None = None, seed: int = DEFAULT_SEED
) -> list[BenchmarkCase]:
    """加载 AgentRx 案例（只读；不写任何生产数据）。

    Args:
        source: ``"tau"`` / ``"magentic"`` / ``None``（两类全部加载）。
        seed: 划分随机种子，默认 42；写入每个案例的 ``split`` 字段。

    Returns:
        按 ground truth 文件顺序排列的 :class:`BenchmarkCase` 列表
        （``source=None`` 时先 tau 后 magentic）。
    """
    if source is not None and source not in SOURCES:
        raise ValueError(f"source 必须是 {SOURCES} 之一或 None，实际为 {source!r}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed 必须是整数")
    cases: list[BenchmarkCase] = []
    if source in (None, "tau"):
        cases.extend(_build_tau_cases(seed))
    if source in (None, "magentic"):
        cases.extend(_build_magentic_cases(seed))
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise AgentRxBenchmarkError("AgentRx case_id 不唯一")
    return cases


# ---------------------------------------------------------------------------
# 划分 / 摘要 / 标签隔离
# ---------------------------------------------------------------------------


def split_cases(
    cases: list[BenchmarkCase], *, seed: int | None = None
) -> dict[str, list[BenchmarkCase]]:
    """把案例划分为 dev / test（默认使用案例上已记录的 split）。

    - ``seed=None``：按 ``BenchmarkCase.split`` 分组（由 :func:`load_cases` 写入）；
    - 指定 ``seed``：按 :func:`split_of` 重新计算，便于复现实验。

    返回 ``{"dev": [...], "test": [...]}``，每组内按 ``case_id`` 排序；两组
    严格互斥且并集等于输入（train/test 隔离）。
    """
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise ValueError("seed 必须是整数或 None")
    groups: dict[str, list[BenchmarkCase]] = {"dev": [], "test": []}
    for case in sorted(cases, key=lambda item: item.case_id):
        split = case.split if seed is None else split_of(case.case_id, seed)
        if split not in groups:
            raise AgentRxBenchmarkError(
                f"案例 {case.case_id} 的 split={split!r} 非法；必须是 {SPLITS}"
            )
        groups[split].append(case)
    ids = [case.case_id for group in groups.values() for case in group]
    if len(ids) != len(set(ids)):
        raise AgentRxBenchmarkError("dev/test 划分存在重复案例（标签/划分泄漏）")
    return groups


def case_labels(case: BenchmarkCase) -> dict[str, Any]:
    """返回评测用的 ground truth 标签（仅用于评测，禁止用于诊断模型输入）。"""
    return {
        "case_id": case.case_id,
        "root_cause_step": case.root_cause_step,
        "root_cause_failure_id": case.root_cause_failure_id,
        "root_cause_category": case.root_cause_category,
        "root_cause_reason": case.root_cause_reason,
        "failure_steps": list(case.failure_steps),
        "failure_categories": list(case.failure_categories),
        "failure_ids": list(case.failure_ids),
        "failure_agents": list(case.failure_agents),
        "failed_agent": case.failed_agent,
        "failure_summary": case.failure_summary,
    }


def case_without_labels(case: BenchmarkCase) -> dict[str, Any]:
    """返回不含任何 ground truth 标签的案例序列化（诊断模型输入专用）。

    输出只包含 case_id / source / domain / task / split / steps（含原始消息），
    不含 :data:`LABEL_FIELDS` 中的任何字段，可安全 ``json.dumps`` 后交给诊断模型。
    """
    return {
        "case_id": case.case_id,
        "source": case.source,
        "domain": case.domain,
        "task": case.task,
        "split": case.split,
        "label_free": True,
        "steps": [
            {
                "step_index": step.step_index,
                "role": step.role,
                "agent": step.agent,
                "content": step.content,
                "tool_name": step.tool_name,
                "tool_call_id": step.tool_call_id,
                "tool_arguments": step.tool_arguments,
                "raw_message": step.raw_message,
            }
            for step in case.steps
        ],
    }


# ---------------------------------------------------------------------------
# TraceRecord 转换（标签隔离）
# ---------------------------------------------------------------------------


def _step_type(step: BenchmarkStep) -> StepType:
    """消息 → StepType。只有原始数据确实包含工具调用/结果时才用 TOOL_CALL。"""
    if step.tool_name or step.tool_call_id or step.role.strip().lower() == "tool":
        return StepType.TOOL_CALL
    if step.agent in {"User", "System"}:
        return StepType.GENERIC
    return StepType.MODEL_CALL


def _trace_id_for(case_id: str) -> str:
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:16]
    return f"trace-agentrx-{digest}"


def to_trace_record(case: BenchmarkCase) -> TraceRecord:
    """把 :class:`BenchmarkCase` 转为可通过 Pydantic 校验的 :class:`TraceRecord`。

    设计约束：

    - 标签隔离：TraceRecord 的步骤与 metadata **不包含** root cause / failure
      标签；标签请使用 :func:`case_labels`；
    - 原始消息保留在 ``step.input`` 与 ``step.metadata["raw_message"]``；
    - 不为 Magentic-One 对话伪造工具调用；tau 的 ``tool_calls`` / ``tool``
      消息按 ``StepType.TOOL_CALL`` 标记；
    - 轨迹没有真实时间戳，使用确定性合成时间并在 metadata 标注；
    - 不写文件、不修改生产 ``data/traces``。
    """
    trace_id = _trace_id_for(case.case_id)
    expected_indices = list(range(1, len(case.steps) + 1))
    if [step.step_index for step in case.steps] != expected_indices:
        raise AgentRxBenchmarkError(
            f"{case.case_id} 的 step_index 不是连续 1..N，无法安全转换 TraceRecord"
        )

    steps: list[TraceStep] = []
    previous_step_id: str | None = None
    for step in case.steps:
        step_id = f"{trace_id}-s{step.step_index:03d}"
        timestamp = _SYNTHETIC_START + timedelta(seconds=step.step_index - 1)
        tool_result = step.content if _step_type(step) is StepType.TOOL_CALL else None
        steps.append(
            TraceStep(
                step_id=step_id,
                trace_id=trace_id,
                parent_step_ids=[previous_step_id] if previous_step_id else [],
                step_type=_step_type(step),
                timestamp=timestamp,
                input=step.raw_message,
                output=step.content,
                tool_name=step.tool_name,
                tool_arguments=step.tool_arguments,
                tool_result=tool_result,
                status=StepStatus.SUCCEEDED,
                metadata={
                    "benchmark": BENCHMARK_NAME,
                    "benchmark_source": case.source,
                    "benchmark_domain": case.domain,
                    "original_step_index": step.step_index,
                    "original_role": step.role,
                    "raw_message": step.raw_message,
                    "license": LICENSE_NAME,
                    "upstream_repo": UPSTREAM_REPO_URL,
                    "upstream_commit": UPSTREAM_COMMIT,
                    "timestamp_synthesized": True,
                    "status_source": "unlabeled",
                },
            )
        )
        previous_step_id = step_id

    return TraceRecord(
        trace_id=trace_id,
        task_id=case.case_id,
        run_id=f"run-agentrx-{hashlib.sha256(case.case_id.encode('utf-8')).hexdigest()[:16]}",
        started_at=_SYNTHETIC_START,
        ended_at=_SYNTHETIC_START + timedelta(seconds=max(1, len(steps))),
        status=TraceStatus.FAILED,
        steps=steps,
        metadata={
            "schema_version": SCHEMA_VERSION,
            "benchmark": BENCHMARK_NAME,
            "benchmark_display_name": "Microsoft AgentRx (public GitHub subset)",
            "benchmark_source": case.source,
            "benchmark_domain": case.domain,
            "original_case_id": case.case_id,
            "split": case.split,
            "license": LICENSE_NAME,
            "license_url": UPSTREAM_LICENSE_URL,
            "upstream_repo": UPSTREAM_REPO_URL,
            "upstream_commit": UPSTREAM_COMMIT,
            "paper_arxiv": ARXIV_ID,
            "data_subset": (
                "public GitHub data/ subset (tau 29 + magentic 44); "
                "not the complete Hugging Face release"
            ),
            "evaluation": True,
            "labels_excluded": True,
            "label_policy": (
                "ground-truth annotations are excluded from this trace; "
                "use agentrx_benchmark.case_labels() for evaluation"
            ),
            "step_type_policy": (
                "model_call for agent messages; tool_call only for genuine "
                "tool_calls / tool messages present in the raw trajectory"
            ),
            "timestamp_synthesized": True,
            "timestamp_note": (
                "AgentRx trajectories do not ship per-message timestamps; "
                "timestamps are deterministic synthetic ordering values"
            ),
            "parent_links_derived": "linear conversation order",
            "status_note": (
                "AgentRx cases are known-failure trajectories selected by the "
                "benchmark; failed status reflects dataset selection, not a "
                "per-step diagnosis"
            ),
            "read_only": True,
        },
    )


def _sorted_counter(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def summarize(cases: list[BenchmarkCase]) -> dict[str, Any]:
    """汇总案例数量、步骤数量、标签分布与划分（只读，用于报告 / 测试）。"""
    source_counts = Counter(case.source for case in cases)
    domain_counts = Counter(case.domain for case in cases)
    split_counts = Counter(case.split for case in cases)
    source_split: dict[str, dict[str, int]] = {}
    for source in SOURCES:
        selected = [case for case in cases if case.source == source]
        if selected:
            counts = Counter(case.split for case in selected)
            source_split[source] = {split: counts.get(split, 0) for split in SPLITS}
    step_counts = [len(case.steps) for case in cases]
    failure_categories: Counter[str] = Counter()
    failure_agents: Counter[str] = Counter()
    root_cause_categories: Counter[str] = Counter()
    root_cause_agents: Counter[str] = Counter()
    for case in cases:
        failure_categories.update(case.failure_categories)
        failure_agents.update(case.failure_agents)
        root_cause_categories[case.root_cause_category] += 1
        root_cause_agents[case.failed_agent] += 1

    return {
        "benchmark": BENCHMARK_NAME,
        "total_cases": len(cases),
        "total_steps": sum(step_counts),
        "step_count": {
            "min": min(step_counts) if step_counts else 0,
            "max": max(step_counts) if step_counts else 0,
            "mean": round(sum(step_counts) / len(step_counts), 2) if step_counts else 0.0,
        },
        "by_source": {
            "tau": source_counts.get("tau", 0),
            "magentic": source_counts.get("magentic", 0),
        },
        "by_domain": _sorted_counter(domain_counts),
        "by_split": {split: split_counts.get(split, 0) for split in SPLITS},
        "by_source_split": source_split,
        "total_failures": sum(len(case.failure_steps) for case in cases),
        "failure_categories": _sorted_counter(failure_categories),
        "failure_agents": _sorted_counter(failure_agents),
        "root_cause_categories": _sorted_counter(root_cause_categories),
        "root_cause_agents": _sorted_counter(root_cause_agents),
        "split_method": SPLIT_METHOD,
        "label_policy": (
            "labels are evaluation-only; case_without_labels() and "
            "to_trace_record() never expose them"
        ),
        "read_only": True,
    }
