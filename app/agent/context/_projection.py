"""将领域消息投影为临时 Provider 输入。"""

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
from app.agent.utils.domain.text import normalize_messages_for_model


def _project_content(content: Any, timestamp: object) -> Any:
    if isinstance(content, str):
        return render_timed_text(content, timestamp)
    if not isinstance(content, list):
        return content
    projected = [dict(part) for part in content]
    prefix = f"[发送时间：{format_local_time(timestamp)}]"
    for part in projected:
        if part.get("type") == "text":
            part["text"] = f"{prefix} {part.get('text', '')}".rstrip()
            return projected
    projected.insert(0, {"type": "text", "text": prefix})
    return projected


def project_messages_with_time(messages: Sequence[BaseMessage]) -> list[dict]:
    """将领域消息投影为模型输入，补充时间和外部真人消息来源，不修改原始消息。

    Args:
        messages (Sequence[BaseMessage]): 需要转换或交付的领域消息集合。

    Returns:
        list[dict]: 补上时间及非桌面真人来源的模型 wire 列表。
    """
    projected: list[dict] = []
    for message in messages:
        wire = message.to_openai_format()
        # 1. 来源仅在模型投影中附加，正文和持久领域值保持原样。
        if isinstance(message, HumanMessage) and message.origin_channel != DESKTOP_CHANNEL:
            content = wire.get("content")
            prefix = f"[来源渠道：{message.origin_channel}] "
            if isinstance(content, str):
                wire["content"] = prefix + content
            elif isinstance(content, list):
                wire["content"] = [{"type": "text", "text": prefix}, *content]
        should_project = (
            isinstance(message, (HumanMessage, RuntimeInputMessage))
            or isinstance(message, AssistantMessage) and not message.tool_calls
        )
        if should_project:
            wire["content"] = _project_content(wire.get("content"), message.created_at)
        projected.append(wire)
    return projected


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
    result[target_index] = replace(target, text=text)
    return tuple(result)


def build_model_window(
    state: AgentState | Sequence[BaseMessage],
) -> list[dict]:
    messages = state.messages if isinstance(state, AgentState) else state
    return normalize_messages_for_model(project_messages_with_time(messages))
