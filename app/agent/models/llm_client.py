"""
LLM API 客户端

基于官方 openai SDK（AsyncOpenAI）实现。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Any

from openai import AsyncOpenAI

from app.agent.state.messages import ToolCall

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    """保存主模型客户端的地址、凭据、模型名称、温度和请求超时。"""
    model: str
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.7
    timeout: float = 120.0


@dataclass
class LLMResponse:
    """保存一次非流式模型响应的正文、工具调用、结束原因和用量。"""
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"  # 模型结束原因，例如 stop、tool_calls 或 length。

    @property
    def has_tool_calls(self) -> bool:
        """判断最终模型响应是否包含需要继续执行的工具调用。

        Returns:
            bool: tool_calls 非空时为 True。
        """
        return len(self.tool_calls) > 0


@dataclass
class TokenUsage:
    """记录模型服务报告的输入、输出及总 token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class StreamChunk:
    """承载一个流式输出片段，可以包含文字、完整工具调用、结束原因或用量。"""
    content: str | None = None
    tool_call: ToolCall | None = None
    finish_reason: str | None = None
    usage: TokenUsage | None = None

    @property
    def is_done(self) -> bool:
        """根据模型片段的结束原因判断是否已结束本次生成。

        Returns:
            bool: finish_reason 已提供时为 True。
        """
        return self.finish_reason is not None


class LLMClient:
    """LLM API 客户端（AsyncOpenAI 后端）"""

    _stream_usage_capabilities: dict[tuple[str, str], bool] = {}

    def __init__(self, config: LLMConfig):
        """按模型地址、凭据和超时装配异步客户端，尚未调用模型。

        Args:
            config (LLMConfig): 调用方提供的配置。
        """
        self.config = config
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=3
        )

    async def close(self) -> None:
        """关闭模型 SDK 客户端及其持有的网络连接。"""
        await self._client.close()

    async def __aenter__(self) -> "LLMClient":
        """异步上下文开始时提供当前模型客户端。

        Returns:
            LLMClient: 当前 LLMClient。
        """
        return self

    async def __aexit__(self, *args) -> None:
        """异步上下文结束时关闭模型连接，不吞掉调用异常。

        Args:
            args (Any): 当前工具调用或被包装函数的参数。
        """
        await self.close()

    # ==================== 内部工具 ====================

    def _base_payload(self, messages: list[dict], **kwargs) -> dict:
        """组合当前消息和模型默认选项，再应用本次调用的覆盖参数。

        Args:
            messages (list[dict]): 按对话顺序排列的消息集合。
            kwargs (Any): 传递给被调用函数的其他关键字参数。
        """
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            **kwargs,
        }
        return payload

    @staticmethod
    def _accumulate_tool_call(
        acc: list[dict],
        active_by_index: dict[int, int],
        tc_delta: Any,
    ) -> None:
        """按 call id 聚合工具 delta，并兼容网关重复使用同一 index。

        Args:
            acc (list[dict]): 按工具调用编号累积的名称、参数和附加协议内容。
            active_by_index (dict[int, int]): 流式工具索引到当前调用记录位置的映射。
            tc_delta (Any): SDK 本次提供的工具调用片段。
        """
        idx = tc_delta.index if tc_delta.index is not None else 0
        incoming_id = tc_delta.id or ""

        slot_position: int | None = None
        if incoming_id:
            slot_position = next(
                (
                    position for position, candidate in enumerate(acc)
                    if candidate["id"] == incoming_id
                ),
                None,
            )

        if slot_position is None:
            active_position = active_by_index.get(idx)
            if active_position is not None and not incoming_id:
                slot_position = active_position
            elif (
                active_position is not None
                and not acc[active_position]["id"]
            ):
                slot_position = active_position
            else:
                slot_position = len(acc)
                acc.append({
                    "id": "",
                    "name": "",
                    "arguments": "",
                    "extra_content": None,
                })

        active_by_index[idx] = slot_position
        slot = acc[slot_position]
        if tc_delta.id:
            slot["id"] = tc_delta.id
        if tc_delta.function:
            if tc_delta.function.name:
                slot["name"] = tc_delta.function.name
            if tc_delta.function.arguments:
                slot["arguments"] += tc_delta.function.arguments
        extra_content = getattr(tc_delta, "extra_content", None)
        if extra_content:
            if hasattr(extra_content, "model_dump"):
                extra_content = extra_content.model_dump(exclude_none=True, mode="json")
            slot["extra_content"] = extra_content

    @staticmethod
    def _parse_tool_arguments(raw_arguments: str) -> dict:
        """解析单个完整的 JSON 对象形式工具参数。

        Args:
            raw_arguments (str): 模型拼接完成但尚未解析的工具参数 JSON 文本。
        """
        if not raw_arguments.strip():
            return {}

        try:
            value = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            logger.warning(
                "工具参数解析失败（长度=%d，错误位置=%d）",
                len(raw_arguments),
                exc.pos,
            )
            raise ValueError("工具参数解析失败：不是单个完整的 JSON 对象") from exc

        if not isinstance(value, dict):
            logger.warning("工具参数解析失败：顶层类型=%s", type(value).__name__)
            raise ValueError("工具参数解析失败：必须是 JSON 对象")

        return value

    @classmethod
    def _build_tool_call(cls, slot: dict) -> ToolCall:
        """从累积的工具调用 slot 构造 ToolCall。

        Args:
            slot (dict): 当前已累积完整的工具名称、编号及参数记录。
        """
        args = cls._parse_tool_arguments(slot["arguments"])
        return ToolCall(
            id=slot["id"],
            name=slot["name"],
            args=args,
            extra_content=slot.get("extra_content"),
        )

    # ==================== 流式调用 ====================

    async def astream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用 LLM

        Args:
            messages (list[dict]): 按对话顺序排列的消息集合。
            tools (list[dict] | None): 当前向模型公开的工具定义。
            kwargs (Any): 传递给被调用函数的其他关键字参数。

        Yields:
            StreamChunk: 文本 delta 即时产出；工具调用在流末一次性产出（避免重复发射）。
        """
        payload = self._base_payload(messages, stream=True, **kwargs)
        if tools:
            payload["tools"] = tools
        # 1. 按地址和模型缓存网关是否支持流式用量，首次请求先尝试读取真实 token 数。
        capability_key = (self.config.base_url.rstrip("/"), self.config.model)
        include_usage = self._stream_usage_capabilities.get(capability_key, True)
        if include_usage:
            payload["stream_options"] = {"include_usage": True}

        # 按 call id 累积工具调用，index 仅用于路由缺少 id 的后续分片。
        tool_acc: list[dict] = []
        active_tool_by_index: dict[int, int] = {}
        final_reason: str | None = None

        try:
            try:
                stream = await self._client.chat.completions.create(**payload)
            except Exception as exc:
                message = str(exc).lower()
                unsupported = "stream_options" in message and any(
                    marker in message
                    for marker in ("unexpected", "unsupported", "unknown", "unrecognized", "invalid")
                )
                if not include_usage or not unsupported:
                    raise
                # 1.1. 只有明确拒绝 stream_options 才去掉该选项重试，其他调用错误继续抛出。
                self._stream_usage_capabilities[capability_key] = False
                payload.pop("stream_options", None)
                stream = await self._client.chat.completions.create(**payload)
            # 2. 用量可能在没有 choices 的末尾片段到达，先读取用量再判断是否有正文。
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    self._stream_usage_capabilities[capability_key] = True
                    yield StreamChunk(usage=TokenUsage(
                        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
                    ))
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                if delta and delta.content:
                    yield StreamChunk(content=delta.content)

                if delta and delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        self._accumulate_tool_call(
                            tool_acc,
                            active_tool_by_index,
                            tc_delta,
                        )

                if choice.finish_reason:
                    final_reason = choice.finish_reason

            # 3. 流正常结束后才交付累积完整的工具调用，避免把参数碎片当作独立执行请求。
            for slot in tool_acc:
                yield StreamChunk(tool_call=self._build_tool_call(slot))

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
        """发起一次非流式模型请求，将 SDK 响应转换为项目模型响应。

        Args:
            messages (list[dict]): 按对话顺序排列的消息集合。
            tools (list[dict] | None): 当前向模型公开的工具定义。
            kwargs (Any): 传递给被调用函数的其他关键字参数。
        """
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
                args = self._parse_tool_arguments(tc.function.arguments or "")
                extra_content = getattr(tc, "extra_content", None)
                if extra_content and hasattr(extra_content, "model_dump"):
                    extra_content = extra_content.model_dump(exclude_none=True, mode="json")
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    args=args,
                    extra_content=extra_content,
                ))

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
        """结构化输出调用 LLM（json_schema 强约束）

        Args:
            messages (list[dict]): 按对话顺序排列的消息集合。
            schema (dict): 当前要检查或转换的 JSON Schema。
            kwargs (Any): 传递给被调用函数的其他关键字参数。
        """
        # 1. 结构化生成最多请求两次，重试使用相同消息和 Schema，不把失败响应正文写入日志。
        max_attempts = 2
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

        for attempt in range(1, max_attempts + 1):
            try:
                completion = await self._client.chat.completions.create(**payload)
            except Exception as e:
                logger.error("LLM 结构化输出调用失败: %s", e)
                raise

            choice = completion.choices[0]
            finish_reason = choice.finish_reason or "unknown"
            content = choice.message.content or ""
            content_length = len(content)

            # 2. 非正常结束的正文可能不完整，第一次重新请求，第二次仍未正常结束则明确失败。
            if finish_reason != "stop":
                logger.warning(
                    "LLM 结构化输出未正常结束: model=%s, attempt=%d/%d, "
                    "finish_reason=%s, content_length=%d",
                    self.config.model,
                    attempt,
                    max_attempts,
                    finish_reason,
                    content_length,
                )
                if attempt < max_attempts:
                    continue
                raise RuntimeError(
                    "LLM 结构化输出未正常结束: "
                    f"finish_reason={finish_reason}, attempts={max_attempts}"
                )

            # 3. 正常结束后再解析 JSON；解析失败只记录位置与长度，最多重试一次。
            try:
                result = json.loads(content)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "LLM 结构化输出 JSONDecodeError: model=%s, attempt=%d/%d, "
                    "finish_reason=%s, content_length=%d, line=%d, column=%d, "
                    "position=%d",
                    self.config.model,
                    attempt,
                    max_attempts,
                    finish_reason,
                    content_length,
                    exc.lineno,
                    exc.colno,
                    exc.pos,
                )
                if attempt < max_attempts:
                    continue
                raise

            if attempt > 1:
                logger.info(
                    "LLM 结构化输出重试成功: model=%s, attempt=%d/%d",
                    self.config.model,
                    attempt,
                    max_attempts,
                )
            return result

        raise RuntimeError("LLM 结构化输出重试流程异常结束")


def create_llm_client(
    model: str,
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    temperature: float = 0.7,
    timeout: float = 120.0,
) -> LLMClient:
    """将调用方提供的模型参数组合为配置并创建异步模型客户端。

    Args:
        model (str): 模型配置或模型类型。
        api_key (str): 模型服务调用凭据，仅用于请求认证。
        base_url (str): 兼容模型接口的服务地址。
        temperature (float): 生成温度，控制模型采样差异。
        timeout (float): 模型请求或系统等待的超时秒数。
    """
    config = LLMConfig(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        timeout=timeout,
    )
    return LLMClient(config)
