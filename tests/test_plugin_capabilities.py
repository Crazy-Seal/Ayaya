"""从工具授权和 MCP 连接公共接口验证插件能力隔离。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.agent.plugins.tool_adapter import PluginToolAdapter
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.core.base import BaseTool, ToolResult
from app.mcp.mcp_connection_manager import MCPConnectionManager
from app.mcp.types import PluginMCPKey
from app.schemas.chat_settings import ChatSettings
from app.schemas.mcp import MCPServersConfig, StdioMCPConnectionConfig
from app.schemas.tool_permissions import ToolPolicy


class EchoTool(BaseTool):
    """验证适配后仍执行原始工具。"""

    name = "echo"
    description = "返回参数"
    parameters_schema = {"type": "object", "properties": {}}

    async def execute(self, args, context) -> ToolResult:
        """返回可观察结果，无外部副作用。"""
        return ToolResult.success("执行成功")


@pytest.mark.asyncio
async def test_plugin_tool_names_and_permissions_are_isolated() -> None:
    """同名工具分别使用插件权限，关闭插件后旧实例也被拒绝。"""
    settings = ChatSettings(session_id="sample", model_name="test", openai_api_key="test",
                            openai_base_url="http://localhost", temperature=0.7,
                            system_prompt="test", context_strategy={}, agent_plugins={
                                "first": {"enabled": True, "tool_permissions": {"local:echo": "allow"}},
                                "second": {"enabled": True, "tool_permissions": {"local:echo": "deny"}},
                            })
    first = PluginToolAdapter("first", EchoTool())
    second = PluginToolAdapter("second", EchoTool())
    authorization = ToolAuthorization(lambda _: settings)
    assert first.name != second.name
    assert first.identity.stable_key != second.identity.stable_key
    assert authorization.get_policy("sample", first.identity) == ToolPolicy.ALLOW
    assert not authorization.is_exposed("sample", second)
    assert (await first.execute({}, None)).text == "执行成功"
    settings.agent_plugins["first"].enabled = False
    assert not authorization.is_exposed("sample", first)


def test_names_preserve_namespace_structure() -> None:
    """名称中的分隔符与本地工具名不能伪装成另一个插件或 MCP。"""
    from app.agent.tools.core.naming import exposed_tool_name
    assert exposed_tool_name("plugin", "a__b", "local", "c") != exposed_tool_name("plugin", "a", "local", "b__local__c")
    assert exposed_tool_name("plugin", "a", "local", "mcp__s__t") != exposed_tool_name("plugin", "a", "mcp", "s", "t")


@pytest.mark.asyncio
async def test_plugin_mcp_connects_without_enabled_and_isolates_sessions() -> None:
    """插件连接没有独立开关，同名服务在不同会话分别连接并可单独关闭。"""
    opened = []
    closed = []

    class Session:
        """模拟外部协议响应，连接生命周期使用真实管理器。"""

        async def initialize(self):
            """返回初始化结果。"""
            return SimpleNamespace(instructions=None)

        async def list_tools(self):
            """返回空目录，测试只观察连接复用与关闭。"""
            return SimpleNamespace(tools=[])

    @asynccontextmanager
    async def factory(config):
        """记录连接打开和关闭。"""
        session = Session()
        opened.append(session)
        try:
            yield session
        finally:
            closed.append(session)

    config = StdioMCPConnectionConfig(id="game", name="游戏", command="test-server")
    assert "enabled" not in config.model_dump()
    manager = MCPConnectionManager(lambda: MCPServersConfig(), session_factory=factory,
                                   plugin_config_loader=lambda _: config)
    first = PluginMCPKey("one", "game_plugin", "game")
    second = PluginMCPKey("two", "game_plugin", "game")
    try:
        await manager.connect(first)
        await manager.connect(first)
        await manager.connect(second)
        assert len(opened) == 2
        await manager.disconnect_plugin("one", "game_plugin")
        assert len(closed) == 1
        assert manager.get_status(second)["status"] == "available"
    finally:
        await manager.close()
    assert len(closed) == 2
