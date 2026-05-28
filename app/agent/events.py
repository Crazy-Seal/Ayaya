"""流式事件类型定义

用于 Agent 流式输出与路由层之间的类型安全通信。
"""

from dataclasses import dataclass
from typing import Union

from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.types import Interrupt


@dataclass
class ToolCallEvent:
    """工具调用事件"""
    tool_name: str


@dataclass
class TextChunk:
    """文本分片"""
    content: str


# 流式事件联合类型
StreamEvent = Union[
    ToolCallEvent,
    TextChunk,
    AIMessage,
    AIMessageChunk,
    Interrupt,
]
