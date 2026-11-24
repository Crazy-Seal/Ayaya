"""验证图片压缩插件按保留轮数和数量上限替换旧图片。"""

import asyncio

from app.agent.plugins.base import HookContext, PluginContext, PluginHook
from app.agent.plugins.image_context import ImageContextPlugin
from app.agent.plugins.image_context.plugin import (
    COMPRESSED_IMAGE_PLACEHOLDER,
    compress_images,
)
from app.agent.state import AgentState
from app.agent.state.messages import (
    HumanMessage,
    ImageContent,
    TextContent,
    ToolCall,
    ToolMessage,
    content_text,
)
from app.crud.sqlite_factory import SqliteFactory
from app.schemas.message_types import MediaKind


def _human_with_image(index: int) -> HumanMessage:
    """构造一条带本地图片的用户消息，供压缩规则测试使用。

    Args:
        index (int): 用于生成独立消息文字和图片文件名的序号。

    Returns:
        HumanMessage: 包含一段文字和一张已保存图片的用户消息。
    """
    return HumanMessage(content=(
        TextContent(f"human-{index}"),
        ImageContent("data:image/png;base64,AA", media_kind=MediaKind.HUMAN, storage_ref=f"human-{index}.png"),
    ))


def _human(index: int) -> HumanMessage:
    """构造一条只含文字的用户消息，用来推进图片保留轮数。

    Args:
        index (int): 用于生成独立消息文字的序号。

    Returns:
        HumanMessage: 只包含一段文字的用户消息。
    """
    return HumanMessage(content=(TextContent(f"human-{index}"),))


def test_image_expires_after_configured_human_messages() -> None:
    messages = (_human_with_image(0), _human(1), _human(2))

    result, count = compress_images(messages, ttl_human_messages=2, max_images=5)

    assert count == 1
    assert isinstance(result[0].content[1], TextContent)
    assert not any(isinstance(part, ImageContent) for part in result[0].content)


def test_newest_image_survives_before_reaching_ttl() -> None:
    messages = (_human_with_image(0), _human(1))

    result, count = compress_images(messages, ttl_human_messages=2, max_images=5)

    assert count == 0
    assert isinstance(result[0].content[1], ImageContent)


def test_oldest_images_are_compressed_above_count_limit() -> None:
    messages = tuple(_human_with_image(index) for index in range(6))

    result, count = compress_images(messages, ttl_human_messages=100, max_images=5)

    assert count == 1
    assert isinstance(result[0].content[1], TextContent)
    assert all(isinstance(result[index].content[1], ImageContent) for index in range(1, 6))


def test_compressed_placeholder_keeps_local_reference() -> None:
    messages = (_human_with_image(0), _human(1))

    result, _ = compress_images(messages, ttl_human_messages=1, max_images=5)

    placeholder = result[0].content[1]
    assert isinstance(placeholder, TextContent)
    assert placeholder.text == f"{COMPRESSED_IMAGE_PLACEHOLDER}（本地保存：image://human-0.png）"


def test_compression_is_idempotent() -> None:
    messages = (_human_with_image(0), _human(1))

    once, first_count = compress_images(messages, ttl_human_messages=1, max_images=5)
    twice, second_count = compress_images(once, ttl_human_messages=1, max_images=5)

    assert first_count == 1
    assert second_count == 0
    assert twice == once


def test_messages_without_images_are_unchanged() -> None:
    messages = (_human(0), _human(1))

    result, count = compress_images(messages, ttl_human_messages=1, max_images=0)

    assert count == 0
    assert result == messages


def test_image_without_local_reference_is_left_for_next_request() -> None:
    messages = (HumanMessage(content=(
        TextContent("human-0"),
        ImageContent("data:image/png;base64,AA", media_kind=MediaKind.HUMAN),
    )), _human(1))

    result, count = compress_images(messages, ttl_human_messages=1, max_images=0)

    assert count == 0
    assert isinstance(result[0].content[1], ImageContent)


def test_plugin_compresses_images_before_model_request() -> None:
    async def scenario() -> tuple[str, int, int]:
        state = AgentState.create_new("image-context")
        state.add_user_message("first", image_urls=("data:image/png;base64,AA",), image_filenames=("human-0.png",))
        for index in range(10):
            state.add_user_message(f"human-{index}")
        message_id = state.messages[0].message_id
        message_count = len(state.messages)
        plugin = ImageContextPlugin(
            context=PluginContext(SqliteFactory()),
            max_images_in_context=5,
            image_ttl_human_messages=10,
        )

        await plugin.execute(HookContext.create(PluginHook.BEFORE_LLM, state))

        first = state.messages[0]
        assert first.message_id == message_id
        assert len(state.messages) == message_count
        assert isinstance(first.content[1], TextContent)
        assert "human-0.png" in first.content[1].text
        return first.content[1].text, message_count, len(state.messages)

    placeholder, before_count, after_count = asyncio.run(scenario())

    assert placeholder == f"{COMPRESSED_IMAGE_PLACEHOLDER}（本地保存：image://human-0.png）"
    assert before_count == after_count


def test_tool_result_image_is_compressed_without_breaking_protocol() -> None:
    async def scenario() -> str:
        state = AgentState.create_new("tool-image")
        state.add_user_message("截图看看")
        call = ToolCall("call-1", "python", {})
        state.record_assistant_step("", (call,))
        state.record_tool_result(call, (
            TextContent("screenshot saved"),
            ImageContent("data:image/png;base64,AA", media_kind=MediaKind.TOOL, storage_ref="tool/shot.png"),
        ), compressible=True)
        for index in range(10):
            state.add_user_message(f"human-{index}")
        plugin = ImageContextPlugin(
            context=PluginContext(SqliteFactory()),
            max_images_in_context=5,
            image_ttl_human_messages=10,
        )

        await plugin.execute(HookContext.create(PluginHook.BEFORE_LLM, state))

        tool_message = next(message for message in state.messages if isinstance(message, ToolMessage))
        assert not any(isinstance(part, ImageContent) for part in tool_message.content)
        return content_text(tool_message.content)

    text = asyncio.run(scenario())

    assert "screenshot saved" in text
    assert "tool/shot.png" in text
