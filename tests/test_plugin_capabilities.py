"""从工具授权和 MCP 连接公共接口验证插件能力隔离。"""

from app.mcp.mcp_connection_manager import SessionFactory
from typing import cast

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


@pytest.mark.parametrize("first_plugin,first_tool,second_plugin,second_tool", [
    ("a__b", "c", "a", "b__c"), ("a", "echo", "a", "echo"),
])
def test_colliding_public_names_keep_the_original_registered_tool(
    first_plugin: str, first_tool: str, second_plugin: str, second_tool: str,
) -> None:
    """公开名称发生冲突时拒绝第二个工具，保留原工具及其权限身份。

    Args:
        first_plugin (str): 第一个工具所属插件。
        first_tool (str): 第一个原始工具名。
        second_plugin (str): 后续工具所属插件。
        second_tool (str): 后续原始工具名。
    """
    from app.agent.core.tool_manager import ToolManager

    original, incoming = EchoTool(), EchoTool()
    original.name, incoming.name = first_tool, second_tool
    first = PluginToolAdapter(first_plugin, original)
    second = PluginToolAdapter(second_plugin, incoming)
    manager = ToolManager()
    manager.register(first)
    with pytest.raises(ValueError, match="工具公开名称冲突"):
        manager.register(second)
    assert manager.get(first.name) is first
    assert manager.get_visible_tool_identities()[first.name] == first.identity


def test_plugin_local_and_mcp_name_collision_is_rejected() -> None:
    """本地工具名看似 MCP 名称时拒绝覆盖，避免公开名称指向另一权限身份。"""
    from app.agent.core.tool_manager import ToolManager
    from app.agent.mcp.adapter import MCPToolAdapter
    from app.mcp.types import MCPToolDescriptor

    local = EchoTool()
    local.name = "mcp__server__echo"
    original = PluginToolAdapter("sample", local)
    remote = MCPToolAdapter(session_id="parent", server_id="server", server_name="测试服务",
        descriptor=MCPToolDescriptor("echo", "远端操作", {}),
        connection_manager=SimpleNamespace(get_server_identity=lambda _: "f" * 64),
        authorization=ToolAuthorization(lambda _: None), plugin_name="sample")
    manager = ToolManager()
    manager.register(original)
    with pytest.raises(ValueError, match="工具公开名称冲突"):
        manager.register(remote)
    assert manager.get(original.name) is original


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
    manager = MCPConnectionManager(lambda: MCPServersConfig(), session_factory=cast(SessionFactory, factory),
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
