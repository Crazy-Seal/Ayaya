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
        if isinstance(event, ToolCallEvent):
            payload = {"tool_name": event.tool_name}
            if event.error_message:
                payload["error_message"] = event.error_message
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

    @staticmethod
    def extract_text(content: Any) -> str:
        """从消息内容中提取文本。

        Args:
            content: 消息内容（字符串、字典或列表）

        Returns:
            提取的文本
        """
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                return text
            return ""
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    text_parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
            return "".join(text_parts)

        # 兼容 LangChain message/chunk 的 text 属性/方法
        if hasattr(content, "text"):
            text_attr = getattr(content, "text")
            if isinstance(text_attr, str):
                return text_attr
            if callable(text_attr):
                text = text_attr()
                if isinstance(text, str):
                    return text
        return ""
