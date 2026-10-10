"""为记忆提取复制并压缩工具结果，不改变共享的原始对话。"""

from dataclasses import replace

from app.agent.state.messages import BaseMessage, ToolMessage
from app.agent.utils.domain.weighted_text import truncate_weighted_text


def compress_tool_results(messages: list[BaseMessage]) -> list[BaseMessage]:
    """按现有 800 加 200 权重压缩工具结果，保留其他消息和消息身份。

    情景和语义提取器粗估超限后调用；只替换返回列表中的工具结果，原始
    消息仍可用于协议历史及另一种记忆提取，不压缩历史或其他消息正文。

    Args:
        messages (list[BaseMessage]): 当前批次的领域消息。

    Returns:
        list[BaseMessage]: 工具结果保留头部 800、尾部 200 权重的新消息列表。
    """
    # 1. 复制工具消息值，保留其调用身份、时间和其他字段，不原地修改共享材料。
    copied: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, ToolMessage):
            message = replace(
                message,
                content=truncate_weighted_text(
                    message.content, max_weight=1_000, head_weight=800, tail_weight=200,
                ),
            )
        copied.append(message)
    return copied
