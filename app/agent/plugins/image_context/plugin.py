"""在主模型请求前压缩旧图片，只把本地文件引用留在模型输入中。"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.plugins.base import BasePlugin, HookContext, PluginContext, PluginHook
from app.agent.state.messages import (
    AssistantMessage,
    BaseMessage,
    ContentPart,
    HumanMessage,
    ImageContent,
    TextContent,
    ToolMessage,
)
from app.agent.utils.domain.images import IMAGE_URI_PREFIX

logger = logging.getLogger(__name__)

COMPRESSED_IMAGE_PLACEHOLDER = "[系统消息]已被压缩的旧图片"


class ImageContextConfig(BaseModel):
    """保存旧图片在模型输入中的保留上限。"""

    model_config = ConfigDict(extra="forbid")

    max_images_in_context: int = Field(
        default=5,
        ge=0,
        title="上下文最大图片数",
        description="模型输入中最多保留多少张未压缩图片，超出时从最旧的图片开始压缩。",
    )
    image_ttl_human_messages: int = Field(
        default=10,
        ge=0,
        title="图片保留轮数",
        description="图片在之后经过多少条用户消息时压缩；取值为零时每次模型请求前都压缩图片。",
    )


def _image_positions(messages: tuple[BaseMessage, ...]) -> list[tuple[int, int]]:
    """列出消息中已保存到本地的图片内容块位置。

    Args:
        messages (tuple[BaseMessage, ...]): 压缩前的 Agent 状态消息。

    Returns:
        list[tuple[int, int]]: 每项是图片所在消息下标和该消息内的消息内容块下标，按对话顺序排列。
    """
    positions: list[tuple[int, int]] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, (HumanMessage, AssistantMessage, ToolMessage)):
            continue
        for part_index, part in enumerate(message.content):
            # 1. 只压缩已保存到本地的图片，其余图片留给保存完成后的请求处理。
            if isinstance(part, ImageContent) and part.storage_ref is not None:
                positions.append((message_index, part_index))
    return positions


def _compressed_part(part: ImageContent) -> TextContent:
    """把一张已保存到本地的图片换成带图片存储引用的文字说明。

    Args:
        part (ImageContent): 需要压缩的图片消息内容块。

    Returns:
        TextContent: 说明这是旧图片并给出可再次读取的图片存储引用的文字消息内容块。
    """
    return TextContent(f"{COMPRESSED_IMAGE_PLACEHOLDER}（本地保存：{IMAGE_URI_PREFIX}{part.storage_ref}）")


def compress_images(
    messages: tuple[BaseMessage, ...],
    *,
    ttl_human_messages: int,
    max_images: int,
) -> tuple[tuple[BaseMessage, ...], int]:
    """按保留轮数和数量上限压缩旧图片，返回替换后的消息集合。

    消息身份、时间、类型和相对顺序保持不变，因此调用方可以把结果提交为 Agent 状态的压缩结果。

    Args:
        messages (tuple[BaseMessage, ...]): 压缩前的 Agent 状态消息。
        ttl_human_messages (int): 图片之后经过多少条用户消息时压缩该图片。
        max_images (int): 允许保留的未压缩图片数量。

    Returns:
        tuple[tuple[BaseMessage, ...], int]: 替换后的消息集合及本次压缩的图片数量；没有可压缩图片时返回原消息。
    """
    positions = _image_positions(messages)
    if not positions:
        return messages, 0

    # 1. 从每条图片所在消息往后数用户消息，达到保留轮数的图片先进入压缩范围。
    expired: set[tuple[int, int]] = set()
    for message_index, part_index in positions:
        human_after = sum(
            1
            for later in messages[message_index + 1:]
            if isinstance(later, HumanMessage)
        )
        if human_after >= ttl_human_messages:
            expired.add((message_index, part_index))

    # 2. 剩余图片超过数量上限时，按对话顺序从最旧的图片开始压缩。
    remaining = [position for position in positions if position not in expired]
    excess = len(remaining) - max_images
    expired.update(remaining[:max(excess, 0)])
    if not expired:
        return messages, 0

    # 3. 按消息归组重建消息内容块，同一消息内的其他文字和保留图片保持原位。
    targets: dict[int, set[int]] = {}
    for message_index, part_index in expired:
        targets.setdefault(message_index, set()).add(part_index)
    result = list(messages)
    for message_index, part_indexes in targets.items():
        message = result[message_index]
        parts: list[ContentPart] = [
            _compressed_part(part) if part_index in part_indexes else part
            for part_index, part in enumerate(message.content)
        ]
        result[message_index] = replace(message, content=tuple(parts))
    return tuple(result), len(expired)


class ImageContextPlugin(BasePlugin):
    """压缩模型输入中的旧图片，只保留本地文件引用。

    插件在每次模型请求前检查 Agent 状态中的图片。图片在之后经过配置条数的用户消息后，
    或未压缩图片数量超过上限时，消息内容块被换成带本地文件路径的文字说明。对话历史保留
    原始图片，只有模型输入和后续 checkpoint 使用压缩后的内容。
    """

    name = "image_context"
    description = "压缩模型输入中的旧图片，只保留本地文件引用。"
    config_model = ImageContextConfig

    def __init__(self, *, context: PluginContext, **config: Any) -> None:
        """保存当前会话的图片保留上限。

        Args:
            context (PluginContext): 框架提供的会话配置快照和数据库工厂。
            **config (Any): 用户在插件页保存的插件参数。
        """
        self.config = ImageContextConfig(**config)

    @property
    def hooks(self) -> list[PluginHook]:
        """订阅每次模型请求前的时机。

        Returns:
            list[PluginHook]: 本插件订阅的钩子。
        """
        return [PluginHook.BEFORE_LLM]

    async def execute(self, context: HookContext) -> None:
        """在模型请求前压缩旧图片，使本次输入只上传仍未压缩的图片。

        Args:
            context (HookContext): 当前钩子和本次模型请求使用的 Agent 状态。
        """
        state = context.agent_state
        candidate, count = compress_images(
            state.messages,
            ttl_human_messages=self.config.image_ttl_human_messages,
            max_images=self.config.max_images_in_context,
        )
        if not count:
            return
        # 1. 压缩结果写入 Agent 状态，随后由执行流程在下一个保存点写入 checkpoint。
        state.commit_compaction(candidate)
        logger.info("[ImageContext] 已压缩 %d 张旧图片，会话：%s", count, state.session_id)
