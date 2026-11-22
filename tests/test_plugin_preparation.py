"""通过会话配置准备入口验证参数校验和 MCP 声明解析各执行一次。"""

import pytest
from pydantic import BaseModel, Field, field_validator

from app.agent.plugins.base import BasePlugin
from app.agent.plugins.registry import PluginRegistry
from app.crud.chat_settings_dao import ChatSettingsDao
from app.schemas.chat_settings import ChatSettings
from app.schemas.mcp import StdioMCPConnectionConfig
from app.services.chat_settings_service import ChatSettingsService
from app.services.plugin_service import PluginConfigurationError


def test_preparation_reuses_validated_config_and_mcp_declarations(tmp_path, monkeypatch, repl_service) -> None:
    """同次准备只校验和解析一次，返回完整装配数据，读取继续使用宽松规则。

    Args:
        repl_service (ReplService): 测试 fixture 提供的懒启动原生 Python 资源。
        tmp_path (Path): Pytest 提供的隔离配置目录。
        monkeypatch (pytest.MonkeyPatch): 注册本次使用的插件声明。
    """
    validations = []
    resolutions = []

    class Config(BaseModel):
        """声明需要整理的用户参数和由模型填入的默认值。"""

        command: str
        port: int = 7000
        tags: list[str] = Field(default_factory=list)

        @field_validator("command")
        @classmethod
        def normalize_command(cls, value: str) -> str:
            """记录校验次数，并去除用户输入前后的空白。

            Args:
                value (str): 当前插件的连接程序名称。

            Returns:
                str: 去除空白后的程序名称。
            """
            validations.append(value)
            return value.strip()

    class Plugin(BasePlugin):
        """由整理后的参数生成 MCP 声明，准备阶段无需创建实例。"""

        name = "prepared_plugin"
        config_model = Config
        hooks = []

        async def execute(self, context) -> None:
            """本用例只验证配置准备，无需执行插件钩子。"""

        @classmethod
        def resolve_mcp_servers(cls, config):
            """记录声明解析使用的参数，再返回服务连接配置。

            Args:
                config (dict): 已经通过配置模型校验的插件参数。

            Returns:
                tuple[MCPConnectionConfig, ...]: 本轮可用于装配的服务声明。
            """
            resolutions.append(dict(config))
            config["tags"].append("解析器临时添加")
            return (StdioMCPConnectionConfig(id="game", name="游戏服务", command=config["command"],
                                              args=[str(config["port"])]),)

    monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
    path = tmp_path / "settings.yaml"
    path.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(path)
    settings = ChatSettings(
        session_id="prepare", model_name="test", openai_api_key="test",
        openai_base_url="http://localhost", temperature=0.7, system_prompt="test",
        context_strategy={}, agent_plugins={"memory": {"enabled": False},
                                           Plugin.name: {"enabled": True, "config": {"command": " game-server "}}},
    )
    dao.add_chat_settings(settings)
    before = path.read_bytes()
    service = ChatSettingsService(dao)
    prepared = service.prepare(settings)
    assert validations == [" game-server "]
    assert resolutions == [{"command": "game-server", "port": 7000, "tags": ["解析器临时添加"]}]
    plugin = prepared.plugins[Plugin.name]
    assert plugin.name == Plugin.name
    assert plugin.config == {"command": "game-server", "port": 7000, "tags": []}
    assert plugin.mcp_servers[0].command == "game-server"
    assert plugin.mcp_servers[0].args == ["7000"]
    assert prepared.settings.agent_plugins[Plugin.name].config == plugin.config
    assert settings.agent_plugins[Plugin.name].config == {"command": " game-server "}
    validations.clear()
    resolutions.clear()
    displayed = service.get_chat_settings_by_session(settings.session_id)
    assert displayed.agent_plugins[Plugin.name].config == plugin.config
    assert validations == [" game-server "]
    assert resolutions == []
    invalid = settings.model_copy(deep=True)
    invalid.agent_plugins[Plugin.name].config = {"port": "待修正"}
    assert service.normalize(invalid, validate_enabled=False).agent_plugins[Plugin.name].config == {"port": "待修正"}
    assert path.read_bytes() == before

    # 1. 从生产装配入口再准备一次，验证交给 Agent 的参数与 MCP 声明来自同次结果。
    from types import SimpleNamespace
    import asyncio
    from app.agent.runtime import agent_factory, runtime_factory
    from app.agent.core.tool_manager import ToolManager
    from app.crud.sqlite_factory import SqliteFactory

    monkeypatch.setattr(runtime_factory, "assemble_runtime_scheduler", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(agent_factory, "Agent", lambda config, sqlite_factory, **kwargs: SimpleNamespace(
        config=config, tool_manager=ToolManager(), register_tool=lambda tool: None))
    validations.clear()
    resolutions.clear()
    scheduler = runtime_factory.create_runtime_scheduler(object(), asyncio.Queue(),
        sqlite_factory=SqliteFactory(tmp_path / "runtime.sqlite3"), hub=object(),
        identity=object(), subagents=object(), repl_service=repl_service,
        settings_dao=dao, settings_service=service, plugin_service=service.plugin_service,
        mcp_manager=SimpleNamespace(disconnect_plugin=lambda session_id: None),
        authorization=object(), mutation_coordinator=service.mutation_coordinator)
    agent_config = scheduler.agent_factory(settings.session_id, object()).config
    assert validations == [" game-server "]
    assert len(resolutions) == 1
    assert agent_config.plugins == [{"name": Plugin.name, "config": plugin.config}]
    assert agent_config.plugin_mcp[Plugin.name] == plugin.mcp_servers
    assert path.read_bytes() == before


@pytest.mark.parametrize("case", ["disabled_invalid_field", "duplicate_server", "invalid_connection"])
def test_preparation_preserves_field_errors_and_declaration_errors(tmp_path, monkeypatch, case: str) -> None:
    """保留参数错误的字段位置，将 MCP 声明错误归到整个插件配置。

    Args:
        tmp_path (Path): Pytest 提供的隔离配置目录。
        monkeypatch (pytest.MonkeyPatch): 注册只在当前测试中使用的插件。
        case (str): 参数错误、重复服务或非法连接声明的测试情形。
    """
    class Config(BaseModel):
        """声明必填参数与需要检查类型的端口。"""

        endpoint: str
        port: int = 7000

    class Plugin(BasePlugin):
        """按测试情形生成合法或非法的 MCP 声明。"""

        name = "invalid_preparation"
        config_model = Config
        hooks = []

        async def execute(self, context) -> None:
            """配置准备无需执行插件钩子。"""

        @classmethod
        def resolve_mcp_servers(cls, config):
            """构造服务声明，触发服务参数校验或重复编号检查。

            Args:
                config (dict): 已校验的插件参数。

            Returns:
                tuple[MCPConnectionConfig, ...]: 用于检查重复服务编号的声明。

            Raises:
                ValidationError: 生成的连接参数没有提供必填的 command。
            """
            if case == "invalid_connection":
                return (StdioMCPConnectionConfig(id="game", name="游戏服务"),)
            server = StdioMCPConnectionConfig(id="game", name="游戏服务", command=config["endpoint"])
            return (server, server)

    monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
    path = tmp_path / "settings.yaml"
    path.write_text("chat_models: []\n", encoding="utf-8")
    service = ChatSettingsService(ChatSettingsDao(path))
    settings = ChatSettings(
        session_id="error", model_name="test", openai_api_key="test", openai_base_url="http://localhost",
        temperature=0.7, system_prompt="test", context_strategy={},
        agent_plugins={"memory": {"enabled": False}, Plugin.name: {
            "enabled": case != "disabled_invalid_field",
            "config": {"port": "无效"} if case == "disabled_invalid_field" else {"endpoint": "test-server"},
        }},
    )
    with pytest.raises(PluginConfigurationError) as error:
        service.prepare(settings)
    expected_path = ["agent_plugins", Plugin.name, "config"]
    if case == "disabled_invalid_field":
        expected_path.append("port")
    assert len(error.value.errors) == 1
    assert error.value.errors[0]["loc"] == expected_path
