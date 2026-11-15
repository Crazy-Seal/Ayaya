"""从会话准备入口验证插件 REPL 声明的解析和错误。"""

from pathlib import Path

import pytest

from app.agent.plugins.base import BasePlugin
from app.agent.plugins.registry import PluginRegistry
from app.crud.chat_settings_dao import ChatSettingsDao
from app.repl.repl_api import ReplCapability
from app.schemas.chat_settings import ChatSettings
from app.services.chat_settings_service import ChatSettingsService
from app.services.plugin_service import PluginConfigurationError


def test_preparation_collects_enabled_capabilities_once_without_importing_them(tmp_path, monkeypatch) -> None:
    """准备阶段调用声明一次，保留参数副本，并绑定真实插件来源。

    Args:
        tmp_path (Path): 隔离设置存储。
        monkeypatch (pytest.MonkeyPatch): 注册测试声明。
    """
    calls = []
    root = Path(__file__).parent / "fixtures" / "repl_capability"

    class Plugin(BasePlugin):
        """只提供 REPL 声明的测试插件。"""

        name = "repl_test"
        hooks = []

        async def execute(self, context) -> None:
            """本测试通过准备入口验证声明，无需执行钩子。"""

        @classmethod
        def resolve_repl_capabilities(cls, config):
            """使用当前参数声明能力，同时模拟解析器修改临时副本。

            Args:
                config (dict): 会话插件配置副本。

            Returns:
                tuple[ReplCapability, ...]: 尚未导入的模块声明。
            """
            calls.append(config["value"])
            config["value"] = 99
            return (ReplCapability(root, "ayaya_test_capability", "sample", config),)

    monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
    dao = ChatSettingsDao(tmp_path / "settings.yaml")
    service = ChatSettingsService(dao)
    settings = ChatSettings(session_id="s", model_name="test", openai_api_key="test",
                            openai_base_url="http://localhost", temperature=0.7, system_prompt="test", context_strategy={},
                            agent_plugins={Plugin.name: {"enabled": True, "config": {"value": 12}}})
    prepared = service.prepare(settings)
    assert calls == [12]
    assert prepared.settings.agent_plugins[Plugin.name].config == {"value": 12}
    capability = prepared.repl_capabilities[0]
    assert capability.plugin_name == Plugin.name
    assert capability.declaration.export_name == "sample"
    assert capability.declaration.config == {"value": 99}
    settings.agent_plugins[Plugin.name].enabled = False
    disabled = service.prepare(settings)
    assert disabled.repl_capabilities == ()
    assert calls == [12]


@pytest.mark.parametrize("case", ["missing_module", "invalid_export", "reserved_export", "bad_config", "duplicate_export"])
def test_preparation_rejects_invalid_or_conflicting_capabilities(tmp_path, monkeypatch, case) -> None:
    """无效模块、暴露名、配置或同名能力在执行前明确失败。

    Args:
        tmp_path (Path): 隔离配置目录。
        monkeypatch (pytest.MonkeyPatch): 安装测试插件。
        case (str): 本次要验证的声明错误。
    """
    root = Path(__file__).parent / "fixtures" / "repl_capability"

    class Plugin(BasePlugin):
        """按用例提供一个无效或冲突声明。"""

        name = "repl_invalid"
        hooks = []

        async def execute(self, context) -> None:
            """本用例不执行钩子。"""

        @classmethod
        def resolve_repl_capabilities(cls, config):
            """生成用例需要的错误声明。

            Args:
                config (dict): 当前插件参数。

            Returns:
                tuple[ReplCapability, ...]: 待检查的声明。
            """
            module = "missing" if case == "missing_module" else "ayaya_test_capability"
            export = "not-valid" if case == "invalid_export" else "display_image" if case == "reserved_export" else "sample"
            data = {"invalid": object()} if case == "bad_config" else {}
            declaration = ReplCapability(root, module, export, data)
            return (declaration, declaration) if case == "duplicate_export" else (declaration,)

    monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
    service = ChatSettingsService(ChatSettingsDao(tmp_path / "settings.yaml"))
    settings = ChatSettings(session_id="s", model_name="test", openai_api_key="test",
                            openai_base_url="http://localhost", temperature=0.7, system_prompt="test", context_strategy={},
                            agent_plugins={Plugin.name: {"enabled": True}})
    with pytest.raises(PluginConfigurationError) as failure:
        service.prepare(settings)
    assert failure.value.errors[0]["loc"] == ["agent_plugins", Plugin.name, "config"]
