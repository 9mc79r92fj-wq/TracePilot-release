"""受控故障注入器（第 20 步）。

为 TracePilot 的故障归因实验提供确定性的可控故障，用于构造"带金标准标注"的
失败场景。所有故障均为进程内模拟，绝不修改真实数据、不破坏数据库、不发起网络攻击。

设计原则：
- 确定性：同一条规则 + 相同的目标工具/步骤，必然产生相同的故障（无随机性）；
- 可控：按故障类型 + 目标工具名 / 目标步骤 ID 精确匹配，可关闭（enable/disable）；
- 可记录：配置（get_config）与触发记录（fired）可序列化，用于写入 Trace metadata；
- 区分注入故障与自然故障：注入故障在 Trace 步骤 metadata.fault_injection 中标记。

故障类型与触发阶段：
- 前置抛异常类（before_tool 命中即抛 FaultInjectionError）：
    TOOL_TIMEOUT / TOOL_EXCEPTION / INVALID_ARGUMENTS / PERMISSION_DENIED；
- 后置篡改结果类（after_tool 命中即替换返回结果）：
    INVALID_TOOL_RESULT / EMPTY_RETRIEVAL。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

__all__ = [
    "FaultType",
    "FaultRule",
    "FaultInjectionError",
    "FaultInjector",
]


class FaultType(str, Enum):
    """六种可控故障类型。"""

    TOOL_TIMEOUT = "tool_timeout"
    TOOL_EXCEPTION = "tool_exception"
    INVALID_TOOL_RESULT = "invalid_tool_result"
    EMPTY_RETRIEVAL = "empty_retrieval"
    INVALID_ARGUMENTS = "invalid_arguments"
    PERMISSION_DENIED = "permission_denied"


# 前置（抛异常）与后置（篡改结果）故障分类
_BEFORE_FAULTS = {
    FaultType.TOOL_TIMEOUT,
    FaultType.TOOL_EXCEPTION,
    FaultType.INVALID_ARGUMENTS,
    FaultType.PERMISSION_DENIED,
}
_AFTER_FAULTS = {
    FaultType.INVALID_TOOL_RESULT,
    FaultType.EMPTY_RETRIEVAL,
}

_DEFAULT_MESSAGES = {
    FaultType.TOOL_TIMEOUT: "注入故障：工具执行超时",
    FaultType.TOOL_EXCEPTION: "注入故障：工具抛出异常",
    FaultType.INVALID_TOOL_RESULT: "注入故障：工具返回格式错误",
    FaultType.EMPTY_RETRIEVAL: "注入故障：检索结果为空",
    FaultType.INVALID_ARGUMENTS: "注入故障：工具参数不合法",
    FaultType.PERMISSION_DENIED: "注入故障：越权访问被拒绝",
}


class FaultInjectionError(Exception):
    """注入故障时抛出的异常（携带故障类型，便于区分注入故障与自然故障）。"""

    def __init__(self, fault_type: FaultType, message: str):
        super().__init__(message)
        self.fault_type = fault_type
        self.message = message


@dataclass
class FaultRule:
    """一条故障注入规则。

    - fault_type: 故障类型；
    - target_tool: 目标工具名（None 表示不限工具）；
    - target_step: 目标步骤 ID（None 表示不限步骤）；
    - count: 该规则最多触发次数（确定性：每命中一次 remaining 减 1）。
    """

    fault_type: FaultType
    target_tool: str | None = None
    target_step: str | None = None
    message: str | None = None
    count: int = 1
    remaining: int = field(init=False)
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("count 必须 >= 1")
        self.remaining = self.count


class FaultInjector:
    """规则驱动的确定性故障注入器。"""

    def __init__(self, rules: list[FaultRule] | None = None) -> None:
        self._rules: list[FaultRule] = list(rules or [])
        self.enabled: bool = True
        self.fired: list[dict[str, Any]] = []  # 已触发记录，供 Trace 关联

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def add_rule(
        self,
        fault_type: FaultType,
        *,
        target_tool: str | None = None,
        target_step: str | None = None,
        message: str | None = None,
        count: int = 1,
    ) -> FaultRule:
        rule = FaultRule(fault_type, target_tool, target_step, message, count)
        self._rules.append(rule)
        return rule

    def disable(self) -> None:
        self.enabled = False

    def enable(self) -> None:
        self.enabled = True

    def get_config(self) -> list[dict[str, Any]]:
        """返回当前规则配置（可序列化，用于写入 Trace metadata）。"""
        return [
            {
                "fault_type": r.fault_type.value,
                "target_tool": r.target_tool,
                "target_step": r.target_step,
                "count": r.count,
                "remaining": r.remaining,
                "enabled": r.enabled,
            }
            for r in self._rules
        ]

    # ------------------------------------------------------------------
    # 匹配与触发
    # ------------------------------------------------------------------

    def _match(self, fault_type: FaultType, tool_name: str, step_id: str) -> FaultRule | None:
        if not self.enabled:
            return None
        for rule in self._rules:
            if not rule.enabled or rule.remaining <= 0:
                continue
            if rule.fault_type != fault_type:
                continue
            if rule.target_tool is not None and rule.target_tool != tool_name:
                continue
            if rule.target_step is not None and rule.target_step != step_id:
                continue
            return rule
        return None

    def _record(self, rule: FaultRule, fault_type: FaultType, tool_name: str, step_id: str, message: str) -> dict:
        rule.remaining -= 1
        fired = {
            "fault_type": fault_type.value,
            "target_tool": tool_name,
            "target_step": step_id,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.fired.append(fired)
        return fired

    # ------------------------------------------------------------------
    # 触发接口
    # ------------------------------------------------------------------

    def before_tool(self, tool_name: str, step_id: str) -> None:
        """工具执行前检查前置类故障；命中则抛出 FaultInjectionError。"""
        for fault_type in _BEFORE_FAULTS:
            rule = self._match(fault_type, tool_name, step_id)
            if rule is not None:
                message = rule.message or _DEFAULT_MESSAGES[fault_type]
                self._record(rule, fault_type, tool_name, step_id, message)
                raise FaultInjectionError(fault_type, message)

    def after_tool(self, tool_name: str, step_id: str, result: Any) -> tuple[Any, dict | None]:
        """工具执行后检查后置类故障；命中则返回 (篡改后的结果, 故障记录)。

        Returns:
            (result, fault_info)：fault_info 为命中故障的记录 dict，未命中为 None。
        """
        for fault_type in _AFTER_FAULTS:
            rule = self._match(fault_type, tool_name, step_id)
            if rule is not None:
                message = rule.message or _DEFAULT_MESSAGES[fault_type]
                fired = self._record(rule, fault_type, tool_name, step_id, message)
                return _corrupt_result(fault_type), fired
        return result, None


def _corrupt_result(fault_type: FaultType) -> Any:
    """按故障类型返回一个被篡改的结果。"""
    if fault_type == FaultType.INVALID_TOOL_RESULT:
        return {"__fault__": "invalid_tool_result", "payload": "not-a-valid-result-shape"}
    if fault_type == FaultType.EMPTY_RETRIEVAL:
        return []
    return None
