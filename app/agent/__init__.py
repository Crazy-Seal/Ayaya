"""
Ayaya Agent

插件化 Agent 框架。

核心特性:
- 自定义消息类型，兼容 OpenAI API 格式
- 插件化架构，支持工具插件、记忆插件、处理插件、MCP 插件
- 事件驱动中断机制
- 工具热插拔
- MCP 协议集成
"""

from app.agent.state import AgentState
from app.agent.state.messages import BaseMessage, HumanMessage, ToolCall
from app.agent.tools.base import ToolContext, ToolResult, InterruptEvent

__all__ = [
    "Agent",
    "AgentState",
    "BaseMessage",
    "HumanMessage",
    "ToolCall",
    "ToolContext",
    "ToolResult",
    "InterruptEvent",
]


def __getattr__(name: str):
    """首次访问包导出的 Agent 时才导入实现，减少包初始化时的循环依赖。

    Args:
        name (str): 当前对象的名称。

    Returns:
        type[Agent]: Agent 类。

    Raises:
        AttributeError: 访问名称不是 Agent。
    """
    if name == "Agent":
        from app.agent.agent import Agent

        return Agent
    raise AttributeError(name)
