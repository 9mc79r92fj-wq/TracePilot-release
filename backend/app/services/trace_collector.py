"""Agent 执行轨迹采集器（第 16 步）。

把 Agent 执行期间采集的原始步骤（raw step dict）组装为符合 Trace Schema 的
TraceRecord，并可保存为 data/traces/{trace_id}.json。

职责：
- 生成唯一 trace_id / run_id / task_id；
- 记录开始 / 结束时间；
- 用 Pydantic 校验并组装 TraceRecord（TraceStep 逐条 model_validate）；
- 敏感字段脱敏（不记录 API Key、密码等凭据）；
- 保存为独立 JSON 文件（model_dump_json）。

安全约定（与 Trace Schema 一致）：
- 不记录 API Key 等敏感信息（redact 递归脱敏）；
- 不记录模型隐藏 Chain-of-Thought（仅显式 answer 内容）；
- 不保存无关环境变量（采集器从不读取环境变量）。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.app.schemas.trace import (
    SCHEMA_VERSION,
    TraceRecord,
    TraceStatus,
    TraceStep,
)

__all__ = [
    "TraceCollector",
    "redact",
    "new_trace_id",
    "new_run_id",
    "new_task_id",
    "DEFAULT_TRACES_DIR",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TRACES_DIR = PROJECT_ROOT / "data" / "traces"

# 敏感键（凭据类）。刻意不含裸 "token"，避免误伤 token_usage 等正常字段。
_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|authorization|password|passwd|secret|"
    r"access[_-]?token|refresh[_-]?token|private[_-]?key|bearer)",
    re.IGNORECASE,
)
REDACTED = "[REDACTED]"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def new_trace_id() -> str:
    return _new_id("trace")


def new_run_id() -> str:
    return _new_id("run")


def new_task_id() -> str:
    return _new_id("task")


def redact(value: Any) -> Any:
    """递归脱敏：字典键命中敏感模式时，其值替换为 [REDACTED]。"""
    if isinstance(value, dict):
        return {
            (REDACTED if isinstance(k, str) and _SENSITIVE_KEY.search(k) else k): (
                REDACTED if isinstance(k, str) and _SENSITIVE_KEY.search(k) else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class TraceCollector:
    """采集并组装 TraceRecord 的轻量采集器。"""

    def __init__(
        self,
        *,
        task_id: str | None = None,
        trace_id: str | None = None,
        run_id: str | None = None,
        started_at: datetime | None = None,
        traces_dir: str | Path | None = None,
        enable_redaction: bool = True,
    ) -> None:
        self.task_id = task_id or new_task_id()
        self.trace_id = trace_id or new_trace_id()
        self.run_id = run_id or new_run_id()
        self.started_at = (
            started_at if started_at is not None else datetime.now(timezone.utc)
        )
        self.traces_dir = Path(traces_dir) if traces_dir else DEFAULT_TRACES_DIR
        self.enable_redaction = enable_redaction

    def build(
        self,
        raw_steps: list[dict[str, Any]],
        *,
        status: TraceStatus | str,
        ended_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceRecord:
        """用 Pydantic 校验原始步骤并组装 TraceRecord。

        Args:
            raw_steps: 原始步骤 dict 列表（字段见 TraceStep）。
            status: Trace 状态。
            ended_at: 结束时间；None 时取当前 UTC 时间。
            metadata: 追加到 metadata 的自定义键（会被脱敏）。

        Raises:
            pydantic.ValidationError: 任一原始步骤不符合 TraceStep Schema。
        """
        steps: list[TraceStep] = []
        for raw in raw_steps:
            data = redact(raw) if self.enable_redaction else dict(raw)
            steps.append(TraceStep.model_validate(data))

        record = TraceRecord(
            trace_id=self.trace_id,
            task_id=self.task_id,
            run_id=self.run_id,
            started_at=self.started_at,
            ended_at=ended_at if ended_at is not None else datetime.now(timezone.utc),
            status=status,
            steps=steps,
            metadata=redact(
                {
                    "schema_version": SCHEMA_VERSION,
                    **(metadata or {}),
                }
            ),
        )
        return record

    def save(self, record: TraceRecord) -> Path:
        """把 TraceRecord 保存为 data/traces/{trace_id}.json。

        Raises:
            OSError: 目录创建或写入失败时给出明确错误。
        """
        try:
            self.traces_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(f"无法创建轨迹保存目录 {self.traces_dir}: {exc}") from exc

        path = self.traces_dir / f"{record.trace_id}.json"
        try:
            path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        except OSError as exc:
            raise OSError(f"无法写入轨迹文件 {path}: {exc}") from exc
        return path
