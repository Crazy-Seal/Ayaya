"""
Ayaya Agent V2

插件化 Agent 框架。

核心特性:
- 自定义消息类型，兼容 OpenAI API 格式
- 插件化架构，支持工具插件、记忆插件、处理插件、MCP 插件
- 事件驱动中断机制
- 工具热插拔
- MCP 协议集成
"""

from app.agent_v2.agent import Agent
from app.agent_v2.state import AgentState
from app.agent_v2.message import Message, ContentPart, ToolCall
from app.agent_v2.context import ToolContext, ToolResult, InterruptEvent

__all__ = [
    "Agent",
    "AgentState",
    "Message",
    "ContentPart",
    "ToolCall",
    "ToolContext",
    "ToolResult",
    "InterruptEvent",
]
