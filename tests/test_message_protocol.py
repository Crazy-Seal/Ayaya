import asyncio
from types import SimpleNamespace

from app.agent.state import AgentState
from app.agent.state.messages import (
    AvailableMedia,
    CompactedMedia,
    HumanMessage,
    MCP_TOOL_IMAGE_MESSAGE_NAME,
    SCREENSHOT_MESSAGE_NAME,
    AssistantMessage,
    ToolCall,
    ToolMediaMessage,
    ToolMessage,
    is_real_human_message,
    is_screenshot_message,
    is_user_message,
)
from app.agent.tools.control_screen import control_screen
from app.agent.tools.control_screen_with_vlm import control_screen_with_vlm
from app.agent.tools.screenshot import screenshot


def test_message_identity_predicates() -> None:
    human = HumanMessage(text="你好")
    screenshot_message = ToolMediaMessage(
        tool_name="screenshot",
        media_kind="screenshot",
        text="[系统消息]截图",
        payload=AvailableMedia(urls=("data:image/png;base64,MQ==",)),
    )
    compressed = ToolMediaMessage(
        tool_name="screenshot",
        media_kind="screenshot",
        text="[系统消息]截图",
        payload=CompactedMedia(),
    )
    assistant = AssistantMessage(text="你好")
    tool = ToolMessage(content="完成", tool_name="read_file", tool_call_id="call")

    assert is_user_message(human)
    assert is_real_human_message(human)
    assert not is_screenshot_message(human)
    for message in (screenshot_message, compressed):
        assert is_user_message(message)
        assert is_screenshot_message(message)
        assert not is_real_human_message(message)
    assert not is_user_message(assistant)
    assert not is_real_human_message(assistant)
    assert not is_user_message(tool)


def test_non_human_media_is_system_injected() -> None:
    for media_kind in ("mcp", "tool"):
        message = ToolMediaMessage(
            tool_name="image_tool",
            media_kind=media_kind,
            text="[系统消息]工具返回图片",
            payload=AvailableMedia(urls=("data:image/png;base64,MQ==",)),
        )
        assert is_user_message(message)
        assert not is_real_human_message(message)


def test_tool_images_are_flushed_after_all_tool_messages() -> None:
    """同一批工具的图片不得插入尚未补齐的 tool 槽位之间。"""
    state = AgentState.create_new("batch-images")
    image_call = ToolCall(id="a", name="image_tool", args={})
    text_call = ToolCall(id="b", name="text_tool", args={})
    state.record_assistant_step("", (image_call, text_call))
    state.record_tool_result(
        image_call,
        "image",
        image_urls=("data:image/png;base64,QQ==",),
        image_message_name=MCP_TOOL_IMAGE_MESSAGE_NAME,
    )
    assert state.deferred_tool_media_count == 1
    assert not any(isinstance(item, ToolMediaMessage) for item in state.messages)

    state.record_tool_result(text_call, "text")

    assert [type(item) for item in state.messages[-3:]] == [
        ToolMessage,
        ToolMessage,
        ToolMediaMessage,
    ]
    assert state.deferred_tool_media_count == 0


def test_deferred_tool_media_survives_checkpoint() -> None:
    """审批中断时延迟图片必须随 checkpoint 持久化。"""
    state = AgentState.create_new("checkpoint-images")
    first = ToolCall(id="a", name="image_tool", args={})
    second = ToolCall(id="b", name="text_tool", args={})
    state.record_assistant_step("", (first, second))
    state.record_tool_result(
        first,
        "image",
        image_urls=("data:image/png;base64,QQ==",),
        image_message_name=MCP_TOOL_IMAGE_MESSAGE_NAME,
    )

    restored = AgentState.from_checkpoint(state.to_checkpoint())

    assert restored.deferred_tool_media_count == 1
    assert restored.to_checkpoint()["_deferred_tool_media"] == (
        state.to_checkpoint()["_deferred_tool_media"]
    )


def test_tool_media_defaults_to_generic_type() -> None:
    state = AgentState.create_new("generic-tool-image")
    call = ToolCall(id="call-image", name="image_tool", args={})
    state.record_assistant_step("", (call,))
    state.record_tool_result(
        call,
        "image",
        image_urls=("data:image/png;base64,QQ==",),
    )

    message = state.messages[-1]
    assert isinstance(message, ToolMediaMessage)
    assert message.media_kind == "tool"


def test_screen_tools_explicitly_classify_returned_images_as_screenshots() -> None:
    image = "data:image/png;base64,QQ=="
    screenshot_context = SimpleNamespace(
        resume_data={"approved": True, "screenshot_data": image},
    )
    action_context = SimpleNamespace(
        resume_data={"approved": True, "executed": True, "screenshot_data": image},
        resume_state={"phase": "awaiting_execution"},
    )

    async def scenario():
        return (
            await screenshot._tool_func(context=screenshot_context),
            await control_screen._tool_func(
                bbox=[0, 0, 10, 10],
                target="button",
                operation="click",
                context=action_context,
            ),
            await control_screen_with_vlm._tool_func(
                target="button",
                operation="click",
                context=action_context,
            ),
        )

    results = asyncio.run(scenario())
    assert [result.image_message_name for result in results] == [
        SCREENSHOT_MESSAGE_NAME,
        SCREENSHOT_MESSAGE_NAME,
        SCREENSHOT_MESSAGE_NAME,
    ]
