"""验证应用装配使 HTTP 设置修改和运行时授权使用同一组依赖。"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.authorization.models import ToolIdentity
from app.dependencies import get_mcp_service
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.crud.sqlite_factory import SqliteFactory
from app.mcp.mcp_connection_manager import MCPConnectionManager
from app.schemas.chat_settings import ChatSettings
from app.schemas.tool_permissions import ToolPolicy
from app.services.chat_history_service import ChatHistoryService
from app.services.chat_settings_service import ChatSettingsService
from app.services.mcp_service import MCPService
from app.services.tool_permission_service import ToolPermissionService


@pytest.fixture
def application_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, dict[str, object]]]:
    """启动真实应用装配，隔离配置和数据库并记录交给运行时的依赖。

    Args:
        tmp_path (Path): 本用例的配置、SQLite 和工作目录。
        monkeypatch (pytest.MonkeyPatch): 将启动装配绑定到本用例的存储。

    Yields:
        tuple[TestClient, dict[str, object]]: HTTP 客户端及运行时接收的共享对象。
    """
    config_file = tmp_path / "chat_settings.yaml"
    config_file.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(config_file)
    for session_id in ("one", "two"):
        dao.add_chat_settings(ChatSettings(
            session_id=session_id, model_name=f"model-{session_id}",
            openai_api_key="test", openai_base_url="http://127.0.0.1:1/v1",
            temperature=0.1, system_prompt=f"角色-{session_id}", context_strategy={},
        ))
    monkeypatch.setenv("AYAYA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AYAYA_CHAT_SETTINGS_FILE", str(config_file))
    monkeypatch.setenv("AYAYA_MCP_SETTINGS_FILE", str(tmp_path / "mcp_servers.yaml"))
    monkeypatch.setattr(main, "SqliteFactory", lambda: SqliteFactory(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(main, "WORKDIR", tmp_path)
    monkeypatch.setattr(main, "detect_shell", lambda: None)
    runtime_dependencies: dict[str, object] = {}
    original_factory = main.create_runtime_scheduler

    def create_scheduler(supervisor, runtime_queue, **dependencies) -> AgentRuntimeScheduler:
        """使用真实工厂创建运行时，并记录应用提供的依赖供行为验证。

        Args:
            supervisor (TaskSupervisor): 本次应用创建的任务管理器。
            runtime_queue (asyncio.Queue): 本次应用的共享请求队列。
            **dependencies (object): 应用显式交给运行时工厂的共享依赖。

        Returns:
            AgentRuntimeScheduler: 使用真实持久化和会话工厂的调度器。
        """
        runtime_dependencies.update(dependencies)
        return original_factory(supervisor, runtime_queue, **dependencies)

    monkeypatch.setattr(main, "create_runtime_scheduler", create_scheduler)
    application = FastAPI(lifespan=main.lifespan)
    for router in (main.agent_router, main.chat_settings_router, main.tools_router,
                   main.memory_router, main.mcp_router, main.plugins_router):
        application.include_router(router)
    with TestClient(application, base_url="http://127.0.0.1") as client:
        yield client, runtime_dependencies


def test_http_permission_updates_reach_runtime_without_changing_other_session(application_client) -> None:
    """权限接口保存后，运行时授权立即读取新值，并保留其他会话的权限。

    Args:
        application_client (tuple): 已启动的真实 HTTP 应用及运行时依赖。
    """
    client, runtime_dependencies = application_client
    authorization = cast(ToolAuthorization, runtime_dependencies["authorization"])
    identity = ToolIdentity.local("read_file")
    assert authorization.get_policy("one", identity) == ToolPolicy.ASK

    saved = client.put("/tool-permissions/one", json={"tool_permissions": {"local:read_file": "deny"}})

    assert saved.status_code == 200
    assert authorization.get_policy("one", identity) == ToolPolicy.DENY
    assert authorization.get_policy("two", identity) == ToolPolicy.ASK
    policies = client.get("/tool-permissions/one")
    assert policies.status_code == 200
    assert next(item for item in policies.json()["data"]["tools"] if item["stable_key"] == "local:read_file")["policy"] == "deny"


def test_settings_updates_reach_runtime_and_preserve_previous_snapshot(application_client) -> None:
    """设置接口修改模型后，运行时准备使用新配置，之前生成的快照保持原值。

    Args:
        application_client (tuple): 已启动的真实 HTTP 应用及运行时依赖。
    """
    client, dependencies = application_client
    service = cast(ChatSettingsService, dependencies["settings_service"])
    previous = service.get_chat_settings_by_session("one")
    updated = previous.model_copy(update={"model_name": "updated-model", "system_prompt": "新角色"})

    saved = client.put("/chat_settings", json=updated.model_dump(mode="json"))

    assert saved.status_code == 200
    current = service.get_chat_settings_by_session("one")
    assert (current.model_name, current.system_prompt) == ("updated-model", "新角色")
    assert previous.model_name == "model-one"
    assert client.get("/health", params={"session_id": "one"}).json()["data"]["model"] == "updated-model"
    assert client.get("/health", params={"session_id": "two"}).json()["data"]["model"] == "model-two"


def test_requests_reuse_services_while_querying_each_session(application_client, monkeypatch) -> None:
    """连续查询多个会话时共用应用服务，各次调用仍返回当前会话的数据。

    Args:
        application_client (tuple): 已启动的真实 HTTP 应用及运行时依赖。
        monkeypatch (pytest.MonkeyPatch): 在服务业务入口记录实际处理请求的实例。
    """
    client, _ = application_client
    histories: list[ChatHistoryService] = []
    mcps: list[MCPService] = []
    permissions: list[ToolPermissionService] = []
    original_history = ChatHistoryService.get_chat_history_last_n
    original_mcp = MCPService.list_servers
    original_permissions = ToolPermissionService.list_policies

    async def query_history(self: ChatHistoryService, session_id: str, n: int = 100,
                            *, through_message_id: str | None = None):
        """记录本次历史请求的服务，并查询真实隔离数据库。

        Args:
            self (ChatHistoryService): HTTP 注入的历史服务。
            session_id (str): 当前查询的会话编号。
            n (int): 返回历史的条数上限。
            through_message_id (str | None): 历史查询结束消息编号。

        Returns:
            list: 当前会话的可见历史。
        """
        histories.append(self)
        return await original_history(self, session_id, n, through_message_id=through_message_id)

    def query_mcps(self: MCPService):
        """记录本次目录请求的服务，并读取真实 MCP 配置。

        Args:
            self (MCPService): HTTP 注入的 MCP 服务。

        Returns:
            list: 当前配置中的 MCP 服务及连接状态。
        """
        mcps.append(self)
        return original_mcp(self)

    def query_permissions(self: ToolPermissionService, session_id: str):
        """记录本次权限请求的服务，并查询指定会话的工具目录。

        Args:
            self (ToolPermissionService): HTTP 注入的权限服务。
            session_id (str): 当前查询的会话编号。

        Returns:
            list: 指定会话的当前工具权限。
        """
        permissions.append(self)
        return original_permissions(self, session_id)

    monkeypatch.setattr(ChatHistoryService, "get_chat_history_last_n", query_history)
    monkeypatch.setattr(MCPService, "list_servers", query_mcps)
    monkeypatch.setattr(ToolPermissionService, "list_policies", query_permissions)
    for session_id in ("one", "two", "one"):
        assert client.get(f"/chat_history_last_n/{session_id}").json()["data"] == []
        assert client.get("/mcp/servers").json()["data"] == []
        result = client.get(f"/tool-permissions/{session_id}")
        assert result.status_code == 200
        assert next(item for item in result.json()["data"]["tools"] if item["stable_key"] == "local:read_file")["policy"] == "ask"
    state = client.app.state
    assert len(histories) == len(mcps) == len(permissions) == 3
    assert all(service is state.chat_history_service for service in histories)
    assert all(service is state.mcp_service for service in mcps)
    assert all(service is state.tool_permission_service for service in permissions)


def test_shared_catalog_observes_saved_mcp_changes(application_client) -> None:
    """MCP 接口变更服务配置后，已存在的 MCP 和权限服务读取新的目录。

    Args:
        application_client (tuple): 已启动的真实 HTTP 应用及运行时依赖。
    """
    client, _ = application_client
    created = client.post("/mcp/servers", json={
        "id": "offline", "name": "离线服务", "transport": "stdio", "command": "unused", "enabled": False,
    })
    assert created.status_code == 200
    settings = client.get("/chat_settings/one").json()["data"]
    settings["mcp"] = {"servers": {"offline": {"enabled": True}}}
    settings["tool_permissions"]["mcp:offline:echo"] = "ask"
    assert client.put("/chat_settings", json=settings).status_code == 200

    servers = client.get("/mcp/servers").json()["data"]
    assert servers[0]["config"]["name"] == "离线服务"
    policies = client.get("/tool-permissions/one").json()["data"]["tools"]
    tool = next(item for item in policies if item["stable_key"] == "mcp:offline:echo")
    assert tool["server_name"] == "离线服务"
    assert tool["available"] is False
    assert all(item["stable_key"] != "mcp:offline:echo"
               for item in client.get("/tool-permissions/two").json()["data"]["tools"])


def test_http_service_override_keeps_application_service_available(application_client, tmp_path: Path) -> None:
    """路由可以覆盖 MCP 服务入口，移除覆盖后继续使用原应用服务。

    Args:
        application_client (tuple): 已启动的真实 HTTP 应用及运行时依赖。
        tmp_path (Path): 替换服务独立读取的配置目录。
    """
    client, _ = application_client
    application = client.app
    assert client.post("/mcp/servers", json={
        "id": "original", "name": "原服务", "transport": "stdio", "command": "unused", "enabled": False,
    }).status_code == 200
    state = application.state
    replacement = MCPService(
        MCPSettingsDao(tmp_path / "replacement.yaml"), state.chat_settings_dao,
        state.mcp_connection_manager, state.plugin_service, state.settings_mutation_coordinator,
    )

    def replacement_service() -> MCPService:
        """提供使用独立空配置存储的 MCP 服务，验证路由依赖替换。

        Returns:
            MCPService: 本次请求使用的替换服务。
        """
        return replacement

    application.dependency_overrides[get_mcp_service] = replacement_service
    try:
        assert client.get("/mcp/servers").json()["data"] == []
    finally:
        application.dependency_overrides.pop(get_mcp_service)
    assert client.get("/mcp/servers").json()["data"][0]["config"]["id"] == "original"


def test_shared_services_handle_concurrent_session_queries(application_client) -> None:
    """两个会话并发查询同一个设置服务时，各请求获得所属会话的模型配置。

    Args:
        application_client (tuple): 已启动的真实 HTTP 应用及运行时依赖。
    """
    client, _ = application_client

    def query_model(session_id: str) -> str:
        """通过真实 HTTP 依赖查询指定会话的配置。

        Args:
            session_id (str): 本请求所属的会话编号。

        Returns:
            str: 设置服务返回的模型名称。
        """
        response = client.get(f"/chat_settings/{session_id}")
        assert response.status_code == 200
        return response.json()["data"]["model_name"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(query_model, ("one", "two", "one", "two")))
    assert results == ["model-one", "model-two", "model-one", "model-two"]


def test_shared_catalog_observes_plugin_enablement(application_client) -> None:
    """已有目录对象在插件启用后显示可用工具，查询继续使用当前会话的设置。

    Args:
        application_client (tuple): 已启动的真实 HTTP 应用及运行时依赖。
    """
    client, _ = application_client
    settings = client.get("/chat_settings/one").json()["data"]
    settings["agent_plugins"]["memory"] = {"enabled": True, "config": {
        "enable_diary": False, "enable_episodic": False, "enable_semantic": False,
    }}
    assert client.put("/chat_settings", json=settings).status_code == 200
    for session_id, enabled in (("one", True), ("two", False)):
        tools = client.get(f"/tool-permissions/{session_id}").json()["data"]["tools"]
        memory_tools = [item for item in tools if item["plugin_name"] == "memory"]
        assert memory_tools
        assert all(item["available"] is enabled for item in memory_tools)


def test_service_construction_failure_closes_created_mcp_manager(tmp_path: Path, monkeypatch) -> None:
    """MCP 管理器创建后其他服务构造失败时，应用仍关闭已经创建的连接管理器。

    Args:
        tmp_path (Path): 本用例独立的应用存储目录。
        monkeypatch (pytest.MonkeyPatch): 替换服务构造并观察管理器关闭。
    """
    closed: list[MCPConnectionManager] = []

    class Manager(MCPConnectionManager):
        """保留真实连接管理行为，记录应用失败退出时的释放。"""

        async def close(self) -> None:
            """释放已有连接后记录本次关闭，验证应用只清理一次。"""
            await super().close()
            closed.append(self)

    def fail_service(**dependencies: object) -> ChatSettingsService:
        """在应用装配设置服务时抛出故障，验证之前创建的资源能够释放。

        Args:
            **dependencies (object): 应用提供给设置服务的共享依赖。

        Raises:
            RuntimeError: 模拟设置服务构造失败。
        """
        raise RuntimeError("设置服务构造失败")

    monkeypatch.setenv("AYAYA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AYAYA_CHAT_SETTINGS_FILE", str(tmp_path / "chat_settings.yaml"))
    monkeypatch.setenv("AYAYA_MCP_SETTINGS_FILE", str(tmp_path / "mcp_servers.yaml"))
    monkeypatch.setattr(main, "MCPConnectionManager", Manager)
    monkeypatch.setattr(main, "ChatSettingsService", fail_service)
    application = FastAPI(lifespan=main.lifespan)
    with pytest.raises(RuntimeError, match="设置服务构造失败"):
        with TestClient(application):
            pytest.fail("应用应当在启动完成前报告设置服务故障")
    assert closed == [application.state.mcp_connection_manager]
