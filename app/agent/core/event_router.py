"""
事件路由器

负责 SSE 事件推送和事件类型定义。
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Callable, Awaitable

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
    """Agent 事件"""
    type: EventType
    data: Any

    def to_sse(self) -> str:
        """转换为 SSE 格式"""
        if self.type == EventType.DONE:
            return "data: [DONE]\n\n"

        import json
        if self.type == EventType.INTERRUPT:
            # 中断事件使用特殊的 event 类型
            data_str = json.dumps({"value": self.data}, ensure_ascii=False)
            return f"event: interrupt\ndata: {data_str}\n\n"

        if self.type == EventType.ERROR:
            # 错误事件使用特殊的 event 类型
            data_str = json.dumps({"detail": str(self.data)}, ensure_ascii=False)
            return f"event: error\ndata: {data_str}\n\n"

        if self.type == EventType.TOOL_CALL:
            # 工具调用事件
            data_str = json.dumps({"tool_name": self.data}, ensure_ascii=False)
            return f"event: tool_call\ndata: {data_str}\n\n"

        # 默认文本事件
        data_str = json.dumps({"response": self.data}, ensure_ascii=False)
        return f"data: {data_str}\n\n"


class EventRouter:
    """事件路由器 - 管理 SSE 事件推送"""

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


class EventCollector:
    """事件收集器 - 收集事件并转换为 AsyncIterator"""

    def __init__(self):
        self._events: list[AgentEvent] = []
        self._done = False

    async def collect(self, event: AgentEvent) -> None:
        """收集事件"""
        self._events.append(event)
        if event.type == EventType.DONE:
            self._done = True

    def get_events(self) -> list[AgentEvent]:
        """获取所有事件"""
        return self._events

    async def to_iterator(self) -> AsyncIterator[AgentEvent]:
        """转换为异步迭代器"""
        for event in self._events:
            yield event


def events_to_sse(events: list[AgentEvent]) -> str:
    """将事件列表转换为 SSE 字符串"""
    return "".join(event.to_sse() for event in events)


async def stream_events_as_sse(
    event_stream: AsyncIterator[AgentEvent]
) -> AsyncIterator[str]:
    """将事件流转换为 SSE 字符串流"""
    async for event in event_stream:
        yield event.to_sse()
