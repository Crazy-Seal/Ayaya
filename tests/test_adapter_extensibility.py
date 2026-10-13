"""验证统一模型入口允许替换实现，不要求业务构造 OpenAI 请求。"""

import asyncio
import pytest
from app.agent.models.llm_client import LLMClient, LLMConfig, LLMResponse
from app.agent.state.messages import HumanMessage, text_content


class RecordingAdapter:
    """按业务消息记录输入的测试实现，不解释供应商角色。"""

    async def ainvoke(self, messages, tools=None, **options):
        """记录业务入参并返回业务内容块。"""
        self.messages = tuple(messages)
        return LLMResponse(text_content("回答"))

    async def close(self):
        """标记资源关闭，供统一接口测试验证。"""
        self.closed = True


def test_client_accepts_alternative_adapter_with_domain_messages() -> None:
    """测试实现可使用另一协议标识，调用方仍只传业务消息。"""
    adapter = RecordingAdapter()
    client = LLMClient(LLMConfig("model", "key", api_format="test_format"), adapter=adapter)
    message = HumanMessage(content=text_content("问题"))

    async def scenario():
        """通过公共请求和关闭接口观察替换实现。"""
        try:
            assert (await client.ainvoke((message,))).text == "回答"
        finally:
            await client.close()
    asyncio.run(scenario())
    assert adapter.messages == (message,) and adapter.closed


def test_unknown_format_fails_instead_of_falling_back_to_openai() -> None:
    """未知格式不导向默认供应商，也不会发起请求。"""
    with pytest.raises(ValueError, match="API 格式"):
        LLMClient(LLMConfig("model", "key", api_format="unknown"))
