"""运行时各分类的稳定取值；只依赖标准库，不引用任何执行或持久化实现。"""

from enum import Enum, StrEnum
from dataclasses import dataclass


class CommandStatus(StrEnum):
    """说明一次命令的处理结果，供调用者决定如何继续。"""

    ACCEPTED = "accepted"
    NOT_FOUND = "not_found"
    CLOSED = "closed"
    STALE = "stale"
    TOO_LATE = "too_late"
    INVALID = "invalid"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """返回命令是否完成规定的受理操作，执行结果随后单独交付。

    Attributes:
        status (CommandStatus): 消息保存、决定保存或控制请求的处理结果。
        context_id (str | None): 实际目标上下文，创建成功时为新子编号。
        run_id (str | None): 本次操作关联的轮次。
        detail (str | None): 拒绝或失败的具体说明。
    """

    status: CommandStatus
    context_id: str | None = None
    run_id: str | None = None
    detail: str | None = None

    @property
    def accepted(self) -> bool:
        """从命令处理结果推导是否受理。

        Returns:
            bool: 规定操作已完成时为 True。
        """
        return self.status == CommandStatus.ACCEPTED


class ExecutionPhase(StrEnum):
    """表示上下文执行、等待、最终保存及保存故障的互斥阶段。"""

    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    STOPPING = "stopping"
    COMMITTING = "committing"
    FAULTED = "faulted"


class SessionState(StrEnum):
    """表达本会话对后续输入的执行政策，执行进度由 AgentExecution 管理。"""

    PAUSED = "paused"
    ENABLED = "enabled"
    CLOSED = "closed"


class TaskExecutionPhase(StrEnum):
    """表示一项普通任务从运行到结果保存结束的进展。"""

    RUNNING = "running"
    STOPPING = "stopping"
    COMMITTING = "committing"
    FINISHED = "finished"
    SAVE_FAILED = "save_failed"


@dataclass(frozen=True, slots=True)
class ApprovalWait:
    """标识已保存、等待用户决定的审批。

    Attributes:
        request_id (str): 审批记录编号，恢复时必须匹配。
    """

    request_id: str


@dataclass(frozen=True, slots=True)
class ParentMessageWait:
    """标识子执行已保存、等待父消息的请求。

    Attributes:
        request_id (str): 父消息等待记录编号。
    """

    request_id: str


ExecutionWait = ApprovalWait | ParentMessageWait


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """提供执行状态的只读副本，供查询和重连展示。

    Attributes:
        context_id (str): 实际 Agent 上下文编号。
        run_id (str | None): 当前轮次，空闲时为空。
        phase (ExecutionPhase): 采样时的执行阶段。
        wait (ExecutionWait | None): WAITING 对应的互斥等待材料。
        error (str | None): FAULTED 对应的保存故障说明。
    """

    context_id: str
    run_id: str | None
    phase: ExecutionPhase
    wait: ExecutionWait | None = None
    error: str | None = None


class AgentRunOutcomeKind(StrEnum):
    """标记执行完成、引导、停止、等待、业务失败或必需保存失败。"""
    COMPLETED = "completed"
    STEERED = "steered"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    SAVE_FAILED = "save_failed"


@dataclass(frozen=True)
class AgentRunOutcome:
    """交还本段 Agent 执行的正文或错误，由父子调度流程决定后续工作。

    Attributes:
        kind (AgentRunOutcomeKind): 完成、引导、停止、等待、业务失败或保存失败。
        result (str): 已保存的执行结果正文，尚未产生正文时为空。
        final_message_id (str | None): 正常完成时最终助手消息的编号。
        error (str | None): 执行或装配失败的具体原因。
        context_id (str | None): 执行归属的实际上下文编号。
        run_id (str | None): 本段执行关联的轮次，审批恢复时沿用。
        wait (ExecutionWait | None): 等待结果携带的已保存请求。
    """
    kind: AgentRunOutcomeKind
    result: str = ""
    final_message_id: str | None = None
    error: str | None = None
    context_id: str | None = None
    run_id: str | None = None
    wait: ExecutionWait | None = None


class CheckpointType(StrEnum):
    """区分仍可恢复执行和已经结束的会话快照。

    Attributes:
        INTERMEDIATE (CheckpointType): 保存执行进度，启动时检查恢复。
        COMPLETED (CheckpointType): 保存已结束的轮次。
    """

    INTERMEDIATE = "intermediate"
    COMPLETED = "completed"




# 工具续跑的内部授权标记不是用户决定，不能加入 ApprovalDecisionKind。
AUTHORIZATION_GRANTED_KIND = "authorization_granted"


class RuntimePhase(str, Enum):
    """表示后端生命周期，控制新命令、任务激活和桌面执行入口。

    Attributes:
        INITIALIZING (RuntimePhase): 持久清理尚未完成。
        RUNNING (RuntimePhase): 允许本次启动的新执行。
        STOPPING (RuntimePhase): 正在关闭，拒绝新的执行意图。
        STOPPED (RuntimePhase): 资源和持久收尾已经完成。
    """
    INITIALIZING = "initializing"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


class SessionPhase(str, Enum):
    """表示现有界面展示阶段，由会话运行事实推导。

    Attributes:
        IDLE (SessionPhase): 空闲，协议值为 idle。
        RUNNING (SessionPhase): 轮次运行中，协议值为 running。
        PENDING_INTERRUPT (SessionPhase): 等待人工审批，协议值为 pending_interrupt。
    """
    IDLE = "idle"
    RUNNING = "running"
    PENDING_INTERRUPT = "pending_interrupt"


class TurnInterruptReason(str, Enum):
    """表示当前轮次已受理的打断原因。

    Attributes:
        STOP (TurnInterruptReason): 停止当前轮次，协议值为 stop。
        STEER (TurnInterruptReason): 用新输入引导，协议值为 steer。
    """
    STOP = "stop"
    STEER = "steer"


class CommandKind(str, Enum):
    """表示 Adapter 支持的已知用户命令，入口仍保留未知命令的业务拒绝。

    Attributes:
        HUMAN_TURN (CommandKind): 普通真人输入，协议值为 human_turn。
        HUMAN_STEER (CommandKind): 引导当前轮次，协议值为 human_steer。
        QUEUE_TURN (CommandKind): 排队到下一轮，协议值为 queue_turn。
        STOP_TURN (CommandKind): 停止当前轮次，协议值为 stop_turn。
        RESOLVE_AUTHORIZATION (CommandKind): 处理人工审批，协议值为 resolve_authorization。
        STOP_BACKGROUND_TASK (CommandKind): 停止独立后台任务，协议值为 stop_background_task。
        CANCEL_QUEUED (CommandKind): 撤销排队输入，协议值为 cancel_queued。
    """
    HUMAN_TURN = "human_turn"
    HUMAN_STEER = "human_steer"
    QUEUE_TURN = "queue_turn"
    STOP_TURN = "stop_turn"
    RESOLVE_AUTHORIZATION = "resolve_authorization"
    STOP_BACKGROUND_TASK = "stop_background_task"
    CANCEL_QUEUED = "cancel_queued"
    INTERACT_SUBAGENT = "interact_subagent"


class InteractionEventKind(str, Enum):
    """表示界面可见事件，与 Agent 内部 EventType 通过显式映射衔接。

    Attributes:
        SESSION_SNAPSHOT (InteractionEventKind): 当前会话快照，协议值为 session_snapshot。
        HUMAN_MESSAGE (InteractionEventKind): 已提交真人消息，协议值为 human_message。
        QUEUED_MESSAGE (InteractionEventKind): 输入已排队，协议值为 queued_message。
        QUEUED_CANCELLED (InteractionEventKind): 排队输入已撤销，协议值为 queued_cancelled。
        TASK_STATUS (InteractionEventKind): 后台任务状态，协议值为 task_status。
        NOTICE (InteractionEventKind): 插件短暂前端提示，协议值为 notice。
        STATUS_NOTICE (InteractionEventKind): 插件持续前端提示，协议值为 status_notice。
        RUN_STARTED (InteractionEventKind): 轮次开始或恢复，协议值为 run_started。
        TEXT_CHUNK (InteractionEventKind): 模型正文分片，协议值为 text_chunk。
        TOOL_CALL (InteractionEventKind): 工具调用提示，协议值为 tool_call。
        TOOL_COMPLETED (InteractionEventKind): 工具结果已保存，协议值为 tool_completed。
        INTERRUPT (InteractionEventKind): 待处理审批或执行请求，协议值为 interrupt。
        AUTHORIZATION_RESOLVED (InteractionEventKind): 审批首个有效决定，协议值为 authorization_resolved。
        RUN_COMPLETED (InteractionEventKind): 轮次结束，协议值为 run_completed。
        ERROR (InteractionEventKind): 运行错误，协议值为 error。
    """
    SESSION_SNAPSHOT = "session_snapshot"
    HUMAN_MESSAGE = "human_message"
    QUEUED_MESSAGE = "queued_message"
    QUEUED_CANCELLED = "queued_cancelled"
    TASK_STATUS = "task_status"
    NOTICE = "notice"
    STATUS_NOTICE = "status_notice"
    RUN_STARTED = "run_started"
    TEXT_CHUNK = "text_chunk"
    TOOL_CALL = "tool_call"
    TOOL_COMPLETED = "tool_completed"
    INTERRUPT = "interrupt"
    AUTHORIZATION_RESOLVED = "authorization_resolved"
    RUN_COMPLETED = "run_completed"
    ERROR = "error"
    SUBAGENT_STATUS = "subagent_status"
    SUBAGENT_INTERRUPT = "subagent_interrupt"
    HISTORY_RESTORED = "history_restored"


class InterruptKind(str, Enum):
    """区分真人审批与子等待父指令，二者共用运行中断阶段。

    Attributes:
        AUTHORIZATION_REQUEST (InterruptKind): 人工审批，协议值为 authorization_request。
        PARENT_MESSAGE (InterruptKind): 子等待父指令，协议值为 parent_message。
    """
    AUTHORIZATION_REQUEST = "authorization_request"
    PARENT_MESSAGE = "parent_message"


class ApprovalDecisionKind(str, Enum):
    """表示用户能够提交的审批决定，不包含系统取消通知。

    Attributes:
        APPROVE (ApprovalDecisionKind): 批准，协议值为 approve。
        REJECT (ApprovalDecisionKind): 拒绝，协议值为 reject。
        STEER (ApprovalDecisionKind): 审批框引导，协议值为 steer。
    """
    APPROVE = "approve"
    REJECT = "reject"
    STEER = "steer"


class AuthorizationResolutionKind(str, Enum):
    """表示审批处理结果，取消来自后端而非可提交的用户决定。

    Attributes:
        APPROVE (AuthorizationResolutionKind): 已批准，协议值为 approve。
        REJECT (AuthorizationResolutionKind): 已拒绝，协议值为 reject。
        STEER (AuthorizationResolutionKind): 被审批框引导取代，协议值为 steer。
        CANCELLED (AuthorizationResolutionKind): 被其他输入取消，协议值为 cancelled。
    """
    APPROVE = "approve"
    REJECT = "reject"
    STEER = "steer"
    CANCELLED = "cancelled"


class InboxKind(str, Enum):
    """区分真人输入、任务终态和插件主动通知。

    Attributes:
        HUMAN (InboxKind): 真人输入，协议值为 human。
        TASK_TERMINAL (InboxKind): 任务终态通知，协议值为 task_terminal。
        PLUGIN_NOTIFICATION (InboxKind): 插件主动通知，协议值为 plugin_notification。
    """
    HUMAN = "human"
    TASK_TERMINAL = "task_terminal"
    PLUGIN_NOTIFICATION = "plugin_notification"
    AGENT_COMMAND = "agent_command"
    SUBAGENT = "subagent"
    # 兼容已有数据库中的父回复记录；新父回复按普通 AGENT_COMMAND 交付。
    PARENT_REPLY = "parent_reply"


class InboxStatus(str, Enum):
    """表示 Inbox 输入的领取和原子交付状态。

    Attributes:
        PENDING (InboxStatus): 尚未领取，协议值为 pending。
        DELIVERED (InboxStatus): 已与 checkpoint 原子提交，协议值为 delivered。
    """
    PENDING = "pending"
    DELIVERED = "delivered"


class InboxDeliveryMode(str, Enum):
    """区分立即交付和正常轮次结束后放行的排队输入。

    Attributes:
        IMMEDIATE (InboxDeliveryMode): 立即参与激活，协议值为 immediate。
        QUEUED (InboxDeliveryMode): 等待排队放行，协议值为 queued。
    """
    IMMEDIATE = "immediate"
    QUEUED = "queued"


class TaskStatus(str, Enum):
    """表示任务的持久状态，不与模型输入的汇总终态混用。

    Attributes:
        RUNNING (TaskStatus): 业务执行或结果保存尚未结束，协议值为 running。
        COMPLETED (TaskStatus): 成功结束，协议值为 completed。
        FAILED (TaskStatus): 业务执行失败，协议值为 failed。
        STOPPED (TaskStatus): 请求停止后完成收尾，协议值为 stopped。
        ORPHANED (TaskStatus): 重启后失去执行所有权，协议值为 orphaned。
    """
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    ORPHANED = "orphaned"


class TaskResultStatus(str, Enum):
    """表示交付给 Agent 的任务通知状态，停止映射取消，混合批次单独标记。

    Attributes:
        COMPLETED (TaskResultStatus): 成功终态，协议值为 completed。
        FAILED (TaskResultStatus): 失败终态，协议值为 failed。
        CANCELLED (TaskResultStatus): 任务被停止，协议值为 cancelled。
        ORPHANED (TaskResultStatus): 无法确认真实结果，协议值为 orphaned。
        MIXED (TaskResultStatus): 批次终态不同，协议值为 mixed。
    """
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"
    MIXED = "mixed"
