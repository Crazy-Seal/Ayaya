"""为临时业务消息副本补充时间、渠道和本轮检索记忆。"""

from dataclasses import replace
from typing import Any, Sequence

from app.schemas.runtime_types import (
    DESKTOP_CHANNEL,
)
from app.agent.context.runtime import RuntimeContext
from app.agent.state.messages import (
    AssistantMessage,
    BaseMessage,
    HumanMessage,
    RuntimeInputMessage,
    SystemMessage,
)
from app.agent.state.model import AgentState
from app.agent.state.time import format_local_time, render_timed_text
from app.agent.models.contracts import ProjectedMessage
from app.agent.state.messages import TextContent, ImageContent, notification_text, with_text




def project_messages_with_time(messages: Sequence[BaseMessage]) -> tuple:
    """在临时业务副本加入时间和渠道，不生成供应商请求字段。"""
    projected = []
    for message in messages:
        if isinstance(message, RuntimeInputMessage):
            projected.append(ProjectedMessage(message, render_timed_text(notification_text(message), message.created_at)))
            continue
        candidate = message
        if isinstance(message, HumanMessage) or isinstance(message, AssistantMessage) and not message.tool_calls:
            parts = list(message.content)
            prefix = f"[发送时间：{format_local_time(message.created_at)}] "
            if isinstance(message, HumanMessage) and message.origin_channel != DESKTOP_CHANNEL:
                prefix += f"[来源渠道：{message.origin_channel}] "
            # 1. 只改第一个文字块，图片及文字的相对顺序保持；无文字时插入时间。
            position = next((index for index, part in enumerate(parts) if isinstance(part, TextContent)), None)
            if position is None:
                parts.insert(0, TextContent(prefix.rstrip()))
            else:
                parts[position] = TextContent(prefix + parts[position].text)
            candidate = replace(message, content=tuple(parts))
        projected.append(candidate)
    return tuple(projected)



def append_transient_context(
    messages: Sequence[BaseMessage],
    turn_context: RuntimeContext,
) -> tuple[BaseMessage, ...]:
    """把本轮时间和检索记忆加入临时投影，后台通知不得借用历史真人消息。

    Args:
        messages (Sequence[BaseMessage]): 需要转换或交付的领域消息集合。
        turn_context (RuntimeContext): 本轮临时投影上下文。

    Returns:
        tuple[BaseMessage, ...]: 包含本轮临时背景的领域消息投影，原消息保持不变。
    """
    result = list(messages)
    target_index = next(
        (
            index for index in range(len(result) - 1, -1, -1)
            if isinstance(result[index], HumanMessage)
            and result[index].message_id == turn_context.latest_human_message_id
        ),
        None,
    )
    suffix = f"[系统提示]\n当前日期时间：{format_local_time(turn_context.captured_at)}"
    if turn_context.recalled_memory.strip():
        suffix += (
            "\n\n以下是自动检索到的可能相关记忆，仅供参考：\n"
            + turn_context.recalled_memory.strip()
        )
    # 1. 后台通知轮次没有当前真人输入时，系统上下文单独插入，不能改写历史真人消息。
    if target_index is None:
        if result and isinstance(result[-1], RuntimeInputMessage):
            result.insert(len(result) - 1, SystemMessage(text=suffix))
        return tuple(result)
    target = result[target_index]
    assert isinstance(target, HumanMessage)
    text = f"{target.text}\n\n{suffix}" if target.text else suffix
    result[target_index] = with_text(target, text)
    return tuple(result)


def build_model_window(
    state: AgentState | Sequence[BaseMessage],
) -> tuple:
    """生成带时间的临时业务消息；供应商字段由模型适配器转换。

    Args:
        state (AgentState | Sequence[BaseMessage]): 当前会话状态，或压缩流程提供的消息集合。

    Returns:
        tuple: 独立的模型输入列表；不会修改领域消息或保存 checkpoint。
    """
    messages = state.messages if isinstance(state, AgentState) else state
    return project_messages_with_time(messages)
