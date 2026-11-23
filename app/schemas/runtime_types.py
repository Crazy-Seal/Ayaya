"""定义会话执行、命令回执、界面事件和持久化记录共用的状态与数据契约。"""

from enum import Enum, StrEnum
from dataclasses import dataclass


class CommandStatus(StrEnum):
    """说明用户或 Agent 提交命令后的受理结果，供调用者决定后续操作。

    Attributes:
        ACCEPTED (CommandStatus): 已完成该命令要求的输入保存、决定保存或控制请求受理。
        NOT_FOUND (CommandStatus): 指定的任务或子智能体等目标不存在。
        CLOSED (CommandStatus): 运行时、会话或子智能体已关闭受理入口。
        STALE (CommandStatus): 目标轮次、等待请求或当前执行条件已失效。
        TOO_LATE (CommandStatus): 当前执行已进入无法受理该控制请求的阶段。
        INVALID (CommandStatus): 命令种类、参数或操作条件无效。
        FAILED (CommandStatus): 保存输入、保存决定或处理命令时失败。
    """

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
        context_id (str | None): 实际目标上下文编号，创建子智能体成功时为新子智能体编号。
        run_id (str | None): 本次操作关联的轮次编号，尚未关联轮次时为空。
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
    """表示一个父会话或子智能体上下文当前所处的执行阶段。

    Attributes:
        IDLE (ExecutionPhase): 当前没有执行轮次，可以开始处理已受理的输入。
        RUNNING (ExecutionPhase): 正在准备或执行当前轮次的模型和工具调用。
        WAITING (ExecutionPhase): 已保存审批或父消息请求，等待对应决定或输入。
        STOPPING (ExecutionPhase): 已受理停止或引导，正在结束当前执行并整理工具结果。
        COMMITTING (ExecutionPhase): 正在最终保存本轮结果，完成后结束或切换轮次。
        FAULTED (ExecutionPhase): 必需保存失败，等待恢复最后成功保存的上下文。
    """

    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    STOPPING = "stopping"
    COMMITTING = "committing"
    FAULTED = "faulted"


class SessionState(StrEnum):
    """决定父会话或子智能体能否继续处理输入，执行进度由 AgentExecution 管理。

    Attributes:
        PAUSED (SessionState): 暂停处理输入，等待明确操作重新启用。
        ENABLED (SessionState): 已获准处理可交付输入，新通知可唤醒会话继续工作。
        CLOSED (SessionState): 会话已进入关闭流程，结束执行并释放所属资源。
    """

    PAUSED = "paused"
    ENABLED = "enabled"
    CLOSED = "closed"


class TaskExecutionPhase(StrEnum):
    """表示一项普通后台任务从业务运行到结果保存结束的进展。

    Attributes:
        RUNNING (TaskExecutionPhase): 任务已建立执行对象，业务尚未结束。
        STOPPING (TaskExecutionPhase): 已受理停止请求，等待业务结束。
        COMMITTING (TaskExecutionPhase): 业务已结束，正在保存结果与任务终态。
        FINISHED (TaskExecutionPhase): 任务终态已保存，等待方可以读取结果。
        SAVE_FAILED (TaskExecutionPhase): 结果保存失败，内存中保留业务结果和保存错误。
    """

    RUNNING = "running"
    STOPPING = "stopping"
    COMMITTING = "committing"
    FINISHED = "finished"
    SAVE_FAILED = "save_failed"


@dataclass(frozen=True, slots=True)
class ApprovalWait:
    """标识已保存、等待用户决定的审批。

    Attributes:
        request_id (str): 已保存的审批记录编号，提交决定和恢复原轮次时用于匹配请求。
    """

    request_id: str


@dataclass(frozen=True, slots=True)
class ParentMessageWait:
    """标识子智能体已保存、等待父智能体消息的请求。

    Attributes:
        request_id (str): 子智能体等待父消息时保存的请求编号。
    """

    request_id: str


# 执行进入 WAITING 时，携带已保存的审批请求或等待父智能体消息的请求。
ExecutionWait = ApprovalWait | ParentMessageWait


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """提供执行状态的只读副本，供查询和重连展示。

    Attributes:
        context_id (str): 父会话或子智能体的实际上下文编号。
        run_id (str | None): 当前轮次编号，空闲时为空，保存故障期间保留失败轮次编号。
        phase (ExecutionPhase): 采样时的执行阶段。
        wait (ExecutionWait | None): WAITING 阶段已保存的审批请求或父消息等待请求。
        error (str | None): FAULTED 阶段的保存故障说明，恢复成功后清除。
    """

    context_id: str
    run_id: str | None
    phase: ExecutionPhase
    wait: ExecutionWait | None = None
    error: str | None = None


class AgentRunOutcomeKind(StrEnum):
    """说明本段 Agent 执行如何交还控制权，供所属会话安排后续工作。

    Attributes:
        COMPLETED (AgentRunOutcomeKind): 本轮正常完成，最终回答和结束状态已保存。
        STEERED (AgentRunOutcomeKind): 当前执行因新输入引导而结束，后续输入由会话继续处理。
        STOPPED (AgentRunOutcomeKind): 当前执行因停止请求而结束。
        INTERRUPTED (AgentRunOutcomeKind): 当前执行等待已保存的审批或父消息请求。
        FAILED (AgentRunOutcomeKind): 执行或准备过程失败，失败收尾已保存。
        SAVE_FAILED (AgentRunOutcomeKind): 必需保存或已提交上下文的恢复失败。
    """
    COMPLETED = "completed"
    STEERED = "steered"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    SAVE_FAILED = "save_failed"


@dataclass(frozen=True)
class AgentRunOutcome:
    """交还本段 Agent 执行的结果，由所属父会话或子智能体会话决定后续工作。

    Attributes:
        kind (AgentRunOutcomeKind): 完成、引导、停止、等待、业务失败或持久化故障。
        result (str): 已保存的执行结果正文，尚未产生正文时为空。
        final_message_id (str | None): 正常完成时最终助手消息的编号。
        error (str | None): 执行、准备、保存或恢复失败的具体原因。
        context_id (str | None): 执行归属的实际上下文编号。
        run_id (str | None): 本段执行关联的轮次，审批恢复时沿用。
        wait (ExecutionWait | None): INTERRUPTED 结果关联的已保存审批或父消息等待请求。
    """
    kind: AgentRunOutcomeKind
    result: str = ""
    final_message_id: str | None = None
    error: str | None = None
    context_id: str | None = None
    run_id: str | None = None
    wait: ExecutionWait | None = None


class CheckpointType(StrEnum):
    """区分执行过程中的进度快照和轮次结束快照，供加载与启动清理使用。

    Attributes:
        INTERMEDIATE (CheckpointType): 保存输入、工具进度或等待请求，启动时检查遗留执行。
        COMPLETED (CheckpointType): 保存正常完成、停止、引导或失败收尾后的轮次。
    """

    INTERMEDIATE = "intermediate"
    COMPLETED = "completed"




# 审批通过后，执行管线向工具续跑流程传入此标记，说明本次调用已取得授权。
AUTHORIZATION_GRANTED_KIND = "authorization_granted"


class RuntimePhase(str, Enum):
    """表示后端生命周期，控制新命令、任务激活和桌面执行入口。

    Attributes:
        INITIALIZING (RuntimePhase): 正在初始化资源并清理上次启动遗留的执行记录。
        RUNNING (RuntimePhase): 允许本次启动的新执行。
        STOPPING (RuntimePhase): 正在关闭，拒绝新的执行意图。
        STOPPED (RuntimePhase): 资源和持久收尾已经完成。
    """
    INITIALIZING = "initializing"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


class SessionPhase(str, Enum):
    """表示界面展示的执行阶段，由实际执行阶段和审批等待信息推导。

    Attributes:
        IDLE (SessionPhase): 当前执行空闲或发生保存故障，故障详情由快照单独携带。
        RUNNING (SessionPhase): 当前轮次正在执行、等待父消息、停止收尾或最终保存。
        PENDING_INTERRUPT (SessionPhase): 当前轮次等待人工审批。
    """
    IDLE = "idle"
    RUNNING = "running"
    PENDING_INTERRUPT = "pending_interrupt"


class TurnInterruptReason(str, Enum):
    """表示当前轮次已受理的打断原因。

    Attributes:
        STOP (TurnInterruptReason): 停止当前轮次并保存收尾结果。
        STEER (TurnInterruptReason): 结束当前执行，使用已保存的新输入继续处理。
    """
    STOP = "stop"
    STEER = "steer"


class CommandKind(str, Enum):
    """表示交互入口提交给运行时处理的用户命令。

    Attributes:
        HUMAN_TURN (CommandKind): 保存用户发送的普通消息并启用会话处理。
        HUMAN_STEER (CommandKind): 保存用户的新输入并请求引导当前轮次。
        QUEUE_TURN (CommandKind): 保存排队消息，等待当前轮次正常结束后放行。
        STOP_TURN (CommandKind): 请求停止当前轮次。
        RESOLVE_AUTHORIZATION (CommandKind): 提交父会话或子智能体的人工审批决定。
        STOP_BACKGROUND_TASK (CommandKind): 请求停止独立后台任务。
        CANCEL_QUEUED (CommandKind): 撤销尚未交付的排队输入。
        INTERACT_SUBAGENT (CommandKind): 向指定子智能体发送消息、请求停止或销毁。
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
    """表示界面订阅的运行时事件，由 Agent 内部事件转换或会话管理流程直接产生。

    Attributes:
        SESSION_SNAPSHOT (InteractionEventKind): 当前会话、执行、待处理输入、任务和子智能体的快照。
        HUMAN_MESSAGE (InteractionEventKind): 真人消息已随执行快照保存，通知界面显示并与历史去重。
        QUEUED_MESSAGE (InteractionEventKind): 输入已保存到队列，通知界面更新排队列表。
        QUEUED_CANCELLED (InteractionEventKind): 排队输入已撤销，通知界面移除对应条目。
        TASK_STATUS (InteractionEventKind): 后台任务状态或结果保存情况发生变化。
        NOTICE (InteractionEventKind): 插件发布的短暂界面提示。
        STATUS_NOTICE (InteractionEventKind): 插件发布的持续状态提示。
        RUN_STARTED (InteractionEventKind): 当前轮次开始执行或从审批等待中恢复。
        TEXT_CHUNK (InteractionEventKind): 模型生成的正文分片，供界面流式显示。
        TOOL_CALL (InteractionEventKind): 当前轮次开始调用工具，通知界面显示调用信息。
        TOOL_COMPLETED (InteractionEventKind): 工具结果已保存，通知界面更新调用结果。
        INTERRUPT (InteractionEventKind): 执行已保存审批或父消息等待请求，由调度器安排展示或通知父会话。
        AUTHORIZATION_RESOLVED (InteractionEventKind): 审批决定已保存或待处理审批已由后端取消。
        RUN_COMPLETED (InteractionEventKind): 当前轮次结束，通知界面完成回答展示。
        ERROR (InteractionEventKind): 执行或保存失败，通知界面显示错误。
        SUBAGENT_STATUS (InteractionEventKind): 子智能体摘要更新或已销毁，通知界面更新或移除面板条目。
        SUBAGENT_INTERRUPT (InteractionEventKind): 子智能体的人工审批轮到展示，通过父会话订阅通知界面。
        HISTORY_RESTORED (InteractionEventKind): 保存故障后已恢复最后成功保存的上下文，通知界面同步历史与执行状态。
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
    """区分执行暂停后等待的人工审批和父智能体消息。

    Attributes:
        AUTHORIZATION_REQUEST (InterruptKind): 工具执行前等待用户审批。
        PARENT_MESSAGE (InterruptKind): 子智能体等待父智能体提供后续消息。
    """
    AUTHORIZATION_REQUEST = "authorization_request"
    WAIT_FOR_PARENT = "wait_for_parent"


class ApprovalDecisionKind(str, Enum):
    """表示用户可以在审批界面提交的决定。

    Attributes:
        APPROVE (ApprovalDecisionKind): 批准待执行的工具调用。
        REJECT (ApprovalDecisionKind): 拒绝待执行的工具调用。
        STEER (ApprovalDecisionKind): 通过审批框发送新输入，引导后续执行。
    """
    APPROVE = "approve"
    REJECT = "reject"
    STEER = "steer"


class AuthorizationResolutionKind(str, Enum):
    """表示已保存的审批处理结果，供界面关闭审批提示并展示处理情况。

    Attributes:
        APPROVE (AuthorizationResolutionKind): 用户已批准工具调用。
        REJECT (AuthorizationResolutionKind): 用户已拒绝工具调用。
        STEER (AuthorizationResolutionKind): 用户已通过审批框发送引导输入。
        CANCELLED (AuthorizationResolutionKind): 后端在新输入、停止或关闭收尾时取消待处理审批。
    """
    APPROVE = "approve"
    REJECT = "reject"
    STEER = "steer"
    CANCELLED = "cancelled"


class InboxKind(str, Enum):
    """区分持久 Inbox 中等待交付给父会话或子智能体的输入来源。

    Attributes:
        HUMAN (InboxKind): 用户向父会话发送的普通、引导或排队输入。
        TASK_TERMINAL (InboxKind): 后台任务结束后交付给所属 Agent 的结果通知。
        PLUGIN_NOTIFICATION (InboxKind): 插件主动发送给会话的通知。
        AGENT_COMMAND (InboxKind): 发给子智能体的任务、引导或回复消息，交付时转为真人消息。
        SUBAGENT (InboxKind): 子智能体发给父智能体的消息，交付时保留子智能体身份。
    """
    HUMAN = "human"
    TASK_TERMINAL = "task_terminal"
    PLUGIN_NOTIFICATION = "plugin_notification"
    AGENT_COMMAND = "agent_command"
    SUBAGENT = "subagent"


class InboxStatus(str, Enum):
    """表示 Inbox 输入是否已随执行快照完成持久化交付。

    Attributes:
        PENDING (InboxStatus): 已保存到 Inbox，等待交付；查询待处理输入时保持此状态。
        DELIVERED (InboxStatus): 已加入 Agent 上下文，并与 checkpoint 在同一事务中保存。
    """
    PENDING = "pending"
    DELIVERED = "delivered"


class InboxDeliveryMode(str, Enum):
    """区分立即交付和正常轮次结束后放行的排队输入。

    Attributes:
        IMMEDIATE (InboxDeliveryMode): 会话获准处理输入时，可直接参与下一批交付。
        QUEUED (InboxDeliveryMode): 先等待当前轮次正常结束放行，再参与后续批次交付。
    """
    IMMEDIATE = "immediate"
    QUEUED = "queued"


class TaskStatus(str, Enum):
    """表示任务记录中保存的业务状态，供任务查询和结果通知使用。

    Attributes:
        RUNNING (TaskStatus): 业务执行或结果保存尚未结束。
        COMPLETED (TaskStatus): 任务成功结束，结果已保存。
        FAILED (TaskStatus): 任务业务执行失败，失败结果已保存。
        STOPPED (TaskStatus): 任务响应停止请求后完成收尾，停止结果已保存。
        ORPHANED (TaskStatus): 应用重启后发现遗留运行记录，原执行的真实结果无法确认。
    """
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    ORPHANED = "orphaned"


class TaskResult(str, Enum):
    """表示交付给 Agent 的单项或批量任务结果状态。

    Attributes:
        COMPLETED (TaskResult): 单项任务成功，或批次内任务均成功。
        FAILED (TaskResult): 单项任务失败，或批次内任务均失败。
        CANCELLED (TaskResult): 单项任务被停止，或批次内任务均被停止。
        ORPHANED (TaskResult): 单项任务真实结果无法确认，或批次内任务均处于该状态。
        MIXED (TaskResult): 批次内任务转换后的结果状态不同。
    """
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"
    MIXED = "mixed"
