"""模型模块的业务配置、工具定义与供应商无关的结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence, AsyncIterator

from app.agent.state.messages import BaseMessage, ContentPart, ProviderData, ToolCall, content_text


OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"


class ModelFinishReason(StrEnum):
    """表示模型适配器转换后的项目结束原因。

    Attributes:
        COMPLETED (ModelFinishReason): 正文正常结束。
        TOOL_CALLS (ModelFinishReason): 模型要求调用工具。
        LENGTH (ModelFinishReason): 达到输出长度限制。
        REFUSED (ModelFinishReason): 模型拒绝生成。
        UNKNOWN (ModelFinishReason): 供应商未提供可识别的结束原因。
    """

    COMPLETED = "completed"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    REFUSED = "refused"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProjectedMessage:
    """保存内部通知的临时文字投影，原业务字段和身份仍由 message 提供。

    Attributes:
        message (BaseMessage): 保留全部身份和业务字段的原始消息。
        text (str): 本次请求添加时间等上下文后的临时正文。
    """
    message: BaseMessage
    text: str


InputMessage = BaseMessage | ProjectedMessage


@dataclass(frozen=True)
class LLMConfig:
    """保存一次客户端生命周期的配置，不加载或修改会话设置。

    Attributes:
        model (str): 本次请求的模型标识。
        api_key (str): 调用凭据，不写入消息与恢复材料。
        base_url (str): 服务地址，同时用于恢复材料归属判断。
        temperature (float): 默认生成温度。
        timeout (float): 单次请求超时秒数。
        max_retries (int): SDK 网络请求的重试上限。
        api_format (str): 适配器选择与用量基线隔离使用的协议标识。
    """
    model: str
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.7
    timeout: float = 120.0
    max_retries: int = 3
    api_format: str = OPENAI_CHAT_COMPLETIONS


@dataclass(frozen=True)
class ToolDefinition:
    """向模型公开工具的业务定义，权限筛选由 ToolManager 完成。

    Attributes:
        name (str): 业务工具名称。
        description (str): 供模型理解的工具说明。
        input_schema (Mapping[str, Any]): 不可变业务参数 Schema。
    """
    name: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        """复制工具 Schema，防止公开定义被调用方在估算后改变。"""
        from app.agent.state.messages import _freeze, _require_mapping
        object.__setattr__(self, "input_schema", _freeze(_require_mapping(self.input_schema, "input_schema")))

    def to_dict(self) -> dict[str, Any]:
        """提供独立业务工具 JSON，供预算指纹及测试使用。

        Returns:
            dict[str, Any]: 供下一处理阶段使用的独立 JSON 对象。
        """
        from app.agent.state.messages import to_json_value
        return {"name": self.name, "description": self.description, "input_schema": to_json_value(self.input_schema)}


@dataclass
class LLMResponse:
    """保存完整模型正文、调用和专用材料，结束原因使用项目语义。

    Attributes:
        content (tuple[ContentPart, ...]): 完整响应的业务正文。
        tool_calls (list[ToolCall]): 模型要求执行的完整调用。
        finish_reason (ModelFinishReason): completed、tool_calls、length、refused 或 unknown。
        provider_data (ProviderData | None): 归属当前调用的专用恢复材料。
        usage (TokenUsage | None): 供应商可选报告的真实用量。
    """
    content: tuple[ContentPart, ...]
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: ModelFinishReason = ModelFinishReason.COMPLETED
    provider_data: ProviderData | None = None
    usage: TokenUsage | None = None

    @property
    def text(self) -> str:
        """读取模型可见文字，不暴露专用材料。

        Returns:
            str: 按顺序拼接的可见文字，不包含图片和供应商材料。
        """
        return content_text(self.content)

    @property
    def has_tool_calls(self) -> bool:
        """判断本次响应是否要求执行工具。

        Returns:
            bool: 满足该业务条件时返回 True。
        """
        return bool(self.tool_calls)


@dataclass(frozen=True)
class TokenUsage:
    """保存模型报告的输入、输出和总 token 用量。

    Attributes:
        prompt_tokens (int): 输入 token 数。
        completion_tokens (int): 输出 token 数。
        total_tokens (int): 输入与输出总数。
    """
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class StreamChunk:
    """即时交付文字，流完整结束后交付工具调用及归属材料。

    Attributes:
        content (str | None): 本次到达的正文片段。
        tool_call (ToolCall | None): 累积完参数后的完整工具调用。
        finish_reason (ModelFinishReason | None): 缺失表示仍在生成，否则使用项目结束语义。
        usage (TokenUsage | None): 此片段报告的用量。
        provider_data (ProviderData | None): 流结束时保存的专用恢复材料。
    """
    content: str | None = None
    tool_call: ToolCall | None = None
    finish_reason: ModelFinishReason | None = None
    usage: TokenUsage | None = None
    provider_data: ProviderData | None = None

    @property
    def is_done(self) -> bool:
        """判断适配器是否已报告生成结束。

        Returns:
            bool: 满足该业务条件时返回 True。
        """
        return self.finish_reason is not None


class ModelContextOverflowError(RuntimeError):
    """模型明确拒绝过长上下文，调用方可按原有规则压缩重试。"""


class ModelAdapter(Protocol):
    """在统一调用接口隐藏协议编码、SDK、错误分类和资源释放。"""

    def estimate_input(self, messages: Sequence[InputMessage], tools: Sequence[ToolDefinition] = ()) -> int:
        """按该适配器实际发送的结构估算输入。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的 Ayaya 消息；方法不会修改原消息。
            tools (Sequence[ToolDefinition]): 当前权限筛选后公开给模型的业务工具定义。

        Returns:
            int: 当前适配器实际请求结构的输入 token 估算。
        """
        ...

    def astream(self, messages: Sequence[InputMessage], tools: Sequence[ToolDefinition] | None = None, **options: Any) -> AsyncIterator[StreamChunk]:
        """产出业务流片段，完整工具参数才能交付执行。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的 Ayaya 消息；方法不会修改原消息。
            tools (Sequence[ToolDefinition] | None): 当前权限筛选后公开给模型的业务工具定义。
            options (Any): 供应商无关的本次生成选项。

        Yields:
            AsyncIterator[StreamChunk]: 供调用方即时消费的正文、完整调用、结束状态和用量片段。
        """
        ...

    async def ainvoke(self, messages: Sequence[InputMessage], tools: Sequence[ToolDefinition] | None = None, **options: Any) -> LLMResponse:
        """异步请求完整业务响应。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的 Ayaya 消息；方法不会修改原消息。
            tools (Sequence[ToolDefinition] | None): 当前权限筛选后公开给模型的业务工具定义。
            options (Any): 供应商无关的本次生成选项。

        Returns:
            LLMResponse: 正文、工具调用、结束原因与可用恢复材料。
        """
        ...

    async def ainvoke_structured(self, messages: Sequence[InputMessage], schema: dict, **options: Any) -> dict:
        """请求符合业务 JSON Schema 的对象。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的 Ayaya 消息；方法不会修改原消息。
            schema (dict): 业务输出必须符合的 JSON Schema。
            options (Any): 供应商无关的本次生成选项。

        Returns:
            dict: 解析并校验后的业务 JSON 输出。
        """
        ...

    async def close(self) -> None:
        """释放异步与已创建的同步请求资源。"""
        ...

    def invoke(self, messages: Sequence[InputMessage], tools: Sequence[ToolDefinition] | None = None, **options: Any) -> LLMResponse:
        """使用独立同步资源发起请求，不跨事件循环复用客户端。

        Args:
            messages (Sequence[InputMessage]): 按对话顺序排列的 Ayaya 消息；方法不会修改原消息。
            tools (Sequence[ToolDefinition] | None): 当前权限筛选后公开给模型的业务工具定义。
            options (Any): 供应商无关的本次生成选项。

        Returns:
            LLMResponse: 同步请求得到的业务响应。
        """
        ...
