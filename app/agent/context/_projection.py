"""将领域消息投影为临时 Provider 输入。"""

from dataclasses import replace
from typing import Any, Sequence

from app.agent.context.runtime import RuntimeContext
from app.agent.state.messages import (
    AssistantMessage,
    BaseMessage,
    HumanMessage,
    RuntimeInputMessage,
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
    projected: list[dict] = []
    for message in messages:
        wire = message.to_openai_format()
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
    """仅向临时 HumanMessage 值追加时间和召回记忆。"""
    result = list(messages)
    target_index = next(
        (
            index for index in range(len(result) - 1, -1, -1)
            if isinstance(result[index], HumanMessage)
            and result[index].message_id == turn_context.latest_human_message_id
        ),
        None,
    )
    if target_index is None:
        target_index = next(
            (
                index for index in range(len(result) - 1, -1, -1)
                if isinstance(result[index], HumanMessage)
            ),
            None,
        )
    if target_index is None:
        return tuple(result)
    target = result[target_index]
    assert isinstance(target, HumanMessage)
    suffix = f"[系统提示]\n当前日期时间：{format_local_time(turn_context.captured_at)}"
    if turn_context.recalled_memory.strip():
        suffix += (
            "\n\n以下是自动检索到的可能相关记忆，仅供参考：\n"
            + turn_context.recalled_memory.strip()
        )
    text = f"{target.text}\n\n{suffix}" if target.text else suffix
    result[target_index] = replace(target, text=text)
    return tuple(result)


def build_model_window(
    state: AgentState | Sequence[BaseMessage],
) -> list[dict]:
    messages = state.messages if isinstance(state, AgentState) else state
    return normalize_messages_for_model(project_messages_with_time(messages))
