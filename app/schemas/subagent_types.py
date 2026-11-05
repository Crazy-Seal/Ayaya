"""子 Agent 定义、公开状态及交互记录；不引用执行器或持久化对象。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.schemas.tool_identity import ToolIdentity

SUBAGENT_ROSTER_CONTEXT_KEY = "core.subagents"
SUBAGENT_PARENT_TOOL_NAMES = frozenset({"create_subagent", "interact_subagent", "get_subagent_details"})
SUBAGENT_MESSAGE_TOOL_NAME = "message_parent"


class SubagentAction(StrEnum):
    """父 Agent 或用户对目标子身份执行的操作。"""
    COMMAND = "command"
    STOP = "stop"
    DESTROY = "destroy"


class SubagentPublicStatus(StrEnum):
    """父模型可以观察的状态，审批在这里映射为正在运行。"""
    IDLE = "idle"
    RUNNING = "running"
    WAITING_PARENT = "waiting_parent"


class SubagentCommandEffect(StrEnum):
    """受理回执中已经采取的动作，与实时运行状态分开表达。"""
    STARTED = "started"
    STEERED = "steered"
    REPLIED = "replied"
    STOPPING = "stopping"
    IDLE = "idle"
    RELEASED = "released"
    FINALIZING = "finalizing"


class SubagentInteractionKind(StrEnum):
    """已受理或发出的父子显式消息类型。"""
    TASK = "task"
    STEER = "steer"
    REPLY = "reply"
    A2A = "a2a"
    FINAL = "final"


class TaskRecordKind(StrEnum):
    """区分普通后台任务和由 Scheduler 管理的子执行结果记录。"""
    ORDINARY = "ordinary"
    SUBAGENT = "subagent"


@dataclass(frozen=True)
class SubagentDefinition:
    """保存稳定子身份和创建选项。

    Attributes:
        subagent_id (str): 子上下文及后续管理操作共用的身份。
        session_id (str): 父会话的配置、权限与 MCP 所属身份。
        name (str): 父模型和用户界面阅读的名称。
        system_prompt (str): 父 Agent 指定的系统提示词。
        tool_whitelist (frozenset[ToolIdentity]): 四类普通工具的可见范围。
        if_retain_context (bool): 结束后是否保留上下文以供再次委派。
        created_at (datetime): UTC 创建时间。
    """
    subagent_id: str
    session_id: str
    name: str
    system_prompt: str
    tool_whitelist: frozenset[ToolIdentity]
    if_retain_context: bool
    created_at: datetime


@dataclass(frozen=True)
class SubagentRecord:
    """保存子定义和唯一的废弃时间。"""
    definition: SubagentDefinition
    retired_at: datetime | None = None

    @property
    def context_available(self) -> bool:
        """返回原子上下文是否仍能受派，销毁中的身份也不接收新委派。"""
        return self.retired_at is None


@dataclass(frozen=True)
class SubagentSummary:
    """从定义和实际 Scheduler 状态生成一次查询摘要。

    Attributes:
        subagent_id (str): 子身份。
        name (str): 展示名称。
        if_retain_context (bool): 可复用或一次性。
        status (SubagentPublicStatus): 父模型读取的空闲、运行或等待父回复状态。
        started_at (str | None): 本次活动任务开始时间，空闲时为空。
        created_at (str | None): 子身份创建时间。
        waiting_for_approval (bool): 仅供界面显示人类审批，环境文本仍映射为运行中。
        error (str | None): 正在由后台自动重试的资源清理失败原因。
    """
    subagent_id: str
    name: str
    if_retain_context: bool
    status: SubagentPublicStatus
    started_at: str | None = None
    created_at: str | None = None
    waiting_for_approval: bool = False
    error: str | None = None


@dataclass(frozen=True)
class SubagentInteraction:
    """保存一条确实受理的父子交互，不包含内部工具过程。"""
    interaction_id: int
    subagent_id: str
    kind: SubagentInteractionKind
    text: str
    created_at: datetime
    source_id: str
