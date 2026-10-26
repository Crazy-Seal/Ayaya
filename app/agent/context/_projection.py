"""为历史消息补充时间和渠道，在请求末尾组装本轮环境背景。"""

from dataclasses import replace
from typing import Sequence

from app.schemas.runtime_types import (
    DESKTOP_CHANNEL,
)
from app.agent.state.messages import (
    AssistantMessage,
    BaseMessage,
    ContentPart,
    HumanMessage,
    RuntimeInputMessage,
    EnvironmentPerceptionMessage,
)
from app.agent.state.model import AgentState
from app.agent.state.time import format_local_time, render_timed_text
from app.agent.models.contracts import ProjectedMessage
from app.agent.state.messages import TextContent, notification_text




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
    state: AgentState,
) -> tuple[BaseMessage, ...]:
    """把 State 的本轮临时条目组装成请求末尾的独立环境消息。

    Args:
        state (AgentState): 保存历史和临时背景的当前状态或压缩候选状态。

    Returns:
        tuple[BaseMessage, ...]: 原历史与一条临时环境消息；条目为空时返回原历史。
    """
    if not state.temporary_context:
        return state.messages
    # 1. 按来源和条目标识固定排列，每项正文后紧跟对应图片，保留多插件背景与图片的对应关系。
    content: list[ContentPart] = []
    for _, item in sorted(state.temporary_context.items()):
        content.append(TextContent(f"[{item.title}]\n{item.text}"))
        content.extend(item.images)
    # 2. 环境消息只进入本次请求；checkpoint 保存的是条目，不保存这条派生消息。
    return (*state.messages, EnvironmentPerceptionMessage(content=tuple(content)))


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
