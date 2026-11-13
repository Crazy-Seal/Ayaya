"""把运行对象提供的事实转换成渠道事件，保持业务决策在运行对象内。"""

from app.agent.core.events import EventType
from app.agent.runtime.requests import ExecutionEvent
from app.agent.state.messages import ImageContent
from app.interaction.interaction_contract import InteractionEvent
from app.schemas.runtime_types import InteractionEventKind


AGENT_EVENT_KINDS = {
    EventType.TEXT_CHUNK: InteractionEventKind.TEXT_CHUNK,
    EventType.TOOL_CALL: InteractionEventKind.TOOL_CALL,
    EventType.TOOL_COMPLETED: InteractionEventKind.TOOL_COMPLETED,
    EventType.INTERRUPT: InteractionEventKind.INTERRUPT,
    EventType.ERROR: InteractionEventKind.ERROR,
    EventType.DONE: InteractionEventKind.RUN_COMPLETED,
    EventType.RUN_STARTED: InteractionEventKind.RUN_STARTED,
}


def to_interaction_event(value: ExecutionEvent | InteractionEvent) -> InteractionEvent:
    """转换执行载荷，保留上下文、轮次和已提交消息身份。

    Args:
        value (ExecutionEvent | InteractionEvent): 生产者已经确定的执行或业务事件。

    Returns:
        InteractionEvent: 尚未分配输出序号的渠道事件。
    """
    if isinstance(value, InteractionEvent):
        return value
    event = value.event
    if event.type == EventType.HUMAN_INPUT_COMMITTED:
        message = event.data["message"]
        return InteractionEvent(value.context_id, InteractionEventKind.HUMAN_MESSAGE,
            {"message": message.text, "images": [part.url for part in message.content if isinstance(part, ImageContent)],
             "input_sequence": event.data["input_sequence"], "message_id": message.message_id},
            origin_channel=message.origin_channel)
    if event.type == EventType.RUN_STARTED:
        payload = {"resumed": bool(event.data["resumed"])}
    else:
        payload = {"value": event.data}
    return InteractionEvent(value.context_id, AGENT_EVENT_KINDS[event.type], payload,
        origin_channel=value.origin_channel, run_id=value.run_id,
        request_id=event.data.get("request_id") if event.type == EventType.INTERRUPT else None)
