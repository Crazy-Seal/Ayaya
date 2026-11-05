"""验证插件声明、会话保存、实际装配和连接清理组成完整流程。"""

from app.mcp.mcp_connection_manager import SessionFactory
from typing import cast

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp.types import Tool
from pydantic import BaseModel

from app.agent.plugins.base import BasePlugin
from app.agent.plugins.registry import PluginRegistry
from app.agent.runtime import agent_factory
from app.agent.tools.authorization.manager import ToolAuthorization
from app.crud.chat_settings_dao import ChatSettingsDao
from app.mcp.mcp_connection_manager import MCPConnectionManager
from app.mcp.types import PluginMCPKey
from app.schemas.chat_settings import ChatSettings
from app.schemas.mcp import MCPServersConfig, StdioMCPConnectionConfig
from app.services.chat_settings_service import ChatSettingsService
from app.services.plugin_service import PluginService
from app.services.settings_mutation import SettingsMutationCoordinator
from app.schemas.tool_permissions import ToolPolicy
from app.mcp.identity import server_identity_fingerprint
from app.mcp.types import MCPToolDescriptor


@pytest.mark.asyncio
async def test_plugin_mcp_binds_permissions_and_disconnects_on_parameter_change(tmp_path, monkeypatch) -> None:
    """真实工厂连接服务、绑定工具身份，修改参数后清除旧 allow 并关闭旧连接。"""
    class Config(BaseModel):
        """声明由插件参数提供的连接程序。"""
        command: str = "first-server"

    class Plugin(BasePlugin):
        """只提供 MCP，配置读取不创建实例。"""
        name = "lifecycle"
        config_model = Config
        hooks = []

        def __init__(self, **config):
            """接收已通过校验的参数。"""

        async def execute(self, context):
            """本用例无钩子业务。"""

        @classmethod
        def resolve_mcp_servers(cls, config):
            """使用当前会话参数生成服务声明。"""
            return (StdioMCPConnectionConfig(id="shared-name", name="插件服务", command=config["command"]),)

    monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
    path = tmp_path / "settings.yaml"
    path.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(path)
    coordinator = SettingsMutationCoordinator()
    plugins = PluginService(dao, coordinator)
    opened, closed = [], []

    class Session:
        """只模拟外部 MCP 响应，其余使用真实配置与运行模块。"""

        async def initialize(self):
            """完成连接握手。"""
            return SimpleNamespace(instructions=None)

        async def list_tools(self):
            """提供一个有权限身份的工具。"""
            return SimpleNamespace(tools=[Tool(name="read", description="读取", inputSchema={"type": "object"})])

    @asynccontextmanager
    async def session_factory(config):
        """记录连接的实际参数与资源释放。"""
        opened.append(config.command)
        try:
            yield Session()
        finally:
            closed.append(config.command)

    manager = MCPConnectionManager(lambda: MCPServersConfig(), session_factory=cast(SessionFactory, session_factory),
                                   plugin_config_loader=plugins.connection_config)
    service = ChatSettingsService(dao, connection_manager=manager, mutation_coordinator=coordinator, plugin_service=plugins)
    settings = ChatSettings(session_id="session", model_name="test", openai_api_key="test",
                            openai_base_url="http://localhost", temperature=0.7, system_prompt="test",
                            context_strategy={}, agent_plugins={"memory": {"enabled": False}, "lifecycle": {"enabled": True}})
    saved = await service.add_chat_settings(settings)
    authorization = ToolAuthorization(dao.get_chat_settings)
    prepared = service.prepare(saved)
    agent = agent_factory.build_agent(prepared.settings, manager, authorization, bind_plugin_mcp=plugins.bind_mcp,
                                      plugin_mcp={"lifecycle": prepared.plugins["lifecycle"].mcp_servers})
    try:
        await agent.initialize()
        assert opened == ["first-server"]
        remote = next(tool for tool in agent.tool_manager.get_all() if getattr(tool, "plugin_name", None) == "lifecycle")
        assert authorization.get_policy("session", remote.identity) == "ask"
        # 1. 模拟插件代码升级导致身份变化，暂时消失的历史工具也必须失去旧 allow。
        stale = dao.get_chat_settings("session").model_copy(deep=True)
        stale_plugin = stale.agent_plugins["lifecycle"]
        stale_plugin.mcp_identities["shared-name"] = server_identity_fingerprint(
            StdioMCPConnectionConfig(id="shared-name", name="旧服务", command="older-server"))
        stale_plugin.tool_permissions["mcp:shared-name:temporarily-missing"] = ToolPolicy.ALLOW
        dao.update_chat_settings("session", stale)
        key = PluginMCPKey("session", "lifecycle", "shared-name")
        server = plugins.connection_config(key)
        await plugins.bind_mcp(key, server, [])
        await plugins.bind_mcp(key, server, [MCPToolDescriptor("temporarily-missing", "恢复出现", {})])
        assert dao.get_chat_settings("session").agent_plugins["lifecycle"].tool_permissions["mcp:shared-name:temporarily-missing"] == ToolPolicy.ASK
        changed = service.get_chat_settings_by_session("session")
        changed.agent_plugins["lifecycle"].tool_permissions["mcp:shared-name:read"] = ToolPolicy.ALLOW
        await service.update_chat_settings(changed)
        assert authorization.get_policy("session", remote.identity) == "allow"
        changed.agent_plugins["lifecycle"].config["command"] = "second-server"
        await service.update_chat_settings(changed)
        current = dao.get_chat_settings("session").agent_plugins["lifecycle"]
        assert current.mcp_identities == {}
        assert current.tool_permissions["mcp:shared-name:read"] == "ask"
        assert closed == ["first-server"]
        assert manager.get_status(PluginMCPKey("session", "lifecycle", "shared-name"))["status"] == "disconnected"
    finally:
        await agent.close()
        await manager.close()
