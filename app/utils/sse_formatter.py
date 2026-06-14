"""SSE 事件格式化器

将流式事件转换为 SSE 格式字符串，统一处理事件格式化逻辑。
"""

import json
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.types import Interrupt

from app.agent.events import StreamEvent, ToolCallEvent, TextChunk


class SSEFormatter:
    """SSE 事件格式化器"""

    @staticmethod
    def format(event: StreamEvent) -> str | None:
        """将事件转换为 SSE 格式字符串。

        Args:
            event: 流式事件对象

        Returns:
            SSE 格式字符串，如果事件不需要输出则返回 None
        """
        # agent_v2 的 AgentEvent：直接复用其 to_sse()。DONE 由路由 done() 收尾，这里过滤。
        try:
            from app.agent_v2.core.event_router import AgentEvent as _V2Event, EventType as _V2Type
        except Exception:
            _V2Event = None
        if _V2Event is not None and isinstance(event, _V2Event):
            if event.type == _V2Type.DONE:
                return None
            return event.to_sse()

        if isinstance(event, ToolCallEvent):
            payload = {"tool_name": event.tool_name}
            data = json.dumps(payload, ensure_ascii=False)
            return f"event: tool_call\ndata: {data}\n\n"

        if isinstance(event, Interrupt):
            # Interrupt 包含 value 字段
            interrupt_data = {
                "value": event.value if hasattr(event, 'value') else str(event)
            }
            data = json.dumps(interrupt_data, ensure_ascii=False, default=str)
            return f"event: interrupt\ndata: {data}\n\n"

        if isinstance(event, TextChunk):
            data = json.dumps({"response": event.content}, ensure_ascii=False)
            return f"data: {data}\n\n"

        if isinstance(event, (AIMessage, AIMessageChunk)):
            # 从 AIMessage 中提取文本
            content = event.content if isinstance(event.content, str) else ""
            if content:
                data = json.dumps({"response": content}, ensure_ascii=False)
                return f"data: {data}\n\n"

        return None

    @staticmethod
    def done() -> str:
        """流结束标记"""
        return "data: [DONE]\n\n"

    @staticmethod
    def error(message: str) -> str:
        """错误事件"""
        data = json.dumps({"detail": message}, ensure_ascii=False)
        return f"event: error\ndata: {data}\n\n"
