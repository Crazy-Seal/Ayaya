"""验证业务内容块与完整工具结果的存储接口。"""
from app.agent.state.messages import text_content

from dataclasses import FrozenInstanceError

import pytest

from app.agent.state.messages import (
    ImageContent, TextContent, ToolMessage, ToolCall, message_from_storage_format,
)


def test_tool_result_roundtrip_contains_images_and_object_arguments() -> None:
    """工具结果往返后保留有序图片及对象参数，不产生 API 角色。"""
    message = ToolMessage(content=(TextContent("结果"), ImageContent("data:image/png;base64,AA==", media_kind="tool")), tool_name="read_image", tool_call_id="call")
    stored = message.to_storage_format()
    assert "role" not in stored
    assert stored["content"][1]["type"] == "image_url"
    assert message_from_storage_format(stored) == message
    assert ToolCall("call", "read_image", {"path": "a.png"}).to_dict()["args"] == {"path": "a.png"}


def test_content_copies_input_and_cannot_be_modified() -> None:
    """外部列表变化不影响消息，图片字段不可修改。"""
    parts = [TextContent("结果"), ImageContent("data:image/png;base64,AA==", media_kind="tool")]
    message = ToolMessage(content=parts, tool_name="read_image", tool_call_id="call")
    parts.clear()
    assert len(message.content) == 2
    with pytest.raises(FrozenInstanceError):
        message.content[1].url = "changed"
