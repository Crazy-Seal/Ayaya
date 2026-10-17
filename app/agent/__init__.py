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

# 底层终端模块也位于本包下；仅导入包路径时不能顺带加载 Agent 状态与模型依赖。
_EXPORT_MODULES = {
    "Agent": "app.agent.agent",
    "AgentState": "app.agent.state",
    "BaseMessage": "app.agent.state.messages",
    "HumanMessage": "app.agent.state.messages",
    "ToolCall": "app.agent.state.messages",
    "ToolContext": "app.agent.tools.core.base",
    "ToolResult": "app.agent.tools.core.base",
    "InterruptEvent": "app.agent.tools.core.base",
}

__all__ = list(_EXPORT_MODULES)


def __getattr__(name: str):
    """首次访问公开类型时才导入实现，底层模块可以独立于完整 Agent 加载。

    Args:
        name (str): 原有包级公开类型名称。

    Returns:
        type: 对应公开类型，保持原有导出名称与类身份。

    Raises:
        AttributeError: 访问名称未在公开导出表中声明。
    """
    from importlib import import_module

    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    # 1. 按需导入并缓存原类型，重复访问不会创建替身或重复装配 Agent。
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
