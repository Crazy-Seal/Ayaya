"""会话持久输入的共享快照与交付异常，不包含数据库访问。"""

from dataclasses import dataclass
from typing import Any

from app.schemas.runtime_types import InboxDeliveryMode, InboxKind, InboxStatus
from app.schemas.agent_storage import AgentStorageScope


class InputNoLongerPending(RuntimeError):
    """输入已被删除或交付，保存调用方回滚后正常结束本次输入处理。"""



@dataclass(frozen=True)
class InboxEnvelope:
    """表示一次持久输入的快照，在 checkpoint 事务中完成验证和交付。

    Attributes:
        id (int): SQLite 自增输入序号，在事件中作为 input_sequence。
        session_id (str): 共享 Ayaya 会话标识。
        kind (InboxKind): 真人输入或后台终态通知。
        payload (dict[str, Any]): 该类别携带的专用载荷。
        status (InboxStatus): 尚未交付或随 checkpoint 交付完成。
        delivery_mode (InboxDeliveryMode): 立即交付或等待正常轮次完成后放行。
    """
    id: int
    session_id: str
    kind: InboxKind
    payload: dict[str, Any]
    status: InboxStatus
    delivery_mode: InboxDeliveryMode = InboxDeliveryMode.IMMEDIATE
    storage_scope: AgentStorageScope = AgentStorageScope.MAIN
