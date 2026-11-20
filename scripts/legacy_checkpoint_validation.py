"""为历史迁移复用消息协议校验，迁移目标版本由各脚本固定声明。"""

from app.agent.state.model import AgentState
from app.agent.state.messages import ToolCall, message_from_storage_format
from app.agent.state.time import normalize_utc, utc_now


def validate_legacy_checkpoint(data: dict) -> None:
    """校验旧 checkpoint 的消息及待执行工具，保持历史脚本的目标数据形状。

    Args:
        data (dict): 历史迁移正在产生的 checkpoint。

    Raises:
        ValueError: 消息协议或待执行工具恢复材料不完整。
    """
    if not isinstance(data.get("session_id"), str) or not isinstance(data.get("messages"), list):
        raise ValueError("checkpoint 缺少会话标识或消息列表")
    state = AgentState(
        session_id=data["session_id"], token_baseline=data.get("token_baseline"),
        pending_tool_calls=tuple(ToolCall.from_dict(item) for item in data.get("pending_tool_calls", [])),
        pending_actions=tuple(ToolCall.from_dict(item) for item in data.get("pending_actions", [])),
        interrupt_data=data.get("interrupt_data"), extra=data.get("extra") or {},
        temporary_context=data.get("temporary_context") or {},
        created_at=normalize_utc(data.get("created_at") or utc_now()),
        updated_at=normalize_utc(data.get("updated_at") or utc_now()),
    )
    state._messages = [message_from_storage_format(item) for item in data["messages"]]
    state.to_checkpoint()
