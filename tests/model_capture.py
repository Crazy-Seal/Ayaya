"""回归用例的模型请求观察器；编码真实适配器，不发起网络请求。"""

from openai import AsyncOpenAI
from typing import cast

from types import SimpleNamespace
from app.agent.models.contracts import LLMConfig
from app.agent.models.openai_chat import OpenAIChatCompletionsAdapter
from app.agent.state.messages import text_content, ImageContent


def capture_input(messages):
    """把脚本模型收到的业务输入编码为 OpenAI 请求，供已有请求断言使用。"""
    if not messages or isinstance(messages[0], dict):
        return messages
    adapter = OpenAIChatCompletionsAdapter(LLMConfig("test-model", "test-key"), sdk_client=cast(AsyncOpenAI, SimpleNamespace()))
    return adapter.encode_messages(messages)


def result_parts(value, image_urls=(), media_kind="tool"):
    """为回归步骤构造完整结果，纯文本转换只存在于测试输入处。"""
    parts = text_content(value) if isinstance(value, str) else tuple(value)
    return (*parts, *(ImageContent(url, media_kind=media_kind) for url in image_urls))


def encoded_tool(tool):
    """观察工具经过实际适配器后的 SDK 定义，不调用模型。"""
    return OpenAIChatCompletionsAdapter(LLMConfig("test-model", "test-key"), sdk_client=cast(AsyncOpenAI, SimpleNamespace())).encode_tools((tool.definition(),))[0]


def image_urls(result):
    """读取结果中有效图片的地址，观察统一内容集合。"""
    return [part.url for part in result.content if isinstance(part, ImageContent)]


def image_kind(result):
    """读取第一张图片的业务类别，没有图片时返回 None。"""
    return next((part.media_kind for part in result.content if isinstance(part, ImageContent)), None)
