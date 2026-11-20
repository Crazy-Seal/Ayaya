from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_plugin_service
from app.agent.plugins.registry import PluginRegistry
from app.routes.plugins import router
from app.services.plugin_service import PluginService


@pytest.fixture
def plugin_client() -> Iterator[TestClient]:
    """为插件元数据路由提供独立服务，查询测试只使用声明目录。

    Yields:
        TestClient: 已注入插件服务的 HTTP 客户端。
    """
    application = FastAPI()
    application.include_router(router)
    service = PluginService()
    application.dependency_overrides[get_plugin_service] = lambda: service
    with TestClient(application, base_url="http://127.0.0.1") as client:
        yield client


def test_plugin_registry_auto_discovers_plugins() -> None:
    """验证插件注册表自动发现内置插件。"""
    assert PluginRegistry.has("memory")
    assert not PluginRegistry.has("runtime")


def test_plugins_endpoint_exposes_metadata_from_config_model(plugin_client: TestClient) -> None:
    """验证插件接口公开配置模型的字段和默认值。

    Args:
        plugin_client (TestClient): 已提供独立插件服务的 HTTP 客户端。
    """
    response = plugin_client.get("/plugins")

    assert response.status_code == 200
    payload = response.json()
    plugins = {item["name"]: item for item in payload["data"]["plugins"]}

    memory = plugins["memory"]
    assert "inherent" not in memory
    assert memory["default_config"]["enable_diary"] is True
    assert memory["default_config"]["long_term_memory_every_human_messages"] == 10


def test_plugin_service_returns_metadata_and_discovery_errors(plugin_client: TestClient, monkeypatch) -> None:
    """插件服务提供完整目录结果，路由无需直接访问注册表。

    Args:
        plugin_client (TestClient): 已提供独立插件服务的 HTTP 客户端。
        monkeypatch (pytest.MonkeyPatch): 设置本次发现失败的说明。
    """
    monkeypatch.setattr(PluginRegistry, "discovery_errors", ["插件包加载失败"])
    catalog = PluginService().list_plugins()
    assert catalog["errors"] == ["插件包加载失败"]
    assert any(plugin["name"] == "memory" for plugin in catalog["plugins"])
    response = plugin_client.get("/plugins")
    assert response.status_code == 200
    assert response.json()["data"] == catalog


def test_plugin_metadata_does_not_execute_data_dependent_defaults(plugin_client: TestClient, monkeypatch) -> None:
    """目录查询通过声明读取需要依赖用户参数才能生成的默认值。

    Args:
        plugin_client (TestClient): 已提供独立插件服务的 HTTP 客户端。
        monkeypatch (pytest.MonkeyPatch): 为本次目录查询注册带动态默认值的插件。
    """
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
    response = plugin_client.get("/plugins")
    assert response.status_code == 200
    plugins = {item["name"]: item for item in response.json()["data"]["plugins"]}
    assert "memory" in plugins
    assert plugins[Plugin.name]["default_config"] == {}
