"""
事件路由器

负责 Agent 内部事件通知和事件类型定义。
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """事件类型"""
    TEXT_CHUNK = "text_chunk"       # 文本分片
    TOOL_CALL = "tool_call"         # 工具调用
    INTERRUPT = "interrupt"         # 中断
    ERROR = "error"                 # 错误
    DONE = "done"                   # 完成


@dataclass
class AgentEvent:
    """携带 Agent 内部事件类型和内容，由 Scheduler 转成渠道可见事件。

    Attributes:
        type (EventType): Agent 内部事件类型。
        data (Any): 待还原的领域存储值。
    """
    type: EventType
    data: Any


class EventRouter:
    """事件路由器 - 管理 Agent 内部事件通知"""

    def __init__(self):
        self._subscribers: list[Callable[[AgentEvent], Awaitable[None]]] = []

    def subscribe(self, callback: Callable[[AgentEvent], Awaitable[None]]) -> None:
        """订阅事件"""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[AgentEvent], Awaitable[None]]) -> None:
        """取消订阅"""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    async def emit(self, event_type: EventType, data: Any) -> None:
        """发送事件给所有订阅者"""
        event = AgentEvent(type=event_type, data=data)
        for callback in self._subscribers:
            try:
                await callback(event)
            except Exception as e:
                logger.error(f"事件订阅者回调失败: {e}")

    async def emit_text(self, text: str) -> None:
        """发送文本事件"""
        await self.emit(EventType.TEXT_CHUNK, text)

    async def emit_tool_call(self, tool_name: str) -> None:
        """发送工具调用事件"""
        await self.emit(EventType.TOOL_CALL, tool_name)

    async def emit_interrupt(self, interrupt_data: dict) -> None:
        """发送中断事件"""
        await self.emit(EventType.INTERRUPT, interrupt_data)

    async def emit_error(self, error: str | Exception) -> None:
        """发送错误事件"""
        await self.emit(EventType.ERROR, str(error))

    async def emit_done(self) -> None:
        """发送完成事件"""
        await self.emit(EventType.DONE, None)
