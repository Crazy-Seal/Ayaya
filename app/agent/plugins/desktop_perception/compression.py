"""为桌面感知插件实现工具截图零保留策略，返回供状态提交的消息副本。"""

from collections.abc import Sequence
from dataclasses import replace

from app.agent.state import BaseMessage, ImageContent, ToolMessage
from app.schemas.message_types import ImageContextState, MediaKind


def compact_tool_screenshots(messages: Sequence[BaseMessage]) -> tuple[BaseMessage, ...]:
    """压缩全部可发送的工具截图，效果等同截图 TTL 和数量上限均为零。

    Args:
        messages (Sequence[BaseMessage]): 当前会话中按协议顺序排列的消息。

    Returns:
        tuple[BaseMessage, ...]: 保留消息身份、动作文字、其他图片和历史文件引用的候选消息。
    """
    candidate = []
    for message in messages:
        # 1. 工具观察由最新桌面截图替代，真人附件及其他来源的图片继续保留。
        if isinstance(message, ToolMessage):
            content = tuple(
                replace(part, url="", context_state=ImageContextState.COMPACTED)
                if isinstance(part, ImageContent) and part.media_kind is MediaKind.SCREENSHOT
                and part.context_state is ImageContextState.AVAILABLE else part
                for part in message.content
            )
            # 2. 仅替换有变化的消息，已压缩截图保持原状，历史文件仍由框架管理。
            if content != message.content:
                message = replace(message, content=content)
        candidate.append(message)
    return tuple(candidate)
