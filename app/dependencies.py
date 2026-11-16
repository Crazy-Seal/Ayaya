from app.crud.sqlite_factory import SqliteFactory
from app.schemas.subagent_types import SubagentDefinition
from app.agent.agent import Agent
from fastapi import Depends, Request
import asyncio

from app.agent.plugins.runtime import PluginRuntime
from functools import lru_cache
from app.repl.repl_service import ReplService

from app.crud.chat_history_dao import ChatHistoryDao
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.agent.runtime.runtime_factory import assemble_runtime_scheduler
from app.schemas.agent_storage import AgentStorageScope
from app.services.agent_identity_service import AgentIdentityService
from app.services.subagent_service import SubagentService
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.interaction.interaction_event_hub import InteractionEventHub
from app.services.channel_subscription_service import ChannelSubscriptionService
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.tools.core.runtime import ToolRuntime
from app.agent.runtime.agent_factory import build_agent
from app.agent.state.manager import SessionStateManager
from app.mcp.mcp_connection_manager import MCPConnectionManager
from app.services.mcp_service import MCPService
from app.services.chat_settings_service import ChatSettingsService
from app.services.plugin_service import PluginService
from app.schemas.chat_settings import ChatSettings
from app.schemas.mcp import MCPConnectionConfig
from app.services.chat_history_service import ChatHistoryService
from app.services.tool_permission_service import ToolPermissionService
from app.services.settings_mutation import SettingsMutationCoordinator
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.authorization.catalog import (
    LocalToolCatalogAdapter,
    MCPToolCatalogAdapter,
    UnifiedToolCatalog,
)








def get_chat_history_dao(request: Request) -> ChatHistoryDao:
    """路由读取应用已经装配的历史 DAO。

    Args:
        request (Request): 提供本次应用实例的请求。
    """
    return request.app.state.chat_history_dao


@lru_cache(maxsize=1)
def get_chat_settings_dao() -> ChatSettingsDao:
    """向路由、工具授权和 Agent 工厂提供共享设置访问对象。

    Returns:
        ChatSettingsDao: 进程内缓存的 ChatSettingsDao。
    """
    return ChatSettingsDao()


@lru_cache(maxsize=1)
def get_mcp_settings_dao() -> MCPSettingsDao:
    """获取进程级 MCP 配置数据访问对象。

    Returns:
        MCPSettingsDao: 缓存的 MCP 配置数据访问对象。
    """
    return MCPSettingsDao()


@lru_cache(maxsize=1)
def get_settings_mutation_coordinator() -> SettingsMutationCoordinator:
    """获取跨配置写入协调器。

    Returns:
        SettingsMutationCoordinator: 进程级共享的写入协调器。
    """
    return SettingsMutationCoordinator()


@lru_cache(maxsize=1)
def get_plugin_service() -> PluginService:
    """提供设置与运行装配共用的插件声明和归属处理。

    Returns:
        PluginService: 使用应用配置存储及共用写入锁的插件模块。
    """
    return PluginService(get_chat_settings_dao(), get_settings_mutation_coordinator())


@lru_cache(maxsize=1)
def get_mcp_connection_manager() -> MCPConnectionManager:
    """构造并缓存进程级 MCP 连接管理器。

    Returns:
        MCPConnectionManager: 进程级 MCP 连接管理器。
    """
    dao = get_mcp_settings_dao()
    return MCPConnectionManager(dao.load, plugin_config_loader=get_plugin_service().connection_config)


@lru_cache(maxsize=1)
def get_tool_authorization() -> ToolAuthorization:
    """装配执行时使用的工具授权对象，每次判断只读取当前会话配置。

    Returns:
        ToolAuthorization: 多次 Agent 执行共享的权限与审批判断对象。
    """
    return ToolAuthorization(get_chat_settings_dao().get_chat_settings)


def get_task_supervisor(request: Request) -> TaskSupervisor:
    """从应用状态取得已创建的任务管理器，路由不负责创建或关闭它。

    Args:
        request (Request): 本次 HTTP 请求或绑定来源的插件请求。

    Returns:
        TaskSupervisor: 应用持有的同一个任务管理器。
    """
    return request.app.state.task_supervisor


def get_runtime_scheduler(request: Request) -> AgentRuntimeScheduler:
    """从应用状态取得本次运行的 Scheduler。

    Args:
        request (Request): 本次 HTTP 请求或绑定来源的插件请求。

    Returns:
        AgentRuntimeScheduler: 应用持有的同一个 Scheduler。
    """
    return request.app.state.runtime_scheduler


def get_interaction_event_hub(request: Request) -> InteractionEventHub:
    """从应用状态取得唯一事件分发对象。

    Args:
        request (Request): 本次 HTTP 请求或绑定来源的插件请求。

    Returns:
        InteractionEventHub: 应用持有的同一个事件分发对象。
    """
    return request.app.state.interaction_event_hub


def get_channel_subscription_service(request: Request) -> ChannelSubscriptionService:
    """路由取得应用持有的共享渠道设置。

    Args:
        request (Request): 提供本次应用实例的请求。
    """
    return request.app.state.channel_subscriptions


def create_runtime_scheduler(supervisor: TaskSupervisor, runtime_queue: asyncio.Queue, *,
    sqlite_factory: SqliteFactory, hub: InteractionEventHub, subscriptions: ChannelSubscriptionService,
    identity: AgentIdentityService, subagents: SubagentService, repl_service: ReplService) -> AgentRuntimeScheduler:
    """借用应用共享依赖装配独立父子会话，向既有 Hub 注入单向发布能力。

    Returns:
        AgentRuntimeScheduler: 共享执行模块。

    Args:
        supervisor (TaskSupervisor): 应用共享的任务提交、查询和有限等待对象。
        runtime_queue (asyncio.Queue): 应用创建、Scheduler 独占消费的共享请求队列。
        sqlite_factory (SqliteFactory): 应用装配提供的 SQLite 连接配置。
        hub (InteractionEventHub): 应用拥有的事件编号及订阅缓冲。
        subscriptions (ChannelSubscriptionService): 命令与输出共用的渠道设置。
        identity (AgentIdentityService): 显式 Agent 命令使用的身份核验服务。
        subagents (SubagentService): 子定义、交互和最终报告的持久业务。
        repl_service (ReplService): 应用提供的唯一原生 Python 执行组件。
    """
    settings_dao = get_chat_settings_dao()
    mcp_manager = get_mcp_connection_manager()
    authorization = get_tool_authorization()
    settings_service = get_chat_settings_service(
        chat_settings_dao=settings_dao,
        mcp_settings_dao=get_mcp_settings_dao(),
        connection_manager=mcp_manager,
        mutation_coordinator=get_settings_mutation_coordinator(),
    )
    def create_agent(session_id: str, session_state_manager: SessionStateManager):
        """真正执行时读取会话设置，经统一参数与权限整理后创建本次 Agent。

        Args:
            session_id (str): 当前执行会话。
            session_state_manager (SessionStateManager): worker 借给本次执行的业务对象。

        Returns:
            Agent: 注入共享工具依赖的本次执行对象。
        """
        prepared = settings_service.prepare(settings_dao.get_chat_settings(session_id))
        return build_agent(
            prepared.settings,
            mcp_manager,
            authorization,
            lambda tool_name, run_id, tool_call_id: ToolRuntime(
                session_id, run_id, tool_name, tool_call_id, _supervisor=supervisor, _runtime_queue=runtime_queue),
            session_state_manager=session_state_manager,
            plugin_runtime_factory=lambda plugin_name, run_id: PluginRuntime(
                plugin_name, session_id, run_id, _supervisor=supervisor, _runtime_queue=runtime_queue),
            bind_plugin_mcp=get_plugin_service().bind_mcp,
            plugin_mcp={name: plugin.mcp_servers for name, plugin in prepared.plugins.items()
                        if (prepared.settings.agent_plugins or {})[name].enabled},
            runtime_queue=runtime_queue, sqlite_factory=sqlite_factory,
            repl_service=repl_service, repl_capabilities=prepared.repl_capabilities,
        )

    def create_subagent(definition: SubagentDefinition, session_state_manager: SessionStateManager) -> Agent:
        """读取父最新配置创建子执行，权限与共享连接仍属于父会话。

        Args:
            definition (SubagentDefinition): 已保存的子身份、所属父会话及保留政策。
            session_state_manager (SessionStateManager): 所属会话借给本次执行的输入和历史对象。
        """
        prepared = settings_service.prepare(settings_dao.get_chat_settings(definition.session_id))
        return build_agent(
            prepared.settings, mcp_manager, authorization,
            lambda tool_name, run_id, tool_call_id: ToolRuntime(
                definition.session_id, run_id, tool_name, tool_call_id,
                _supervisor=supervisor, _runtime_queue=runtime_queue, subagent_id=definition.subagent_id),
            session_state_manager=session_state_manager,
            bind_plugin_mcp=get_plugin_service().bind_mcp,
            plugin_mcp={name: plugin.mcp_servers for name, plugin in prepared.plugins.items()
                        if (prepared.settings.agent_plugins or {})[name].enabled},
            subagent=definition, runtime_queue=runtime_queue, sqlite_factory=sqlite_factory,
            repl_service=repl_service,
        )

    def create_manager(context_id: str) -> SessionStateManager:
        """为父会话和启动整理创建拥有独立连接的持久状态对象。

        Args:
            context_id (str): 当前实际上下文。
        """
        return SessionStateManager(context_id, sqlite_factory)

    def create_child_manager(definition: SubagentDefinition) -> SessionStateManager:
        """为子上下文创建子表范围的持久状态对象。

        Args:
            definition (SubagentDefinition): 固定子定义及父归属。
        """
        return SessionStateManager(definition.subagent_id, sqlite_factory,
            storage_scope=AgentStorageScope.SUBAGENT, parent_session_id=definition.session_id)

    return assemble_runtime_scheduler(runtime_queue=runtime_queue, agent_factory=create_agent,
        subagent_factory=create_subagent, manager_factory=create_manager, child_manager_factory=create_child_manager,
        identity=identity, subagents=subagents, task_supervisor=supervisor, subscriptions=subscriptions,
        mutation_coordinator=get_settings_mutation_coordinator(), hub=hub,
        cleanup_session_connections=mcp_manager.disconnect_plugin)


@lru_cache(maxsize=1)
def get_chat_settings_service(
    chat_settings_dao: ChatSettingsDao = Depends(get_chat_settings_dao),
    mcp_settings_dao: MCPSettingsDao = Depends(get_mcp_settings_dao),
    connection_manager: MCPConnectionManager = Depends(get_mcp_connection_manager),
    mutation_coordinator: SettingsMutationCoordinator = Depends(get_settings_mutation_coordinator),
) -> ChatSettingsService:
    """FastAPI 解析依赖后组装并缓存设置服务，共用 MCP 连接和配置写入协调器。

    Args:
        chat_settings_dao (ChatSettingsDao): 按会话读取和保存模型配置的数据访问对象。
        mcp_settings_dao (MCPSettingsDao): 读取和保存 MCP 服务配置的数据访问对象。
        connection_manager (MCPConnectionManager): 进程共用的 MCP 连接及工具目录管理器。
        mutation_coordinator (SettingsMutationCoordinator): 保证关联配置修改按顺序完成的协调器。

    Returns:
        ChatSettingsService: 已装配依赖的会话设置服务。
    """
    return ChatSettingsService(
        chat_settings_dao=chat_settings_dao,
        mcp_settings_dao=mcp_settings_dao,
        connection_manager=connection_manager,
        mutation_coordinator=mutation_coordinator,
        plugin_service=get_plugin_service(),
    )


def get_mcp_service(
    mcp_settings_dao: MCPSettingsDao = Depends(get_mcp_settings_dao),
    chat_settings_dao: ChatSettingsDao = Depends(get_chat_settings_dao),
    connection_manager: MCPConnectionManager = Depends(get_mcp_connection_manager),
    mutation_coordinator: SettingsMutationCoordinator = Depends(get_settings_mutation_coordinator),
) -> MCPService:
    """组装 MCP 应用服务。

    Args:
        mcp_settings_dao (MCPSettingsDao): MCP Server 配置数据访问对象。
        chat_settings_dao (ChatSettingsDao): 模型配置数据访问对象。
        connection_manager (MCPConnectionManager): 进程级 MCP 连接管理器。
        mutation_coordinator (SettingsMutationCoordinator): 跨配置写入协调器。

    Returns:
        MCPService: 已完成依赖注入的 MCP 应用服务。
    """
    return MCPService(
        mcp_settings_dao,
        chat_settings_dao,
        connection_manager,
        get_plugin_service(),
        mutation_coordinator,
    )


def get_tool_permission_service(
    chat_settings_dao: ChatSettingsDao = Depends(get_chat_settings_dao),
    mcp_settings_dao: MCPSettingsDao = Depends(get_mcp_settings_dao),
    connection_manager: MCPConnectionManager = Depends(get_mcp_connection_manager),
    mutation_coordinator: SettingsMutationCoordinator = Depends(get_settings_mutation_coordinator),
) -> ToolPermissionService:
    """为权限设置页装配配置存储、工具目录及共用写入锁。

    Args:
        chat_settings_dao (ChatSettingsDao): 当前应用共享的会话配置存储。
        mcp_settings_dao (MCPSettingsDao): 工具目录查询所需的 MCP 服务配置。
        connection_manager (MCPConnectionManager): 提供当前服务身份和工具目录的共享连接管理器。
        mutation_coordinator (SettingsMutationCoordinator): 与会话设置、MCP 服务共用的配置写入锁。

    Returns:
        ToolPermissionService: 独立处理权限配置的应用服务，不持有执行时授权对象。
    """
    plugins = get_plugin_service()

    def plugin_servers(settings: ChatSettings, plugin_name: str) -> tuple[MCPConnectionConfig, ...]:
        """为权限目录准备指定启用插件的连接声明。

        Args:
            settings (ChatSettings): 权限页正在查询的会话设置。
            plugin_name (str): 当前目录项所属的插件名称。

        Returns:
            tuple[MCPConnectionConfig, ...]: 启用插件的服务声明，禁用时为空。
        """
        item = (settings.agent_plugins or {})[plugin_name]
        return plugins.prepare_plugin(plugin_name, item).mcp_servers if item.enabled else ()

    return ToolPermissionService(
        chat_settings_dao,
        UnifiedToolCatalog(
            LocalToolCatalogAdapter(plugins.local_tools),
            MCPToolCatalogAdapter(mcp_settings_dao, connection_manager, plugin_servers),
        ),
        mutation_coordinator,
    )


@lru_cache(maxsize=1)
def get_chat_history_service(chat_history_dao: ChatHistoryDao = Depends(get_chat_history_dao)) -> ChatHistoryService:
    """FastAPI 解析历史访问依赖后组装并缓存历史服务。

    Args:
        chat_history_dao (ChatHistoryDao): 把协议日志转换为界面聊天历史的数据访问对象。

    Returns:
        ChatHistoryService: 共享聊天历史查询服务。
    """
    return ChatHistoryService(chat_history_dao=chat_history_dao)
