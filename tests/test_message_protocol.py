import asyncio
from types import SimpleNamespace

from app.agent.state.messages import (
    MCP_TOOL_IMAGE_MESSAGE_NAME,
    SCREENSHOT_COMPRESSED_NAME,
    SCREENSHOT_MESSAGE_NAME,
    TOOL_IMAGE_MESSAGE_NAME,
    is_real_human_message,
    is_screenshot_message,
    is_user_message,
)
from app.agent.state import AgentState
from app.agent.tools.control_screen import control_screen
from app.agent.tools.control_screen_with_vlm import control_screen_with_vlm
from app.agent.tools.screenshot import screenshot


def test_message_identity_predicates() -> None:
    human = {"role": "user", "content": "你好"}
    screenshot = {
        "role": "user",
        "content": [],
        "name": SCREENSHOT_MESSAGE_NAME,
    }
    compressed = {
        "role": "user",
        "content": "已压缩",
        "name": SCREENSHOT_COMPRESSED_NAME,
    }
    assistant = {"role": "assistant", "content": "你好"}
    tool = {"role": "tool", "content": "完成"}

    assert is_user_message(human)
    assert is_real_human_message(human)
    assert not is_screenshot_message(human)

    assert is_user_message(screenshot)
    assert is_screenshot_message(screenshot)
    assert not is_real_human_message(screenshot)

    assert is_user_message(compressed)
    assert is_screenshot_message(compressed)
    assert not is_real_human_message(compressed)

    assert not is_user_message(assistant)
    assert not is_real_human_message(assistant)
    assert not is_user_message(tool)


def test_mcp_tool_image_is_system_injected_not_real_human() -> None:
    message = {
        "role": "user",
        "name": MCP_TOOL_IMAGE_MESSAGE_NAME,
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,MQ=="}}],
    }

    assert not is_real_human_message(message)


def test_generic_tool_image_is_system_injected_not_real_human() -> None:
    message = {
        "role": "user",
        "name": TOOL_IMAGE_MESSAGE_NAME,
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,MQ=="}}],
    }

    assert not is_real_human_message(message)


def test_tool_images_are_flushed_after_all_tool_messages() -> None:
    """同一批工具的图片不得插入尚未补齐的 tool 槽位之间。"""
    state = AgentState.create_new("batch-images")
    state.add_tool_message("image", "image_tool", "a")
    state.deferred_tool_images.append({
        "tool_call_id": "a",
        "tool_name": "image_tool",
        "image_message_name": MCP_TOOL_IMAGE_MESSAGE_NAME,
        "image_urls": ["data:image/png;base64,QQ=="],
    })
    state.add_tool_message("text", "text_tool", "b")

    assert [item["role"] for item in state.messages] == ["tool", "tool"]
    assert len(state.deferred_tool_images) == 1
    state.flush_deferred_tool_images()
    assert [item["role"] for item in state.messages] == ["tool", "tool", "user"]
    assert state.deferred_tool_images == []


def test_deferred_tool_images_survive_checkpoint() -> None:
    """审批中断时延迟图片必须随 checkpoint 持久化。"""
    state = AgentState.create_new("checkpoint-images")
    state.deferred_tool_images.append({
        "tool_call_id": "a",
        "tool_name": "image_tool",
        "image_message_name": MCP_TOOL_IMAGE_MESSAGE_NAME,
        "image_urls": ["data:image/png;base64,QQ=="],
    })

    restored = AgentState.from_checkpoint(state.to_checkpoint())

    assert restored.deferred_tool_images == state.deferred_tool_images


def test_deferred_tool_image_defaults_to_generic_type() -> None:
    state = AgentState.create_new("generic-tool-image")
    state.add_tool_message("image", "image_tool", "call-image")
    state.deferred_tool_images.append({
        "tool_call_id": "call-image",
        "tool_name": "image_tool",
        "image_urls": ["data:image/png;base64,QQ=="],
    })

    state.flush_deferred_tool_images()

    assert state.messages[-1]["name"] == TOOL_IMAGE_MESSAGE_NAME


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
