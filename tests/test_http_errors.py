"""通过实际 HTTP 请求检查业务错误和内部故障的区别。"""

from pathlib import Path
from types import SimpleNamespace
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import NoReturn
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.chat_history_dao import ChatHistoryDao
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.crud.sqlite_factory import SqliteFactory
from app.agent.tools.authorization.catalog import LocalToolCatalogAdapter, MCPToolCatalogAdapter, UnifiedToolCatalog
from app.errors import InvalidOperationError
from app.mcp.mcp_connection_manager import MCPConnectionManager, MCPConnectionError
from app.mcp.types import PluginMCPKey
from app.dependencies import (
    get_chat_settings_dao, get_chat_settings_service, get_chat_history_service,
    get_mcp_service, get_runtime_scheduler, get_tool_permission_service,
)
from app.http_errors import install_http_error_handlers
from app.routes.agent import router
from app.routes.chat_settings import router as settings_router
from app.routes.chat_history import router as history_router
from app.routes.mcp import router as mcp_router
from app.routes.tools import router as tools_router
from app.routes.plugins import router as plugins_router
from app.schemas.chat_settings import ChatSettings
from app.schemas.mcp import MCPServerConfig, MCPServersConfig, StdioMCPServerConfig
from app.schemas.runtime_types import TaskStatus
from app.schemas.task_types import TaskSnapshot
from app.services.channel_subscription_service import ChannelSubscriptionService
from app.services.chat_history_service import ChatHistoryService
from app.services.chat_settings_service import ChatSettingsService
from app.services.mcp_service import MCPService
from app.services.plugin_service import PluginService
from app.services.settings_mutation import SettingsMutationCoordinator
from app.services.tool_permission_service import ToolPermissionService


SETTINGS = {
    "session_id": "session", "model_name": "model", "openai_api_key": "test",
    "openai_base_url": "http://127.0.0.1:1/v1", "temperature": 0.1, "system_prompt": "提示词",
    "context_strategy": {"compression_trigger_tokens_k": 100},
}
SERVER = {"id": "demo", "name": "示例", "transport": "stdio", "command": "test", "enabled": False}


@pytest.fixture
def http_client(tmp_path: Path) -> Iterator[TestClient]:
    """装配真实配置服务和隔离存储，仅替代外部连接及运行时执行入口。

    Args:
        tmp_path (Path): 当前用例专用的 YAML 和 SQLite 目录。

    Yields:
        TestClient: 安装实际业务异常处理器的 HTTP 客户端。
    """
    config = tmp_path / "chat_settings.yaml"
    config.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(config)
    dao.add_chat_settings(ChatSettings.model_validate(SETTINGS))
    mcp_dao = MCPSettingsDao(tmp_path / "mcp.yaml")
    mcp_dao.save(MCPServersConfig(servers=[StdioMCPServerConfig.model_validate(SERVER)]))
    sqlite = SqliteFactory(tmp_path / "state.sqlite3")
    coordinator = SettingsMutationCoordinator()
    plugins = PluginService(dao, coordinator)

    @asynccontextmanager
    async def failed_connection(_config: MCPServerConfig) -> AsyncIterator:
        """模拟 MCP 初始化失败，验证配置保存与连接错误的实际处理。

        Args:
            _config (MCPServerConfig): 本次连接或测试的服务配置。

        Yields:
            SimpleNamespace: 初始化时报告连接故障的客户端替身。
        """
        yield SimpleNamespace(initialize=AsyncMock(side_effect=RuntimeError("连接失败")))

    manager = MCPConnectionManager(mcp_dao.load, session_factory=failed_connection,
                                   plugin_config_loader=plugins.connection_config)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        """测试退出时关闭共享连接管理器。

        Args:
            _application (FastAPI): 本用例装配的应用。

        Yields:
            None: 供客户端执行 HTTP 请求。
        """
        try:
            yield
        finally:
            await manager.close()

    application = FastAPI(lifespan=lifespan)
    install_http_error_handlers(application)
    for routes in (router, settings_router, history_router, mcp_router, tools_router, plugins_router):
        application.include_router(routes)
    application.state.chat_settings_dao = dao
    application.state.chat_settings_service = ChatSettingsService(dao, mcp_dao, manager, coordinator, plugins)
    application.state.mcp_service = MCPService(mcp_dao, dao, manager, plugins, coordinator)
    application.state.tool_permission_service = ToolPermissionService(dao, UnifiedToolCatalog(
        LocalToolCatalogAdapter(plugins.local_tools), MCPToolCatalogAdapter(mcp_dao, manager)), coordinator)
    application.state.chat_history_service = ChatHistoryService(ChatHistoryDao(sqlite))
    application.state.channel_subscriptions = ChannelSubscriptionService(sqlite)
    application.state.channel_subscriptions.register_channel("external", enabled=False)
    application.state.interaction_event_hub = Mock()
    application.state.runtime_scheduler = Mock()
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


def test_health_missing_session_returns_not_found(tmp_path: Path) -> None:
    """查询已删除会话的健康信息时返回 404。

    Args:
        tmp_path (Path): 当前用例的隔离设置目录。
    """
    config = tmp_path / "chat_settings.yaml"
    config.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(config)
    application = FastAPI()
    install_http_error_handlers(application)
    application.include_router(router)
    application.dependency_overrides[get_chat_settings_dao] = lambda: dao
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/health", params={"session_id": "deleted"})
    assert response.status_code == 404
    assert response.json() == {"detail": "'session_id not found: deleted'"}


@pytest.mark.parametrize("dependency,operation,method,url,payload", [
    (get_chat_settings_service, "add_chat_settings", "POST", "/chat_settings", SETTINGS),
    (get_chat_settings_service, "get_chat_settings_by_session", "GET", "/chat_settings/session", None),
    (get_mcp_service, "create_server", "POST", "/mcp/servers", SERVER),
    (get_mcp_service, "update_server", "PUT", "/mcp/servers/demo", SERVER),
    (get_runtime_scheduler, "get_subagent_details", "GET", "/agent/subagents/parent/child", None),
    (get_chat_history_service, "get_chat_history_last_n", "GET", "/chat_history_last_n/session", None),
    (get_tool_permission_service, "set_policies", "PUT", "/tool-permissions/session", {"tool_permissions": {}}),
])
@pytest.mark.parametrize("error_type", [KeyError, ValueError, LookupError])
def test_unexpected_errors_remain_server_failures(
    dependency: Callable, operation: str, method: str, url: str, payload: dict | None,
    error_type: type[Exception],
) -> None:
    """业务方法里的意外错误保持 500，避免误报为不存在或编号冲突。

    Args:
        dependency (Callable): 当前接口的依赖入口。
        operation (str): 模拟内部故障的业务方法。
        method (str): HTTP 请求方法。
        url (str): 请求地址。
        payload (dict | None): 合法请求内容。
        error_type (type[Exception]): 未归类为业务拒绝的内部异常。
    """
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        """模拟业务方法内部的程序或数据错误。

        Args:
            *_args (object): 当前请求的业务参数。
            **_kwargs (object): 当前请求的具名参数。

        Raises:
            Exception: 当前用例指定的内部错误。
        """
        raise error_type("内部数据错误")

    application = FastAPI()
    install_http_error_handlers(application)
    for routes in (router, settings_router, history_router, mcp_router, tools_router):
        application.include_router(routes)
    service = SimpleNamespace(**{operation: fail})
    application.dependency_overrides[dependency] = lambda: service
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.request(method, url, json=payload)
    assert response.status_code == 500


@pytest.mark.parametrize("method,url,payload,detail", [
    ("GET", "/chat_settings/deleted", None, "'session_id not found: deleted'"),
    ("PUT", "/chat_settings", {**SETTINGS, "session_id": "deleted"}, "'session_id not found: deleted'"),
    ("DELETE", "/chat_settings/deleted", None, "'session_id not found: deleted'"),
    ("GET", "/tool-permissions/deleted", None, "'session_id not found: deleted'"),
    ("PUT", "/tool-permissions/deleted", {"tool_permissions": {}}, "'session_id not found: deleted'"),
    ("GET", "/plugins/mcp-status/deleted", None, "'session_id not found: deleted'"),
    ("GET", "/mcp/servers/missing/tools", None, "'MCP server_id not found: missing'"),
    ("PUT", "/mcp/servers/missing", {**SERVER, "id": "missing"}, "'MCP server_id not found: missing'"),
    ("DELETE", "/mcp/servers/missing", None, "'MCP server_id not found: missing'"),
    ("POST", "/mcp/servers/missing/reconnect", None, "'MCP server_id not found: missing'"),
    ("GET", "/chat_history_last_n/session?through_message_id=missing", None, "历史查询终点不存在于当前会话"),
])
def test_missing_resources_return_404(
    http_client: TestClient, method: str, url: str, payload: dict | None, detail: str,
) -> None:
    """真实服务在明确找不到资源时返回 404，并保留原错误正文。

    Args:
        http_client (TestClient): 使用隔离配置和数据库的客户端。
        method (str): HTTP 方法。
        url (str): 查询或修改不存在资源的地址。
        payload (dict | None): 请求体。
        detail (str): 接口应保留的错误说明。
    """
    response = http_client.request(method, url, json=payload)
    assert response.status_code == 404
    assert response.json() == {"detail": detail}
    http_client.app.state.runtime_scheduler.schedule_parent_cleanup.assert_not_called()


@pytest.mark.parametrize("method,url,payload,status,detail", [
    ("POST", "/chat_settings", SETTINGS, 409, "session_id already exists: session"),
    ("POST", "/mcp/servers", SERVER, 409, "MCP server_id already exists: demo"),
    ("PUT", "/mcp/servers/demo", {**SERVER, "id": "changed"}, 409, "MCP server_id 不允许修改"),
    ("PUT", "/tool-permissions/session", {"tool_permissions": {"local:unknown": "allow"}}, 400, "包含未知工具权限: local:unknown"),
    ("PUT", "/agent/channels/session/desktop", {"enabled": False}, 400, "桌面渠道不能修改"),
    ("PUT", "/agent/channels/session/unknown", {"enabled": True}, 400, "交互渠道未注册或已关闭"),
    ("GET", "/agent/events/session?channel=unknown", None, 400, "交互渠道未注册或已关闭"),
    ("GET", "/agent/events/session?channel=external", None, 400, "该会话未订阅此渠道"),
    ("POST", "/mcp/servers/demo/reconnect", None, 409, "MCP Server 'demo' 已禁用"),
])
def test_rejected_operations_keep_saved_configuration(
    http_client: TestClient, method: str, url: str, payload: dict | None, status: int, detail: str,
) -> None:
    """拒绝编号冲突、无效权限和渠道操作，并保持原有配置。

    Args:
        http_client (TestClient): 使用真实服务的隔离客户端。
        method (str): HTTP 方法。
        url (str): 会被业务规则拒绝的地址。
        payload (dict | None): 请求体。
        status (int): 业务拒绝对应的 HTTP 状态。
        detail (str): 具体拒绝原因。
    """
    dao = http_client.app.state.chat_settings_dao
    mcp_dao = http_client.app.state.mcp_service.settings_dao
    before = (dao.config_file.read_bytes(), mcp_dao.config_file.read_bytes())
    response = http_client.request(method, url, json=payload)
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert (dao.config_file.read_bytes(), mcp_dao.config_file.read_bytes()) == before


@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_plugin_field_errors_return_422_without_saving(http_client: TestClient, method: str) -> None:
    """保存无效插件参数时返回字段路径，保留原会话配置。

    Args:
        http_client (TestClient): 使用实际插件校验的隔离客户端。
        method (str): 覆盖新增和修改配置。
    """
    dao = http_client.app.state.chat_settings_dao
    before = dao.config_file.read_bytes()
    response = http_client.request(method, "/chat_settings", json={**SETTINGS, "agent_plugins": {
        "memory": {"enabled": True, "config": {"long_term_memory_every_human_messages": 0}}}})
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert errors == [{"loc": ["agent_plugins", "memory", "config", "long_term_memory_every_human_messages"],
                       "msg": "Input should be greater than or equal to 1", "type": "greater_than_equal"}]
    assert dao.config_file.read_bytes() == before


def test_mcp_connection_failure_keeps_saved_server_and_reports_reconnect_error(http_client: TestClient) -> None:
    """新增和修改先保存配置；连接测试及显式重连失败返回 400。

    Args:
        http_client (TestClient): MCP 初始化被替身设为失败的隔离客户端。
    """
    created = http_client.post("/mcp/servers", json={**SERVER, "id": "new", "enabled": True})
    assert created.status_code == 200
    assert created.json()["data"]["runtime"]["status"] == "error"
    assert "连接失败" in created.json()["data"]["runtime"]["last_error"]
    updated = http_client.put("/mcp/servers/demo", json={**SERVER, "enabled": True})
    assert updated.status_code == 200
    assert updated.json()["data"]["runtime"]["status"] == "error"
    for url in ("/mcp/servers/test", "/mcp/servers/demo/reconnect"):
        response = http_client.post(url, json=SERVER if url.endswith("test") else None)
        assert response.status_code == 400
        assert "连接失败" in response.json()["detail"]
    saved = http_client.app.state.mcp_service.settings_dao.load().servers
    assert [(item.id, item.enabled) for item in saved] == [("demo", True), ("new", True)]


@pytest.mark.parametrize("owner,exists,status,detail", [
    ("other", True, 404, "任务不存在"), ("session", False, 404, "任务不存在"),
    ("session", True, 400, "此任务没有终端输出"),
])
def test_task_output_distinguishes_missing_tasks_and_no_terminal(
    http_client: TestClient, owner: str, exists: bool, status: int, detail: str,
) -> None:
    """先校验任务归属，再由共用读取方法报告缺少终端输出。

    Args:
        http_client (TestClient): 安装实际错误处理器的客户端。
        owner (str): 保存的任务归属。
        exists (bool): 是否存在任务记录。
        status (int): 预期状态码。
        detail (str): 预期错误正文。
    """
    record = TaskSnapshot("task", owner, None, TaskStatus.COMPLETED, None, None, None, "now", "now")
    http_client.app.state.task_supervisor = SimpleNamespace(get_task_snapshot=AsyncMock(return_value=record if exists else None))
    response = http_client.get("/tasks/session/task/output")
    assert response.status_code == status
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize("url", ["/chat_history/session", "/chat_history_last_n/session", "/plugins/mcp-status/session"])
def test_empty_queries_remain_successful(http_client: TestClient, url: str) -> None:
    """没有历史或插件时仍返回成功的空集合。

    Args:
        http_client (TestClient): 使用空历史和无插件会话的客户端。
        url (str): 本次查询地址。
    """
    response = http_client.get(url)
    assert response.status_code == 200
    assert response.json()["data"] in ([], {"plugins": {}})


def test_mcp_missing_session_preserves_runtime_error_text(http_client: TestClient) -> None:
    """会话删除后，运行时读取插件 MCP 参数仍取得原来的连接错误正文。

    Args:
        http_client (TestClient): 包含实际配置 DAO 和 MCP 管理器的客户端。
    """
    key = PluginMCPKey("deleted", "sample", "server")
    manager = http_client.app.state.mcp_service.connection_manager
    with pytest.raises(MCPConnectionError) as error:
        manager.get_server_config(key)
    assert str(error.value) == f"插件 MCP '{key}' 不可用: 'session_id not found: deleted'"


def test_openapi_declares_business_error_statuses(http_client: TestClient) -> None:
    """接口文档列出实际的业务拒绝、资源缺失和契约错误状态。

    Args:
        http_client (TestClient): 挂载所有受影响路由的客户端。
    """
    paths = http_client.app.openapi()["paths"]
    for path, method, statuses in [
        ("/chat_settings", "post", {409, 422}), ("/chat_settings", "put", {404, 422}),
        ("/health", "get", {404}), ("/tool-permissions/{session_id}", "get", {404}),
        ("/tool-permissions/{session_id}", "put", {400, 404}),
        ("/mcp/servers/{server_id}/reconnect", "post", {400, 404, 409}),
        ("/chat_history_last_n/{session_id}", "get", {404, 500}),
        ("/agent/subagents/{session_id}/{subagent_id}", "get", {404, 422}),
        ("/tasks/{session_id}/{task_id}/output", "get", {400, 404, 422}),
    ]:
        assert {str(status) for status in statuses} <= paths[path][method]["responses"].keys()
