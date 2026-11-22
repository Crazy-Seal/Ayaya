"""交互入口共享的命令和观察事件；不依赖 Agent 的 Implementation。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from app.agent.state.time import utc_now
from app.schemas.runtime_types import CommandKind, InteractionEventKind


@dataclass(frozen=True, slots=True)
class InteractionCommand:
    """描述会话中的统一用户动作，不是模型消息，也不包含自动重试标识。

    Attributes:
        session_id (str): 共享 Ayaya 会话标识。
        kind (CommandKind | str): 已知命令使用枚举，未知字符串留给 Scheduler 业务拒绝。
        payload (dict[str, Any]): 该类别携带的专用载荷。
        target_run_id (str | None): 停止或引导针对的活动 run，省略时按命令规则处理。
        request_id (str | None): checkpoint 中的持久中断标识。
        task_id (str | None): 受管进程任务标识。
    """
    session_id: str
    kind: CommandKind | str
    payload: dict[str, Any] = field(default_factory=dict)
    target_run_id: str | None = None
    request_id: str | None = None
    task_id: str | None = None
    subagent_id: str | None = None

    def __post_init__(self) -> None:
        """归一化已知命令，保留未知字符串的业务拒绝路径而不新增请求校验。"""
        try:
            object.__setattr__(self, "kind", CommandKind(self.kind))
        except (ValueError, TypeError):
            pass




@dataclass(frozen=True, slots=True)
class InteractionStreamReady:
    """通知接收方全局订阅已登记，可以开始查询会话快照。

    Attributes:
        stream_epoch (str): 当前后端的事件编号命名空间。
        kind (Literal["stream_ready"]): 连接就绪控制帧类别。
    """

    stream_epoch: str
    kind: Literal["stream_ready"] = "stream_ready"


@dataclass(frozen=True, slots=True)
class InteractionEvent:
    """承载会话可见状态，发布序号只控制界面快照和增量的衔接。

    Attributes:
        session_id (str): 共享 Ayaya 会话标识。
        kind (InteractionEventKind | str): 已知交互事件或沿原路径透传的扩展事件。
        payload (dict[str, Any]): 该类别携带的专用载荷。
        run_id (str | None): 当前运行轮次标识。
        request_id (str | None): checkpoint 中的持久中断标识。
        task_id (str | None): 受管进程任务标识。
        stream_epoch (str): 后端启动生成的事件序号命名空间。
        event_sequence (int): 当前 epoch 内按会话递增的可见事件水位。
        timestamp (datetime): 事件创建的 UTC 时间。
    """
    session_id: str
    kind: InteractionEventKind | str
    payload: dict[str, Any]
    run_id: str | None = None
    request_id: str | None = None
    task_id: str | None = None
    stream_epoch: str = ""
    event_sequence: int = 0
    timestamp: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        """归一化已知事件，保留 EventHub 既有扩展事件的投递行为。"""
        try:
            object.__setattr__(self, "kind", InteractionEventKind(self.kind))
        except (ValueError, TypeError):
            pass
