"""消息规范化（OpenAI dict 版）。

修正/丢弃非法的 tool 消息，避免被部分网关（如 Gemini 兼容层）拒绝：
- 为缺 name 的 tool 消息回填工具名（从前面的 assistant.tool_calls 推断）
- 实在无法确定工具名的 tool 消息直接丢弃
"""

import logging

logger = logging.getLogger(__name__)


def normalize_messages_for_model(messages: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    tool_call_names: dict[str, str] = {}

    for message in messages:
        role = message.get("role")

        if role == "assistant":
            for tool_call in message.get("tool_calls") or []:
                tc_id = tool_call.get("id")
                fn = tool_call.get("function") or {}
                fn_name = fn.get("name")
                if tc_id and fn_name:
                    tool_call_names[tc_id] = fn_name
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
