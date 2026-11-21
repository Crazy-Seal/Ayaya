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


def test_plugin_metadata_preserves_nested_schema_and_python_default_serialization(
    plugin_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """插件配置中的嵌套 Schema、日期和路径继续使用原 JSON 表达。

    Args:
        plugin_client (TestClient): 已注入插件服务的 HTTP 客户端。
        monkeypatch (pytest.MonkeyPatch): 为本次查询注册独立插件。
    """
    from datetime import datetime, timezone
    from pathlib import Path

    from pydantic import BaseModel, Field
    from app.agent.plugins.base import BasePlugin

    class Options(BaseModel):
        """声明供编辑器展开的嵌套插件配置。"""

        active: bool = False
        labels: list[str | None] = Field(default_factory=lambda: ["a", None], serialization_alias="displayLabels")

    class Config(BaseModel):
        """包含原接口已经支持的 Python 配置默认值。"""

        directory: Path = Field(default_factory=lambda: Path("plugin-data"))
        created_at: datetime = datetime(2026, 11, 21, tzinfo=timezone.utc)
        choices: tuple[str, ...] = ("a", "b")
        options: Options = Field(default_factory=Options)

    class Plugin(BasePlugin):
        """只为元数据查询提供配置声明。"""

        name = "nested_defaults"
        config_model = Config
        hooks = []

        async def execute(self, context) -> None:
            """提供声明所需的钩子入口。

            Args:
                context (HookContext): 本次钩子执行的通用上下文。
            """

    monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
    response = plugin_client.get("/plugins")
    assert response.status_code == 200
    plugin = next(item for item in response.json()["data"]["plugins"] if item["name"] == Plugin.name)
    assert plugin["default_config"] == {"directory": "plugin-data", "created_at": "2026-11-21T00:00:00Z",
        "choices": ["a", "b"], "options": {"active": False, "displayLabels": ["a", None]}}
    assert plugin["config_schema"]["$defs"]["Options"]["properties"]["active"]["default"] is False
