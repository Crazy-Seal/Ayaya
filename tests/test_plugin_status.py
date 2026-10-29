"""通过应用服务和 HTTP 接口验证插件 MCP 状态查询的分层与只读行为。"""

from app.mcp.mcp_connection_manager import SessionFactory
from typing import cast

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.plugins.base import BasePlugin
from app.agent.plugins.registry import PluginRegistry
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.dependencies import get_mcp_service
from app.mcp.mcp_connection_manager import MCPConnectionManager
from app.mcp.types import PluginMCPKey
from app.routes.plugins import router
from app.schemas.chat_settings import ChatSettings
from app.schemas.mcp import MCPServersConfig, StdioMCPConnectionConfig
from app.services.mcp_service import MCPService
from app.services.plugin_service import PluginService


@pytest.mark.asyncio
async def test_plugin_status_queries_are_read_only_and_keep_http_contract(tmp_path, monkeypatch) -> None:
    """查询按会话隔离状态，声明失败保留其他结果，HTTP 接口只包装服务返回值。

    Args:
        tmp_path (Path): 测试使用的隔离配置目录。
        monkeypatch (pytest.MonkeyPatch): 注册本次查询使用的插件声明。
    """
    class Plugin(BasePlugin):
        """提供一个 MCP 服务，查询阶段只使用类声明。"""

        name = "status_plugin"
        hooks = []
        mcp_servers = (StdioMCPConnectionConfig(id="shared", name="插件服务", command="test-server"),)

        def __init__(self, **config) -> None:
            """查询配置时创建实例属于错误，直接使测试失败。

            Args:
                **config (dict): 插件运行时才需要的配置。
            """
            raise AssertionError("状态查询不能创建插件实例")

        @classmethod
        def resolve_mcp_servers(cls, config):
            """整理顶层和嵌套参数，确认框架交给解析器的是独立副本。

            Args:
                config (dict): 仅供本次解析使用的插件参数。

            Returns:
                tuple[MCPConnectionConfig, ...]: 当前插件声明的测试服务。
            """
            config.pop("local_only", None)
            config.setdefault("resolved", True)
            config["nested"]["server_name"] = "解析时使用的名称"
            config["nested"]["labels"].append("解析时添加的标签")
            return cls.mcp_servers

        async def execute(self, context) -> None:
            """本用例只查询状态，无需执行钩子。

            Args:
                context (HookContext): 运行时调用钩子提供的上下文。
            """

    class BrokenPlugin(Plugin):
        """声明解析失败，用于检查局部错误不会中断其他插件查询。"""

        name = "broken_status_plugin"

        @classmethod
        def resolve_mcp_servers(cls, config):
            """模拟当前插件参数无法生成有效声明。

            Args:
                config (dict): 当前会话的插件参数。

            Raises:
                ValueError: 当前插件的声明解析失败。
            """
            raise ValueError("插件声明错误")

    monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
    monkeypatch.setitem(PluginRegistry._plugins, BrokenPlugin.name, BrokenPlugin)
    config_file = tmp_path / "chat_settings.yaml"
    config_file.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(config_file)
    for session_id in ("one", "two"):
        dao.add_chat_settings(ChatSettings(
            session_id=session_id, model_name="test", openai_api_key="test",
            openai_base_url="http://localhost", temperature=0.7, system_prompt="test",
            context_strategy={}, agent_plugins={
                "memory": {"enabled": False}, Plugin.name: {"enabled": True, "config": {
                    "local_only": True, "nested": {"server_name": "保存的名称", "labels": ["保存的标签"]},
                }},
                BrokenPlugin.name: {"enabled": True},
            },
        ))
    plugins = PluginService(dao)
    opened = []

    class Session:
        """只替代外部 MCP 服务响应，连接和状态查询使用真实管理器。"""

        async def initialize(self):
            """返回连接握手结果。"""
            return SimpleNamespace(instructions=None)

        async def list_tools(self):
            """返回空工具目录，状态查询只观察连接是否复用。"""
            return SimpleNamespace(tools=[])

    @asynccontextmanager
    async def session_factory(config):
        """记录实际建连次数，确认读取状态不会额外建连。

        Args:
            config (MCPConnectionConfig): 当前连接的服务参数。

        Yields:
            Session: 本次外部连接的测试响应对象。
        """
        opened.append(config.id)
        yield Session()

    manager = MCPConnectionManager(lambda: MCPServersConfig(), session_factory=cast(SessionFactory, session_factory),
                                   plugin_config_loader=plugins.connection_config)
    service = MCPService(MCPSettingsDao(tmp_path / "mcp_servers.yaml"), dao, manager, plugin_service=plugins)
    before = config_file.read_bytes()
    cached = dao.get_chat_settings("one")
    original = cached.model_dump(mode="json")
    try:
        statuses = service.get_plugin_mcp_status("one")
        assert cached.model_dump(mode="json") == original
        assert statuses[Plugin.name] == [{"id": "shared", "name": "插件服务", "runtime": {
            "status": "disconnected", "tool_count": 0, "last_error": None, "instructions": None,
        }}]
        assert statuses["memory"] == []
        assert statuses[BrokenPlugin.name][0]["runtime"]["last_error"] == "插件声明错误"
        assert statuses[BrokenPlugin.name][0]["runtime"]["status"] == "error"
        assert opened == []
        await manager.connect(PluginMCPKey("one", Plugin.name, "shared"))
        connected = service.get_plugin_mcp_status("one")
        assert connected[Plugin.name][0]["runtime"]["status"] == "available"
        assert service.get_plugin_mcp_status("two")[Plugin.name][0]["runtime"]["status"] == "disconnected"
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_mcp_service] = lambda: service
        with TestClient(app) as client:
            response = client.get("/plugins/mcp-status/one")
            assert response.status_code == 200
            assert response.json() == {"data": {"plugins": connected}, "msg": "success", "code": 200}
            assert client.get("/plugins/mcp-status/missing").status_code == 404
        assert opened == ["shared"]
        assert dao.get_chat_settings("one").model_dump(mode="json") == original
        assert config_file.read_bytes() == before
        assert not (tmp_path / "mcp_servers.yaml").exists()
    finally:
        await manager.close()
