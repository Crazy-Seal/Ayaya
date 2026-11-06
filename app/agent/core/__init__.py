"""
核心管理器模块

包含:
- ToolManager: 工具注册、查找、热插拔
- PluginManager: 插件索引管理、业务钩子分发
- SessionStateManager: 状态持久化、checkpoint 管理
- AgentEvent / EventType: 交给会话调度器的执行事件
- ExecutionPipeline: 执行管道
"""

from app.agent.core.tool_manager import ToolManager
from app.agent.core.plugin_manager import PluginManager, PluginHook
from app.agent.state.manager import SessionStateManager
from app.agent.core.events import EventType, AgentEvent
from app.agent.core.pipeline import ExecutionPipeline

__all__ = [
    "ToolManager",
    "PluginManager",
    "PluginHook",
    "SessionStateManager",
    "EventType",
    "AgentEvent",
    "ExecutionPipeline",
]
