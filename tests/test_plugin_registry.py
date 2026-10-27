from fastapi.testclient import TestClient

from main import app
from app.agent.plugins.registry import PluginRegistry


def test_plugin_registry_auto_discovers_plugins() -> None:
    """验证插件注册表自动发现内置插件。"""
    assert PluginRegistry.has("memory")
    assert not PluginRegistry.has("runtime")


def test_plugins_endpoint_exposes_metadata_from_config_model() -> None:
    """验证插件接口公开配置模型的字段和默认值。"""
    response = TestClient(app, base_url="http://127.0.0.1").get(
        "/plugins",
        headers={"Authorization": f"Bearer {'t' * 43}"},
    )

    assert response.status_code == 200
    payload = response.json()
    plugins = {item["name"]: item for item in payload["data"]["plugins"]}

    memory = plugins["memory"]
    assert "inherent" not in memory
    assert memory["default_config"]["enable_diary"] is True
    assert memory["default_config"]["long_term_memory_every_human_messages"] == 10


def test_plugin_service_returns_metadata_and_discovery_errors(monkeypatch) -> None:
    """插件服务提供完整目录结果，路由无需直接访问注册表。

    Args:
        monkeypatch (pytest.MonkeyPatch): 设置本次发现失败的说明。
    """
    from app.services.plugin_service import PluginService

    monkeypatch.setattr(PluginRegistry, "discovery_errors", ["插件包加载失败"])
    catalog = PluginService().list_plugins()
    assert catalog["errors"] == ["插件包加载失败"]
    assert any(plugin["name"] == "memory" for plugin in catalog["plugins"])
    response = TestClient(app, base_url="http://127.0.0.1").get(
        "/plugins", headers={"Authorization": f"Bearer {'t' * 43}"},
    )
    assert response.status_code == 200
    assert response.json()["data"] == catalog


def test_plugin_metadata_does_not_execute_data_dependent_defaults(monkeypatch) -> None:
    """需要用户参数才能生成的默认值，不影响其他插件的目录查询。"""
    from pydantic import BaseModel, Field
    from app.agent.plugins.base import BasePlugin

    class Config(BaseModel):
        """根据必填参数生成派生配置。"""
        endpoint: str
        derived: str = Field(default_factory=lambda data: data["endpoint"])

    class Plugin(BasePlugin):
        """声明动态默认值，不需要创建插件实例就能查询字段。"""
        name = "data_default"
        config_model = Config
        hooks = []

        async def execute(self, context) -> None:
            """本测试只使用配置目录。"""

    monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
    response = TestClient(app, base_url="http://127.0.0.1").get(
        "/plugins", headers={"Authorization": f"Bearer {'t' * 43}"})
    assert response.status_code == 200
    plugins = {item["name"]: item for item in response.json()["data"]["plugins"]}
    assert "memory" in plugins
    assert plugins[Plugin.name]["default_config"] == {}
