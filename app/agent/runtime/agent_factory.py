"""按会话设置和应用提供的依赖创建 Agent。"""

from app.agent.agent import Agent, AgentConfig
from app.agent.plugins.runtime import PluginRuntimeFactory
from app.agent.state.manager import SessionStateManager
from app.agent.memory.manager import get_memory_manager
from app.schemas.chat_settings import (
    MEMORY_DEFAULT_CONFIG,
    ChatSettings,
)
from app.mcp.mcp_connection_manager import MCPConnectionManager
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.core.runtime import ToolRuntimeFactory
from app.schemas.desktop import DesktopOperations


def _plugin_config(chat_settings: ChatSettings, name: str, defaults: dict) -> dict:
    """从会话设置取得插件配置，在默认值副本上合并覆盖项。

    Args:
        chat_settings (ChatSettings): 当前会话的模型、角色及插件设置。
        name (str): 当前对象的名称。
        defaults (dict): 该插件的默认配置副本来源。

    Returns:
        dict: 该插件当前使用的独立配置字典。
    """
    settings = chat_settings.agent_plugins.get(name)
    config = dict(defaults)
    if settings:
        config.update(settings.config)
    return config


def build_agent(
    chat_settings: ChatSettings,
    mcp_connection_manager: MCPConnectionManager | None = None,
    tool_authorization: ToolAuthorization | None = None,
    tool_runtime_factory: ToolRuntimeFactory | None = None,
    desktop: DesktopOperations | None = None,
    session_state_manager: SessionStateManager | None = None,
    plugin_runtime_factory: PluginRuntimeFactory | None = None,
) -> Agent:
    """按会话设置创建 Agent，并注入共享授权与任务启动端口。

    Args:
        chat_settings (ChatSettings): 会话模型和插件配置。
        mcp_connection_manager (MCPConnectionManager | None): 共享 MCP 连接管理器。
        tool_authorization (ToolAuthorization | None): 工具单次授权验证器。
        tool_runtime_factory (ToolRuntimeFactory | None): 按工具、轮次及调用编号创建运行时。
        desktop (DesktopOperations | None): 应用共享桌面接口，各会话使用同一实例。
        session_state_manager (SessionStateManager | None): worker 拥有并借给本次 Agent 的业务对象。
        plugin_runtime_factory (PluginRuntimeFactory | None): 按插件名称和来源轮次创建 PluginRuntime 的注入工厂。

    Returns:
        Agent: 按最新会话设置装配的 Agent，尚未开始模型轮次。
    """
    plugins: list[dict] = [
        {
            "name": "memory",
            "config": _plugin_config(chat_settings, "memory", MEMORY_DEFAULT_CONFIG),
        },
    ]

    config = AgentConfig(
        session_id=chat_settings.session_id,
        model_name=chat_settings.model_name,
        api_key=chat_settings.openai_api_key,
        base_url=chat_settings.openai_base_url,
        temperature=chat_settings.temperature,
        system_prompt=chat_settings.system_prompt,
        context_strategy=chat_settings.context_strategy,
        plugins=plugins,
        skills=list(chat_settings.skills or []),
        mcp=chat_settings.mcp,
    )
    if mcp_connection_manager is None:
        return Agent(
            config,
            diary_coverage=get_memory_manager(chat_settings.session_id),
            chat_settings=chat_settings,
            tool_authorization=tool_authorization,
            tool_runtime_factory=tool_runtime_factory,
            desktop=desktop,
            session_state_manager=session_state_manager,
            plugin_runtime_factory=plugin_runtime_factory,
        )
    return Agent(
        config,
        mcp_connection_manager=mcp_connection_manager,
        diary_coverage=get_memory_manager(chat_settings.session_id),
        chat_settings=chat_settings,
        tool_authorization=tool_authorization,
        tool_runtime_factory=tool_runtime_factory,
        desktop=desktop,
        session_state_manager=session_state_manager,
        plugin_runtime_factory=plugin_runtime_factory,
    )
