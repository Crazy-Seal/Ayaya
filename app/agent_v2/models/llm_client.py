"""
LLM API 客户端

基于官方 openai SDK（AsyncOpenAI）实现。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Any

from openai import AsyncOpenAI

from app.agent_v2.message import ToolCall

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    """LLM 配置"""
    model: str
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.7
    max_tokens: int | None = None
    timeout: float = 120.0


@dataclass
class LLMResponse:
    """LLM 响应（非流式）"""
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"  # "stop" | "tool_calls" | "length" | ...

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


@dataclass
class StreamChunk:
    """流式响应块"""
    content: str | None = None
    tool_call: ToolCall | None = None
    finish_reason: str | None = None

    @property
    def is_done(self) -> bool:
        return self.finish_reason is not None


class LLMClient:
    """LLM API 客户端（AsyncOpenAI 后端）"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=3
        )

    async def close(self) -> None:
        """关闭客户端"""
        await self._client.close()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    # ==================== 内部工具 ====================

    def _base_payload(self, messages: list[dict], **kwargs) -> dict:
        """构造公共请求参数"""
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            **kwargs,
        }
        if self.config.max_tokens:
            payload["max_tokens"] = self.config.max_tokens
        return payload

    @staticmethod
    def _accumulate_tool_call(acc: dict[int, dict], tc_delta: Any) -> None:
        """把流式工具调用 delta 累积进 acc（按 index 聚合）"""
        idx = tc_delta.index if tc_delta.index is not None else 0
        slot = acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
        if tc_delta.id:
            slot["id"] = tc_delta.id
        if tc_delta.function:
            if tc_delta.function.name:
                slot["name"] = tc_delta.function.name
            if tc_delta.function.arguments:
                slot["arguments"] += tc_delta.function.arguments

    @staticmethod
    def _build_tool_call(slot: dict) -> ToolCall:
        """从累积的工具调用 slot 构造 ToolCall（容错解析参数）"""
        try:
            args = json.loads(slot["arguments"]) if slot["arguments"] else {}
        except json.JSONDecodeError:
            logger.warning("工具参数解析失败，回退为空对象: %s", slot["arguments"])
            args = {}
        return ToolCall(id=slot["id"], name=slot["name"], args=args)

    # ==================== 流式调用 ====================

    async def astream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用 LLM

        Yields:
            StreamChunk: 文本 delta 即时产出；工具调用在流末一次性产出（避免重复发射）。
        """
        payload = self._base_payload(messages, stream=True, **kwargs)
        if tools:
            payload["tools"] = tools

        # 按 index 累积工具调用，唯一在流末 flush
        tool_acc: dict[int, dict] = {}
        final_reason: str | None = None

        try:
            stream = await self._client.chat.completions.create(**payload)
            async for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                if delta and delta.content:
                    yield StreamChunk(content=delta.content)

                if delta and delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        self._accumulate_tool_call(tool_acc, tc_delta)

                if choice.finish_reason:
                    final_reason = choice.finish_reason

            # 流结束：一次性产出完整工具调用
            for idx in sorted(tool_acc.keys()):
                yield StreamChunk(tool_call=self._build_tool_call(tool_acc[idx]))

            yield StreamChunk(finish_reason=final_reason or "stop")

        except Exception as e:
            logger.error("LLM 流式调用失败: %s", e)
            raise

    # ==================== 非流式调用 ====================

    async def ainvoke(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """非流式调用 LLM"""
        payload = self._base_payload(messages, stream=False, **kwargs)
        if tools:
            payload["tools"] = tools

        try:
            completion = await self._client.chat.completions.create(**payload)
        except Exception as e:
            logger.error("LLM 调用失败: %s", e)
            raise

        choice = completion.choices[0]
        message = choice.message

        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))

        return LLMResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
        )

    # ==================== 结构化输出 ====================

    async def ainvoke_structured(
        self,
        messages: list[dict],
        schema: dict,
        **kwargs,
    ) -> dict:
        """结构化输出调用 LLM（json_schema 强约束）"""
        payload = self._base_payload(
            messages,
            stream=False,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": True,
                    "schema": schema,
                },
            },
            **kwargs,
        )

        try:
            completion = await self._client.chat.completions.create(**payload)
        except Exception as e:
            logger.error("LLM 结构化输出调用失败: %s", e)
            raise

        content = completion.choices[0].message.content or "{}"
        return json.loads(content)


def create_llm_client(
    model: str,
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    temperature: float = 0.7,
    max_tokens: int | None = None,
    timeout: float = 120.0,
) -> LLMClient:
    """创建 LLM 客户端的便捷函数"""
    config = LLMConfig(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    return LLMClient(config)
