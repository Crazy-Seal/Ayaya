"""统一模型入口：装配适配器，调用方只处理 Ayaya 消息。"""

from __future__ import annotations

from typing import Any, AsyncIterator, Sequence, TYPE_CHECKING
from types import TracebackType
from app.agent.models.contracts import OPENAI_CHAT_COMPLETIONS, InputMessage, LLMConfig, LLMResponse, StreamChunk, TokenUsage, ToolDefinition, ModelAdapter, ModelContextOverflowError
from app.agent.state.messages import BaseMessage

if TYPE_CHECKING:
    from openai import AsyncOpenAI


class LLMClient:
    """按协议装配模型模块，允许测试通过同一接口替换适配器。

    Attributes:
        config (LLMConfig): 本实例固定使用的调用配置。
    """

    def __init__(self, config: LLMConfig, *, adapter: ModelAdapter | None = None, sdk_client: AsyncOpenAI | None = None):
        """装配实现，不调用模型。

        Args:
            config (LLMConfig): 地址、模型及内部协议选择。
            adapter (ModelAdapter | None): 业务接口的测试或扩展实现。
            sdk_client (AsyncOpenAI | None): 可注入的 OpenAI 异步客户端，测试时替换其网络请求。
        Raises:
            ValueError: 未知协议且没有显式适配器。
        """
        self.config = config
        if adapter is not None:
            self._adapter = adapter
        elif config.api_format == OPENAI_CHAT_COMPLETIONS:
            # 1. 只有选择该格式时才导入厂商实现，未知格式不能静默回退。
            from app.agent.models.openai_chat import OpenAIChatCompletionsAdapter
            self._adapter = OpenAIChatCompletionsAdapter(config, sdk_client=sdk_client)
        else:
            raise ValueError(f"不支持的模型 API 格式: {config.api_format}")

    @staticmethod
    def _check_options(options: dict) -> None:
        """公共选项使用业务语义，供应商字段只能由适配器生成。

        Args:
            options (dict): 供应商无关的本次生成选项。
        """
        unknown = set(options) - {"temperature", "max_output_tokens", "top_p", "json_mode", "tool_choice"}
        if unknown:
            raise ValueError(f"未知模型生成选项: {sorted(unknown)}")

    def estimate_input(self, messages: Sequence[InputMessage], tools: Sequence[ToolDefinition] = ()) -> int:
        """按适配器实际投影估算输入，不修改消息。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的 Ayaya 消息；方法不会修改原消息。
            tools (Sequence[ToolDefinition]): 当前权限筛选后公开给模型的业务工具定义。

        Returns:
            int: 当前适配器实际请求结构的输入 token 估算。
        """
        return self._adapter.estimate_input(messages, tools)

    async def astream(self, messages: Sequence[InputMessage], tools: Sequence[ToolDefinition] | None = None, **options: Any) -> AsyncIterator[StreamChunk]:
        """产生业务流片段，取消等待时也关闭底层生成器。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的 Ayaya 消息；方法不会修改原消息。
            tools (Sequence[ToolDefinition] | None): 当前权限筛选后公开给模型的业务工具定义。
            options (Any): 供应商无关的本次生成选项。

        Yields:
            AsyncIterator[StreamChunk]: 供调用方即时消费的正文、完整调用、结束状态和用量片段。
        """
        self._check_options(options)
        stream = self._adapter.astream(messages, tools, **options)
        try:
            async for chunk in stream:
                yield chunk
        finally:
            await stream.aclose()

    async def ainvoke(self, messages: Sequence[InputMessage], tools: Sequence[ToolDefinition] | None = None, **options: Any) -> LLMResponse:
        """请求完整业务响应。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的 Ayaya 消息；方法不会修改原消息。
            tools (Sequence[ToolDefinition] | None): 当前权限筛选后公开给模型的业务工具定义。
            options (Any): 供应商无关的本次生成选项。

        Returns:
            LLMResponse: 正文、工具调用、结束原因与可用恢复材料。
        """
        self._check_options(options)
        return await self._adapter.ainvoke(messages, tools, **options)

    async def ainvoke_structured(self, messages: Sequence[InputMessage], schema: dict, **options: Any) -> dict:
        """按业务 schema 请求对象，重试与响应校验由适配器执行。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的 Ayaya 消息；方法不会修改原消息。
            schema (dict): 业务输出必须符合的 JSON Schema。
            options (Any): 供应商无关的本次生成选项。

        Returns:
            dict: 解析并校验后的业务 JSON 输出。
        """
        self._check_options(options)
        return await self._adapter.ainvoke_structured(messages, schema, **options)

    def invoke(self, messages: Sequence[InputMessage], tools: Sequence[ToolDefinition] | None = None, **options: Any) -> LLMResponse:
        """同步请求不运行异步事件循环或复用其 HTTP 客户端。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的 Ayaya 消息；方法不会修改原消息。
            tools (Sequence[ToolDefinition] | None): 当前权限筛选后公开给模型的业务工具定义。
            options (Any): 供应商无关的本次生成选项。

        Returns:
            LLMResponse: 同步请求得到的业务响应。
        """
        self._check_options(options)
        return self._adapter.invoke(messages, tools, **options)

    async def close(self) -> None:
        """释放适配器的请求资源。"""
        await self._adapter.close()

    async def __aenter__(self) -> "LLMClient":
        """进入异步资源管理范围。"""
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None,
                        exc: BaseException | None, traceback: TracebackType | None) -> None:
        """离开资源范围时关闭连接，不吞掉调用错误。

        Args:
            exc_type (type[BaseException] | None): 退出时的异常类型，正常结束时为空。
            exc (BaseException | None): 当前异常，正常结束时为空。
            traceback (TracebackType | None): 当前异常的调用记录。
        """
        await self.close()
