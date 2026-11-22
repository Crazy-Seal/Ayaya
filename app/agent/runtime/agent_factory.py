"""按会话设置和应用提供的依赖创建 Agent。"""

import asyncio
from typing import Any
from app.crud.sqlite_factory import SqliteFactory

from app.agent.agent import Agent, AgentConfig
from app.agent.runtime.requests import RuntimeRequest
from app.agent.runtime.subagent_requests import subagent_environment
from app.schemas.subagent_types import SubagentDefinition
from app.agent.plugins.runtime import PluginRuntimeFactory
from app.agent.state.manager import SessionStateManager
from app.schemas.chat_settings import ChatSettings
from app.mcp.mcp_connection_manager import MCPConnectionManager
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.core.runtime import ToolRuntimeFactory
from app.schemas.mcp import MCPConnectionConfig
from app.mcp.types import PluginMCPBinder
from app.repl.repl_service import ReplService
from app.repl.contracts import PreparedReplCapability


def build_agent(
    chat_settings: ChatSettings,
    mcp_connection_manager: MCPConnectionManager | None = None,
    tool_authorization: ToolAuthorization | None = None,
    tool_runtime_factory: ToolRuntimeFactory | None = None,
    session_state_manager: SessionStateManager | None = None,
    plugin_runtime_factory: PluginRuntimeFactory | None = None,
    bind_plugin_mcp: PluginMCPBinder | None = None,
    plugin_mcp: dict[str, tuple[MCPConnectionConfig, ...]] | None = None,
    subagent: SubagentDefinition | None = None,
    runtime_queue: asyncio.Queue[RuntimeRequest] | None = None,
    *, sqlite_factory: SqliteFactory,
    repl_service: ReplService,
    repl_capabilities: tuple[PreparedReplCapability, ...] = (),
) -> Agent:
    """按会话设置创建 Agent，并注入共享授权与任务启动端口。

    Args:
        chat_settings (ChatSettings): 会话模型和插件配置。
        mcp_connection_manager (MCPConnectionManager | None): 共享 MCP 连接管理器。
        tool_authorization (ToolAuthorization | None): 工具单次授权验证器。
        tool_runtime_factory (ToolRuntimeFactory | None): 按工具、轮次及调用编号创建运行时。
        session_state_manager (SessionStateManager | None): worker 拥有并借给本次 Agent 的业务对象。
        plugin_runtime_factory (PluginRuntimeFactory | None): 按插件名称创建所属会话 PluginRuntime 的注入工厂。
        bind_plugin_mcp (PluginMCPBinder | None): 连接成功后的应用身份保存入口。
        plugin_mcp (dict | None): 应用层根据会话参数准备的插件连接声明。
        subagent (SubagentDefinition | None): 子身份及固定定义；父配置仍作为授权和模型配置来源。
        runtime_queue (asyncio.Queue[RuntimeRequest] | None): 应用共享队列，用于管理工具和临时名单查询。

        sqlite_factory (SqliteFactory): 应用装配提供的 SQLite 连接配置。
        repl_service (ReplService): 应用持有的唯一 Python 执行组件。
        repl_capabilities (tuple[PreparedReplCapability, ...]): 已准备的启用插件能力。

    Returns:
        Agent: 按最新会话设置装配的 Agent，尚未开始模型轮次。
    """
    # 1. 应用层已经校验参数并准备连接声明，构造入口只组装运行对象。
    plugins: list[str | dict[str, Any]] = [
        {"name": name, "config": dict(settings.config)}
        for name, settings in (chat_settings.agent_plugins or {}).items()
        if settings.enabled
    ]

    config = AgentConfig(
        session_id=subagent.subagent_id if subagent else chat_settings.session_id,
        model_name=chat_settings.model_name,
        api_key=chat_settings.openai_api_key,
        base_url=chat_settings.openai_base_url,
        temperature=chat_settings.temperature,
        system_prompt=subagent.system_prompt if subagent else chat_settings.system_prompt,
        context_strategy=chat_settings.context_strategy,
        plugins=plugins,
        mcp=chat_settings.mcp,
        plugin_mcp=dict(plugin_mcp or {}),
    )
    async def environment() -> str:
        """只捕获共享队列和父 ID，不捕获 Scheduler 或绑定方法。"""
        assert runtime_queue is not None
        return await subagent_environment(runtime_queue, chat_settings.session_id)

    agent = Agent(
        config, sqlite_factory,
        mcp_connection_manager=mcp_connection_manager,
        chat_settings=chat_settings,
        tool_authorization=tool_authorization,
        tool_runtime_factory=tool_runtime_factory,
        session_state_manager=session_state_manager,
        plugin_runtime_factory=plugin_runtime_factory,
        bind_plugin_mcp=bind_plugin_mcp,
        permission_session_id=chat_settings.session_id,
        tool_whitelist=subagent.tool_whitelist if subagent else None,
        enable_plugin_hooks=subagent is None,
        environment_provider=environment if subagent is None and runtime_queue is not None else None,
    )
    # 2. 父子统一装配普通工具及其依赖，使用范围由工具声明和 ToolManager 筛选。
    from app.agent.tools.python import PythonTool
    from app.agent.tools.python_reset import PythonResetTool
    agent.register_tool(PythonTool(repl_service, repl_capabilities))
    agent.register_tool(PythonResetTool(repl_service))
    if tool_runtime_factory is not None:
        from app.agent.tools.create_subagent import CreateSubagentTool
        from app.agent.tools.interact_subagent import InteractSubagentTool
        from app.agent.tools.get_subagent_details import GetSubagentDetailsTool
        from app.agent.tools._subagent_support import MessageParentTool
        for tool in (CreateSubagentTool(agent.tool_manager.get_delegatable_tool_identities()),
                     InteractSubagentTool(), GetSubagentDetailsTool()):
            agent.register_tool(tool)
        # 3. 子 Agent 另外装配框架提供的父子通信入口。
        if subagent is not None:
            agent.tool_manager.register_framework_tool(MessageParentTool())
    return agent
