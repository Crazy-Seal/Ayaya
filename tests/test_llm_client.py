import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from app.agent.models.llm_client import LLMClient, LLMConfig, TokenUsage


SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}


def _completion(content: str | None, finish_reason: str | None = "stop"):
    """构造带正文、结束原因和工具信息的模型响应对象。

    Args:
        content (str | None): 当前处理的正文。
        finish_reason (str | None): 脚本模型响应的结束原因。
    """
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def _client_with_responses(responses):
    """创建按顺序返回用例响应的模型客户端。

    Args:
        responses (Any): 按模型调用顺序准备的最终响应。
    """
    client = LLMClient(
        LLMConfig(model="structured-model", api_key="test-key", base_url="http://x/v1")
    )
    pending = list(responses)
    calls: list[dict] = []

    async def fake_create(**kwargs):
        """按脚本返回模型响应或模型流，替代 SDK 的远程创建请求。

        Args:
            kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
        """
        calls.append(kwargs)
        return pending.pop(0)

    client._client.chat.completions.create = fake_create
    return client, calls


def _invoke(client: LLMClient):
    """运行结构化模型请求，供外层检查响应和重试次数。

    Args:
        client (LLMClient): 本用例提供的模型或 HTTP 客户端。
    """
    async def scenario():
        """由外层测试调用，完成“用例”的异步步骤。"""
        try:
            return await client.ainvoke_structured(
                [{"role": "user", "content": "extract"}],
                SCHEMA,
            )
        finally:
            await client.close()

    return asyncio.run(scenario())


def test_ainvoke_structured_returns_valid_json_without_retry() -> None:
    """验证合法结构化响应直接返回不重试。"""
    client, calls = _client_with_responses([
        _completion('{"value":"ok"}'),
    ])

    assert _invoke(client) == {"value": "ok"}
    assert len(calls) == 1
    assert calls[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "structured_output",
            "strict": True,
            "schema": SCHEMA,
        },
    }
    assert "max_tokens" not in calls[0]
    assert "tools" not in calls[0]


@pytest.mark.parametrize(
    "finish_reason",
    ["length", "content_filter", "tool_calls", None],
)
def test_ainvoke_structured_retries_any_non_stop_finish_reason(
    finish_reason,
    caplog,
) -> None:
    """验证所有非 stop 结束原因触发结构化重试。

    Args:
        finish_reason (Any): 脚本模型响应的结束原因。
        caplog (pytest.LogCaptureFixture): Pytest 收集的日志，供检查级别、诊断和敏感内容是否泄漏。
    """
    client, calls = _client_with_responses([
        _completion('{"value":"incomplete"}', finish_reason),
        _completion('{"value":"ok"}'),
    ])

    with caplog.at_level(logging.INFO, logger="app.agent.models.llm_client"):
        result = _invoke(client)

    expected_reason = finish_reason or "unknown"
    assert result == {"value": "ok"}
    assert len(calls) == 2
    assert f"finish_reason={expected_reason}" in caplog.text
    assert "attempt=1/2" in caplog.text
    assert "结构化输出重试成功" in caplog.text


def test_ainvoke_structured_retries_json_decode_error_without_logging_content(
    caplog,
) -> None:
    """验证JSON 解析失败重试且日志不泄漏响应正文。

    Args:
        caplog (pytest.LogCaptureFixture): Pytest 收集的日志，供检查级别、诊断和敏感内容是否泄漏。
    """
    secret_marker = "PRIVATE_MEMORY_CONTENT"
    client, calls = _client_with_responses([
        _completion(f'{{"value":"{secret_marker}'),
        _completion('{"value":"ok"}'),
    ])

    with caplog.at_level(logging.INFO, logger="app.agent.models.llm_client"):
        result = _invoke(client)

    assert result == {"value": "ok"}
    assert len(calls) == 2
    assert "JSONDecodeError" in caplog.text
    assert "line=1" in caplog.text
    assert "column=" in caplog.text
    assert "position=" in caplog.text
    assert secret_marker not in caplog.text


def test_ainvoke_structured_retries_empty_content() -> None:
    """验证空结构化正文触发重试。"""
    client, calls = _client_with_responses([
        _completion(None),
        _completion('{"value":"ok"}'),
    ])

    assert _invoke(client) == {"value": "ok"}
    assert len(calls) == 2


def test_ainvoke_structured_raises_after_two_non_stop_responses() -> None:
    """验证两次非 stop 响应后明确失败。"""
    client, calls = _client_with_responses([
        _completion('{"value":"first"}', "length"),
        _completion('{"value":"second"}', "content_filter"),
    ])

    with pytest.raises(
        RuntimeError,
        match=r"finish_reason=content_filter.*attempts=2",
    ):
        _invoke(client)

    assert len(calls) == 2


def test_ainvoke_structured_reraises_last_json_decode_error() -> None:
    """验证重试后重新抛出最后的 JSON 解析错误。"""
    client, calls = _client_with_responses([
        _completion('{"value":"first'),
        _completion('{"value":"second'),
    ])

    with pytest.raises(json.JSONDecodeError) as exc_info:
        _invoke(client)

    assert exc_info.value.doc == '{"value":"second'
    assert len(calls) == 2


def test_astream_reads_usage_from_empty_choices_chunk() -> None:
    """验证没有 choices 的末尾片段仍读取真实用量。"""
    async def scenario():
        """由外层测试调用，完成“没有 choices 的末尾片段仍读取真实用量”的异步步骤。"""
        client = LLMClient(LLMConfig(model="m", api_key="k", base_url="http://x/v1"))
        calls = []

        async def chunks():
            """向外层测试或模型客户端逐项产出本用例准备的流式片段。

            该定义仅用于外层用例：验证没有 choices 的末尾片段仍读取真实用量。
            """
            yield SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=123, completion_tokens=7, total_tokens=130),
            )

        async def fake_create(**kwargs):
            """按脚本返回模型响应或模型流，替代 SDK 的远程创建请求。

            该定义仅用于外层用例：验证没有 choices 的末尾片段仍读取真实用量。

            Args:
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            calls.append(kwargs)
            return chunks()

        client._client.chat.completions.create = fake_create
        try:
            received = [chunk async for chunk in client.astream([{"role": "user", "content": "x"}])]
            return calls, received
        finally:
            await client.close()

    calls, chunks = asyncio.run(scenario())
    assert calls[0]["stream_options"] == {"include_usage": True}
    assert chunks[0].usage == TokenUsage(prompt_tokens=123, completion_tokens=7, total_tokens=130)
    assert chunks[-1].finish_reason == "stop"


def test_astream_retries_without_usage_option_and_caches_gateway_capability() -> None:
    """验证不支持 usage 选项时降级重试并缓存网关能力。"""
    async def scenario():
        """由外层测试调用，完成“不支持 usage 选项时降级重试并缓存网关能力”的异步步骤。"""
        client = LLMClient(LLMConfig(model="m", api_key="k", base_url="http://no-usage/v1"))
        calls = []

        async def empty_stream():
            """提供不包含最终回复的模型流，验证空回复被明确拒绝。

            该定义仅用于外层用例：验证不支持 usage 选项时降级重试并缓存网关能力。
            """
            if False:
                yield None

        async def fake_create(**kwargs):
            """按脚本返回模型响应或模型流，替代 SDK 的远程创建请求。

            该定义仅用于外层用例：验证不支持 usage 选项时降级重试并缓存网关能力。

            Args:
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            calls.append(kwargs)
            if "stream_options" in kwargs:
                raise TypeError("unexpected keyword argument 'stream_options'")
            return empty_stream()

        client._client.chat.completions.create = fake_create
        try:
            for _ in range(2):
                _ = [chunk async for chunk in client.astream([{"role": "user", "content": "x"}])]
            return calls
        finally:
            await client.close()

    calls = asyncio.run(scenario())
    assert "stream_options" in calls[0]
    assert all("stream_options" not in call for call in calls[1:])
