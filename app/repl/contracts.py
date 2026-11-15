"""集中声明应用调用 REPL 时使用的来源和结果。"""

from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import TypeAlias

from app.repl.repl_api import JsonValue, ReplCapability, ReplEndReason


@dataclass(frozen=True)
class PreparedReplCapability:
    """把经过准备的能力与真实来源插件绑定。"""

    plugin_name: str
    declaration: ReplCapability


@dataclass(frozen=True)
class ReplCallContext:
    """为一次代码调用提供会话、来源轮次及能力集合。"""

    session_id: str
    run_id: str
    tool_call_id: str
    capabilities: tuple[PreparedReplCapability, ...]


@dataclass(frozen=True)
class ReplText:
    """保存代码产生的一段文字。"""

    text: str


@dataclass(frozen=True)
class ReplImage:
    """保存已经编码的 PNG 或 JPEG 工具图片。"""

    data: bytes
    mime_type: str


@dataclass(frozen=True)
class ReplResult:
    """返回代码输出、实际结束原因和运行进程编号。"""

    outputs: tuple[ReplText | ReplImage, ...]
    reason: ReplEndReason
    error: str = ""
    pid: int | None = None


@dataclass(frozen=True)
class ReplPluginEvent:
    """交付父端已经绑定真实能力来源的一条插件事件。"""

    execution_id: str
    context: ReplCallContext
    capability: PreparedReplCapability
    name: str
    payload: dict[str, JsonValue]


@dataclass(frozen=True)
class ReplExecutionFinished:
    """通知应用层关闭本次执行尚未关闭的展示资源。"""

    execution_id: str
    context: ReplCallContext


ReplEventHandler: TypeAlias = Callable[[ReplPluginEvent | ReplExecutionFinished], Awaitable[None]]
