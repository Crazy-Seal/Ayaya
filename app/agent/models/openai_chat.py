"""OpenAI Chat Completions 的唯一请求与响应协议实现。"""

import json
import logging
from typing import Any, AsyncIterator, Sequence

from openai import AsyncOpenAI, OpenAI
from app.agent.models.contracts import ModelFinishReason, InputMessage, LLMConfig, LLMResponse, StreamChunk, TokenUsage, ToolDefinition, ModelContextOverflowError, ProjectedMessage
from app.schemas.message_types import COMPRESSED_SCREENSHOT_PLACEHOLDER, MediaKind, ImageContextState
from app.agent.state.messages import (BaseMessage, HumanMessage, AssistantMessage, ToolMessage, SystemMessage,
    ContextSummaryMessage, TaskResultMessage, EnvironmentPerceptionMessage, SubagentMessage,
    ImageContent, TextContent, ToolCall, ProviderData, text_content, notification_text, to_json_value)
from app.agent.models.openai_message_names import (SCREENSHOT_MESSAGE_NAME, TOOL_IMAGE_MESSAGE_NAME, MCP_TOOL_IMAGE_MESSAGE_NAME,
    SCREENSHOT_COMPRESSED_NAME, CONTEXT_SUMMARY_MESSAGE_NAME)
from app.agent.tools.core.schema import normalize_tool_schema_for_model
from app.agent.utils.domain.token_estimation import RoughTokenEstimator

logger = logging.getLogger("app.agent.models.llm_client")

class OpenAIChatCompletionsAdapter:
    """集中实现 Chat Completions 请求、输出、预算和资源管理。"""

    _stream_usage_capabilities: dict[tuple[str, str], bool] = {}

    def __init__(self, config: LLMConfig, *, sdk_client: Any = None):
        """按模型地址、凭据和超时装配异步客户端，尚未调用模型。

        Args:
            config (LLMConfig): 调用方提供的配置。
        """
        self.config = config
        self._sync_client = None
        self._client = sdk_client or AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=config.max_retries
        )

    async def close(self) -> None:
        """关闭模型 SDK 客户端及其持有的网络连接。"""
        if hasattr(self._client, "close"):
            await self._client.close()
        if self._sync_client is not None:
            self._sync_client.close()

    async def __aenter__(self) -> "OpenAIChatCompletionsAdapter":
        """异步上下文开始时提供当前模型客户端。

        Returns:
            OpenAIChatCompletionsAdapter: 当前协议实现。
        """
        return self

    async def __aexit__(self, *args) -> None:
        """异步上下文结束时关闭模型连接，不吞掉调用异常。

        Args:
            args (Any): 当前工具调用或被包装函数的参数。
        """
        await self.close()

    # ==================== 内部工具 ====================

    def _base_payload(self, messages: Sequence[InputMessage], **kwargs) -> dict:
        """组合当前消息和模型默认选项，再应用本次调用的覆盖参数。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的消息集合。
            kwargs (Any): 传递给被调用函数的其他关键字参数。

        Returns:
            dict: 供下一处理阶段使用的独立 JSON 对象。
        """
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": self.encode_messages(messages),
            "temperature": self.config.temperature,
            **kwargs,
        }
        if "max_output_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_output_tokens")
        if payload.pop("json_mode", False):
            payload["response_format"] = {"type": "json_object"}
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
            provider_data=cls._provider_data(slot.get("extra_content"), slot["config"]),
        )

    # ==================== 流式调用 ====================

    async def astream(
        self,
        messages: Sequence[InputMessage],
        tools: Sequence[ToolDefinition] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用 LLM

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的消息集合。
            tools (Sequence[ToolDefinition] | None): 当前向模型公开的工具定义。
            kwargs (Any): 传递给被调用函数的其他关键字参数。

        Yields:
            StreamChunk: 文本 delta 即时产出；工具调用在流末一次性产出（避免重复发射）。
        """
        payload = self._base_payload(messages, stream=True, **kwargs)
        if tools:
            payload["tools"] = self.encode_tools(tools)
        # 1. 按地址和模型缓存网关是否支持流式用量，首次请求先尝试读取真实 token 数。
        capability_key = (self.config.base_url.rstrip("/"), self.config.model)
        include_usage = self._stream_usage_capabilities.get(capability_key, True)
        if include_usage:
            payload["stream_options"] = {"include_usage": True}

        # 按 call id 累积工具调用，index 仅用于路由缺少 id 的后续分片。
        tool_acc: list[dict] = []
        active_tool_by_index: dict[int, int] = {}
        final_reason: str | None = None
        assistant_extra: Any = None
        refused = False
        stream = None

        try:
            try:
                stream = await self._request(payload)
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
                stream = await self._request(payload)
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
                refused = refused or bool(getattr(delta, "refusal", None))
                if delta is not None and getattr(delta, "extra_content", None):
                    assistant_extra = delta.extra_content

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
                yield StreamChunk(tool_call=self._build_tool_call({**slot, "config": self.config}))

            yield StreamChunk(finish_reason=ModelFinishReason.REFUSED if refused else self._finish_reason(final_reason or "stop"),
                              provider_data=self._provider_data(assistant_extra, self.config))

        except Exception as e:
            logger.error("LLM 流式调用失败: %s", e)
            self._raise_model_error(e)
            raise
        finally:
            # 4. 正常结束、停止回答及取消等待都释放本次响应连接；客户端仍供下一轮使用。
            if stream is not None and hasattr(stream, "close"):
                await stream.close()

    # ==================== 非流式调用 ====================

    async def ainvoke(
        self,
        messages: Sequence[InputMessage],
        tools: Sequence[ToolDefinition] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """发起一次非流式模型请求，将 SDK 响应转换为项目模型响应。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的消息集合。
            tools (Sequence[ToolDefinition] | None): 当前向模型公开的工具定义。
            kwargs (Any): 传递给被调用函数的其他关键字参数。

        Returns:
            LLMResponse: 正文、工具调用、结束原因与可用恢复材料。
        """
        payload = self._base_payload(messages, stream=False, **kwargs)
        if tools:
            payload["tools"] = self.encode_tools(tools)

        try:
            completion = await self._request(payload)
        except Exception as e:
            logger.error("LLM 调用失败: %s", e)
            raise

        choice = completion.choices[0]
        message = choice.message

        tool_calls: list[ToolCall] = []
        if getattr(message, "tool_calls", None):
            for tc in message.tool_calls:
                args = self._parse_tool_arguments(tc.function.arguments or "")
                extra_content = getattr(tc, "extra_content", None)
                if extra_content and hasattr(extra_content, "model_dump"):
                    extra_content = extra_content.model_dump(exclude_none=True, mode="json")
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    args=args,
                    provider_data=self._provider_data(extra_content, self.config),
                ))

        return LLMResponse(
            content=text_content(message.content or ""),
            provider_data=self._provider_data(getattr(message, "extra_content", None), self.config),
            tool_calls=tool_calls,
            finish_reason=ModelFinishReason.REFUSED if getattr(message, "refusal", None) else self._finish_reason(getattr(choice, "finish_reason", None)),
            usage=self._usage(getattr(completion, "usage", None)),
        )

    # ==================== 结构化输出 ====================

    async def ainvoke_structured(
        self,
        messages: Sequence[InputMessage],
        schema: dict,
        **kwargs,
    ) -> dict:
        """结构化输出调用 LLM（json_schema 强约束）

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的消息集合。
            schema (dict): 当前要检查或转换的 JSON Schema。
            kwargs (Any): 传递给被调用函数的其他关键字参数。

        Returns:
            dict: 解析并校验后的业务 JSON 输出。
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
                completion = await self._request(payload)
            except Exception as e:
                logger.error("LLM 结构化输出调用失败: %s", e)
                raise

            choice = completion.choices[0]
            finish_reason = choice.finish_reason or ModelFinishReason.UNKNOWN
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


    @staticmethod
    def _usage(usage: Any) -> TokenUsage | None:
        """读取可选 SDK 用量，同步与异步完整响应使用相同业务结构。

        Args:
            usage (Any): 网关返回的用量对象；可能缺失。
        Returns:
            TokenUsage | None: 已规范化的用量，缺失时返回 None。
        """
        if usage is None:
            return None
        return TokenUsage(*(int(getattr(usage, key, 0) or 0) for key in
                            ("prompt_tokens", "completion_tokens", "total_tokens")))

    @staticmethod
    def _finish_reason(reason: str | None) -> ModelFinishReason:
        """把 SDK 原因转换成稳定的项目结束语义。

        Args:
            reason (str | None): 供应商返回的生成结束原因。

        Returns:
            ModelFinishReason: 供执行管道判断的项目结束原因。
        """
        return {"stop": ModelFinishReason.COMPLETED, "tool_calls": ModelFinishReason.TOOL_CALLS, "length": ModelFinishReason.LENGTH,
                "content_filter": ModelFinishReason.REFUSED}.get(reason, ModelFinishReason.UNKNOWN)

    @staticmethod
    def _provider_data(extra: Any, config: LLMConfig) -> ProviderData | None:
        """保存当前已支持的附加材料，归属信息来自发起请求的配置。

        Args:
            extra (Any): 模型回复中已支持的专用恢复材料。
            config (LLMConfig): 本次模型客户端固定使用的配置。
        """
        if not extra:
            return None
        if hasattr(extra, "model_dump"):
            extra = extra.model_dump(exclude_none=True, mode="json")
        return ProviderData(config.api_format, config.base_url, config.model, {"extra_content": extra})

    def _replay_data(self, data: ProviderData | None) -> dict:
        """只回传协议、地址和模型一致的已知材料，其他来源保留在本地。

        Args:
            data (ProviderData | None): 新协议存储的业务对象。

        Returns:
            dict: 供下一处理阶段使用的独立 JSON 对象。
        """
        if data is None or (data.api_format, data.endpoint, data.model) != (self.config.api_format, self.config.base_url.rstrip("/"), self.config.model):
            return {}
        return {key: to_json_value(value) for key, value in data.payload.items() if key == "extra_content"}

    def _encode_call(self, call: ToolCall) -> dict:
        """把对象参数编码为 Chat Completions function，仅在请求时字符串化。

        Args:
            call (ToolCall): 包含对象参数的业务工具调用。

        Returns:
            dict: 供下一处理阶段使用的独立 JSON 对象。
        """
        return {"id": call.id, "type": "function", "function": {"name": call.name,
            "arguments": json.dumps(to_json_value(call.args), ensure_ascii=False, sort_keys=True, separators=(",", ":"))},
            **self._replay_data(call.provider_data)}

    @staticmethod
    def _encode_content(content: Sequence[TextContent | ImageContent]) -> list[dict]:
        """去除业务图片元数据，压缩图片不读取文件、不重新加载 Base64。

        Args:
            content (Sequence[TextContent | ImageContent]): 按顺序排列的业务文字或图片内容块。
        """
        return [part.to_dict() if isinstance(part, TextContent) else {
            "type": "image_url", "image_url": {"url": part.url, **({"detail": part.detail} if part.detail else {})}}
            for part in content if isinstance(part, TextContent) or part.context_state == ImageContextState.AVAILABLE]

    def encode_messages(self, messages: Sequence[InputMessage]) -> list[dict]:
        """把业务消息投影为完整请求，整批工具结果之后再发送图片。

        Args:
            messages (Sequence[InputMessage]): 不可修改的临时业务输入。
        Returns:
            list[dict]: 不含内部字段的独立 SDK 请求消息。
        Raises:
            ValueError: 工具调用未配对、名称不一致或类型不受支持。
        """
        result: list[dict] = []
        expected: dict[str, str] = {}
        deferred: list[dict] = []
        # 1. 业务结果保持完整，只有这个临时投影拆分文字与图片。
        for message in messages:
            override = message.text if isinstance(message, ProjectedMessage) else None
            message = message.message if isinstance(message, ProjectedMessage) else message
            if not isinstance(message, BaseMessage):
                raise TypeError("模型入口只接受 Ayaya 消息")
            if expected and not isinstance(message, ToolMessage):
                raise ValueError("工具批次未闭合，不能发送下一条消息")
            if isinstance(message, ToolMessage):
                if expected.pop(message.tool_call_id, None) != message.tool_name:
                    raise ValueError("工具结果与请求不匹配")
                result.append({"role": "tool", "name": message.tool_name, "tool_call_id": message.tool_call_id, "content": message.text})
                images = [part for part in message.content if isinstance(part, ImageContent)]
                if images:
                    available = [part for part in images if part.context_state == ImageContextState.AVAILABLE]
                    if available:
                        kind = available[0].media_kind
                        name = {MediaKind.SCREENSHOT: SCREENSHOT_MESSAGE_NAME, MediaKind.MCP: MCP_TOOL_IMAGE_MESSAGE_NAME}.get(kind, TOOL_IMAGE_MESSAGE_NAME)
                        deferred.append({"role": "user", "name": name, "content": [
                            {"type": "text", "text": f"[系统消息]工具 {message.tool_name} 返回图片: "}, *self._encode_content(available)]})
                    if any(part.context_state == ImageContextState.COMPACTED for part in images):
                        deferred.append({"role": "user", "name": SCREENSHOT_COMPRESSED_NAME, "content": COMPRESSED_SCREENSHOT_PLACEHOLDER})
                # 1.1. 一批结果齐全后才展开图片，避免图片打断 SDK 工具协议。
                if not expected:
                    result.extend(deferred)
                    deferred.clear()
                continue
            if isinstance(message, AssistantMessage):
                item = {"role": "assistant", "content": message.text, **self._replay_data(message.provider_data)}
                if message.tool_calls:
                    item["tool_calls"] = [self._encode_call(call) for call in message.tool_calls]
                    expected = {call.id: call.name for call in message.tool_calls}
                    if len(expected) != len(message.tool_calls):
                        raise ValueError("重复工具调用编号")
                result.append(item)
            elif isinstance(message, HumanMessage):
                content = self._encode_content(message.content) if any(isinstance(part, ImageContent) for part in message.content) else message.text
                result.append({"role": "user", "content": content})
            elif isinstance(message, SystemMessage):
                result.append({"role": "system", "content": message.text})
            elif isinstance(message, ContextSummaryMessage):
                result.append({"role": "user", "name": CONTEXT_SUMMARY_MESSAGE_NAME, "content": message.text})
            elif isinstance(message, (TaskResultMessage, EnvironmentPerceptionMessage, SubagentMessage)):
                names = {"task_result": "runtime_task_result", "environment_perception": "runtime_environment_perception", "subagent": "runtime_subagent"}
                result.append({"role": "user", "name": names[message.message_type], "content": notification_text(message)})
            else:
                raise ValueError("适配器不支持的业务消息")
            if override is not None:
                result[-1]["content"] = override
        # 2. 不允许只发出工具请求的一半或孤立图片。
        if expected:
            raise ValueError("工具调用缺少结果")
        return result

    @staticmethod
    def encode_tools(tools: Sequence[ToolDefinition]) -> list[dict]:
        """在发送时生成 SDK 工具包装并规范化参数 schema。

        Args:
            tools (Sequence[ToolDefinition]): 当前权限筛选后公开给模型的业务工具定义。
        """
        return [{"type": "function", "function": {"name": tool.name, "description": tool.description,
            "parameters": normalize_tool_schema_for_model(to_json_value(tool.input_schema))}} for tool in tools]

    def estimate_input(self, messages: Sequence[InputMessage], tools: Sequence[ToolDefinition] = ()) -> int:
        """按与请求相同的投影估算，不把图片 Base64 当作正文 token。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的 Ayaya 消息；方法不会修改原消息。
            tools (Sequence[ToolDefinition]): 当前权限筛选后公开给模型的业务工具定义。

        Returns:
            int: 当前适配器实际请求结构的输入 token 估算。
        """
        encoded = self.encode_messages(messages)
        system = encoded.pop(0)["content"] if encoded and encoded[0]["role"] == "system" else ""
        return RoughTokenEstimator().estimate_raw(system_prompt=system, tools=self.encode_tools(tools), messages=encoded)

    async def _request(self, payload: dict) -> Any:
        """发起 SDK 请求，将明确上下文超限转换为公共异常。

        Args:
            payload (dict): 适配器生成的独立 SDK 请求。
        """
        try:
            return await self._client.chat.completions.create(**payload)
        except Exception as exc:
            self._raise_model_error(exc)
            raise

    @staticmethod
    def _raise_model_error(error: Exception) -> None:
        """仅识别明确长度错误，其他失败保持原有错误链。

        Args:
            error (Exception): 模型请求或流读取期间产生的异常。
        """
        text = str(error).lower()
        if "context_length_exceeded" in text or "maximum context" in text:
            raise ModelContextOverflowError("模型输入超过上下文限制") from error

    def invoke(self, messages: Sequence[InputMessage], tools: Sequence[ToolDefinition] | None = None, **options: Any) -> LLMResponse:
        """使用独立同步 SDK 资源，不在同步调用中运行异步事件循环。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的 Ayaya 消息；方法不会修改原消息。
            tools (Sequence[ToolDefinition] | None): 当前权限筛选后公开给模型的业务工具定义。
            options (Any): 供应商无关的本次生成选项。

        Returns:
            LLMResponse: 同步请求得到的业务响应。
        """
        if self._sync_client is None:
            self._sync_client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url,
                                      timeout=self.config.timeout, max_retries=self.config.max_retries)
        payload = self._base_payload(messages, **options)
        if tools:
            payload["tools"] = self.encode_tools(tools)
        try:
            completion = self._sync_client.chat.completions.create(**payload)
        except Exception as exc:
            self._raise_model_error(exc)
            raise
        choice = completion.choices[0]
        calls = [ToolCall(call.id, call.function.name, self._parse_tool_arguments(call.function.arguments or "{}"),
                         self._provider_data(getattr(call, "extra_content", None), self.config))
                 for call in choice.message.tool_calls or []]
        return LLMResponse(text_content(choice.message.content or ""), calls,
                           ModelFinishReason.REFUSED if getattr(choice.message, "refusal", None) else self._finish_reason(choice.finish_reason),
                           self._provider_data(getattr(choice.message, "extra_content", None), self.config),
                           self._usage(getattr(completion, "usage", None)))
