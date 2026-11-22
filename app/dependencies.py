"""向 HTTP 路由提供本次应用启动时已经装配的共享对象。"""

from fastapi import Request

from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.crud.chat_history_dao import ChatHistoryDao
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.interaction.interaction_event_hub import InteractionEventHub
from app.mcp.mcp_connection_manager import MCPConnectionManager
from app.services.chat_history_service import ChatHistoryService
from app.services.chat_settings_service import ChatSettingsService
from app.services.mcp_service import MCPService
from app.services.plugin_service import PluginService
from app.services.settings_mutation import SettingsMutationCoordinator
from app.services.tool_permission_service import ToolPermissionService


def get_chat_history_dao(request: Request) -> ChatHistoryDao:
    """向 HTTP 请求提供应用共享的聊天历史存储。

    Args:
        request (Request): 提供当前应用实例的请求。

    Returns:
        ChatHistoryDao: 应用启动时创建的历史访问对象。
    """
    return request.app.state.chat_history_dao


def get_chat_settings_dao(request: Request) -> ChatSettingsDao:
    """向 HTTP 请求提供应用共享的会话设置存储。

    Args:
        request (Request): 提供当前应用实例的请求。

    Returns:
        ChatSettingsDao: 与运行时共用配置缓存的设置访问对象。
    """
    return request.app.state.chat_settings_dao


def get_mcp_settings_dao(request: Request) -> MCPSettingsDao:
    """向 HTTP 请求提供应用共享的 MCP 配置存储。

    Args:
        request (Request): 提供当前应用实例的请求。

    Returns:
        MCPSettingsDao: 应用启动时创建的 MCP 配置访问对象。
    """
    return request.app.state.mcp_settings_dao


def get_settings_mutation_coordinator(request: Request) -> SettingsMutationCoordinator:
    """向 HTTP 请求提供与运行时共用的配置写入协调器。

    Args:
        request (Request): 提供当前应用实例的请求。

    Returns:
        SettingsMutationCoordinator: 应用共享的配置写入和执行受理协调器。
    """
    return request.app.state.settings_mutation_coordinator


def get_plugin_service(request: Request) -> PluginService:
    """向插件设置接口提供应用启动时完成发现的插件服务。

    Args:
        request (Request): 提供当前应用实例的请求。

    Returns:
        PluginService: 与运行时共用配置存储和写入协调器的插件服务。
    """
    return request.app.state.plugin_service


def get_mcp_connection_manager(request: Request) -> MCPConnectionManager:
    """向 HTTP 请求提供应用拥有并负责关闭的 MCP 连接管理器。

    Args:
        request (Request): 提供当前应用实例的请求。

    Returns:
        MCPConnectionManager: 与 Agent 共用连接状态的管理器。
    """
    return request.app.state.mcp_connection_manager


def get_task_supervisor(request: Request) -> TaskSupervisor:
    """向任务接口提供应用共享的后台任务管理器。

    Args:
        request (Request): 提供当前应用实例的请求。

    Returns:
        TaskSupervisor: 应用创建并负责关闭的任务管理器。
    """
    return request.app.state.task_supervisor


def get_runtime_scheduler(request: Request) -> AgentRuntimeScheduler:
    """向命令和查询接口提供应用共享的会话调度器。

    Args:
        request (Request): 提供当前应用实例的请求。

    Returns:
        AgentRuntimeScheduler: 应用创建并负责关闭的调度器。
    """
    return request.app.state.runtime_scheduler


def get_interaction_event_hub(request: Request) -> InteractionEventHub:
    """向事件订阅接口提供应用共享的输出分发对象。

    Args:
        request (Request): 提供当前应用实例的请求。

    Returns:
        InteractionEventHub: 保存本次应用输出序号和订阅缓冲的对象。
    """
    return request.app.state.interaction_event_hub




def get_chat_settings_service(request: Request) -> ChatSettingsService:
    """向设置接口提供与 Agent 创建共用的会话设置服务。

    Args:
        request (Request): 提供当前应用实例的请求。

    Returns:
        ChatSettingsService: 应用启动时装配的设置服务。
    """
    return request.app.state.chat_settings_service


def get_mcp_service(request: Request) -> MCPService:
    """向 MCP 接口和插件状态接口提供应用共享的 MCP 服务。

    Args:
        request (Request): 提供当前应用实例的请求。

    Returns:
        MCPService: 借用应用配置、连接和协调器的 MCP 服务。
    """
    return request.app.state.mcp_service


def get_tool_permission_service(request: Request) -> ToolPermissionService:
    """向工具权限接口提供应用共享的权限设置服务。

    Args:
        request (Request): 提供当前应用实例的请求。

    Returns:
        ToolPermissionService: 使用共享工具目录和配置写入协调器的权限服务。
    """
    return request.app.state.tool_permission_service


def get_chat_history_service(request: Request) -> ChatHistoryService:
    """向历史接口提供应用共享的聊天历史查询服务。

    Args:
        request (Request): 提供当前应用实例的请求。

    Returns:
        ChatHistoryService: 应用启动时装配的历史服务。
    """
    return request.app.state.chat_history_service
