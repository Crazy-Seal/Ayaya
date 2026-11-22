"""把应用提供的依赖装配为独立父子会话，保持运行对象构造显式。"""

import asyncio
from collections.abc import Awaitable, Callable

from app.agent.agent import Agent
from app.agent.plugins.runtime import PluginRuntime
from app.agent.runtime.agent_execution import AgentExecution
from app.agent.runtime.agent_factory import build_agent
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.runtime.parent_session import ParentSession
from app.agent.runtime.subagent_session import SubagentSession
from app.agent.runtime.requests import RuntimeRequest
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.state.manager import SessionStateManager
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.core.runtime import ToolRuntime
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.sqlite_factory import SqliteFactory
from app.interaction.interaction_event_hub import InteractionEventHub
from app.mcp.mcp_connection_manager import MCPConnectionManager
from app.repl.repl_service import ReplService
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.subagent_types import SubagentDefinition
from app.services.agent_identity_service import AgentIdentityService
from app.services.chat_settings_service import ChatSettingsService
from app.services.plugin_service import PluginService
from app.services.settings_mutation import SettingsMutationCoordinator
from app.services.subagent_service import SubagentService


def create_runtime_scheduler(supervisor: TaskSupervisor, runtime_queue: asyncio.Queue[RuntimeRequest], *,
    sqlite_factory: SqliteFactory, hub: InteractionEventHub, identity: AgentIdentityService, subagents: SubagentService, repl_service: ReplService,
    settings_dao: ChatSettingsDao, settings_service: ChatSettingsService, plugin_service: PluginService,
    mcp_manager: MCPConnectionManager, authorization: ToolAuthorization,
    mutation_coordinator: SettingsMutationCoordinator,
) -> AgentRuntimeScheduler:
    """用应用提供的共享依赖创建父子 Agent 工厂，并装配会话调度器。

    Args:
        supervisor (TaskSupervisor): 应用共享的任务提交、查询和有限等待对象。
        runtime_queue (asyncio.Queue[RuntimeRequest]): 应用创建、Scheduler 独占消费的共享请求队列。
        sqlite_factory (SqliteFactory): 应用装配提供的 SQLite 连接配置。
        hub (InteractionEventHub): 应用拥有的事件编号及订阅缓冲。
        identity (AgentIdentityService): 显式 Agent 命令使用的身份核验服务。
        subagents (SubagentService): 子定义、交互和最终报告的持久业务。
        repl_service (ReplService): 应用提供的唯一原生 Python 执行组件。
        settings_dao (ChatSettingsDao): 每次创建 Agent 时读取当前会话配置的存储。
        settings_service (ChatSettingsService): 整理本次 Agent 的插件参数和权限。
        plugin_service (PluginService): 提供插件 MCP 身份绑定回调。
        mcp_manager (MCPConnectionManager): 应用拥有的共享 MCP 连接管理器。
        authorization (ToolAuthorization): 根据最新会话配置判断工具权限的对象。
        mutation_coordinator (SettingsMutationCoordinator): 与 HTTP 设置修改共用的协调器。

    Returns:
        AgentRuntimeScheduler: 已装配父子会话工厂、尚未启动的调度器。
    """
    def create_agent(session_id: str, session_state_manager: SessionStateManager) -> Agent:
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
            plugin_runtime_factory=lambda plugin_name: PluginRuntime(
                plugin_name, session_id, _supervisor=supervisor, _runtime_queue=runtime_queue),
            bind_plugin_mcp=plugin_service.bind_mcp,
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

        Returns:
            Agent: 保持父权限归属的子 Agent。
        """
        prepared = settings_service.prepare(settings_dao.get_chat_settings(definition.session_id))
        return build_agent(
            prepared.settings, mcp_manager, authorization,
            lambda tool_name, run_id, tool_call_id: ToolRuntime(
                definition.session_id, run_id, tool_name, tool_call_id,
                _supervisor=supervisor, _runtime_queue=runtime_queue, subagent_id=definition.subagent_id),
            session_state_manager=session_state_manager,
            bind_plugin_mcp=plugin_service.bind_mcp,
            plugin_mcp={name: plugin.mcp_servers for name, plugin in prepared.plugins.items()
                        if (prepared.settings.agent_plugins or {})[name].enabled},
            subagent=definition, runtime_queue=runtime_queue, sqlite_factory=sqlite_factory,
            repl_service=repl_service,
        )

    def create_manager(context_id: str) -> SessionStateManager:
        """为父会话和启动整理创建拥有独立连接的持久状态对象。

        Args:
            context_id (str): 当前实际上下文。

        Returns:
            SessionStateManager: 由会话或启动整理操作负责关闭的状态对象。
        """
        return SessionStateManager(context_id, sqlite_factory)

    def create_child_manager(definition: SubagentDefinition) -> SessionStateManager:
        """为子上下文创建子表范围的持久状态对象。

        Args:
            definition (SubagentDefinition): 固定子定义及父归属。

        Returns:
            SessionStateManager: 由子会话负责关闭的状态对象。
        """
        return SessionStateManager(definition.subagent_id, sqlite_factory,
            storage_scope=AgentStorageScope.SUBAGENT, parent_session_id=definition.session_id)

    return assemble_runtime_scheduler(runtime_queue=runtime_queue, agent_factory=create_agent,
        subagent_factory=create_subagent, manager_factory=create_manager, child_manager_factory=create_child_manager,
        identity=identity, subagents=subagents, task_supervisor=supervisor, mutation_coordinator=mutation_coordinator, hub=hub,
        cleanup_session_connections=mcp_manager.disconnect_plugin)


def assemble_runtime_scheduler(*, runtime_queue: asyncio.Queue[RuntimeRequest],
    agent_factory: Callable[[str, SessionStateManager], Agent],
    subagent_factory: Callable[[SubagentDefinition, SessionStateManager], Agent],
    manager_factory: Callable[[str], SessionStateManager],
    child_manager_factory: Callable[[SubagentDefinition], SessionStateManager],
    identity: AgentIdentityService, subagents: SubagentService,
    task_supervisor: TaskSupervisor, mutation_coordinator: SettingsMutationCoordinator, hub: InteractionEventHub,
    cleanup_session_connections: Callable[[str], Awaitable[None]] | None = None) -> AgentRuntimeScheduler:
    """为应用或隔离实例装配父子会话工厂及 Scheduler。

    Args:
        runtime_queue (asyncio.Queue): 本应用唯一运行队列。
        agent_factory (Callable): 按当前父配置创建 Agent。
        subagent_factory (Callable): 按子定义创建 Agent。
        manager_factory (Callable): 创建父会话或启动整理的持久状态对象。
        child_manager_factory (Callable): 创建使用子历史表的状态对象。
        identity (AgentIdentityService): 显式命令身份规则。
        subagents (SubagentService): 子持久业务。
        task_supervisor (TaskSupervisor): 普通任务管理器。
        mutation_coordinator (SettingsMutationCoordinator): 应用短操作协调。
        hub (InteractionEventHub): 应用拥有的发布和缓冲对象。
        cleanup_session_connections (Callable | None): 父删除后释放其共享连接。

    Returns:
        AgentRuntimeScheduler: 依赖全部明确、尚未启动的调度对象。
    """
    def create_parent(context_id: str) -> ParentSession:
        """为此父上下文创建独立 Manager、执行状态和会话政策。

        Args:
            context_id (str): 父实际上下文编号。
        """
        manager = manager_factory(context_id)
        execution = AgentExecution(context_id, manager=manager,
            agent_factory=lambda state: agent_factory(context_id, state), events=runtime_queue)
        return ParentSession(context_id, manager=manager, execution=execution, events=runtime_queue)

    def create_child(definition: SubagentDefinition) -> SubagentSession:
        """为实际子编号创建独立运行对象，资源和状态不会与父上下文合并。

        Args:
            definition (SubagentDefinition): 已保存的子定义。
        """
        manager = child_manager_factory(definition)
        execution = AgentExecution(definition.subagent_id, manager=manager,
            agent_factory=lambda state: subagent_factory(definition, state), events=runtime_queue)
        return SubagentSession(definition, manager=manager, execution=execution, events=runtime_queue,
                               store=subagents, task_supervisor=task_supervisor)

    return AgentRuntimeScheduler(runtime_queue=runtime_queue, parent_factory=create_parent, child_factory=create_child,
        manager_factory=manager_factory, identity=identity, subagents=subagents, task_supervisor=task_supervisor,
        mutation_coordinator=mutation_coordinator, publish=hub.publish,
        watermark=hub.watermark, cleanup_session_connections=cleanup_session_connections)
