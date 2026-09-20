"""领域消息查询与最终 Provider wire 格式归一化。"""

import logging

from app.agent.state.messages import BaseMessage, HumanMessage

logger = logging.getLogger(__name__)


def get_last_human_text(messages: list[BaseMessage] | tuple[BaseMessage, ...]) -> str:
    """返回最新真人文本，忽略所有 RuntimeInput。"""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.text
    return ""


def normalize_messages_for_model(messages: list[dict]) -> list[dict]:
    """归一化最终 Provider payload，不让该 Adapter 泄漏进领域模型。

    部分 OpenAI-compatible gateway 要求每条 tool 消息都有 ``name``。优先从前置
    assistant 调用恢复名称；无法恢复时丢弃该工具槽，避免发送畸形 wire 数据。
    """
    normalized: list[dict] = []
    tool_call_names: dict[str, str] = {}

    for original_message in messages:
        message = {
            key: value
            for key, value in original_message.items()
            if not key.startswith("_")
            and key not in {"image_description", "image_filenames"}
        }
        role = message.get("role")

        if role == "assistant":
            for tool_call in message.get("tool_calls") or []:
                tc_id = tool_call.get("id")
                function = tool_call.get("function") or {}
                function_name = function.get("name")
                if tc_id and function_name:
                    tool_call_names[tc_id] = function_name
            normalized.append(message)
            continue

        if role == "tool":
            tool_name = message.get("name")
            if not tool_name and message.get("tool_call_id"):
                tool_name = tool_call_names.get(message["tool_call_id"])

            if not tool_name:
                logger.warning(
                    "[Agent] 丢弃无 name 的 tool 消息，tool_call_id=%s",
                    message.get("tool_call_id"),
                )
                continue

            if tool_name != message.get("name"):
                message = {**message, "name": tool_name}

        normalized.append(message)

    return normalized
