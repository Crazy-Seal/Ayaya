"""
核心管理器模块

包含:
- ToolManager: 工具注册、查找、热插拔
- PluginManager: 插件生命周期管理、钩子分发
- StateManager: 状态持久化、checkpoint 管理
- EventRouter: SSE 事件推送
- ExecutionPipeline: 执行管道
"""

from app.agent.core.tool_manager import ToolManager
from app.agent.core.plugin_manager import PluginManager, PluginHook
from app.agent.core.state_manager import StateManager
from app.agent.core.event_router import EventRouter, EventType, AgentEvent
from app.agent.core.pipeline import ExecutionPipeline

__all__ = [
    "ToolManager",
    "PluginManager",
    "PluginHook",
    "StateManager",
    "EventRouter",
    "EventType",
    "AgentEvent",
    "ExecutionPipeline",
]
