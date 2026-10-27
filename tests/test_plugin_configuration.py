"""通过应用配置入口验证插件参数和权限归属。"""

import pytest
from pydantic import BaseModel

from app.agent.plugins.base import BasePlugin
from app.agent.plugins.registry import PluginRegistry
from app.schemas.chat_settings import ChatSettings
from app.services.plugin_service import PluginService, PluginConfigurationError
from app.services.chat_settings_service import ChatSettingsService
from app.crud.chat_settings_dao import ChatSettingsDao


def test_chat_settings_owns_local_tool_permission_defaults(tmp_path, monkeypatch) -> None:
    """设置服务统一补齐两类本地权限，插件参数整理保持权限原值，读取不写文件。

    Args:
        tmp_path (Path): Pytest 创建的隔离配置目录。
        monkeypatch (pytest.MonkeyPatch): 提供本次测试使用的工具和插件声明。
    """
    from app.agent.tools.core.base import BaseTool, ToolResult
    from app.agent.tools.core.registry import ToolRegistry
    from app.crud.chat_settings_dao import ChatSettingsDao
    from app.services.chat_settings_service import ChatSettingsService

    class ReadTool(BaseTool):
        """声明与独立工具同名的插件工具，用于检查权限保存位置。"""

        name = "read_file"
        description = "读取测试内容"
        parameters_schema = {"type": "object", "properties": {}}

        async def execute(self, args, context) -> ToolResult:
            """提供完整工具声明；配置整理只查询工具类。"""
            return ToolResult.success("读取完成")

    class InspectTool(ReadTool):
        """声明尚未保存权限的另一个插件工具。"""

        name = "inspect"

    class Config(BaseModel):
        """启用时要求地址，禁用时允许用户稍后填写。"""

        endpoint: str

    class Plugin(BasePlugin):
        """提供本地工具与必填参数，配置整理无需创建插件。"""

        name = "permission_helper"
        config_model = Config
        tools = (ReadTool, InspectTool)
        hooks = []

        async def execute(self, context) -> None:
            """本测试通过配置入口验证行为，无需执行插件钩子。"""

    class DisabledPlugin(Plugin):
        """提供禁用且参数尚未填写完整的插件。"""

        name = "disabled_helper"

    def local_tool_names() -> list[str]:
        """返回独立工具目录，其中一个工具尚未保存权限。"""
        return ["read_file", "exec_command"]

    monkeypatch.setattr(ToolRegistry, "list_tools", local_tool_names)
    monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
    monkeypatch.setitem(PluginRegistry._plugins, DisabledPlugin.name, DisabledPlugin)
    config_file = tmp_path / "settings.yaml"
    config_file.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(config_file)
    settings = ChatSettings(
        session_id="permissions", model_name="test", openai_api_key="test",
        openai_base_url="http://localhost", temperature=0.7, system_prompt="test",
        context_strategy={}, tool_permissions={"local:read_file": "allow"},
        agent_plugins={
            Plugin.name: {"enabled": True, "config": {"endpoint": "local-game"},
                          "tool_permissions": {"local:read_file": "deny"}},
            DisabledPlugin.name: {"enabled": False, "config": {}},
        },
    )
    dao.add_chat_settings(settings)
    before = config_file.read_bytes()
    plugins = PluginService()
    plugin_only = plugins.prepare_plugin(Plugin.name, settings.agent_plugins[Plugin.name])
    assert plugin_only.config == {"endpoint": "local-game"}
    assert settings.agent_plugins[Plugin.name].tool_permissions == {"local:read_file": "deny"}
    service = ChatSettingsService(dao, plugin_service=plugins)
    normalized = service.normalize(settings)
    assert normalized.tool_permissions == {"local:read_file": "allow", "local:exec_command": "ask"}
    assert normalized.agent_plugins[Plugin.name].tool_permissions == {
        "local:read_file": "deny", "local:inspect": "ask",
    }
    assert normalized.agent_plugins[DisabledPlugin.name].tool_permissions == {
        "local:read_file": "ask", "local:inspect": "ask",
    }
    assert normalized.agent_plugins[DisabledPlugin.name].config == {}
    assert settings.tool_permissions == {"local:read_file": "allow"}
    assert settings.agent_plugins[DisabledPlugin.name].tool_permissions == {}
    assert service.get_chat_settings_by_session(settings.session_id) == normalized
    assert service.normalize(normalized) == normalized
    assert config_file.read_bytes() == before


def test_required_parameters_only_block_enabled_plugins(tmp_path, monkeypatch) -> None:
    """禁用插件允许待填写配置，启用后报告具体字段路径。

    Args:
        tmp_path (Path): Pytest 提供的隔离配置目录。
        monkeypatch (pytest.MonkeyPatch): 注册需要填写参数的测试插件。
    """
    class Config(BaseModel):
        """测试无默认值的用户参数。"""
        endpoint: str

    class Plugin(BasePlugin):
        """提供需要用户配置的插件。"""
        name = "required_plugin"
        config_model = Config
        hooks = []

        async def execute(self, context) -> None:
            """本测试只检查配置，无需执行钩子。"""

    monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
    settings = ChatSettings(session_id="sample", model_name="test", openai_api_key="test",
                            openai_base_url="http://localhost", temperature=0.7,
                            system_prompt="test", context_strategy={},
                            agent_plugins={"required_plugin": {"enabled": False}})
    path = tmp_path / "settings.yaml"
    path.write_text("chat_models: []\n", encoding="utf-8")
    service = ChatSettingsService(ChatSettingsDao(path))
    assert service.normalize(settings).agent_plugins[Plugin.name].config == {}
    settings.agent_plugins[Plugin.name].enabled = True
    with pytest.raises(PluginConfigurationError) as error:
        service.normalize(settings)
    assert error.value.errors[0]["loc"] == ["agent_plugins", Plugin.name, "config", "endpoint"]


def test_nested_plugin_config_can_be_hashed() -> None:
    """嵌套配置可用于缓存键，权限变化也会改变键。"""
    settings = ChatSettings(session_id="sample", model_name="test", openai_api_key="test",
                            openai_base_url="http://localhost", temperature=0.7,
                            system_prompt="test", context_strategy={},
                            agent_plugins={"example": {"enabled": False, "config": {"nested": [1, {"a": True}]}}})
    assert hash(settings) == hash(settings.model_copy(deep=True))


@pytest.mark.asyncio
async def test_removed_plugin_cleanup_requires_successful_discovery(tmp_path, monkeypatch) -> None:
    """完整发现清理移除插件，导入失败则保留全部设置供修复。"""
    from app.crud.chat_settings_dao import ChatSettingsDao
    monkeypatch.setattr(PluginRegistry, "discovery_errors", [])
    config = tmp_path / "settings.yaml"
    config.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(config)
    original = ChatSettings(session_id="sample", model_name="test", openai_api_key="test",
                            openai_base_url="http://localhost", temperature=0.7,
                            system_prompt="test", context_strategy={},
                            agent_plugins={"removed": {"enabled": False, "tool_permissions": {"local:old": "allow"}}})
    dao.add_chat_settings(original)
    root = tmp_path / "plugins"
    broken = root / "broken"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("raise RuntimeError('加载失败')\n", encoding="utf-8")
    service = PluginService(dao)
    assert await service.discover(root)
    assert "removed" in dao.get_chat_settings("sample").agent_plugins
    (broken / "__init__.py").unlink()
    broken.rmdir()
    assert await service.discover(root) == []
    assert "removed" not in dao.get_chat_settings("sample").agent_plugins
