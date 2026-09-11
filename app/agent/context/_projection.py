"""从持久状态构造只供模型调用使用的消息副本。"""

from copy import deepcopy
from typing import Any

from app.agent.context.runtime import RuntimeContext
from app.agent.state.message_names import SYSTEM_INJECTED_USER_MESSAGE_NAMES
from app.agent.state.messages import is_real_human_message
from app.agent.state.model import AgentState
from app.agent.state.time import format_local_time, render_timed_text
from app.agent.utils.domain.text import normalize_messages_for_model


def _project_content(content: Any, timestamp: str) -> Any:
    if isinstance(content, str):
        return render_timed_text(content, timestamp)
    if not isinstance(content, list):
        return content
    projected = deepcopy(content)
    prefix = f"[发送时间：{format_local_time(timestamp)}]"
    for part in projected:
        if isinstance(part, dict) and part.get("type") == "text":
            part["text"] = f"{prefix} {part.get('text', '')}".rstrip()
            return projected
    projected.insert(0, {"type": "text", "text": prefix})
    return projected


def project_messages_with_time(messages: list[dict]) -> list[dict]:
    projected = deepcopy(messages)
    for message in projected:
        timestamp = message.get("_created_at")
        if not timestamp:
            continue
        role = message.get("role")
        if role == "user" and message.get("name") in SYSTEM_INJECTED_USER_MESSAGE_NAMES:
            continue
        is_plain_assistant = role == "assistant" and not message.get("tool_calls")
        if role != "user" and not is_plain_assistant:
            continue
        message["content"] = _project_content(message.get("content"), timestamp)
    return projected


def append_transient_context(
    messages: list[dict],
    turn_context: RuntimeContext,
) -> list[dict]:
    """只在模型输入副本中给最新真人消息追加时间和召回记忆。"""
    result = deepcopy(messages)
    target = next(
        (
            message for message in reversed(result)
            if message.get("_message_id") == turn_context.latest_human_message_id
            and is_real_human_message(message)
        ),
        None,
    )
    if target is None:
        target = next((m for m in reversed(result) if is_real_human_message(m)), None)
    if target is None:
        return result
    suffix = f"[系统提示]\n当前日期时间：{format_local_time(turn_context.captured_at)}"
    if turn_context.recalled_memory.strip():
        suffix += (
            "\n\n以下是自动检索到的可能相关记忆，仅供参考：\n"
            + turn_context.recalled_memory.strip()
        )
    content = target.get("content", "")
    if isinstance(content, list):
        target["content"] = [*content, {"type": "text", "text": suffix}]
    else:
        target["content"] = f"{content}\n\n{suffix}" if content else suffix
    return result


def build_model_window(state: AgentState | list[dict]) -> list[dict]:
    messages = state.messages if isinstance(state, AgentState) else state
    return normalize_messages_for_model(project_messages_with_time(deepcopy(messages)))
