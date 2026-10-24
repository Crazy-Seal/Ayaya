"""Ayaya 业务消息、不可变内容块及独立 JSON 编码。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
import json
from types import MappingProxyType
from typing import Any, ClassVar, Iterator, Mapping, Sequence
from uuid import uuid4

from app.schemas.message_types import MessageType, MediaKind, ImageContextState
from app.schemas.runtime_types import DESKTOP_CHANNEL, TaskResultStatus
from app.agent.state.time import normalize_utc, utc_now


_STORAGE_MESSAGE_TYPES = tuple(MessageType)
PROTOCOL_MESSAGE_TYPES: tuple[MessageType, ...] = tuple(
    message_type
    for message_type in _STORAGE_MESSAGE_TYPES
    if message_type not in {MessageType.SYSTEM, MessageType.CONTEXT_SUMMARY, MessageType.ENVIRONMENT_PERCEPTION}
)


class MessageContractError(ValueError):
    """持久化消息不符合当前消息契约。"""


class _FrozenMapping(Mapping[str, Any]):
    """保存只读字典，配合递归转换防止工具参数在审批后被调用方修改。"""

    __slots__ = ("_view",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        """首次创建只读字典副本，禁止对同一对象再次初始化。

        Args:
            values (Mapping[str, Any]): 已递归转换为不可变值的键值集合。

        Raises:
            TypeError: 试图重新初始化已存在的只读映射。
        """
        try:
            object.__getattribute__(self, "_view")
        except AttributeError:
            object.__setattr__(self, "_view", MappingProxyType(dict(values)))
            return
        raise TypeError("immutable mapping")

    def __setattr__(self, _name: str, _value: object) -> None:
        """拒绝修改只读映射的属性，防止绕过键值不可变约束。

        Args:
            _name (str): 试图修改的属性名。
            _value (object): 试图写入的值。

        Raises:
            TypeError: 任何属性赋值操作。
        """
        raise TypeError("immutable mapping")

    def __getitem__(self, key: str) -> Any:
        """按键读取不可变工具参数。

        Args:
            key (str): 要查找或保存的字段名。

        Returns:
            Any: 该键对应的不可变值。

        Raises:
            KeyError: 该键不存在。
        """
        return self._view[key]

    def __iter__(self) -> Iterator[str]:
        """按原字典顺序遍历参数名称。

        Returns:
            Iterator[str]: 只读映射键名的迭代器。
        """
        return iter(self._view)

    def __len__(self) -> int:
        """返回只读工具参数的字段数量。

        Returns:
            int: 映射中键的数量。
        """
        return len(self._view)

    def __setitem__(self, _key: str, _value: object) -> None:
        """拒绝替换或新增工具参数，保持审批与执行使用同一份参数。

        Args:
            _key (str): 试图修改的键名。
            _value (object): 试图写入的值。

        Raises:
            TypeError: 任何键值写入操作。
        """
        raise TypeError("immutable mapping")

    def __deepcopy__(self, _memo):
        """复制会话状态时复用不可变映射，无需再复制已不可变的工具参数。

        Args:
            _memo (Any): deepcopy 用于追踪已复制对象的字典；不可变值不需复制。

        Returns:
            _FrozenMapping: 当前映射对象自身。
        """
        return self


def _freeze(value: Any) -> Any:
    """递归复制字典和序列为不可变容器，固定工具参数及附加协议字段。

    Args:
        value (Any): 当前需要校验或转换的值。

    Returns:
        Any: 字典转为只读映射、序列转为元组后的值；标量保持原样。
    """
    if isinstance(value, Mapping):
        return _FrozenMapping({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """递归把不可变消息字段转换为可供 JSON 序列化的字典和列表副本。

    Args:
        value (Any): 当前需要校验或转换的值。

    Returns:
        Any: 由标准字典、列表和原标量组成的独立值。
    """
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def to_json_value(value: Any) -> Any:
    """将不可变消息字段转换为 JSON 容器，供 checkpoint 和工具协议序列化。

    Args:
        value (Any): 当前需要校验或转换的值。

    Returns:
        Any: 递归转换后的标准字典、列表或标量。
    """
    return _thaw(value)


def _require_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    """解码消息时确认字段为对象，在错误中保留具体字段名。

    Args:
        value (object): 当前需要校验或转换的值。
        field_name (str): 出错时用于定位字段的名称。

    Returns:
        Mapping[str, Any]: 通过类型检查的原映射。

    Raises:
        ValueError: 字段不是映射对象。
    """
    if not isinstance(value, Mapping):
        raise ValueError(f"消息字段 {field_name} 必须是对象")
    return value


def _require_string(value: object, field_name: str, *, allow_empty: bool = True) -> str:
    """解码消息时检查字符串类型，并按字段约束决定是否接受空值。

    Args:
        value (object): 当前需要校验或转换的值。
        field_name (str): 出错时用于定位字段的名称。
        allow_empty (bool): 是否允许空字符串。

    Returns:
        str: 通过检查的原字符串。

    Raises:
        ValueError: 字段不是字符串，或禁止为空但传入空串。
    """
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"消息字段 {field_name} 必须是字符串")
    return value


@dataclass(frozen=True, slots=True)
class TextContent:
    """保存一段原始业务文字。

    Attributes:
        text (str): 正文；不包含模型投影加入的时间或来源。
    """
    text: str

    def __post_init__(self) -> None:
        """拒绝非字符串正文，避免存储时隐式改变工具结果。"""
        if not isinstance(self.text, str):
            raise TypeError("TextContent.text 必须是字符串")

    def to_dict(self) -> dict[str, Any]:
        """返回独立的文字内容块 JSON 对象。

        Returns:
            dict[str, Any]: 供下一处理阶段使用的独立 JSON 对象。
        """
        return {"type": "text", "text": self.text}


@dataclass(frozen=True, slots=True)
class ImageContent:
    """保存图片及其在本地历史和当前上下文中的状态。

    Attributes:
        url (str): 图片地址、data URL 或历史中的相对文件路径。
        detail (str | None): 图片处理提示，模型适配器决定如何使用。
        media_kind (MediaKind): human、tool、screenshot 或 mcp。
        storage_ref (str | None): 已保存的本地相对路径。
        context_state (ImageContextState): available 表示可以发送；compacted 表示不能重载图片。
    """
    url: str
    detail: str | None = None
    media_kind: MediaKind = MediaKind.HUMAN
    storage_ref: str | None = None
    context_state: ImageContextState = ImageContextState.AVAILABLE

    def __post_init__(self) -> None:
        """检查图片来源和压缩状态，已压缩截图不得保留可发送地址。"""
        if self.media_kind not in set(MediaKind):
            raise ValueError("未知图片类别")
        if self.context_state not in set(ImageContextState):
            raise ValueError("未知图片上下文状态")
        object.__setattr__(self, "media_kind", MediaKind(self.media_kind))
        object.__setattr__(self, "context_state", ImageContextState(self.context_state))
        if not isinstance(self.url, str) or self.context_state == ImageContextState.AVAILABLE and not self.url:
            raise ValueError("可用图片必须有地址")
        if self.context_state == ImageContextState.COMPACTED and (self.media_kind != MediaKind.SCREENSHOT or self.url):
            raise ValueError("压缩截图不能保留模型图片地址")
        if self.storage_ref is not None and (not isinstance(self.storage_ref, str) or not self.storage_ref):
            raise ValueError("图片文件引用必须是非空字符串")
        if self.detail is not None and not isinstance(self.detail, str):
            raise ValueError("图片 detail 必须是字符串")

    def to_dict(self) -> dict[str, Any]:
        """导出图片结构和业务字段，不在此读取图片文件。

        Returns:
            dict[str, Any]: 供下一处理阶段使用的独立 JSON 对象。
        """
        image = {"url": self.url}
        if self.detail is not None:
            image["detail"] = self.detail
        return {"type": "image_url", "image_url": image, "media_kind": self.media_kind.value,
                "storage_ref": self.storage_ref, "context_state": self.context_state.value}


ContentPart = TextContent | ImageContent


def text_content(text: str) -> tuple[ContentPart, ...]:
    """把纯文本转换为有序业务正文，不创建新的消息身份。

    Args:
        text (str): 用户、助手或工具提供的文字。
    Returns:
        tuple[ContentPart, ...]: 一个文字块。
    """
    return (TextContent(text),)


def content_from_json(value: object) -> tuple[ContentPart, ...]:
    """严格读取新协议的内容块数组，不接受旧字符串正文。

    Args:
        value (object): JSON 数组。
    Returns:
        tuple[ContentPart, ...]: 已校验的不可变内容。
    Raises:
        MessageContractError: 内容类型、图片字段或业务状态无效。
    """
    if not isinstance(value, list):
        raise MessageContractError("消息 content 必须是内容块数组")
    parts: list[ContentPart] = []
    for item in value:
        block = _require_mapping(item, "content[]")
        if block.get("type") == "text":
            parts.append(TextContent(_require_string(block.get("text"), "content[].text")))
        elif block.get("type") == "image_url":
            image = _require_mapping(block.get("image_url"), "content[].image_url")
            parts.append(ImageContent(url=_require_string(image.get("url"), "image_url.url"),
                detail=image.get("detail"), media_kind=block.get("media_kind", MediaKind.HUMAN),
                storage_ref=block.get("storage_ref"), context_state=block.get("context_state", ImageContextState.AVAILABLE)))
        else:
            raise MessageContractError("未知内容块类型")
    return tuple(parts)


def content_text(content: Sequence[ContentPart]) -> str:
    """按原顺序读取文字块，图片不被解释为真人文字。

    Args:
        content (Sequence[ContentPart]): 按顺序排列的业务文字或图片内容块。

    Returns:
        str: 供历史、记忆和日志使用的业务文字。
    """
    return "\n".join(part.text for part in content if isinstance(part, TextContent))


def format_tool_arguments(call: ToolCall) -> str:
    """把业务参数写入日记和记忆文本，不构造或保存供应商调用结构。

    Args:
        call (ToolCall): 包含对象参数的业务工具调用。

    Returns:
        str: 供历史、记忆和日志使用的业务文字。
    """
    return json.dumps(to_json_value(call.args), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_content(content: Sequence[ContentPart]) -> tuple[ContentPart, ...]:
    """复制内容序列为元组，拒绝混入 SDK 对象或可变字典。

    Args:
        content (Sequence[ContentPart]): 按顺序排列的业务文字或图片内容块。

    Returns:
        tuple[ContentPart, ...]: 复制并校验后的不可变内容集合。
    """
    result = tuple(content)
    if not all(isinstance(part, (TextContent, ImageContent)) for part in result):
        raise TypeError("content 只能包含 Ayaya 内容块")
    return result


@dataclass(frozen=True, slots=True)
class ProviderData:
    """保存仅由所属模型适配器解释的恢复材料。

    Attributes:
        api_format (str): 原调用协议。
        endpoint (str): 原服务地址；不含凭据。
        model (str): 原模型标识。
        payload (Mapping[str, Any]): 不透明、不可变的专用数据。
    """
    api_format: str
    endpoint: str
    model: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        """复制专用材料并要求明确归属，不能猜测旧数据属于哪个供应商。"""
        if not all(isinstance(value, str) and value for value in (self.api_format, self.endpoint, self.model)):
            raise ValueError("供应商恢复材料必须有协议、地址和模型")
        object.__setattr__(self, "endpoint", self.endpoint.rstrip("/"))
        object.__setattr__(self, "payload", _freeze(_require_mapping(self.payload, "provider_data.payload")))

    def to_dict(self) -> dict[str, Any]:
        """返回可持久化的材料副本，不执行供应商转换。

        Returns:
            dict[str, Any]: 供下一处理阶段使用的独立 JSON 对象。
        """
        return {"api_format": self.api_format, "endpoint": self.endpoint, "model": self.model,
                "payload": to_json_value(self.payload)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProviderData:
        """从已标明归属的存储对象恢复专用材料。

        Args:
            data (Mapping[str, Any]): 新协议存储的业务对象。

        Returns:
            ProviderData: 经过业务字段校验的不可变对象。
        """
        return cls(**dict(data))


@dataclass(frozen=True, slots=True)
class ToolCall:
    """保存一次不可变业务调用，参数是对象而非供应商 JSON 字符串。

    Attributes:
        id (str): 调用编号。
        name (str): 工具名称。
        args (Mapping[str, Any]): 审批与执行共享的不可变参数。
        provider_data (ProviderData | None): 所属模型的专用恢复材料。
    """
    id: str
    name: str
    args: Mapping[str, Any]
    provider_data: ProviderData | None = None

    def __post_init__(self) -> None:
        """固定参数和恢复材料，拒绝没有身份的工具请求。"""
        if not all(isinstance(value, str) and value for value in (self.id, self.name)):
            raise ValueError("工具调用编号和名称不能为空")
        object.__setattr__(self, "args", _freeze(_require_mapping(self.args, "tool_call.args")))
        if self.provider_data is not None and not isinstance(self.provider_data, ProviderData):
            raise TypeError("provider_data 必须是 ProviderData")

    def to_dict(self) -> dict[str, Any]:
        """导出独立业务调用 JSON，参数保持对象形式。

        Returns:
            dict[str, Any]: 供下一处理阶段使用的独立 JSON 对象。
        """
        return {"id": self.id, "name": self.name, "args": to_json_value(self.args),
                "provider_data": self.provider_data.to_dict() if self.provider_data else None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ToolCall:
        """严格恢复业务调用；旧 function 格式必须先迁移。

        Args:
            data (Mapping[str, Any]): 新协议存储的业务对象。

        Returns:
            ToolCall: 经过业务字段校验的不可变对象。
        """
        return cls(id=_require_string(data.get("id"), "tool_call.id", allow_empty=False),
                   name=_require_string(data.get("name"), "tool_call.name", allow_empty=False),
                   args=_require_mapping(data.get("args"), "tool_call.args"),
                   provider_data=ProviderData.from_dict(data["provider_data"]) if data.get("provider_data") else None)


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseMessage(ABC):
    """约定业务身份、UTC 时间及供应商无关的 JSON 编码。

    Attributes:
        message_id (str): 持久化与去重使用的稳定编号。
        created_at (datetime): 创建时间。
    """
    message_type: ClassVar[MessageType]
    message_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=utc_now)

    @property
    @abstractmethod
    def message_type(self) -> MessageType:
        """要求具体业务类型声明自己的存储标识。"""
        raise NotImplementedError

    def __post_init__(self) -> None:
        """统一 UTC 时间并校验身份；构造不写数据库。"""
        object.__setattr__(self, "created_at", normalize_utc(self.created_at))
        if not isinstance(self.message_id, str) or not self.message_id:
            raise ValueError("message_id 不能为空")

    def to_storage_format(self) -> dict[str, Any]:
        """逐字段编码业务值；模型角色、工具 function 和时间前缀均不参与存储。

        Returns:
            dict[str, Any]: 包含身份、时间与完整业务字段的独立 JSON 对象。
        """
        result: dict[str, Any] = {"message_type": self.message_type.value}
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "created_at":
                value = value.isoformat()
            elif item.name == "content" and isinstance(value, tuple):
                value = [part.to_dict() for part in value]
            elif item.name == "tool_calls":
                value = [call.to_dict() for call in value]
            elif isinstance(value, ProviderData):
                value = value.to_dict()
            else:
                value = to_json_value(value)
            result[item.name] = value
        return result

    @property
    def enters_protocol_log(self) -> bool:
        """告知存储层是否归档原始业务消息。

        Returns:
            bool: 满足该业务条件时返回 True。
        """
        return True

    @property
    def text(self) -> str:
        """提供统一纯文本读取接口，具体类型实现正文含义。

        Returns:
            str: 按顺序拼接的可见文字，不包含图片和供应商材料。
        """
        return ""


@dataclass(frozen=True, slots=True, kw_only=True)
class HumanMessage(BaseMessage):
    """保存真人发送的完整有序正文和入口渠道。"""
    message_type: ClassVar[MessageType] = MessageType.HUMAN
    content: tuple[ContentPart, ...]
    origin_channel: str = DESKTOP_CHANNEL

    def __post_init__(self) -> None:
        """固定正文并校验来源，图片文件引用位于图片块中。"""
        BaseMessage.__post_init__(self)
        object.__setattr__(self, "content", validate_content(self.content))
        if not self.origin_channel:
            raise ValueError("真人来源不能为空")

    @property
    def text(self) -> str:
        """供历史、召回和日志读取真人文字，忽略图片地址。

        Returns:
            str: 按顺序拼接的可见文字，不包含图片和供应商材料。
        """
        return content_text(self.content)


@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessage(BaseMessage):
    """保存模型正文、工具调用和可持久恢复材料。"""
    message_type: ClassVar[MessageType] = MessageType.ASSISTANT
    content: tuple[ContentPart, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    origin_channel: str = DESKTOP_CHANNEL
    provider_data: ProviderData | None = None

    def __post_init__(self) -> None:
        """固定模型输出，正文和调用都不能被外部改变。"""
        BaseMessage.__post_init__(self)
        object.__setattr__(self, "content", validate_content(self.content))
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if not all(isinstance(call, ToolCall) for call in self.tool_calls) or not self.origin_channel:
            raise ValueError("助手调用或来源无效")
        if self.provider_data is not None and not isinstance(self.provider_data, ProviderData):
            raise TypeError("助手恢复材料必须是 ProviderData")

    @property
    def text(self) -> str:
        """读取模型可见文字，不暴露供应商恢复材料。

        Returns:
            str: 按顺序拼接的可见文字，不包含图片和供应商材料。
        """
        return content_text(self.content)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolMessage(BaseMessage):
    """保存一次工具调用的完整文字、图片和结果压缩许可。"""
    message_type: ClassVar[MessageType] = MessageType.TOOL
    content: tuple[ContentPart, ...]
    tool_name: str
    tool_call_id: str
    compressible: bool = False

    def __post_init__(self) -> None:
        """固定完整结果并要求能与原请求准确配对。"""
        BaseMessage.__post_init__(self)
        object.__setattr__(self, "content", validate_content(self.content))
        if not self.tool_name or not self.tool_call_id:
            raise ValueError("工具名称和调用编号不能为空")

    @property
    def text(self) -> str:
        """读取工具文字，图片仍保存在本条结果内。

        Returns:
            str: 按顺序拼接的可见文字，不包含图片和供应商材料。
        """
        return content_text(self.content)


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemMessage(BaseMessage):
    """保存临时系统提示词，不进入 AgentState 和历史。"""
    message_type: ClassVar[MessageType] = MessageType.SYSTEM
    text: str

    def __post_init__(self) -> None:
        """校验临时提示词，保持纯文本配置接口。"""
        BaseMessage.__post_init__(self)
        if not isinstance(self.text, str):
            raise TypeError("SystemMessage.text 必须是字符串")

    @property
    def enters_protocol_log(self) -> bool:
        """阻止系统提示词作为对话事实归档。

        Returns:
            bool: 满足该业务条件时返回 True。
        """
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextSummaryMessage(BaseMessage):
    """保存替代旧轮次的上下文摘要及覆盖边界，只进入 checkpoint，不作为原始聊天历史。

    Attributes:
        content (str): 带摘要前缀的结构化摘要文本。
        summary_schema_version (int): 摘要结构版本。
        covered_through_message_id (str): 被替换历史中最后一条原消息编号。
        covered_effective_date (str): 覆盖边界对应的日记有效日期。
        compression_ratio (float): 摘要粗估 token 数与原内容粗估 token 数之比。
    """
    message_type: ClassVar[MessageType] = MessageType.CONTEXT_SUMMARY
    content: str
    summary_schema_version: int
    covered_through_message_id: str
    covered_effective_date: str
    compression_ratio: float

    def __post_init__(self) -> None:
        """校验摘要文本、结构版本和历史覆盖边界，避免产生无法清理的摘要。

        Raises:
            TypeError: 摘要正文不是字符串。
            ValueError: 结构版本非正数，或缺少消息覆盖边界和有效日期。
        """
        BaseMessage.__post_init__(self)
        if not isinstance(self.content, str):
            raise TypeError("ContextSummaryMessage.content 必须是字符串")
        if self.summary_schema_version <= 0:
            raise ValueError("摘要 schema_version 必须为正整数")
        if not self.covered_through_message_id or not self.covered_effective_date:
            raise ValueError("摘要覆盖边界不能为空")

    @property
    def text(self) -> str:
        """供上下文读取统一取得摘要正文。

        Returns:
            str: content 中的摘要文本。
        """
        return self.content


    @property
    def enters_protocol_log(self) -> bool:
        """排除压缩产生的摘要，协议日志继续保留原始对话事实。

        Returns:
            bool: 始终为 False。
        """
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeInputMessage(BaseMessage, ABC):
    """可激活 Agent、但不展示为真人输入的抽象消息。"""


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskResultMessage(RuntimeInputMessage):
    """表示后台任务终态或合并终态通知，未知和混合状态不能伪称成功。

    Attributes:
        message_type (ClassVar[MessageType]): 该消息对应的领域类型常量。
        task_id (str): 受管进程任务标识。
        task_name (str): 后台通知展示的任务名称，当前由命令正文或批次标题产生。
        status (TaskResultStatus): 交付给模型的通知终态，不与受管进程状态混用。
        result (str): 桌面执行结果或任务终态输出摘要。
    """
    message_type: ClassVar[MessageType] = MessageType.TASK_RESULT
    task_id: str
    task_name: str
    status: TaskResultStatus
    result: str

    def __post_init__(self) -> None:
        """校验任务标识、名称、终态和结果正文，拒绝未知状态值。

        Raises:
            ValueError: 后台任务标识与名称不能为空。
            TypeError: TaskResultMessage.result 必须是字符串。
        """
        BaseMessage.__post_init__(self)
        if not self.task_id or not self.task_name:
            raise ValueError("后台任务标识与名称不能为空")
        if self.status not in set(TaskResultStatus):
            raise ValueError(f"未知后台任务状态: {self.status!r}")
        object.__setattr__(self, "status", TaskResultStatus(self.status))
        if not isinstance(self.result, str):
            raise TypeError("TaskResultMessage.result 必须是字符串")

    @property
    def text(self) -> str:
        """供记忆提取和历史读取取得后台任务结果正文。

        Returns:
            str: result 中的任务结果。
        """
        return self.result


@dataclass(frozen=True, slots=True, kw_only=True)
class EnvironmentPerceptionMessage(BaseMessage):
    """提供本次模型请求的环境背景，只在请求末尾临时组装。

    Attributes:
        perception_kind (str): 本次背景的类别。
        description (str): 从 State 临时条目组装的背景正文。
    """
    message_type: ClassVar[MessageType] = MessageType.ENVIRONMENT_PERCEPTION
    perception_kind: str
    description: str

    def __post_init__(self) -> None:
        """创建请求背景后校验类别和说明正文。

        Raises:
            ValueError: 环境背景类别为空。
            TypeError: 说明不是字符串。
        """
        BaseMessage.__post_init__(self)
        if not self.perception_kind:
            raise ValueError("EnvironmentPerceptionMessage.perception_kind 不能为空")
        if not isinstance(self.description, str):
            raise TypeError("EnvironmentPerceptionMessage.description 必须是字符串")

    @property
    def text(self) -> str:
        """供模型适配器读取环境背景正文。

        Returns:
            str: description 中的环境变化正文。
        """
        return self.description

    @property
    def enters_protocol_log(self) -> bool:
        """使请求临时背景不作为原始对话归档。

        Returns:
            bool: 始终为 False。
        """
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentMessage(RuntimeInputMessage):
    """保存子 Agent 返回的结果及其身份，作为内部通知交给当前会话继续处理。"""
    message_type: ClassVar[MessageType] = MessageType.SUBAGENT
    subagent_id: str
    subagent_name: str
    result: str

    def __post_init__(self) -> None:
        """创建子 Agent 结果后校验编号、名称和文本，避免失去结果来源。

        Raises:
            ValueError: 子 Agent 编号或名称为空。
            TypeError: 结果不是字符串。
        """
        BaseMessage.__post_init__(self)
        if not self.subagent_id or not self.subagent_name:
            raise ValueError("SubagentMessage 标识与名称不能为空")
        if not isinstance(self.result, str):
            raise TypeError("SubagentMessage.result 必须是字符串")

    @property
    def text(self) -> str:
        """供历史和记忆读取取得子 Agent 结果正文。

        Returns:
            str: result 中的返回内容。
        """
        return self.result


def message_from_storage_format(data: Mapping[str, Any]) -> BaseMessage:
    """按新业务类型严格恢复消息，拒绝旧协议和未知字段。

    Args:
        data (Mapping[str, Any]): 新消息 JSON 对象。
    Returns:
        BaseMessage: 完整业务消息。
    Raises:
        MessageContractError: 类型、字段或正文不合法。
    """
    constructors = {MessageType.HUMAN: HumanMessage, MessageType.ASSISTANT: AssistantMessage, MessageType.TOOL: ToolMessage,
        MessageType.SYSTEM: SystemMessage, MessageType.CONTEXT_SUMMARY: ContextSummaryMessage,
        MessageType.TASK_RESULT: TaskResultMessage, MessageType.ENVIRONMENT_PERCEPTION: EnvironmentPerceptionMessage,
        MessageType.SUBAGENT: SubagentMessage}
    kind = data.get("message_type")
    if kind not in constructors:
        raise MessageContractError(f"未知或缺失的 message_type: {kind!r}；需要先迁移旧格式")
    constructor = constructors[kind]
    values = dict(data)
    del values["message_type"]
    try:
        if kind in {MessageType.HUMAN, MessageType.ASSISTANT, MessageType.TOOL}:
            values["content"] = content_from_json(values.get("content"))
        if kind == MessageType.ASSISTANT:
            values["tool_calls"] = tuple(ToolCall.from_dict(call) for call in values.get("tool_calls", []))
            if values.get("provider_data"):
                values["provider_data"] = ProviderData.from_dict(values["provider_data"])
        values["message_id"] = _require_string(values.get("message_id"), "message_id", allow_empty=False)
        values["created_at"] = normalize_utc(values.get("created_at"))
        return constructor(**values)
    except (TypeError, ValueError, KeyError) as exc:
        raise MessageContractError(f"{kind} 消息不符合业务协议: {exc}") from exc


def notification_text(message: BaseMessage) -> str:
    """以项目语义描述后台通知，供临时模型输入和记忆使用。

    Args:
        message (BaseMessage): 待读取或转换的业务消息。

    Returns:
        str: 供历史、记忆和日志使用的业务文字。
    """
    if isinstance(message, TaskResultMessage):
        labels = {TaskResultStatus.COMPLETED: "完成",
                  TaskResultStatus.FAILED: "失败",
                  TaskResultStatus.CANCELLED: "取消",
                  TaskResultStatus.ORPHANED: "状态未知",
                  TaskResultStatus.MIXED: "状态汇总"}
        return f"[后台任务{labels[message.status.value]}：{message.task_name}]\n{message.result}"
    if isinstance(message, EnvironmentPerceptionMessage):
        return f"[环境感知：{message.perception_kind}]\n{message.description}"
    if isinstance(message, SubagentMessage):
        return f"[子 Agent 返回：{message.subagent_name}]\n{message.result}"
    return message.text


def message_from_history(*, message_json: Mapping[str, Any], message_type: str, message_id: str, created_at: str) -> BaseMessage:
    """使用统一解码器恢复历史并校验查询列，防止损坏索引指向另一条消息。

    Args:
        message_json (Mapping[str, Any]): 持久化的完整新协议业务消息。
        message_type (str): 历史行保存的业务类型，用于校验 JSON。
        message_id (str): 历史行保存的稳定消息编号。
        created_at (str): 历史行保存的 UTC 创建时间。

    Returns:
        BaseMessage: 经过业务字段校验的不可变对象。
    """
    message = message_from_storage_format(message_json)
    if message.message_type != message_type or message.message_id != message_id or message.created_at != normalize_utc(created_at):
        raise MessageContractError("历史索引与消息身份不一致")
    return message


def is_real_human_message(message: BaseMessage) -> bool:
    """只把真人输入识别为真人消息。

    Args:
        message (BaseMessage): 待读取或转换的业务消息。

    Returns:
        bool: 满足该业务条件时返回 True。
    """
    return isinstance(message, HumanMessage)


def is_screenshot_message(message: BaseMessage) -> bool:
    """识别包含屏幕截图的工具结果，包括已压缩截图。

    Args:
        message (BaseMessage): 待读取或转换的业务消息。

    Returns:
        bool: 满足该业务条件时返回 True。
    """
    return isinstance(message, ToolMessage) and any(isinstance(part, ImageContent) and part.media_kind == MediaKind.SCREENSHOT for part in message.content)


def is_system_injected_user_message(message: BaseMessage) -> bool:
    """识别内部通知与摘要，不依赖供应商角色。

    Args:
        message (BaseMessage): 待读取或转换的业务消息。

    Returns:
        bool: 满足该业务条件时返回 True。
    """
    return isinstance(message, (ContextSummaryMessage, RuntimeInputMessage))


def is_user_message(message: BaseMessage) -> bool:
    """识别真人或内部输入；工具图片属于 ToolMessage。

    Args:
        message (BaseMessage): 待读取或转换的业务消息。

    Returns:
        bool: 满足该业务条件时返回 True。
    """
    return isinstance(message, (HumanMessage, ContextSummaryMessage, RuntimeInputMessage))


def with_text(message: HumanMessage | AssistantMessage | ToolMessage, text: str) -> BaseMessage:
    """替换消息文字并保留图片及身份，供打断后缀和临时上下文使用。

    Args:
        message (HumanMessage | AssistantMessage | ToolMessage): 待读取或转换的业务消息。
        text (str): 替换或读取的业务文字。

    Returns:
        BaseMessage: 经过业务字段校验的不可变对象。
    """
    return replace(message, content=(*text_content(text), *(part for part in message.content if isinstance(part, ImageContent))))
