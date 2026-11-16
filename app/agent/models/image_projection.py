"""在统一模型入口将本地图片引用投影为本次请求的可用图文。"""

from dataclasses import replace
import logging
from collections.abc import Sequence

from app.agent.models.contracts import InputMessage, ProjectedMessage
from app.agent.state.messages import (
    AssistantMessage, ContentPart, EnvironmentPerceptionMessage, HumanMessage,
    ImageContent, TextContent, ToolMessage,
)
from app.agent.utils.domain.images import load_image_data_url

logger = logging.getLogger(__name__)


def resolve_message_images(messages: Sequence[InputMessage]) -> tuple[InputMessage, ...]:
    """为模型调用复制消息，将文件引用解析成图片或缺图文字。

    Args:
        messages (Sequence[InputMessage]): 业务消息及带时间、来源的文字投影。
    Returns:
        tuple[InputMessage, ...]: 供估算及模型请求共用的独立消息集合。
    """
    resolved: list[InputMessage] = []
    for item in messages:
        message = item.message if isinstance(item, ProjectedMessage) else item
        if isinstance(message, (HumanMessage, AssistantMessage, ToolMessage, EnvironmentPerceptionMessage)):
            content: list[ContentPart] = []
            for part in message.content:
                if isinstance(part, ImageContent) and part.storage_ref is not None:
                    try:
                        content.append(replace(part, url=load_image_data_url(part.storage_ref)))
                    except (OSError, ValueError) as exc:
                        logger.warning("模型图片不可用: %s，原因: %s", part.storage_ref, exc)
                        content.append(TextContent(f"[图片不可用：{part.storage_ref}]"))
                else:
                    content.append(part)
            message = replace(message, content=tuple(content))
        resolved.append(replace(item, message=message) if isinstance(item, ProjectedMessage) else message)
    return tuple(resolved)
