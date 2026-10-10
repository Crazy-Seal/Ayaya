"""agent 工厂 - 把 ChatSettings 映射为 Agent。"""

from app.agent.agent import Agent, AgentConfig
from app.agent.memory.manager import get_memory_manager
from app.schemas.chat_settings import (
    MEMORY_DEFAULT_CONFIG,
    ChatSettings,
)
from app.services.mcp_connection_manager import MCPConnectionManager
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.base import TaskLauncher


def _plugin_config(chat_settings: ChatSettings, name: str, defaults: dict) -> dict:
    settings = chat_settings.agent_plugins.get(name)
    config = dict(defaults)
    if settings:
        config.update(settings.config)
    return config


def build_agent(
    chat_settings: ChatSettings,
    mcp_connection_manager: MCPConnectionManager | None = None,
    tool_authorization: ToolAuthorization | None = None,
    task_launcher: TaskLauncher | None = None,
) -> Agent:
    """按会话设置创建 Agent，并注入共享授权与任务启动端口。

    Args:
        chat_settings (ChatSettings): 会话模型和插件配置。
        mcp_connection_manager (MCPConnectionManager | None): 共享 MCP 连接管理器。
        tool_authorization (ToolAuthorization | None): 工具单次授权验证器。
        task_launcher (TaskLauncher | None): 工具使用的共享进程启动端口。

    Returns:
        Agent: 当前查询或执行结果。
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
            task_launcher=task_launcher,
        )
    return Agent(
        config,
        mcp_connection_manager=mcp_connection_manager,
        diary_coverage=get_memory_manager(chat_settings.session_id),
        chat_settings=chat_settings,
        tool_authorization=tool_authorization,
        task_launcher=task_launcher,
    )
