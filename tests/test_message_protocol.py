"""验证完整工具结果与业务图片类别。"""

from app.agent.state import AgentState
from app.agent.state.messages import HumanMessage, ToolMessage, ToolCall, TextContent, ImageContent, text_content, is_real_human_message


def test_tool_image_is_part_of_result_and_never_human_input() -> None:
    """工具图片不增加真人消息，也不产生独立媒体消息。"""
    state = AgentState.create_new("s")
    state.add_user_message("读取")
    call = ToolCall("call", "tool", {})
    state.record_assistant_step("", (call,))
    result = state.record_tool_result(call, (TextContent("完成"), ImageContent("data:image/png;base64,AA==", media_kind="tool")))
    assert not is_real_human_message(result)
    assert len(state.messages) == 3 and isinstance(state.messages[-1], ToolMessage)
    assert sum(isinstance(message, HumanMessage) for message in state.messages) == 1


def test_full_image_result_survives_checkpoint_before_batch_closes() -> None:
    """后续调用仍待执行时，第一项结果的图片已经进入可恢复状态。"""
    state = AgentState.create_new("s")
    first, second = ToolCall("first", "read_image", {}), ToolCall("second", "read_file", {})
    state.record_assistant_step("", (first, second))
    state.record_tool_result(first, (TextContent("完成"), ImageContent("data:image/png;base64,AA==", media_kind="tool")))
    restored = AgentState.from_checkpoint(state.to_checkpoint())
    assert restored.pending_tool_calls == (second,)
    assert isinstance(restored.messages[-1].content[-1], ImageContent)
    assert "_deferred_tool_media" not in restored.to_checkpoint()


def test_tool_images_use_business_media_category() -> None:
    """普通工具结果携带工具图片类别，不携带模型 user 名称。"""
    from app.agent.tools.core.base import ToolResult
    result = ToolResult.success("截图", image_url="data:image/png;base64,AA==", media_kind="tool")
    assert result.content[-1].media_kind == "tool"
