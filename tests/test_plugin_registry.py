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
    assert memory["inherent"] is True
    assert memory["default_config"]["enable_diary"] is True
    assert memory["default_config"]["long_term_memory_every_human_messages"] == 10
