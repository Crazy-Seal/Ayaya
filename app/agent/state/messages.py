"""与 Provider 无关的不可变消息值及持久化 Adapter。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
import json
from types import MappingProxyType
from typing import Any, ClassVar, Iterator, Literal, Mapping, Sequence, cast, get_args
from uuid import uuid4

from app.schemas.runtime_types import DESKTOP_CHANNEL, TaskResultStatus
from app.agent.state.message_names import (
    CONTEXT_SUMMARY_MESSAGE_NAME,
    MCP_TOOL_IMAGE_MESSAGE_NAME,
    SCREENSHOT_COMPRESSED_NAME,
    SCREENSHOT_MESSAGE_NAME,
    TOOL_IMAGE_MESSAGE_NAME,
)
from app.agent.state.time import normalize_utc, utc_now


MessageType = Literal[
    "human",
    "system",
    "assistant",
    "tool",
    "tool_media",
    "context_summary",
    "task_result",
    "environment_perception",
    "subagent",
]
MediaKind = Literal["screenshot", "tool", "mcp"]

_STORAGE_MESSAGE_TYPES = cast(tuple[MessageType, ...], get_args(MessageType))
PROTOCOL_MESSAGE_TYPES: tuple[MessageType, ...] = tuple(
    message_type
    for message_type in _STORAGE_MESSAGE_TYPES
    if message_type not in {"system", "context_summary"}
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
class ToolCall:
    """保存模型请求的一次工具调用，审批、恢复与结果配对都使用同一编号和不可变参数。

    Attributes:
        id (str): 模型工具调用编号，不允许为空。
        name (str): 注册的工具名称。
        args (Mapping[str, Any]): 递归不可变的调用参数。
        extra_content (Mapping[str, Any] | None): 模型服务要求原样带回的附加协议内容。
    """

    id: str
    name: str
    args: Mapping[str, Any]
    extra_content: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        """dataclass 创建工具调用后校验编号、名称和对象字段，再递归固定参数。

        Raises:
            ValueError: 调用编号或工具名称为空。
            TypeError: 参数或附加协议内容不是映射对象。
        """
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("ToolCall.id 不能为空")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("ToolCall.name 不能为空")
        if not isinstance(self.args, Mapping):
            raise TypeError("ToolCall.args 必须是对象")
        if self.extra_content is not None and not isinstance(self.extra_content, Mapping):
            raise TypeError("ToolCall.extra_content 必须是对象")
        object.__setattr__(self, "args", _freeze(self.args))
        if self.extra_content is not None:
            object.__setattr__(self, "extra_content", _freeze(self.extra_content))

    def to_dict(self) -> dict[str, Any]:
        """把工具调用转换为模型消息格式，参数按稳定键序编码为 JSON 字符串。

        Returns:
            dict[str, Any]: 包含 function 和可选 extra_content 的工具调用副本。
        """
        result: dict[str, Any] = {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(
                    _thaw(self.args),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        }
        if self.extra_content:
            result["extra_content"] = _thaw(self.extra_content)
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ToolCall":
        """从模型工具调用或 checkpoint 中解析参数，并构造经过校验的不可变调用。

        Args:
            data (Mapping[str, Any]): 包含 id、function 和可选 extra_content 的工具调用记录。

        Returns:
            ToolCall: 恢复出的工具调用对象。

        Raises:
            ValueError: 必需字段缺失、为空，参数 JSON 无效或参数不是对象。
        """
        function = _require_mapping(data.get("function"), "tool_call.function")
        raw_arguments = function.get("arguments", "{}")
        args = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        if not isinstance(args, Mapping):
            raise ValueError("工具调用参数必须是 JSON 对象")
        return cls(
            id=_require_string(data.get("id"), "tool_call.id", allow_empty=False),
            name=_require_string(function.get("name"), "tool_call.function.name", allow_empty=False),
            args=args,
            extra_content=(
                _require_mapping(data["extra_content"], "tool_call.extra_content")
                if data.get("extra_content") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseMessage(ABC):
    """约定所有领域消息的编号、UTC 时间及模型/存储转换接口。

    Attributes:
        message_type (ClassVar[MessageType]): 子类声明的领域消息类型。
        message_id (str): 用于持久保存和界面去重的稳定消息编号。
        created_at (datetime): 消息创建时间，构造后统一为 UTC。
    """

    message_type: ClassVar[MessageType]
    message_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        """dataclass 创建消息后统一 UTC 时间，并拒绝缺少稳定编号的消息。

        Raises:
            ValueError: 消息编号为空或时间无法解析。
        """
        object.__setattr__(self, "created_at", normalize_utc(self.created_at))
        if not self.message_id:
            raise ValueError("message_id 不能为空")

    @abstractmethod
    def to_openai_format(self) -> dict[str, Any]:
        """由具体消息类型实现模型请求格式转换，不包含内部存储字段。

        Returns:
            dict[str, Any]: 该消息对应的模型协议字典。

        Raises:
            NotImplementedError: 子类未实现转换方法。
        """
        raise NotImplementedError

    def _storage_fields(self) -> dict[str, Any]:
        """提供子类追加专用存储字段的入口，基础消息没有附加字段。

        Returns:
            dict[str, Any]: 空字典，子类可覆盖以保存领域元数据。
        """
        return {}

    def to_storage_format(self) -> dict[str, Any]:
        """在模型正文之外加入领域类型、稳定编号、UTC 时间和子类元数据，供 checkpoint 保存。

        Returns:
            dict[str, Any]: 包含完整恢复材料的消息字典。
        """
        return {
            **self.to_openai_format(),
            "_message_type": self.message_type,
            "_message_id": self.message_id,
            "_created_at": self.created_at.isoformat(),
            **self._storage_fields(),
        }

    @property
    def enters_protocol_log(self) -> bool:
        """告知 StateManager 该消息是否需要写入长期保留的对话协议日志。

        Returns:
            bool: 基础实现为 True，系统提示词和上下文摘要由子类排除。
        """
        return True

    @property
    def text(self) -> str:
        """提供纯文本读取接口，正文由具体消息子类定义。

        Returns:
            str: 基础实现返回空串。
        """
        return ""


@dataclass(frozen=True, slots=True, kw_only=True)
class HumanMessage(BaseMessage):
    """保存不可变真人输入及渠道来源，模型 wire 与存储元数据分别投影。

    Attributes:
        message_type (ClassVar[MessageType]): 该消息对应的领域类型常量。
        text (str): 需要保存或追加的文本。
        image_urls (tuple[str, ...]): 真人附件的模型图片地址。
        image_filenames (tuple[str, ...]): 已外置保存的附件路径。
        origin_channel (str): 输入或本轮回复所属的渠道。
    """
    message_type: ClassVar[MessageType] = "human"
    text: str
    image_urls: tuple[str, ...] = ()
    image_filenames: tuple[str, ...] = ()
    origin_channel: str = DESKTOP_CHANNEL

    def __post_init__(self) -> None:
        """校验真人正文、渠道来源和附件数量，规范化不可变附件集合。

        Raises:
            TypeError: HumanMessage.text 必须是字符串。
            ValueError: HumanMessage.origin_channel 不能为空。
        """
        BaseMessage.__post_init__(self)
        if not isinstance(self.text, str):
            raise TypeError("HumanMessage.text 必须是字符串")
        if not isinstance(self.origin_channel, str) or not self.origin_channel:
            raise ValueError("HumanMessage.origin_channel 不能为空")
        object.__setattr__(self, "image_urls", tuple(self.image_urls))
        object.__setattr__(self, "image_filenames", tuple(self.image_filenames))
        if not all(isinstance(item, str) and item for item in self.image_urls):
            raise ValueError("HumanMessage.image_urls 必须是非空字符串")
        if not all(isinstance(item, str) and item for item in self.image_filenames):
            raise ValueError("HumanMessage.image_filenames 必须是非空字符串")
        if self.image_filenames and len(self.image_filenames) != len(self.image_urls):
            raise ValueError("真人消息的图片引用数量必须与图片数量一致")

    def to_openai_format(self) -> dict[str, Any]:
        """把真人输入转换为 user 消息；有附件时同时发送正文和图片地址。

        Returns:
            dict[str, Any]: 只包含模型需要的正文和附件的 user 消息，不含渠道元数据。
        """
        if not self.image_urls:
            return {"role": "user", "content": self.text}
        content: list[dict[str, Any]] = [{"type": "text", "text": self.text}]
        content.extend(
            {"type": "image_url", "image_url": {"url": url}}
            for url in self.image_urls
        )
        return {"role": "user", "content": content}

    def _storage_fields(self) -> dict[str, Any]:
        """保存真人附件的文件引用和输入来源，用于历史展示及重启恢复。

        Returns:
            dict[str, Any]: 以 _human 为键的附件和来源元数据。
        """
        return {"_human": {
            "image_filenames": list(self.image_filenames),
            "origin_channel": self.origin_channel,
        }}


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemMessage(BaseMessage):
    """表示只在构造模型请求时使用的系统提示词，不进入对话协议日志。

    Attributes:
        text (str): 模型系统提示词正文。
    """
    message_type: ClassVar[MessageType] = "system"
    text: str

    def __post_init__(self) -> None:
        """创建系统提示词消息后统一时间和编号，确认正文为字符串。

        Raises:
            TypeError: 系统提示词正文不是字符串。
        """
        BaseMessage.__post_init__(self)
        if not isinstance(self.text, str):
            raise TypeError("SystemMessage.text 必须是字符串")

    def to_openai_format(self) -> dict[str, Any]:
        """将系统提示词转换为 system 消息，供临时模型输入使用。

        Returns:
            dict[str, Any]: 角色为 system 的文本消息。
        """
        return {"role": "system", "content": self.text}

    @property
    def enters_protocol_log(self) -> bool:
        """排除临时系统提示词，避免将提示词作为聊天历史保存。

        Returns:
            bool: 始终为 False。
        """
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessage(BaseMessage):
    """保存不可变 Assistant 文本、工具调用及所属轮次的入口来源。

    Attributes:
        message_type (ClassVar[MessageType]): 该消息对应的领域类型常量。
        text (str): 需要保存或追加的文本。
        tool_calls (tuple[ToolCall, ...]): 本步模型请求的工具调用。
        origin_channel (str): 输入或本轮回复所属的渠道。
    """
    message_type: ClassVar[MessageType] = "assistant"
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    origin_channel: str = DESKTOP_CHANNEL

    def __post_init__(self) -> None:
        """校验 Assistant 正文和来源，将工具调用转为不可变集合。

        Raises:
            TypeError: AssistantMessage.text 必须是字符串。
            ValueError: AssistantMessage.origin_channel 不能为空。
        """
        BaseMessage.__post_init__(self)
        if not isinstance(self.text, str):
            raise TypeError("AssistantMessage.text 必须是字符串")
        if not isinstance(self.origin_channel, str) or not self.origin_channel:
            raise ValueError("AssistantMessage.origin_channel 不能为空")
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if not all(isinstance(call, ToolCall) for call in self.tool_calls):
            raise TypeError("AssistantMessage.tool_calls 只能包含 ToolCall")

    def to_openai_format(self) -> dict[str, Any]:
        """转换模型回复正文，并在模型请求工具时附上对应的工具调用列表。

        Returns:
            dict[str, Any]: 包含正文和可选 tool_calls 的 assistant 消息。
        """
        result: dict[str, Any] = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            result["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        return result

    def _storage_fields(self) -> dict[str, Any]:
        """单独保存本轮回答的来源渠道，供历史展示和恢复时识别归属。

        Returns:
            dict[str, Any]: 以 _assistant 为键的来源元数据。
        """
        return {"_assistant": {"origin_channel": self.origin_channel}}


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolMessage(BaseMessage):
    """保存一次工具调用的文本结果，通过调用编号与模型请求配对。

    Attributes:
        content (str): 工具结果正文或明确的取消/错误说明。
        tool_name (str): 实际调用的工具名称。
        tool_call_id (str): 对应模型工具请求的编号。
        compressible (bool): 后续历史压缩是否允许缩短该结果。
    """
    message_type: ClassVar[MessageType] = "tool"
    content: str
    tool_name: str
    tool_call_id: str
    compressible: bool = False

    def __post_init__(self) -> None:
        """创建工具结果后校验正文、工具名称和调用编号，禁止无法与请求配对的结果。

        Raises:
            TypeError: 结果正文不是字符串。
            ValueError: 工具名称或调用编号为空。
        """
        BaseMessage.__post_init__(self)
        if not isinstance(self.content, str):
            raise TypeError("ToolMessage.content 必须是字符串")
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ValueError("ToolMessage.tool_name 不能为空")
        if not isinstance(self.tool_call_id, str) or not self.tool_call_id:
            raise ValueError("ToolMessage.tool_call_id 不能为空")

    @property
    def text(self) -> str:
        """让历史展示和记忆读取统一取得工具结果正文。

        Returns:
            str: content 中的工具结果文本。
        """
        return self.content

    def to_openai_format(self) -> dict[str, Any]:
        """把工具结果转换为 tool 消息，保留对应调用编号供模型继续回答。

        Returns:
            dict[str, Any]: 包含正文、工具名称和 tool_call_id 的模型消息。
        """
        result: dict[str, Any] = {
            "role": "tool",
            "content": self.content,
            "name": self.tool_name,
        }
        result["tool_call_id"] = self.tool_call_id
        return result

    def _storage_fields(self) -> dict[str, Any]:
        """保存该工具结果是否允许压缩，不把此内部标记发送给模型。

        Returns:
            dict[str, Any]: 以 _tool 为键的压缩许可元数据。
        """
        return {"_tool": {"compressible": self.compressible}}


@dataclass(frozen=True, slots=True, kw_only=True)
class AvailableMedia:
    """保存仍可发送给模型的工具图片，以及已外置保存的历史文件引用。

    Attributes:
        urls (tuple[str, ...]): 模型可读取的图片地址，至少一项。
        storage_refs (tuple[str, ...]): 与图片一一对应的历史文件引用，尚未保存时为空。
    """
    kind: ClassVar[Literal["available"]] = "available"
    urls: tuple[str, ...]
    storage_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """将图片集合固定为元组，校验非空地址及历史文件引用数量。

        Raises:
            ValueError: 没有图片、引用为空字符串，或文件引用数量与图片数量不一致。
        """
        object.__setattr__(self, "urls", tuple(self.urls))
        object.__setattr__(self, "storage_refs", tuple(self.storage_refs))
        if not self.urls:
            raise ValueError("AvailableMedia 至少需要一张图片")
        if not all(isinstance(item, str) and item for item in self.urls):
            raise ValueError("AvailableMedia.urls 必须是非空字符串")
        if not all(isinstance(item, str) and item for item in self.storage_refs):
            raise ValueError("AvailableMedia.storage_refs 必须是非空字符串")
        if self.storage_refs and len(self.storage_refs) != len(self.urls):
            raise ValueError("媒体文件引用数量必须与图片数量一致")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactedMedia:
    """表示截图已从模型输入移除，仍保留可供聊天历史读取的文件引用。

    Attributes:
        storage_refs (tuple[str, ...]): 压缩前已保存的截图文件引用。
    """
    kind: ClassVar[Literal["compacted"]] = "compacted"
    storage_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """固定压缩截图的文件引用集合，并拒绝无效引用。

        Raises:
            ValueError: 文件引用不是非空字符串。
        """
        object.__setattr__(self, "storage_refs", tuple(self.storage_refs))
        if not all(isinstance(item, str) and item for item in self.storage_refs):
            raise ValueError("CompactedMedia.storage_refs 必须是非空字符串")


MediaPayload = AvailableMedia | CompactedMedia


_MEDIA_NAMES: dict[MediaKind, str] = {
    "screenshot": SCREENSHOT_MESSAGE_NAME,
    "tool": TOOL_IMAGE_MESSAGE_NAME,
    "mcp": MCP_TOOL_IMAGE_MESSAGE_NAME,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolMediaMessage(BaseMessage):
    """在工具文本结果之后向模型提供图片，或表示旧截图已经被压缩。

    Attributes:
        tool_name (str): 返回图片的工具名称。
        media_kind (MediaKind): 区分截图、本地工具图片和 MCP 图片。
        text (str): 图片的文字说明。
        payload (MediaPayload): 可用图片地址或已压缩截图的历史引用。
        associated_message_id (str | None): 对应工具文本结果的消息编号。
    """
    message_type: ClassVar[MessageType] = "tool_media"
    tool_name: str
    media_kind: MediaKind
    text: str
    payload: MediaPayload
    associated_message_id: str | None = None

    def __post_init__(self) -> None:
        """创建图片消息后检查工具、媒体类型和内容，只允许截图使用压缩占位。

        Raises:
            ValueError: 工具名为空、媒体类型未知，或普通图片使用压缩内容。
            TypeError: 图片说明不是字符串或 payload 类型无效。
        """
        BaseMessage.__post_init__(self)
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ValueError("ToolMediaMessage.tool_name 不能为空")
        if self.media_kind not in _MEDIA_NAMES:
            raise ValueError(f"未知工具媒体类型: {self.media_kind!r}")
        if not isinstance(self.text, str):
            raise TypeError("ToolMediaMessage.text 必须是字符串")
        if not isinstance(self.payload, (AvailableMedia, CompactedMedia)):
            raise TypeError("ToolMediaMessage.payload 类型无效")
        if isinstance(self.payload, CompactedMedia) and self.media_kind != "screenshot":
            raise ValueError("只有截图媒体支持压缩 payload")

    def to_openai_format(self) -> dict[str, Any]:
        """将可用工具图片转成多模态 user 消息，已压缩截图则转成明确的文字占位。

        Returns:
            dict[str, Any]: 带媒体类别名称的图片消息或截图压缩占位。
        """
        if isinstance(self.payload, CompactedMedia):
            return {
                "role": "user",
                "name": SCREENSHOT_COMPRESSED_NAME,
                "content": "[系统消息]已被压缩的旧截图",
            }
        content: list[dict[str, Any]] = [{"type": "text", "text": self.text}]
        content.extend(
            {"type": "image_url", "image_url": {"url": url}}
            for url in self.payload.urls
        )
        return {
            "role": "user",
            "name": _MEDIA_NAMES[self.media_kind],
            "content": content,
        }

    def _storage_fields(self) -> dict[str, Any]:
        """保存工具图片类别、结果关联和历史文件引用，图片正文由模型消息格式承载。

        Returns:
            dict[str, Any]: 以 _tool_media 为键的媒体恢复材料。
        """
        payload: dict[str, Any] = {
            "kind": self.payload.kind,
            "storage_refs": list(self.payload.storage_refs),
        }
        return {
            "_tool_media": {
                "tool_name": self.tool_name,
                "media_kind": self.media_kind,
                "associated_message_id": self.associated_message_id,
                "payload": payload,
            }
        }


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
    message_type: ClassVar[MessageType] = "context_summary"
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

    def to_openai_format(self) -> dict[str, Any]:
        """把内部摘要转换为带专用名称的 user 消息，供模型理解前段历史。

        Returns:
            dict[str, Any]: 包含摘要正文的模型消息。
        """
        return {
            "role": "user",
            "name": CONTEXT_SUMMARY_MESSAGE_NAME,
            "content": self.content,
        }

    def _storage_fields(self) -> dict[str, Any]:
        """保存摘要版本、覆盖边界和压缩比例，供后续摘要合并与日记清理。

        Returns:
            dict[str, Any]: 以 _context_summary 为键的摘要元数据。
        """
        return {
            "_context_summary": {
                "schema_version": self.summary_schema_version,
                "covered_through_message_id": self.covered_through_message_id,
                "covered_effective_date": self.covered_effective_date,
                "compression_ratio": self.compression_ratio,
            }
        }

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
    message_type: ClassVar[MessageType] = "task_result"
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

    def to_openai_format(self) -> dict[str, Any]:
        """把后台任务结果转为内部 user 通知，用明确标签区分完成、失败、取消和未知。

        Returns:
            dict[str, Any]: 名称为 runtime_task_result 的任务通知消息。
        """
        status_label = {
            TaskResultStatus.COMPLETED: "完成",
            TaskResultStatus.FAILED: "失败",
            TaskResultStatus.CANCELLED: "取消",
            TaskResultStatus.ORPHANED: "状态未知",
            TaskResultStatus.MIXED: "状态汇总",
        }[self.status]
        return {
            "role": "user",
            "name": "runtime_task_result",
            "content": f"[后台任务{status_label}] {self.task_name}\n{self.result}",
        }

    def _storage_fields(self) -> dict[str, Any]:
        """保存后台任务编号、名称和终态，便于协议日志恢复准确任务事实。

        Returns:
            dict[str, Any]: 以 _task 为键的任务元数据。
        """
        return {
            "_task": {
                "task_id": self.task_id,
                "task_name": self.task_name,
                "status": self.status.value,
            }
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class EnvironmentPerceptionMessage(RuntimeInputMessage):
    """保存环境变化通知，使用专用消息类型与真人输入区分。"""
    message_type: ClassVar[MessageType] = "environment_perception"
    perception_kind: str
    description: str

    def __post_init__(self) -> None:
        """创建环境通知后校验变化类别和说明正文。

        Raises:
            ValueError: 环境变化类别为空。
            TypeError: 说明不是字符串。
        """
        BaseMessage.__post_init__(self)
        if not self.perception_kind:
            raise ValueError("EnvironmentPerceptionMessage.perception_kind 不能为空")
        if not isinstance(self.description, str):
            raise TypeError("EnvironmentPerceptionMessage.description 必须是字符串")

    @property
    def text(self) -> str:
        """供历史和记忆读取取得环境变化说明。

        Returns:
            str: description 中的环境变化正文。
        """
        return self.description

    def to_openai_format(self) -> dict[str, Any]:
        """将环境变化转为带类别标签的内部 user 通知。

        Returns:
            dict[str, Any]: 名称为 runtime_environment_perception 的模型消息。
        """
        return {
            "role": "user",
            "name": "runtime_environment_perception",
            "content": f"[环境变化：{self.perception_kind}]\n{self.description}",
        }

    def _storage_fields(self) -> dict[str, Any]:
        """保存环境变化类别，恢复时不依赖从正文猜测类别。

        Returns:
            dict[str, Any]: 以 _environment 为键的变化类别元数据。
        """
        return {"_environment": {"perception_kind": self.perception_kind}}


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentMessage(RuntimeInputMessage):
    """保存子 Agent 返回的结果及其身份，作为内部通知交给当前会话继续处理。"""
    message_type: ClassVar[MessageType] = "subagent"
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

    def to_openai_format(self) -> dict[str, Any]:
        """把子 Agent 返回值转为带来源名称的内部 user 通知。

        Returns:
            dict[str, Any]: 名称为 runtime_subagent 的模型消息。
        """
        return {
            "role": "user",
            "name": "runtime_subagent",
            "content": f"[子 Agent 返回：{self.subagent_name}]\n{self.result}",
        }

    def _storage_fields(self) -> dict[str, Any]:
        """保存子 Agent 编号和名称，用于恢复结果的真实来源。

        Returns:
            dict[str, Any]: 以 _subagent 为键的来源元数据。
        """
        return {
            "_subagent": {
                "subagent_id": self.subagent_id,
                "subagent_name": self.subagent_name,
            }
        }


def _common_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    """从 checkpoint 消息中读取必需编号和创建时间，并将时间统一为 UTC。

    Args:
        data (Mapping[str, Any]): 需要解析或保存的结构化数据。

    Returns:
        dict[str, Any]: 构造领域消息使用的 message_id 和 created_at 参数。

    Raises:
        ValueError: 必需字段缺失、为空或时间无效。
    """
    return {
        "message_id": _require_string(data.get("_message_id"), "_message_id", allow_empty=False),
        "created_at": normalize_utc(_require_string(data.get("_created_at"), "_created_at", allow_empty=False)),
    }


def _decode_human_content(content: object) -> tuple[str, tuple[str, ...]]:
    """解析真人或工具图片消息的正文结构，分离文本和图片地址。

    Args:
        content (object): 当前处理的正文。

    Returns:
        tuple[str, tuple[str, ...]]: 按出现顺序合并的文本及图片地址元组。

    Raises:
        ValueError: 内容类型、分段类型或图片地址结构无效。
    """
    if isinstance(content, str):
        return content, ()
    if not isinstance(content, list):
        raise ValueError("HumanMessage.content 必须是字符串或多模态数组")
    texts: list[str] = []
    images: list[str] = []
    for part in content:
        mapping = _require_mapping(part, "content[]")
        if mapping.get("type") == "text":
            texts.append(_require_string(mapping.get("text", ""), "content[].text"))
        elif mapping.get("type") == "image_url":
            image_url = _require_mapping(mapping.get("image_url"), "content[].image_url")
            images.append(_require_string(image_url.get("url"), "content[].image_url.url", allow_empty=False))
        else:
            raise ValueError(f"不支持的 HumanMessage content part: {mapping.get('type')!r}")
    return "".join(texts), tuple(images)


def message_from_storage_format(data: Mapping[str, Any]) -> BaseMessage:
    """按领域消息类型恢复 checkpoint 值，缺少渠道字段的旧消息解释为 desktop。

    Args:
        data (Mapping[str, Any]): 待还原的领域存储值。

    Returns:
        BaseMessage: 按存储类型恢复的领域消息，包含原编号、时间及专属元数据。

    Raises:
        MessageContractError: 未知或缺失的 _message_type: {message_type}；数据库与当前消息契约不兼容。
        ValueError: _human.image_filenames 必须是字符串数组。
    """

    message_type = data.get("_message_type")
    if not isinstance(message_type, str) or message_type not in _STORAGE_MESSAGE_TYPES:
        raise MessageContractError(
            f"未知或缺失的 _message_type: {message_type!r}；数据库与当前消息契约不兼容"
        )
    common = _common_fields(data)
    content = data.get("content", "")
    # 1. 真人来源从领域元数据读取，旧记录省略字段时按 desktop 恢复。
    if message_type == "human":
        text, images = _decode_human_content(content)
        human = _require_mapping(data.get("_human"), "_human")
        refs = human.get("image_filenames") or []
        if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
            raise ValueError("_human.image_filenames 必须是字符串数组")
        return HumanMessage(
            text=text, image_urls=images, image_filenames=tuple(refs),
            origin_channel=_require_string(human.get("origin_channel", DESKTOP_CHANNEL), "_human.origin_channel", allow_empty=False),
            **common,
        )
    if message_type == "system":
        return SystemMessage(text=_require_string(content, "content"), **common)
    # 2. Assistant 来源从专用存储字段恢复，模型 wire 仍只包含文本和工具调用。
    if message_type == "assistant":
        raw_calls = data.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ValueError("AssistantMessage.tool_calls 必须是数组")
        return AssistantMessage(
            text=_require_string(content, "content"),
            tool_calls=tuple(ToolCall.from_dict(call) for call in raw_calls),
            origin_channel=_require_string(
                _require_mapping(data.get("_assistant", {}), "_assistant").get("origin_channel", DESKTOP_CHANNEL),
                "_assistant.origin_channel", allow_empty=False,
            ),
            **common,
        )
    if message_type == "tool":
        tool = _require_mapping(data.get("_tool"), "_tool")
        return ToolMessage(
            content=_require_string(content, "content"),
            tool_name=_require_string(data.get("name"), "name", allow_empty=False),
            tool_call_id=_require_string(
                data.get("tool_call_id"), "tool_call_id", allow_empty=False
            ),
            compressible=bool(tool.get("compressible", False)),
            **common,
        )
    if message_type == "tool_media":
        media = _require_mapping(data.get("_tool_media"), "_tool_media")
        payload_data = _require_mapping(media.get("payload"), "_tool_media.payload")
        refs = payload_data.get("storage_refs") or []
        if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
            raise ValueError("_tool_media.payload.storage_refs 必须是字符串数组")
        text, urls = _decode_human_content(content)
        if payload_data.get("kind") == "available":
            payload: MediaPayload = AvailableMedia(urls=urls, storage_refs=tuple(refs))
        elif payload_data.get("kind") == "compacted":
            payload = CompactedMedia(storage_refs=tuple(refs))
        else:
            raise ValueError("未知的工具媒体 payload kind")
        return ToolMediaMessage(
            tool_name=_require_string(media.get("tool_name", ""), "_tool_media.tool_name"),
            media_kind=_require_string(media.get("media_kind"), "_tool_media.media_kind", allow_empty=False),  # type: ignore[arg-type]
            text=text,
            payload=payload,
            associated_message_id=(
                str(media["associated_message_id"])
                if media.get("associated_message_id")
                else None
            ),
            **common,
        )
    if message_type == "context_summary":
        summary = _require_mapping(data.get("_context_summary"), "_context_summary")
        return ContextSummaryMessage(
            content=_require_string(content, "content"),
            summary_schema_version=int(summary.get("schema_version", 0)),
            covered_through_message_id=_require_string(summary.get("covered_through_message_id"), "_context_summary.covered_through_message_id", allow_empty=False),
            covered_effective_date=_require_string(summary.get("covered_effective_date"), "_context_summary.covered_effective_date", allow_empty=False),
            compression_ratio=float(summary.get("compression_ratio", 0.0)),
            **common,
        )
    if message_type == "task_result":
        task = _require_mapping(data.get("_task"), "_task")
        rendered = _require_string(content, "content")
        result = rendered.split("\n", 1)[1] if "\n" in rendered else rendered
        return TaskResultMessage(
            task_id=_require_string(task.get("task_id"), "_task.task_id", allow_empty=False),
            task_name=_require_string(task.get("task_name"), "_task.task_name", allow_empty=False),
            status=_require_string(task.get("status"), "_task.status", allow_empty=False),  # type: ignore[arg-type]
            result=result,
            **common,
        )
    if message_type == "environment_perception":
        environment = _require_mapping(data.get("_environment"), "_environment")
        rendered = _require_string(content, "content")
        description = rendered.split("\n", 1)[1] if "\n" in rendered else rendered
        return EnvironmentPerceptionMessage(
            perception_kind=_require_string(environment.get("perception_kind"), "_environment.perception_kind", allow_empty=False),
            description=description,
            **common,
        )
    if message_type == "subagent":
        subagent = _require_mapping(data.get("_subagent"), "_subagent")
        rendered = _require_string(content, "content")
        result = rendered.split("\n", 1)[1] if "\n" in rendered else rendered
        return SubagentMessage(
            subagent_id=_require_string(subagent.get("subagent_id"), "_subagent.subagent_id", allow_empty=False),
            subagent_name=_require_string(subagent.get("subagent_name"), "_subagent.subagent_name", allow_empty=False),
            result=result,
            **common,
        )
    raise MessageContractError(
        f"未知或缺失的 _message_type: {message_type!r}；数据库与当前消息契约不兼容"
    )


def message_from_protocol_format(
    *,
    message_type: str | None,
    message_json: Mapping[str, Any],
    message_id: str,
    created_at: str | datetime,
    domain_json: Mapping[str, Any] | None = None,
    associated_message_id: str | None = None,
) -> BaseMessage:
    """从协议正文和领域元数据恢复强类型消息，保留真人及回复的入口来源。

    Args:
        message_type (str | None): 协议行的领域消息类型。
        message_json (Mapping[str, Any]): 模型协议正文。
        message_id (str): 领域消息稳定标识。
        created_at (str | datetime): 原消息 UTC 创建时间。
        domain_json (Mapping[str, Any] | None): 领域专用存储元数据。
        associated_message_id (str | None): 工具媒体关联的消息标识。

    Returns:
        BaseMessage: 由协议正文和领域元数据恢复的强类型消息。

    Raises:
        MessageContractError: 协议日志 message_type={message_type} 非法；数据库与当前消息契约不兼容。
        ValueError: 协议日志 assistant.tool_calls 必须是数组。
    """
    if not isinstance(message_type, str) or message_type not in PROTOCOL_MESSAGE_TYPES:
        raise MessageContractError(
            f"协议日志 message_type={message_type!r} 非法；数据库与当前消息契约不兼容"
        )
    wire = dict(message_json)
    domain = dict(domain_json or {})
    common = {"message_id": message_id, "created_at": normalize_utc(created_at)}
    content = wire.get("content", "")
    # 1. 使用协议正文恢复真人内容，来源保存在 domain_json 而非供应商 wire。
    if message_type == "human":
        text, images = _decode_human_content(content)
        human = domain.get("human") if isinstance(domain.get("human"), Mapping) else {}
        refs = human.get("image_filenames") or []
        return HumanMessage(
            text=text,
            image_urls=images,
            image_filenames=tuple(str(item) for item in refs),
            origin_channel=_require_string(human.get("origin_channel", DESKTOP_CHANNEL), "human.origin_channel", allow_empty=False),
            **common,
        )
    # 2. 同样从 Assistant 领域字段取来源，旧协议记录缺字段时使用 desktop。
    if message_type == "assistant":
        calls = wire.get("tool_calls") or []
        if not isinstance(calls, list):
            raise ValueError("协议日志 assistant.tool_calls 必须是数组")
        return AssistantMessage(
            text=_require_string(content, "content"),
            tool_calls=tuple(ToolCall.from_dict(item) for item in calls),
            origin_channel=_require_string(
                (domain.get("assistant") if isinstance(domain.get("assistant"), Mapping) else {}).get("origin_channel", DESKTOP_CHANNEL),
                "assistant.origin_channel", allow_empty=False,
            ),
            **common,
        )
    if message_type == "tool":
        tool = domain.get("tool") if isinstance(domain.get("tool"), Mapping) else {}
        return ToolMessage(
            content=_require_string(content, "content"),
            tool_name=_require_string(wire.get("name"), "name", allow_empty=False),
            tool_call_id=_require_string(
                wire.get("tool_call_id"), "tool_call_id", allow_empty=False
            ),
            compressible=bool(tool.get("compressible", False)),
            **common,
        )
    if message_type == "tool_media":
        text, urls = _decode_human_content(content)
        payload_kind = domain.get("payload_kind")
        refs = domain.get("storage_refs") or []
        payload: MediaPayload
        if payload_kind == "compacted":
            payload = CompactedMedia(storage_refs=tuple(str(item) for item in refs))
        elif payload_kind == "available":
            payload = AvailableMedia(
                urls=urls,
                storage_refs=tuple(str(item) for item in refs),
            )
        else:
            raise ValueError("协议日志 tool_media.payload_kind 无效")
        media_kind = _require_string(domain.get("media_kind"), "domain.media_kind", allow_empty=False)
        return ToolMediaMessage(
            tool_name=str(domain.get("tool_name") or ""),
            media_kind=media_kind,  # type: ignore[arg-type]
            text=text,
            payload=payload,
            associated_message_id=(
                associated_message_id
                or (str(domain["associated_message_id"]) if domain.get("associated_message_id") else None)
            ),
            **common,
        )
    storage = {
        **wire,
        "_message_type": message_type,
        "_message_id": message_id,
        "_created_at": normalize_utc(created_at).isoformat(),
        **{f"_{key}": value for key, value in domain.items()},
    }
    return message_from_storage_format(storage)


def messages_to_openai_format(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """按领域消息顺序生成模型协议消息，不附加 checkpoint 的内部元数据。

    Args:
        messages (Sequence[BaseMessage]): 按对话顺序排列的消息集合。

    Returns:
        list[dict[str, Any]]: 与原消息逐项对应的模型消息列表。
    """
    return [message.to_openai_format() for message in messages]


def is_real_human_message(message: BaseMessage) -> bool:
    """判断消息是否为真人输入，截图和后台通知不会被当作真人输入。

    Args:
        message (BaseMessage): 当前需要读取或追加的消息。

    Returns:
        bool: 消息为 HumanMessage 时为 True。
    """
    return isinstance(message, HumanMessage)


def is_screenshot_message(message: BaseMessage) -> bool:
    """识别工具截图消息，包含仍可用与已压缩的截图。

    Args:
        message (BaseMessage): 当前需要读取或追加的消息。

    Returns:
        bool: 消息为 screenshot 类别的 ToolMediaMessage 时为 True。
    """
    return isinstance(message, ToolMediaMessage) and message.media_kind == "screenshot"


def is_system_injected_user_message(message: BaseMessage) -> bool:
    """识别以 user 角色发给模型的内部信息，区别于真人发送的消息。

    Args:
        message (BaseMessage): 当前需要读取或追加的消息。

    Returns:
        bool: 工具图片、上下文摘要或运行时通知为 True。
    """
    return isinstance(message, (ToolMediaMessage, ContextSummaryMessage, RuntimeInputMessage))


def is_user_message(message: BaseMessage) -> bool:
    """判断领域消息在模型请求中是否采用 user 角色。

    Args:
        message (BaseMessage): 当前需要读取或追加的消息。

    Returns:
        bool: 真人输入、工具图片、上下文摘要或运行时通知为 True。
    """
    return isinstance(message, (HumanMessage, ToolMediaMessage, ContextSummaryMessage, RuntimeInputMessage))


__all__ = [
    "AssistantMessage",
    "AvailableMedia",
    "BaseMessage",
    "CompactedMedia",
    "ContextSummaryMessage",
    "EnvironmentPerceptionMessage",
    "HumanMessage",
    "MessageContractError",
    "PROTOCOL_MESSAGE_TYPES",
    "RuntimeInputMessage",
    "SubagentMessage",
    "SystemMessage",
    "TaskResultMessage",
    "ToolCall",
    "ToolMediaMessage",
    "ToolMessage",
    "is_real_human_message",
    "is_screenshot_message",
    "is_system_injected_user_message",
    "is_user_message",
    "message_from_storage_format",
    "message_from_protocol_format",
    "to_json_value",
    "messages_to_openai_format",
]
