"""让父子进程通过私有管道交换 JSON 消息。"""

import json
from enum import StrEnum
from multiprocessing.connection import PipeConnection
from typing import TYPE_CHECKING, Literal, TypeAlias, TypedDict, TypeVar, cast

# 1. 后端按应用包加载，独立 runner 按脚本目录加载；类型检查统一使用应用包路径。
if TYPE_CHECKING:
    from app.repl.repl_api import JsonValue, ReplEndReason
elif __package__:
    from .repl_api import JsonValue, ReplEndReason
else:
    from repl_api import JsonValue, ReplEndReason


class MessageKind(StrEnum):
    """标记 runner 初始化、执行和输出的消息用途。"""

    READY = "ready"
    INITIALIZE = "initialize"
    ERROR = "error"
    EVENT = "event"
    EXECUTE = "execute"
    INTERRUPT = "interrupt"
    INTERRUPT_ACK = "interrupt_ack"
    TEXT = "text"
    IMAGE = "image"
    FINISHED = "finished"


class RunnerPhase(StrEnum):
    """限定子进程受理中断的执行位置。"""

    IDLE = "idle"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    CLEANUP = "cleanup"


class CapabilityDeclaration(TypedDict):
    """将插件能力的模块位置、绑定名称和初始化配置交给子进程。"""

    module_root: str
    module_name: str
    export_name: str
    config: dict[str, JsonValue]


class InitializeCommand(TypedDict):
    """在子进程就绪前装配本会话启用的插件能力。"""

    kind: Literal[MessageKind.INITIALIZE]
    capabilities: list[CapabilityDeclaration]


class ExecuteCommand(TypedDict):
    """执行一次经过审批的模型代码。"""

    kind: Literal[MessageKind.EXECUTE]
    execution_id: str
    code: str


class InterruptCommand(TypedDict):
    """请求中断指定编号的代码执行。"""

    kind: Literal[MessageKind.INTERRUPT]
    execution_id: str


class ReadyMessage(TypedDict):
    """告知父进程全部能力初始化完成，可以接收代码。"""

    kind: Literal[MessageKind.READY]
    execution_id: str


class ErrorMessage(TypedDict):
    """向父进程报告能力初始化失败的原因。"""

    kind: Literal[MessageKind.ERROR]
    execution_id: str
    error: str


class InterruptAckMessage(TypedDict):
    """告知父进程指定执行的中断请求是否已被受理。"""

    kind: Literal[MessageKind.INTERRUPT_ACK]
    execution_id: str
    accepted: bool


class TextMessage(TypedDict):
    """将代码或库产生的文字加入当前工具结果。"""

    kind: Literal[MessageKind.TEXT]
    execution_id: str
    text: str


class ImageMessage(TypedDict):
    """将显式输出图片的编码内容和格式交给父进程。"""

    kind: Literal[MessageKind.IMAGE]
    execution_id: str
    data: str
    mime_type: str


class EventMessage(TypedDict):
    """将指定能力发布的事件交给父进程绑定插件来源。"""

    kind: Literal[MessageKind.EVENT]
    execution_id: str
    capability: str
    name: str
    payload: dict[str, JsonValue]


class FinishedMessage(TypedDict):
    """报告本次执行及清理已结束，允许父进程释放占用。"""

    kind: Literal[MessageKind.FINISHED]
    execution_id: str
    reason: ReplEndReason
    error: str
    pid: int


Command: TypeAlias = InitializeCommand | ExecuteCommand | InterruptCommand
ResultMessage: TypeAlias = (
    ReadyMessage | ErrorMessage | InterruptAckMessage | TextMessage | ImageMessage | EventMessage | FinishedMessage
)
Message: TypeAlias = Command | ResultMessage
T = TypeVar("T")


def send(connection: PipeConnection, message: Message) -> None:
    """把一条完整消息编码后写入专用管道。

    Args:
        connection (PipeConnection): 本进程拥有的管道写端。
        message (Message): 已按消息用途提供完整字段的命令或结果。
    """
    connection.send_bytes(json.dumps(message, ensure_ascii=True, allow_nan=False).encode("utf-8"))


def _field(message: dict[str, object], name: str, expected: type[T]) -> T:
    """在接收边界读取必填字段，字段错误时报告协议故障。

    Args:
        message (dict[str, object]): JSON 解码后的消息或能力声明。
        name (str): 当前消息必须提供的字段。
        expected (type[T]): 字段应使用的 Python 类型。

    Returns:
        T: 已检查类型的字段值。

    Raises:
        ValueError: 字段缺失或类型与协议约定不符。
    """
    value = message.get(name)
    if not isinstance(value, expected) or expected is int and isinstance(value, bool):
        raise ValueError(f"REPL 消息字段 {name} 应为 {expected.__name__}")
    return value


def _object(value: object) -> dict[str, object]:
    """读取 JSON 对象，供消息或嵌套能力声明检查字段。

    Args:
        value (object): JSON 解析器产生的值。

    Returns:
        dict[str, object]: JSON 解析器保证键为字符串的对象。

    Raises:
        ValueError: 当前值不是 JSON 对象。
    """
    if not isinstance(value, dict):
        raise ValueError("REPL 消息或能力声明应为 JSON 对象")
    return cast(dict[str, object], value)


def _json_fields(message: dict[str, object], name: str) -> dict[str, JsonValue]:
    """读取由插件自行定义字段的配置或事件载荷。

    Args:
        message (dict[str, object]): JSON 解析器产生的消息或能力声明。
        name (str): 保存插件数据的字段名。

    Returns:
        dict[str, JsonValue]: 已确认对象结构、内容来自 JSON 解析器的插件数据。
    """
    return cast(dict[str, JsonValue], _object(message.get(name)))


def receive(connection: PipeConnection) -> Message:
    """读取完整消息并检查固定字段，交付可按种类区分的命令或结果。

    Args:
        connection (PipeConnection): 本进程拥有的管道读端。

    Returns:
        Message: 字段完整且类型符合对应消息种类的命令或结果。

    Raises:
        ValueError: JSON、消息种类或固定字段不符合协议。
    """
    raw: object = json.loads(connection.recv_bytes())
    message = _object(raw)
    kind = MessageKind(_field(message, "kind", str))
    # 1. 初始化消息包含能力列表，逐项检查模块声明，配置内容交给能力工厂解释。
    if kind == MessageKind.INITIALIZE:
        values = _field(message, "capabilities", list)
        capabilities: list[CapabilityDeclaration] = []
        for value in values:
            declaration = _object(value)
            capabilities.append({
                "module_root": _field(declaration, "module_root", str),
                "module_name": _field(declaration, "module_name", str),
                "export_name": _field(declaration, "export_name", str),
                "config": _json_fields(declaration, "config"),
            })
        return {"kind": MessageKind.INITIALIZE, "capabilities": capabilities}
    # 2. 其余消息携带执行编号，就绪与初始化失败时编号为空。
    execution_id = _field(message, "execution_id", str)
    match kind:
        case MessageKind.EXECUTE:
            return {"kind": kind, "execution_id": execution_id, "code": _field(message, "code", str)}
        case MessageKind.INTERRUPT:
            return {"kind": kind, "execution_id": execution_id}
        case MessageKind.READY:
            return {"kind": kind, "execution_id": execution_id}
        case MessageKind.ERROR:
            return {"kind": kind, "execution_id": execution_id, "error": _field(message, "error", str)}
        case MessageKind.INTERRUPT_ACK:
            return {"kind": kind, "execution_id": execution_id, "accepted": _field(message, "accepted", bool)}
        case MessageKind.TEXT:
            return {"kind": kind, "execution_id": execution_id, "text": _field(message, "text", str)}
        case MessageKind.IMAGE:
            return {"kind": kind, "execution_id": execution_id,
                    "data": _field(message, "data", str), "mime_type": _field(message, "mime_type", str)}
        case MessageKind.EVENT:
            return {"kind": kind, "execution_id": execution_id, "capability": _field(message, "capability", str),
                    "name": _field(message, "name", str), "payload": _json_fields(message, "payload")}
        case MessageKind.FINISHED:
            return {"kind": kind, "execution_id": execution_id,
                    "reason": ReplEndReason(_field(message, "reason", str)),
                    "error": _field(message, "error", str), "pid": _field(message, "pid", int)}
