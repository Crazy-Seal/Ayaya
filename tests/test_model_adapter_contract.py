"""通过统一模型接口验证工具图片与供应商材料转换。"""

from openai import AsyncOpenAI
from typing import cast

import asyncio
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionMessageCustomToolCall
from openai.types.chat.chat_completion_message_custom_tool_call import Custom

from app.agent.models.llm_client import LLMClient, LLMConfig
from app.agent.state.messages import AssistantMessage, ToolCall, ToolMessage, TextContent, ImageContent, ProviderData


def test_openai_custom_tool_call_is_rejected_before_function_parsing() -> None:
    """SDK 返回自由文本自定义工具时，报告不支持的调用，不读取不存在的 function 字段。"""
    async def create(**payload: object) -> SimpleNamespace:
        """提供真实 SDK 自定义工具对象，验证适配器按工具种类处理响应。

        Args:
            payload (object): 本次 SDK 请求的具名参数。

        Returns:
            SimpleNamespace: 包含 SDK 自定义工具调用的普通模型响应。
        """
        call = ChatCompletionMessageCustomToolCall(
            id="custom-call", type="custom", custom=Custom(name="freeform", input="内容"),
        )
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=[call]), finish_reason="tool_calls",
        )])

    client = LLMClient(LLMConfig("test", "test"), sdk_client=cast(AsyncOpenAI, SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )))
    with pytest.raises(ValueError, match="不支持的自定义工具调用"):
        asyncio.run(client.ainvoke([AssistantMessage(content=(TextContent("继续"),))]))


def test_openai_batch_places_images_after_all_tool_results() -> None:
    """通过 SDK 测试替身观察完整请求，业务结果不被拆分或改写。"""
    calls = []

    async def create(**payload):
        """模型接口调用时记录实际请求，返回固定普通回答。"""
        calls.append(payload)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="完成", tool_calls=[]), finish_reason="stop")])

    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    messages = (
        AssistantMessage(tool_calls=(ToolCall("a", "python", {}), ToolCall("b", "read_file", {}))),
        ToolMessage(content=(TextContent("观察成功"), ImageContent("data:image/png;base64,AA==", media_kind="tool")), tool_name="python", tool_call_id="a"),
        ToolMessage(content=(TextContent("文件内容"),), tool_name="read_file", tool_call_id="b"),
    )
    client = LLMClient(LLMConfig("test", "test"), sdk_client=cast(AsyncOpenAI, sdk))
    response = asyncio.run(client.ainvoke(messages))
    assert response.text == "完成"
    assert [m["role"] for m in calls[0]["messages"]] == ["assistant", "tool", "tool", "user"]
    assert len(messages) == 3 and len(messages[1].content) == 2


def test_foreign_provider_material_is_ignored_without_dropping_call() -> None:
    """不兼容专用字段不会进入请求，工具调用仍存在。"""
    calls = []

    async def create(**payload):
        """记录经过适配器过滤的请求，供外层检查调用配对。"""
        calls.append(payload)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="完成", tool_calls=[]), finish_reason="stop")])

    foreign = ProviderData("anthropic_messages", "https://other.example", "other", {"extra_content": {"signature": "foreign"}})
    messages = [AssistantMessage(tool_calls=(ToolCall("a", "read_file", {}, foreign),)), ToolMessage(content=(TextContent("结果"),), tool_name="read_file", tool_call_id="a")]
    client = LLMClient(LLMConfig("test", "test"), sdk_client=cast(AsyncOpenAI, SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))))
    asyncio.run(client.ainvoke(messages))
    assert calls[0]["messages"][0]["tool_calls"][0]["id"] == "a"
    assert "extra_content" not in calls[0]["messages"][0]["tool_calls"][0]
