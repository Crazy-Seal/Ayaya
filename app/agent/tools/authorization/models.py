"""工具授权 Module 的领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from app.agent.tools.base import InterruptEvent, ToolResult
from app.schemas.runtime_types import ApprovalDecisionKind


ToolSource = Literal["local", "mcp"]
AuthorizationTiming = Literal["before_execute", "after_prepare"]


@dataclass(frozen=True)
class ToolIdentity:
    """不依赖模型暴露名称的稳定工具身份。"""

    source: ToolSource
    name: str
    server_id: str | None = None

    @classmethod
    def local(cls, name: str) -> "ToolIdentity":
        return cls(source="local", name=name)

    @classmethod
    def mcp(cls, server_id: str, name: str) -> "ToolIdentity":
        return cls(source="mcp", server_id=server_id, name=name)

    @property
    def stable_key(self) -> str:
        if self.source == "local":
            return f"local:{self.name}"
        if not self.server_id:
            raise ValueError("MCP 工具身份缺少 server_id")
        return f"mcp:{self.server_id}:{self.name}"


@dataclass(frozen=True)
class ToolDescriptor:
    identity: ToolIdentity
    exposed_name: str
    display_name: str
    description: str
    input_schema: dict[str, Any]
    contract_fingerprint: str
    authorization_timing: AuthorizationTiming = "before_execute"
    available: bool = True
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class ToolInvocation:
    session_id: str
    tool_call_id: str
    descriptor: ToolDescriptor
    arguments: dict[str, Any]
    prepared_action: dict[str, Any] | None = None


@dataclass(frozen=True)
class AuthorizationGrant:
    """保存一次已批准调用的授权快照，不允许携带拒绝或引导决定。

    Attributes:
        request_id (str): 原审批请求身份。
        tool_call_id (str): 获准的工具调用身份。
        tool_identity (str): 工具的稳定来源身份。
        arguments_fingerprint (str): 获准参数的摘要。
        contract_fingerprint (str): 工具契约的摘要。
        prepared_action_fingerprint (str | None): 已准备动作的摘要。
        issued_at (str): 授权发放时间。
        decision (Literal[ApprovalDecisionKind.APPROVE]): 仅表示已经批准。
    """
    request_id: str
    tool_call_id: str
    tool_identity: str
    arguments_fingerprint: str
    contract_fingerprint: str
    prepared_action_fingerprint: str | None
    issued_at: str
    decision: Literal[ApprovalDecisionKind.APPROVE] = ApprovalDecisionKind.APPROVE

    def to_dict(self) -> dict[str, Any]:
        """输出可持久化的授权字段，决定始终保持既有字符串值。

        Returns:
            dict[str, Any]: 不含枚举名称的授权快照。
        """
        return {**self.__dict__, "decision": self.decision.value if isinstance(self.decision, ApprovalDecisionKind) else self.decision}


@dataclass(frozen=True)
class Granted:
    grant: AuthorizationGrant | None = None


@dataclass(frozen=True)
class AuthorizationRequired:
    interrupt: InterruptEvent
    resume_state: dict[str, Any] = field(default_factory=dict)


AuthorizationOutcome: TypeAlias = Granted | AuthorizationRequired | ToolResult
