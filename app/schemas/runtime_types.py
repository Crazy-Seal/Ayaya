"""运行时各分类的稳定取值；只依赖标准库，不引用任何执行或持久化实现。"""

from enum import Enum


# 固定桌面渠道不是渠道枚举，未来 Adapter 的渠道标识仍可自由注册。
DESKTOP_CHANNEL = "desktop"
SYSTEM_INPUT_ORIGIN = "system"
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
    """表示会话展示与调度阶段，与轮次内部执行阶段分别建模。

    Attributes:
        IDLE (SessionPhase): 空闲，协议值为 idle。
        RUNNING (SessionPhase): 轮次运行中，协议值为 running。
        PENDING_INTERRUPT (SessionPhase): 等待人工审批，协议值为 pending_interrupt。
    """
    IDLE = "idle"
    RUNNING = "running"
    PENDING_INTERRUPT = "pending_interrupt"


class TurnPhase(str, Enum):
    """表示一轮 Agent 的安全点阶段，最终提交后不能再接受打断。

    Attributes:
        IDLE (TurnPhase): 尚未开始执行，协议值为 idle。
        LLM (TurnPhase): 等待模型分片，协议值为 llm。
        TOOL (TurnPhase): 执行工具，协议值为 tool。
        COMMITTING (TurnPhase): 最终持久提交，协议值为 committing。
    """
    IDLE = "idle"
    LLM = "llm"
    TOOL = "tool"
    COMMITTING = "committing"


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


class InteractionEventKind(str, Enum):
    """表示渠道可见事件，与 Agent 内部 EventType 通过显式映射衔接。

    Attributes:
        SESSION_SNAPSHOT (InteractionEventKind): 当前会话快照，协议值为 session_snapshot。
        HUMAN_MESSAGE (InteractionEventKind): 已提交真人消息，协议值为 human_message。
        QUEUED_MESSAGE (InteractionEventKind): 输入已排队，协议值为 queued_message。
        QUEUED_CANCELLED (InteractionEventKind): 排队输入已撤销，协议值为 queued_cancelled。
        TASK_STATUS (InteractionEventKind): 后台任务状态，协议值为 task_status。
        RUN_STARTED (InteractionEventKind): 轮次开始或恢复，协议值为 run_started。
        TEXT_CHUNK (InteractionEventKind): 模型正文分片，协议值为 text_chunk。
        TOOL_CALL (InteractionEventKind): 工具调用提示，协议值为 tool_call。
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
    RUN_STARTED = "run_started"
    TEXT_CHUNK = "text_chunk"
    TOOL_CALL = "tool_call"
    INTERRUPT = "interrupt"
    AUTHORIZATION_RESOLVED = "authorization_resolved"
    RUN_COMPLETED = "run_completed"
    ERROR = "error"


class InterruptKind(str, Enum):
    """区分人工授权请求与独立桌面执行 continuation。

    Attributes:
        AUTHORIZATION_REQUEST (InterruptKind): 人工审批，协议值为 authorization_request。
    """
    AUTHORIZATION_REQUEST = "authorization_request"




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
    """表示持久输入类别，真人输入与任务终态通知分别交付。

    Attributes:
        HUMAN (InboxKind): 真人输入，协议值为 human。
        TASK_TERMINAL (InboxKind): 任务终态通知，协议值为 task_terminal。
    """
    HUMAN = "human"
    TASK_TERMINAL = "task_terminal"


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
    """表示受管进程状态，不与模型输入的汇总终态混用。

    Attributes:
        RUNNING (TaskStatus): 进程运行中，协议值为 running。
        COMPLETED (TaskStatus): 成功结束，协议值为 completed。
        FAILED (TaskStatus): 执行失败，协议值为 failed。
        STOPPED (TaskStatus): 用户停止，协议值为 stopped。
        ORPHANED (TaskStatus): 重启后失去进程所有权，协议值为 orphaned。
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
