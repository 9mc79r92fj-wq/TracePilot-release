"""AgentRx 公开故障 Benchmark 接入测试（pytest）。

覆盖 V2「公开 Agent 故障 Benchmark 接入」验收点：

1. 数据子集与 manifest：来源 URL / commit / MIT 许可 / 子集数量 / 限制说明，
   以及全部复制文件的 SHA-256 校验；
2. 加载数量正确：tau 29 + magentic 44 = 73，task / role / content 与原始
   轨迹逐条对齐；
3. 标签映射：root cause step 在轨迹范围内，failure_steps 与 categories 对齐；
4. 划分稳定互斥：sha256(case_id + seed) 稳定 dev/test（约 50/50），
   train/test 严格隔离；
5. 标签隔离：case_without_labels() 与 to_trace_record() 序列化后不含
   root_cause / failure 标签字段；
6. to_trace_record() 输出可被 TraceRecord 通过 Pydantic 校验，原始消息
   保留、不伪造工具调用、确定性可复现。

全部为本地只读测试：不访问网络、不写生产 data/traces、不调用付费 API。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.schemas.trace import (  # noqa: E402
    StepStatus,
    StepType,
    TraceRecord,
    TraceStatus,
)
from backend.app.services import agentrx_benchmark as ab  # noqa: E402

BENCHMARK_DIR = PROJECT_ROOT / "data" / "benchmarks" / "agentrx"
TAU_GT_PATH = BENCHMARK_DIR / "ground_truth" / "tau_ground_truth.json"
MAGENTIC_GT_PATH = BENCHMARK_DIR / "ground_truth" / "magentic_one_ground_truth.json"
TAU_TRAJ_PATH = BENCHMARK_DIR / "tau_retail" / "tau_dataset_failed.json"
MAGENTIC_TRAJ_DIR = BENCHMARK_DIR / "magentic_dataset"
MANIFEST_PATH = BENCHMARK_DIR / "manifest.json"

TAU_CASE_COUNT = 29
MAGENTIC_CASE_COUNT = 44
TOTAL_CASE_COUNT = TAU_CASE_COUNT + MAGENTIC_CASE_COUNT
TOTAL_STEP_COUNT = 3265
TOTAL_FAILURE_COUNT = 334
SPLIT_COUNTS_SEED_42 = {"dev": 38, "test": 35}
STEP_TYPE_ALLOWED = {StepType.MODEL_CALL, StepType.TOOL_CALL, StepType.GENERIC}


# ---------------------------------------------------------------------------
# 只读 fixture / 工具
# ---------------------------------------------------------------------------


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _collect_keys(value) -> set[str]:
    """递归收集 JSON 结构中的全部 key（用于标签泄漏检查）。"""
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(key)
            keys |= _collect_keys(child)
    elif isinstance(value, list):
        for child in value:
            keys |= _collect_keys(child)
    return keys


@pytest.fixture(scope="module")
def cases() -> list[ab.BenchmarkCase]:
    return ab.load_cases()


@pytest.fixture(scope="module")
def cases_by_id(cases) -> dict[str, ab.BenchmarkCase]:
    return {case.case_id: case for case in cases}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return ab.load_manifest()


@pytest.fixture(scope="module")
def traces(cases) -> dict[str, TraceRecord]:
    return {case.case_id: ab.to_trace_record(case) for case in cases}


@pytest.fixture(scope="module")
def tau_ground_truth() -> dict[str, dict]:
    return {
        str(item["trajectory_id"]): item
        for item in _load_json(TAU_GT_PATH)
    }


@pytest.fixture(scope="module")
def magentic_ground_truth() -> dict[str, dict]:
    return {
        str(item["trajectory_id"]): item
        for item in _load_json(MAGENTIC_GT_PATH)
    }


@pytest.fixture(scope="module")
def tau_dataset() -> dict[int, dict]:
    return {
        int(item["task_id"]): item
        for item in _load_json(TAU_TRAJ_PATH)
    }


# ---------------------------------------------------------------------------
# 1. manifest / 数据子集
# ---------------------------------------------------------------------------


def test_manifest_provenance(manifest):
    """manifest 如实记录公开 GitHub 来源、commit、MIT 许可与子集数量。"""
    upstream = manifest["upstream"]
    assert upstream["repo"] == "https://github.com/microsoft/AgentRx"
    assert upstream["commit"] == ab.UPSTREAM_COMMIT
    assert re.fullmatch(r"[0-9a-f]{40}", upstream["commit"])
    assert upstream["commit_short"] == ab.UPSTREAM_COMMIT[:7]
    assert upstream["license"] == "MIT"
    assert "License" in upstream["copyright"] or "Microsoft" in upstream["copyright"]
    assert upstream["license_url"].endswith("/LICENSE.txt")
    assert upstream["paper"]["arxiv_id"] == "2602.02475"
    assert upstream["paper"]["url"] == "https://arxiv.org/abs/2602.02475"

    counts = manifest["subset"]["counts"]
    assert counts["tau_cases"] == TAU_CASE_COUNT
    assert counts["magentic_cases"] == MAGENTIC_CASE_COUNT
    assert counts["total_cases"] == TOTAL_CASE_COUNT
    assert counts["trajectory_files"] == MAGENTIC_CASE_COUNT + 1
    assert counts["copied_files"] == len(manifest["subset"]["files"]) == 48

    # 诚实披露 HF gated 与「非官方完整 170 条」
    hf = upstream["huggingface"]
    assert hf["gated"] is True
    assert hf["downloaded_for_this_subset"] is False
    limitations = "\n".join(manifest["limitations"]).lower()
    assert "gated" in limitations
    assert "170" in limitations
    assert "not" in limitations


def test_manifest_files_exist_and_hash_match(manifest):
    """manifest 中每个文件都存在且 SHA-256 与快照一致。"""
    for entry in manifest["subset"]["files"]:
        path = BENCHMARK_DIR / entry["local_path"]
        assert path.is_file(), f"manifest 文件不存在: {entry['local_path']}"
        assert path.stat().st_size == entry["bytes"]
        assert _sha256(path) == entry["sha256"]
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
    verification = manifest["generation"]["verification"]
    assert verification["files_checked"] == 48
    assert verification["sha256_checked"] == 48
    assert verification["git_blob_sha1_match_upstream_commit"] == 48


def test_subset_does_not_include_gated_or_unneeded_files():
    """只复制必要 JSON：不含 PDF / policies / 未标注的 magentic 轨迹 / tau_retail 标注。"""
    all_files = [path for path in BENCHMARK_DIR.rglob("*") if path.is_file()]
    assert not any(path.suffix.lower() == ".pdf" for path in all_files)
    assert not any("policies" in path.parts for path in all_files)
    assert not any(path.name == "tau_retail.json" for path in all_files)
    assert not any(path.name in {"steps_by_id.json", "count.py"} for path in all_files)
    magentic_files = sorted(MAGENTIC_TRAJ_DIR.glob("*.json"))
    assert len(magentic_files) == MAGENTIC_CASE_COUNT
    assert (BENCHMARK_DIR / "LICENSE.txt").is_file()
    assert MANIFEST_PATH.is_file()


def test_license_is_mit(manifest):
    """上游 MIT 许可证随数据子集原样保存。"""
    text = (BENCHMARK_DIR / "LICENSE.txt").read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Microsoft Corporation" in text
    assert manifest["upstream"]["license_file"] == "LICENSE.txt"


# ---------------------------------------------------------------------------
# 2. 加载数量与任务 / 消息映射
# ---------------------------------------------------------------------------


def test_load_case_counts(cases):
    """tau 29 条 + magentic 44 条，case_id 唯一且任务非空。"""
    assert len(cases) == TOTAL_CASE_COUNT
    source_counts = Counter(case.source for case in cases)
    assert source_counts == {"tau": TAU_CASE_COUNT, "magentic": MAGENTIC_CASE_COUNT}
    domain_counts = Counter(case.domain for case in cases)
    assert domain_counts == {"tau_retail": TAU_CASE_COUNT, "magentic_one": MAGENTIC_CASE_COUNT}
    case_ids = [case.case_id for case in cases]
    assert len(case_ids) == len(set(case_ids))
    assert {case.split for case in cases} == {"dev", "test"}
    for case in cases:
        assert case.task and case.task.strip()
        assert case.steps
        assert [step.step_index for step in case.steps] == list(range(1, len(case.steps) + 1))


def test_load_cases_source_filter():
    """source 过滤与非法参数行为。"""
    tau_cases = ab.load_cases(source="tau")
    magentic_cases = ab.load_cases(source="magentic")
    assert len(tau_cases) == TAU_CASE_COUNT
    assert len(magentic_cases) == MAGENTIC_CASE_COUNT
    assert all(case.source == "tau" for case in tau_cases)
    assert all(case.source == "magentic" for case in magentic_cases)
    with pytest.raises(ValueError):
        ab.load_cases(source="flash")
    with pytest.raises(ValueError):
        ab.load_cases(seed=True)


def test_tau_task_and_steps_mapping(cases_by_id, tau_dataset):
    """tau：task 来自 info.task.instruction；traj 原样映射为 1-based steps。"""
    case = cases_by_id["tau-2"]
    raw = tau_dataset[2]
    assert case.task == raw["info"]["task"]["instruction"]
    assert len(case.steps) == len(raw["traj"])
    for step, raw_message in zip(case.steps, raw["traj"]):
        assert step.raw_message == raw_message
        assert step.step_index == raw_message["index"]  # 1-based 对齐
        assert step.role == raw_message["role"]
        assert step.content == raw_message.get("content")
    # 允许 assistant 空内容（仅携带 tool_calls）
    empty_assistant = [
        step for step in case.steps if step.role == "assistant" and step.content is None
    ]
    assert empty_assistant, "tau-2 应包含 content=null 的 assistant tool_calls 消息"
    assert all(step.raw_message.get("tool_calls") for step in empty_assistant)


def test_tau_all_cases_step_alignment(cases, tau_dataset):
    """tau 全部案例：每个失败步骤映射到真实存在的 1-based 消息，且与 traj.index 一致。"""
    tau_cases = [case for case in cases if case.source == "tau"]
    for case in tau_cases:
        raw = tau_dataset[int(case.case_id.removeprefix("tau-"))]
        assert len(case.steps) == len(raw["traj"])
        for step_index in case.failure_steps:
            raw_message = raw["traj"][step_index - 1]
            assert raw_message["index"] == step_index
            assert case.steps[step_index - 1].raw_message == raw_message


def test_magentic_task_and_steps_mapping(cases_by_id, magentic_ground_truth):
    """magentic：task 取第一条 human 消息；UUID 文件原样映射为 1-based steps。"""
    trajectory_id = "5f982798-16b9-4051-ab57-cfc7ebdb2a91"
    case = cases_by_id[f"magentic-{trajectory_id}"]
    raw_messages = _load_json(MAGENTIC_TRAJ_DIR / f"{trajectory_id}.json")
    first_human = next(
        message["content"]
        for message in raw_messages
        if str(message.get("role", "")).strip().lower() == "human"
    )
    assert case.task == first_human
    assert len(case.steps) == len(raw_messages)
    assert case.root_cause_step == 33
    assert case.root_cause_failure_id == 3
    assert case.root_cause_category == "Invention of new information"
    assert case.failed_agent == "FileSurfer"
    assert case.failure_steps == [13, 17, 33]
    for step, raw_message in zip(case.steps, raw_messages):
        assert step.raw_message == raw_message
        assert step.role == raw_message["role"]
        assert step.content == raw_message["content"]


def test_label_step_alignment_with_ground_truth(
    cases, tau_ground_truth, magentic_ground_truth
):
    """root cause / failure step 与原始 ground truth 完全对齐。"""
    tau_cases = [case for case in cases if case.source == "tau"]
    for case in tau_cases:
        raw_gt = tau_ground_truth[case.case_id.removeprefix("tau-")]
        _assert_case_labels_match_ground_truth(case, raw_gt)
    magentic_cases = [case for case in cases if case.source == "magentic"]
    for case in magentic_cases:
        raw_gt = magentic_ground_truth[case.case_id.removeprefix("magentic-")]
        _assert_case_labels_match_ground_truth(case, raw_gt)


def _assert_case_labels_match_ground_truth(case, raw_gt):
    failures = sorted(
        raw_gt["failures"], key=lambda item: (item["step_number"], str(item["failure_id"]))
    )
    assert case.failure_steps == [failure["step_number"] for failure in failures]
    assert case.failure_categories == [failure["failure_category"] for failure in failures]
    assert case.failure_ids == [failure["failure_id"] for failure in failures]
    assert case.failure_agents == [failure["failed_agent"] for failure in failures]
    root_failure = next(
        failure
        for failure in failures
        if str(failure["failure_id"]) == str(raw_gt["root_cause"]["failure_id"])
    )
    assert case.root_cause_step == root_failure["step_number"]
    assert str(case.root_cause_failure_id) == str(raw_gt["root_cause"]["failure_id"])
    assert case.root_cause_category == root_failure["failure_category"]
    assert case.failed_agent == root_failure["failed_agent"]
    assert case.root_cause_reason == raw_gt["root_cause"]["reason_for_root_cause"]


def test_root_cause_step_in_range_and_categories_aligned(cases):
    """所有案例：根因步骤合法，failure_steps/categories 严格对齐且在范围内。"""
    assert len(cases) == TOTAL_CASE_COUNT
    for case in cases:
        assert 1 <= case.root_cause_step <= len(case.steps)
        assert len(case.failure_steps) == len(case.failure_categories)
        assert len(case.failure_steps) == len(case.failure_ids)
        assert len(case.failure_steps) == len(case.failure_agents)
        assert case.failure_steps == sorted(case.failure_steps)
        assert all(1 <= step <= len(case.steps) for step in case.failure_steps)
        root_index = case.failure_steps.index(case.root_cause_step)
        assert case.failure_categories[root_index] == case.root_cause_category
        assert case.failed_agent in case.failure_agents
    # 上游 failure_id 类型不一致的已知案例："1" vs 1，必须仍正确解析根因
    quirk = next(
        case
        for case in cases
        if case.case_id == "magentic-08cae58d-4084-4616-b6dd-dd6534e4825b"
    )
    assert quirk.root_cause_failure_id == "1"
    assert quirk.root_cause_step == 5
    assert quirk.root_cause_category == "Instruction/Plan Adherence Failure"
    assert quirk.failed_agent == "WebSurfer"


def test_failed_agent_alignment_with_message_roles(
    cases, tau_ground_truth, magentic_ground_truth
):
    """失败步骤的 failed_agent 与原始消息 role / agent 基本一致（映射有效的证据）。"""
    matched = 0
    total = 0
    for case in cases:
        raw_gt = (
            tau_ground_truth[case.case_id.removeprefix("tau-")]
            if case.source == "tau"
            else magentic_ground_truth[case.case_id.removeprefix("magentic-")]
        )
        for failure in raw_gt["failures"]:
            total += 1
            step = case.steps[failure["step_number"] - 1]
            failed_agent = failure["failed_agent"].lower()
            role = step.role.lower()
            agent = step.agent.lower()
            if (
                failed_agent in role
                or role in failed_agent
                or failed_agent in agent
                or agent in failed_agent
            ):
                matched += 1
    assert total == TOTAL_FAILURE_COUNT
    # 上游标注与消息 role 的 3 处已知错位：magentic 2 处（FileSurfer/Assistant
    # vs Orchestrator）+ tau 1 处（Assistant 标注落在 user 消息上）
    assert matched == TOTAL_FAILURE_COUNT - 3
    assert matched / total >= 0.99


# ---------------------------------------------------------------------------
# 3. 划分：稳定、互斥、train/test 隔离
# ---------------------------------------------------------------------------


def test_split_stable_and_disjoint(cases):
    """seed=42 的 dev/test 稳定、互斥、并集完整（train/test 严格隔离）。"""
    groups = ab.split_cases(cases)
    assert set(groups) == {"dev", "test"}
    dev_ids = [case.case_id for case in groups["dev"]]
    test_ids = [case.case_id for case in groups["test"]]
    assert len(dev_ids) == SPLIT_COUNTS_SEED_42["dev"]
    assert len(test_ids) == SPLIT_COUNTS_SEED_42["test"]
    assert set(dev_ids).isdisjoint(test_ids)
    assert sorted(dev_ids + test_ids) == sorted(case.case_id for case in cases)

    # 重复调用 / 重新加载必须完全一致
    again = ab.split_cases(ab.load_cases(seed=42), seed=42)
    assert [case.case_id for case in again["dev"]] == dev_ids
    assert [case.case_id for case in again["test"]] == test_ids

    # 每个案例的 split 字段与 split_cases 分组一致
    for case in cases:
        assert ab.split_of(case.case_id, 42) == case.split
        assert (case.case_id in dev_ids) == (case.split == "dev")

    # 每组约 50/50，且按来源也接近均分
    for source in ab.SOURCES:
        source_cases = [case for case in cases if case.source == source]
        source_groups = ab.split_cases(source_cases)
        source_dev = len(source_groups["dev"])
        source_test = len(source_groups["test"])
        assert abs(source_dev - source_test) <= 3
        assert abs(source_dev / len(source_cases) - 0.5) <= 0.1


def test_split_seed_changes_assignment(cases):
    """不同 seed 产生不同划分；同一 seed 恒定。"""
    groups_42 = ab.split_cases(cases, seed=42)
    groups_43 = ab.split_cases(cases, seed=43)
    ids_42 = {case.case_id for case in groups_42["dev"]}
    ids_43 = {case.case_id for case in groups_43["dev"]}
    assert ids_42 != ids_43
    assert {case.case_id for case in groups_43["dev"]}.isdisjoint(
        {case.case_id for case in groups_43["test"]}
    )
    assert ab.split_of("tau-2", 42) == ab.split_of("tau-2", 42)
    with pytest.raises(ValueError):
        ab.split_of("")
    with pytest.raises(ValueError):
        ab.split_of("tau-2", True)


def test_summarize_counts(cases):
    """summarize() 的数量、步骤与标签分布符合子集事实。"""
    summary = ab.summarize(cases)
    assert summary["benchmark"] == "agentrx"
    assert summary["total_cases"] == TOTAL_CASE_COUNT
    assert summary["total_steps"] == TOTAL_STEP_COUNT
    assert summary["by_source"] == {"tau": TAU_CASE_COUNT, "magentic": MAGENTIC_CASE_COUNT}
    assert summary["by_domain"] == {"tau_retail": TAU_CASE_COUNT, "magentic_one": MAGENTIC_CASE_COUNT}
    assert summary["by_split"] == SPLIT_COUNTS_SEED_42
    assert summary["by_source_split"] == {
        "tau": {"dev": 15, "test": 14},
        "magentic": {"dev": 23, "test": 21},
    }
    assert summary["step_count"]["min"] == 5
    assert summary["step_count"]["max"] == 130
    assert summary["step_count"]["mean"] == 44.73
    assert summary["total_failures"] == TOTAL_FAILURE_COUNT
    assert sum(summary["failure_categories"].values()) == TOTAL_FAILURE_COUNT
    assert sum(summary["root_cause_categories"].values()) == TOTAL_CASE_COUNT
    assert sum(summary["root_cause_agents"].values()) == TOTAL_CASE_COUNT
    assert summary["split_method"] == ab.SPLIT_METHOD
    assert summary["read_only"] is True


# ---------------------------------------------------------------------------
# 4. 标签隔离：序列化后不含 root_cause / failure 标签
# ---------------------------------------------------------------------------


def test_case_without_labels_has_no_label_fields(cases):
    """case_without_labels() 递归 key 与 JSON 文本中都不含评测标签。"""
    label_fields = set(ab.LABEL_FIELDS)
    assert label_fields
    for case in cases:
        payload = ab.case_without_labels(case)
        assert payload["label_free"] is True
        assert payload["case_id"] == case.case_id
        assert payload["task"] == case.task
        len_steps = len(payload["steps"])
        assert len_steps == len(case.steps)
        assert not (_collect_keys(payload) & label_fields)
        dumped = json.dumps(payload, ensure_ascii=False)
        assert "root_cause" not in dumped
        for field in label_fields:
            assert f'"{field}"' not in dumped


def test_trace_record_has_no_label_fields(cases, traces):
    """to_trace_record() 序列化后不含 root_cause / failure 标签。"""
    label_fields = set(ab.LABEL_FIELDS)
    for case in cases:
        record = traces[case.case_id]
        payload = record.model_dump(mode="json")
        assert not (_collect_keys(payload) & label_fields)
        dumped = json.dumps(payload, ensure_ascii=False)
        assert "root_cause" not in dumped
        for field in label_fields:
            assert f'"{field}"' not in dumped
        # 允许在 metadata 中记录「标签已排除」的策略说明，但不得携带标签值
        assert record.metadata["labels_excluded"] is True
        assert "root_cause" not in json.dumps(record.metadata, ensure_ascii=False)


def test_case_labels_exposes_ground_truth(cases_by_id):
    """评测标签只通过 case_labels() 显式暴露，且与 case 字段一致。"""
    case = cases_by_id["tau-2"]
    labels = ab.case_labels(case)
    assert set(labels) == set(ab.LABEL_FIELDS) | {"case_id"}
    assert labels["root_cause_step"] == 7
    assert labels["root_cause_failure_id"] == 2
    assert labels["root_cause_category"] == "Misinterpretation of Tool Output"
    assert labels["failed_agent"] == "Assistant"
    assert labels["failure_steps"] == [3, 7]
    assert labels["failure_categories"] == [
        "Instruction Adherence Failure",
        "Misinterpretation of Tool Output",
    ]
    json.dumps(labels, ensure_ascii=False)  # 可 JSON 序列化


# ---------------------------------------------------------------------------
# 5. TraceRecord 转换：Pydantic 校验 / 原始消息保留 / 不伪造工具调用
# ---------------------------------------------------------------------------


def test_to_trace_record_validates_as_trace_record(cases, traces):
    """全部 73 条案例转换结果都能通过 TraceRecord 校验。"""
    assert len(traces) == TOTAL_CASE_COUNT
    for case in cases:
        record = traces[case.case_id]
        validated = TraceRecord.model_validate(record.model_dump(mode="json"))
        assert isinstance(validated, TraceRecord)
        assert validated.trace_id == record.trace_id
        assert validated.trace_id.startswith("trace-agentrx-")
        assert re.fullmatch(r"trace-agentrx-[0-9a-f]{16}", validated.trace_id)
        assert validated.task_id == case.case_id
        assert validated.run_id.startswith("run-agentrx-")
        assert validated.status is TraceStatus.FAILED
        assert validated.started_at.tzinfo is not None
        assert validated.ended_at is not None and validated.ended_at.tzinfo is not None
        assert len(validated.steps) == len(case.steps)
        step_ids = [step.step_id for step in validated.steps]
        assert len(step_ids) == len(set(step_ids))
        assert {step.step_type for step in validated.steps} <= STEP_TYPE_ALLOWED
        for position, step in enumerate(validated.steps):
            assert step.trace_id == validated.trace_id
            assert step.status is StepStatus.SUCCEEDED
            expected_parents = [] if position == 0 else [validated.steps[position - 1].step_id]
            assert step.parent_step_ids == expected_parents
        # trace metadata 标注 benchmark 来源、原始 case_id、许可
        assert validated.metadata["benchmark"] == "agentrx"
        assert validated.metadata["benchmark_source"] == case.source
        assert validated.metadata["original_case_id"] == case.case_id
        assert validated.metadata["license"] == "MIT"
        assert validated.metadata["upstream_repo"] == ab.UPSTREAM_REPO_URL
        assert validated.metadata["upstream_commit"] == ab.UPSTREAM_COMMIT
        assert validated.metadata["read_only"] is True


def test_to_trace_record_preserves_raw_messages(cases, traces):
    """TraceStep 的 input / metadata["raw_message"] 与原始消息逐字一致。"""
    for case in cases:
        record = traces[case.case_id]
        for benchmark_step, trace_step in zip(case.steps, record.steps):
            assert trace_step.input == benchmark_step.raw_message
            assert trace_step.metadata["raw_message"] == benchmark_step.raw_message
            assert trace_step.metadata["original_step_index"] == benchmark_step.step_index
            assert trace_step.metadata["original_role"] == benchmark_step.role
            assert trace_step.output == benchmark_step.content


def test_to_trace_record_no_fabricated_tool_calls(cases, traces):
    """只有原始数据真实存在 tool_calls / tool 消息时才出现 TOOL_CALL。"""
    tau_tool_steps = 0
    magentic_steps = 0
    for case in cases:
        record = traces[case.case_id]
        for benchmark_step, trace_step in zip(case.steps, record.steps):
            raw = benchmark_step.raw_message
            genuine_tool = (
                benchmark_step.role.strip().lower() == "tool"
                or bool(raw.get("tool_calls"))
            )
            if trace_step.step_type is StepType.TOOL_CALL:
                assert genuine_tool, (
                    f"{case.case_id} step {benchmark_step.step_index} 被标记为 tool_call "
                    "但原始消息没有工具调用"
                )
            if case.source == "magentic":
                # Magentic-One 消息是 Agent 对话，不应被伪造成工具调用
                assert trace_step.step_type is not StepType.TOOL_CALL
                magentic_steps += 1
            if case.source == "tau" and trace_step.step_type is StepType.TOOL_CALL:
                tau_tool_steps += 1
    assert tau_tool_steps == 568
    # Magentic-One 共 2201 条消息，全部不能被标记为工具调用
    assert magentic_steps == 2201


def test_to_trace_record_deterministic(traces, cases_by_id):
    """同一案例重复转换得到完全一致的 trace_id 与序列化结果。"""
    case = cases_by_id["tau-2"]
    first = ab.to_trace_record(case)
    second = ab.to_trace_record(case)
    assert first.trace_id == second.trace_id == traces[case.case_id].trace_id
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    trace_ids = {record.trace_id for record in traces.values()}
    assert len(trace_ids) == TOTAL_CASE_COUNT
