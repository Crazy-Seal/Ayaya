"""验证模型网络响应释放、恢复材料及 MCP 内容顺序。"""

import asyncio
import base64
from types import SimpleNamespace

from app.agent.mcp.adapter import MCPToolAdapter
from app.agent.models.llm_client import LLMClient, LLMConfig
from app.agent.state.messages import HumanMessage, ImageContent, TextContent, text_content
from app.agent.tools.base import ToolResult
from app.agent.tools.result_limits import MAX_IMAGES_PER_CALL
from app.agent.tools.result_normalizer import ToolResultNormalizer
from app.agent.context import RuntimeContext
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.state import AgentState
from app.agent.models.llm_client import StreamChunk


def test_stopping_stream_closes_sdk_response() -> None:
    """业务调用方停止读取后，统一入口释放 SDK 响应连接。"""
    class Stream:
        """记录网络响应被关闭的次数，流在第一段正文后仍可继续。"""
        closed = 0

        def __aiter__(self):
            """让适配器读取模拟网络响应。

            Returns:
                Stream: 当前响应。
            """
            return self

        async def __anext__(self):
            """提供未结束的正文片段。

            Returns:
                SimpleNamespace: 固定 SDK 片段。
            """
            return SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="正文", tool_calls=None), finish_reason=None)])

        async def close(self):
            """记录适配器释放响应连接。"""
            self.closed += 1

    response = Stream()

    async def create(**payload):
        """模型发起请求时返回可观察的响应。

        Args:
            payload (dict): 适配器生成的 SDK 请求。
        Returns:
            Stream: 未结束的模拟响应。
        """
        return response

    async def stop_after_first_chunk():
        """模拟停止回答，关闭统一入口产生的流。"""
        client = LLMClient(LLMConfig("test", "test"), sdk_client=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        stream = client.astream([HumanMessage(content=text_content("问题"))])
        assert (await anext(stream)).content == "正文"
        await stream.aclose()

    asyncio.run(stop_after_first_chunk())
    assert response.closed == 1


def test_complete_response_retains_usage_refusal_and_owned_material() -> None:
    """完整响应归一化用量和拒绝，同时保留当前模型的恢复材料。"""
    async def create(**payload):
        """返回含拒绝和用量的 SDK 响应。

        Args:
            payload (dict): 实际请求。
        Returns:
            SimpleNamespace: 固定的供应商响应。
        """
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
            content="", tool_calls=[], refusal="拒绝", extra_content={"signature": "opaque"}))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3, total_tokens=15))

    client = LLMClient(LLMConfig("test", "test"), sdk_client=SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    response = asyncio.run(client.ainvoke([HumanMessage(content=text_content("问题"))]))
    assert response.finish_reason == "refused"
    assert response.usage.total_tokens == 15
    assert response.provider_data.model == "test"
    assert response.provider_data.payload["extra_content"]["signature"] == "opaque"


def test_mcp_interleaved_content_preserves_order_and_image_quota() -> None:
    """MCP 文字与图片交错顺序不变，共享同一图片对象也不能绕过数量限制。"""
    data = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
    converted = MCPToolAdapter._convert_result(SimpleNamespace(content=[
        SimpleNamespace(type="text", text="前"),
        SimpleNamespace(type="image", mimeType="image/png", data=data),
        SimpleNamespace(type="text", text="后"),
    ]))
    normalized = ToolResultNormalizer().normalize(converted)
    assert [type(block) for block in normalized.content] == [TextContent, ImageContent, TextContent]
    assert normalized.content[0].text == "前" and normalized.content[2].text == "后"
    image = normalized.content[1]
    limited = ToolResultNormalizer().normalize(ToolResult(content=(image,) * (MAX_IMAGES_PER_CALL + 1)))
    assert sum(isinstance(part, ImageContent) for part in limited.content) == MAX_IMAGES_PER_CALL
    assert "数量超过限制" in limited.text


def test_pipeline_close_immediately_closes_inner_model_stream() -> None:
    """停止正文交付后，管道立即关闭内层流，无需等待垃圾回收。"""
    closed = []
    kept = []

    async def stream(messages, tools=None):
        """记录底层模型生成器释放时机。

        Args:
            messages (list): 准备后的业务输入。
            tools (list | None): 当前公开工具。
        Yields:
            StreamChunk: 模拟第一段正文。
        """
        try:
            yield StreamChunk(content="正文")
        finally:
            closed.append(True)

    def start_stream(messages, tools=None):
        """保留底层引用，排除垃圾回收碰巧关闭生成器的影响。

        Args:
            messages (list): 业务输入。
            tools (list | None): 工具定义。
        Returns:
            AsyncIterator[StreamChunk]: 可观察关闭时机的模型流。
        """
        result = stream(messages, tools)
        kept.append(result)
        return result

    async def prepare(**kwargs):
        """绕过独立上下文测试，提供固定业务输入。

        Args:
            kwargs (dict): 管道传入的状态与提示。
        Returns:
            SimpleNamespace: 无持久状态变化的请求材料。
        """
        return SimpleNamespace(state_changed=False, messages=[])

    async def close_pipeline():
        """模拟停止点关闭管道的模型调用生成器。"""
        pipeline = object.__new__(ExecutionPipeline)
        pipeline._tool_manager = SimpleNamespace(get_tools=list)
        pipeline._prompt_source = SimpleNamespace(system_prompt="系统提示")
        pipeline._llm_client = SimpleNamespace(astream=start_stream)
        pipeline._model_input = SimpleNamespace(prepare=prepare)
        outer = pipeline._call_llm(AgentState(session_id="isolated"), RuntimeContext.capture())
        assert (await anext(outer)).content == "正文"
        await outer.aclose()
        assert closed == [True]

    asyncio.run(close_pipeline())
