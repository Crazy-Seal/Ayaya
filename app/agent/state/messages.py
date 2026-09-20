"""与 Provider 无关的不可变消息值及持久化 Adapter。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
import json
from types import MappingProxyType
from typing import Any, ClassVar, Iterator, Literal, Mapping, Sequence, cast, get_args
from uuid import uuid4

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
    """由只读代理承载的递归不可变 mapping。"""

    __slots__ = ("_view",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        try:
            object.__getattribute__(self, "_view")
        except AttributeError:
            object.__setattr__(self, "_view", MappingProxyType(dict(values)))
            return
        raise TypeError("immutable mapping")

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("immutable mapping")

    def __getitem__(self, key: str) -> Any:
        return self._view[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._view)

    def __len__(self) -> int:
        return len(self._view)

    def __setitem__(self, _key: str, _value: object) -> None:
        raise TypeError("immutable mapping")

    def __deepcopy__(self, _memo):
        return self


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenMapping({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def to_json_value(value: Any) -> Any:
    """将不可变消息值投影为标准 JSON 容器。"""
    return _thaw(value)


def _require_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"消息字段 {field_name} 必须是对象")
    return value


def _require_string(value: object, field_name: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"消息字段 {field_name} 必须是字符串")
    return value


@dataclass(frozen=True, slots=True)
class ToolCall:
    """一个不可变的 OpenAI-compatible 工具调用。"""

    id: str
    name: str
    args: Mapping[str, Any]
    extra_content: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
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
    """消息公共 Interface；领域专用字段由具体子类持有。"""

    message_type: ClassVar[MessageType]
    message_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", normalize_utc(self.created_at))
        if not self.message_id:
            raise ValueError("message_id 不能为空")

    @abstractmethod
    def to_openai_format(self) -> dict[str, Any]:
        raise NotImplementedError

    def _storage_fields(self) -> dict[str, Any]:
        return {}

    def to_storage_format(self) -> dict[str, Any]:
        return {
            **self.to_openai_format(),
            "_message_type": self.message_type,
            "_message_id": self.message_id,
            "_created_at": self.created_at.isoformat(),
            **self._storage_fields(),
        }

    @property
    def enters_protocol_log(self) -> bool:
        return True

    @property
    def text(self) -> str:
        return ""


@dataclass(frozen=True, slots=True, kw_only=True)
class HumanMessage(BaseMessage):
    message_type: ClassVar[MessageType] = "human"
    text: str
    image_urls: tuple[str, ...] = ()
    image_filenames: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        BaseMessage.__post_init__(self)
        if not isinstance(self.text, str):
            raise TypeError("HumanMessage.text 必须是字符串")
        object.__setattr__(self, "image_urls", tuple(self.image_urls))
        object.__setattr__(self, "image_filenames", tuple(self.image_filenames))
        if not all(isinstance(item, str) and item for item in self.image_urls):
            raise ValueError("HumanMessage.image_urls 必须是非空字符串")
        if not all(isinstance(item, str) and item for item in self.image_filenames):
            raise ValueError("HumanMessage.image_filenames 必须是非空字符串")
        if self.image_filenames and len(self.image_filenames) != len(self.image_urls):
            raise ValueError("真人消息的图片引用数量必须与图片数量一致")

    def to_openai_format(self) -> dict[str, Any]:
        if not self.image_urls:
            return {"role": "user", "content": self.text}
        content: list[dict[str, Any]] = [{"type": "text", "text": self.text}]
        content.extend(
            {"type": "image_url", "image_url": {"url": url}}
            for url in self.image_urls
        )
        return {"role": "user", "content": content}

    def _storage_fields(self) -> dict[str, Any]:
        return {"_human": {"image_filenames": list(self.image_filenames)}}


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemMessage(BaseMessage):
    message_type: ClassVar[MessageType] = "system"
    text: str

    def __post_init__(self) -> None:
        BaseMessage.__post_init__(self)
        if not isinstance(self.text, str):
            raise TypeError("SystemMessage.text 必须是字符串")

    def to_openai_format(self) -> dict[str, Any]:
        return {"role": "system", "content": self.text}

    @property
    def enters_protocol_log(self) -> bool:
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessage(BaseMessage):
    message_type: ClassVar[MessageType] = "assistant"
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()

    def __post_init__(self) -> None:
        BaseMessage.__post_init__(self)
        if not isinstance(self.text, str):
            raise TypeError("AssistantMessage.text 必须是字符串")
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if not all(isinstance(call, ToolCall) for call in self.tool_calls):
            raise TypeError("AssistantMessage.tool_calls 只能包含 ToolCall")

    def to_openai_format(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            result["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolMessage(BaseMessage):
    message_type: ClassVar[MessageType] = "tool"
    content: str
    tool_name: str
    tool_call_id: str
    compressible: bool = False

    def __post_init__(self) -> None:
        BaseMessage.__post_init__(self)
        if not isinstance(self.content, str):
            raise TypeError("ToolMessage.content 必须是字符串")
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ValueError("ToolMessage.tool_name 不能为空")
        if not isinstance(self.tool_call_id, str) or not self.tool_call_id:
            raise ValueError("ToolMessage.tool_call_id 不能为空")

    @property
    def text(self) -> str:
        return self.content

    def to_openai_format(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": "tool",
            "content": self.content,
            "name": self.tool_name,
        }
        result["tool_call_id"] = self.tool_call_id
        return result

    def _storage_fields(self) -> dict[str, Any]:
        return {"_tool": {"compressible": self.compressible}}


@dataclass(frozen=True, slots=True, kw_only=True)
class AvailableMedia:
    kind: ClassVar[Literal["available"]] = "available"
    urls: tuple[str, ...]
    storage_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
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
    kind: ClassVar[Literal["compacted"]] = "compacted"
    storage_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
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
    message_type: ClassVar[MessageType] = "tool_media"
    tool_name: str
    media_kind: MediaKind
    text: str
    payload: MediaPayload
    associated_message_id: str | None = None

    def __post_init__(self) -> None:
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
    message_type: ClassVar[MessageType] = "context_summary"
    content: str
    summary_schema_version: int
    covered_through_message_id: str
    covered_effective_date: str
    compression_ratio: float

    def __post_init__(self) -> None:
        BaseMessage.__post_init__(self)
        if not isinstance(self.content, str):
            raise TypeError("ContextSummaryMessage.content 必须是字符串")
        if self.summary_schema_version <= 0:
            raise ValueError("摘要 schema_version 必须为正整数")
        if not self.covered_through_message_id or not self.covered_effective_date:
            raise ValueError("摘要覆盖边界不能为空")

    @property
    def text(self) -> str:
        return self.content

    def to_openai_format(self) -> dict[str, Any]:
        return {
            "role": "user",
            "name": CONTEXT_SUMMARY_MESSAGE_NAME,
            "content": self.content,
        }

    def _storage_fields(self) -> dict[str, Any]:
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
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeInputMessage(BaseMessage, ABC):
    """可激活 Agent、但不展示为真人输入的抽象消息。"""


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskResultMessage(RuntimeInputMessage):
    message_type: ClassVar[MessageType] = "task_result"
    task_id: str
    task_name: str
    status: Literal["completed", "failed", "cancelled"]
    result: str

    def __post_init__(self) -> None:
        BaseMessage.__post_init__(self)
        if not self.task_id or not self.task_name:
            raise ValueError("后台任务标识与名称不能为空")
        if self.status not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"未知后台任务状态: {self.status!r}")
        if not isinstance(self.result, str):
            raise TypeError("TaskResultMessage.result 必须是字符串")

    @property
    def text(self) -> str:
        return self.result

    def to_openai_format(self) -> dict[str, Any]:
        status_label = {
            "completed": "完成",
            "failed": "失败",
            "cancelled": "取消",
        }[self.status]
        return {
            "role": "user",
            "name": "runtime_task_result",
            "content": f"[后台任务{status_label}] {self.task_name}\n{self.result}",
        }

    def _storage_fields(self) -> dict[str, Any]:
        return {
            "_task": {
                "task_id": self.task_id,
                "task_name": self.task_name,
                "status": self.status,
            }
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class EnvironmentPerceptionMessage(RuntimeInputMessage):
    message_type: ClassVar[MessageType] = "environment_perception"
    perception_kind: str
    description: str

    def __post_init__(self) -> None:
        BaseMessage.__post_init__(self)
        if not self.perception_kind:
            raise ValueError("EnvironmentPerceptionMessage.perception_kind 不能为空")
        if not isinstance(self.description, str):
            raise TypeError("EnvironmentPerceptionMessage.description 必须是字符串")

    @property
    def text(self) -> str:
        return self.description

    def to_openai_format(self) -> dict[str, Any]:
        return {
            "role": "user",
            "name": "runtime_environment_perception",
            "content": f"[环境变化：{self.perception_kind}]\n{self.description}",
        }

    def _storage_fields(self) -> dict[str, Any]:
        return {"_environment": {"perception_kind": self.perception_kind}}


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentMessage(RuntimeInputMessage):
    message_type: ClassVar[MessageType] = "subagent"
    subagent_id: str
    subagent_name: str
    result: str

    def __post_init__(self) -> None:
        BaseMessage.__post_init__(self)
        if not self.subagent_id or not self.subagent_name:
            raise ValueError("SubagentMessage 标识与名称不能为空")
        if not isinstance(self.result, str):
            raise TypeError("SubagentMessage.result 必须是字符串")

    @property
    def text(self) -> str:
        return self.result

    def to_openai_format(self) -> dict[str, Any]:
        return {
            "role": "user",
            "name": "runtime_subagent",
            "content": f"[子 Agent 返回：{self.subagent_name}]\n{self.result}",
        }

    def _storage_fields(self) -> dict[str, Any]:
        return {
            "_subagent": {
                "subagent_id": self.subagent_id,
                "subagent_name": self.subagent_name,
            }
        }


def _common_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "message_id": _require_string(data.get("_message_id"), "_message_id", allow_empty=False),
        "created_at": normalize_utc(_require_string(data.get("_created_at"), "_created_at", allow_empty=False)),
    }


def _decode_human_content(content: object) -> tuple[str, tuple[str, ...]]:
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
    """严格恢复 schema-v2 checkpoint，不猜测旧消息类型。"""

    message_type = data.get("_message_type")
    if not isinstance(message_type, str) or message_type not in _STORAGE_MESSAGE_TYPES:
        raise MessageContractError(
            f"未知或缺失的 _message_type: {message_type!r}；数据库与当前消息契约不兼容"
        )
    common = _common_fields(data)
    content = data.get("content", "")
    if message_type == "human":
        text, images = _decode_human_content(content)
        human = _require_mapping(data.get("_human"), "_human")
        refs = human.get("image_filenames") or []
        if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
            raise ValueError("_human.image_filenames 必须是字符串数组")
        return HumanMessage(text=text, image_urls=images, image_filenames=tuple(refs), **common)
    if message_type == "system":
        return SystemMessage(text=_require_string(content, "content"), **common)
    if message_type == "assistant":
        raw_calls = data.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ValueError("AssistantMessage.tool_calls 必须是数组")
        return AssistantMessage(
            text=_require_string(content, "content"),
            tool_calls=tuple(ToolCall.from_dict(call) for call in raw_calls),
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
    """通过 schema-v2 Adapter 恢复一条强类型协议日志。"""
    if not isinstance(message_type, str) or message_type not in PROTOCOL_MESSAGE_TYPES:
        raise MessageContractError(
            f"协议日志 message_type={message_type!r} 非法；数据库与当前消息契约不兼容"
        )
    wire = dict(message_json)
    domain = dict(domain_json or {})
    common = {"message_id": message_id, "created_at": normalize_utc(created_at)}
    content = wire.get("content", "")
    if message_type == "human":
        text, images = _decode_human_content(content)
        human = domain.get("human") if isinstance(domain.get("human"), Mapping) else {}
        refs = human.get("image_filenames") or []
        return HumanMessage(
            text=text,
            image_urls=images,
            image_filenames=tuple(str(item) for item in refs),
            **common,
        )
    if message_type == "assistant":
        calls = wire.get("tool_calls") or []
        if not isinstance(calls, list):
            raise ValueError("协议日志 assistant.tool_calls 必须是数组")
        return AssistantMessage(
            text=_require_string(content, "content"),
            tool_calls=tuple(ToolCall.from_dict(item) for item in calls),
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
    return [message.to_openai_format() for message in messages]


def is_real_human_message(message: BaseMessage) -> bool:
    return isinstance(message, HumanMessage)


def is_screenshot_message(message: BaseMessage) -> bool:
    return isinstance(message, ToolMediaMessage) and message.media_kind == "screenshot"


def is_system_injected_user_message(message: BaseMessage) -> bool:
    return isinstance(message, (ToolMediaMessage, ContextSummaryMessage, RuntimeInputMessage))


def is_user_message(message: BaseMessage) -> bool:
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
