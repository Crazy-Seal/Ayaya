"""验证配置保存后的缓存行为及运行时权限更新。"""

import asyncio
from collections.abc import Iterator
from functools import partial
from pathlib import Path

import pytest

import app.agent.memory.manager as memory_module
import app.dependencies as dependencies
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.authorization.models import (
    AuthorizationRequired, Granted, ToolDescriptor, ToolIdentity, ToolInvocation,
)
from app.agent.tools.core.base import ToolResult
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.mcp.identity import server_identity_fingerprint
from app.mcp.mcp_connection_manager import MCPConnectionError
from app.mcp.types import MCPToolDescriptor
from app.schemas.chat_settings import ChatSettings
from app.schemas.mcp import MCPModelSettings, MCPServersConfig, StdioMCPServerConfig
from app.schemas.tool_permissions import ToolPolicy
from app.services.chat_settings_service import ChatSettingsService
from app.services.mcp_service import MCPService
from app.services.settings_mutation import SettingsMutationCoordinator
from app.services.tool_permission_service import ToolPermissionService


def make_settings(session_id: str = "session") -> ChatSettings:
    """构造关闭全部记忆提取器的配置，真实记忆缓存不创建外部模型或向量客户端。

    Args:
        session_id (str): 测试配置所属的会话编号。

    Returns:
        ChatSettings: 可保存到隔离 YAML 的完整会话配置。
    """
    return ChatSettings(
        session_id=session_id, model_name="old-model", openai_api_key="test",
        openai_base_url="http://127.0.0.1:1/v1", temperature=0.1,
        system_prompt="测试", context_strategy={},
        agent_plugins={"memory": {"config": {
            "enable_diary": False, "enable_episodic": False, "enable_semantic": False,
        }}},
    )


@pytest.fixture
def chat_dao(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ChatSettingsDao]:
    """让真实记忆工厂读取本用例的配置文件，并在用例结束后释放工厂缓存引用。

    Args:
        tmp_path (Path): Pytest 创建的隔离目录。
        monkeypatch (pytest.MonkeyPatch): 将记忆工厂中的配置读取绑定到隔离 YAML。

    Yields:
        ChatSettingsDao: 已保存一个测试会话的真实 DAO。
    """
    config_file = tmp_path / "chat_settings.yaml"
    config_file.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(config_file)
    dao.add_chat_settings(make_settings())
    monkeypatch.setattr(memory_module, "ChatSettingsDao", partial(ChatSettingsDao, config_file))
    memory_module.get_memory_manager.cache_clear()
    try:
        yield dao
    finally:
        memory_module.get_memory_manager.cache_clear()


def test_dao_refreshes_own_settings_without_invalidating_memory(chat_dao: ChatSettingsDao) -> None:
    """直接使用 DAO 保存只刷新配置读取，不反向操作已缓存的记忆实例。

    Args:
        chat_dao (ChatSettingsDao): 本用例隔离的配置存储。
    """
    previous = memory_module.get_memory_manager("session")
    settings = chat_dao.get_chat_settings("session")
    chat_dao.update_chat_settings("session", settings.model_copy(update={"model_name": "new-model"}))

    assert chat_dao.get_chat_settings("session").model_name == "new-model"
    assert memory_module.get_memory_manager("session") is previous
    assert previous.chat_settings.model_name == "old-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "update", "delete"])
async def test_chat_settings_service_invalidates_memory_after_save(
    chat_dao: ChatSettingsDao, operation: str,
) -> None:
    """通过设置服务修改真实 YAML 后，下一次记忆装配读取新配置或拒绝已删除会话。

    Args:
        chat_dao (ChatSettingsDao): 本用例隔离的配置存储。
        operation (str): 覆盖新增、更新或删除设置入口。
    """
    previous = memory_module.get_memory_manager("session")
    service = ChatSettingsService(chat_dao)
    if operation == "add":
        await service.add_chat_settings(make_settings("new-session"))
        assert memory_module.get_memory_manager("new-session").session_id == "new-session"
    elif operation == "update":
        await service.update_chat_settings(make_settings().model_copy(update={"model_name": "new-model"}))
        assert memory_module.get_memory_manager("session").chat_settings.model_name == "new-model"
    else:
        await service.delete_chat_settings("session")
        with pytest.raises(KeyError):
            memory_module.get_memory_manager("session")
        return
    assert memory_module.get_memory_manager("session") is not previous


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "update", "delete"])
async def test_failed_settings_write_preserves_memory_cache(
    chat_dao: ChatSettingsDao, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    """配置文件保存失败时不清除仍然有效的记忆配置。

    Args:
        chat_dao (ChatSettingsDao): 本用例隔离的配置存储。
        monkeypatch (pytest.MonkeyPatch): 在真实 DAO 的文件写入边界注入失败。
        operation (str): 覆盖新增、更新或删除设置入口。
    """
    def fail_save(data: dict) -> None:
        """模拟文件写入失败，原配置文件保留。

        Args:
            data (dict): 本次试图保存的配置内容。

        Raises:
            OSError: 模拟磁盘写入失败。
        """
        raise OSError("保存失败")

    previous = memory_module.get_memory_manager("session")
    service = ChatSettingsService(chat_dao)
    monkeypatch.setattr(chat_dao, "_save_chat_settings_file", fail_save)
    with pytest.raises(OSError, match="保存失败"):
        if operation == "add":
            await service.add_chat_settings(make_settings("new-session"))
        elif operation == "update":
            await service.update_chat_settings(make_settings().model_copy(update={"model_name": "new-model"}))
        else:
            await service.delete_chat_settings("session")
    assert memory_module.get_memory_manager("session") is previous


class ConnectionProbe:
    """提供成功目录或连接故障，不启动外部 MCP 进程。

    Attributes:
        fail_connect (bool): 是否模拟服务连接失败。
        fail_disconnect (bool): 是否在会话引用已保存后模拟断连失败。
    """

    def __init__(self) -> None:
        """默认允许连接与断连，测试按场景开启故障。"""
        self.fail_connect = False
        self.fail_disconnect = False

    async def connect(self, server_id: str) -> list[MCPToolDescriptor]:
        """服务调用连接入口时返回固定目录或模拟失联。

        Args:
            server_id (str): 请求连接的服务编号。

        Returns:
            list[MCPToolDescriptor]: 当前测试服务公开的工具。

        Raises:
            MCPConnectionError: 测试要求模拟连接失败。
        """
        if self.fail_connect:
            raise MCPConnectionError("连接失败")
        return [MCPToolDescriptor("read", "读取", {"type": "object"})]

    async def reconnect(self, server_id: str) -> list[MCPToolDescriptor]:
        """重连使用相同的目录与故障设置。

        Args:
            server_id (str): 请求重新连接的服务编号。

        Returns:
            list[MCPToolDescriptor]: 当前测试服务公开的工具。
        """
        return await self.connect(server_id)

    async def disconnect(self, server_id: str) -> None:
        """模拟资源清理；可以在配置保存之后报告断连失败。

        Args:
            server_id (str): 请求断开的服务编号。

        Raises:
            MCPConnectionError: 测试要求模拟断连失败。
        """
        if self.fail_disconnect:
            raise MCPConnectionError("断连失败")

    def get_status(self, server_id: str) -> dict:
        """服务构造响应时提供连接状态，不执行外部调用。

        Args:
            server_id (str): 查询连接状态的服务编号。

        Returns:
            dict: 固定的可用状态。
        """
        return {"status": "available"}


@pytest.fixture
def permission_service(
    chat_dao: ChatSettingsDao, tmp_path: Path,
) -> tuple[ToolPermissionService, SettingsMutationCoordinator]:
    """使用生产工厂装配权限服务，让测试能够控制与其他配置写入共用的锁。

    Args:
        chat_dao (ChatSettingsDao): 本用例隔离的会话配置存储。
        tmp_path (Path): MCP 配置文件的隔离目录。

    Returns:
        tuple[ToolPermissionService, SettingsMutationCoordinator]: 真实权限服务及其共用写入锁。
    """
    mcp_dao = MCPSettingsDao(tmp_path / "mcp_servers.yaml")
    mcp_dao.save(MCPServersConfig())
    coordinator = SettingsMutationCoordinator()
    return dependencies.get_tool_permission_service(
        chat_dao, mcp_dao, ConnectionProbe(), coordinator,
    ), coordinator


@pytest.mark.asyncio
async def test_unknown_permission_rejects_whole_batch_without_invalidating_cache(
    chat_dao: ChatSettingsDao, permission_service: tuple[ToolPermissionService, SettingsMutationCoordinator],
) -> None:
    """一批权限包含未知工具时，不保存其中的有效项，也不使现有记忆缓存失效。

    Args:
        chat_dao (ChatSettingsDao): 本用例隔离的配置存储。
        permission_service (tuple): 真实权限服务及共用配置写入锁。
    """
    service, _ = permission_service
    previous = memory_module.get_memory_manager("session")
    before = chat_dao.config_file.read_bytes()
    with pytest.raises(ValueError, match="local:missing_tool"):
        await service.set_policies("session", {
            "local:read_file": ToolPolicy.DENY, "local:missing_tool": ToolPolicy.ALLOW,
        })
    assert chat_dao.config_file.read_bytes() == before
    assert memory_module.get_memory_manager("session") is previous


@pytest.mark.asyncio
async def test_permission_update_reads_configuration_after_acquiring_shared_lock(
    chat_dao: ChatSettingsDao, permission_service: tuple[ToolPermissionService, SettingsMutationCoordinator],
) -> None:
    """权限写入等其他配置修改释放锁后再读取，保留刚修改的模型及另一工具权限。

    Args:
        chat_dao (ChatSettingsDao): 本用例隔离的配置存储。
        permission_service (tuple): 真实权限服务及共用配置写入锁。
    """
    service, coordinator = permission_service
    pending = None
    try:
        # 1. 模拟另一配置入口持有共用锁；权限请求必须等待，不能提前读取旧配置。
        async with coordinator.lock:
            pending = asyncio.create_task(service.set_policies("session", {"local:read_file": ToolPolicy.DENY}))
            await asyncio.sleep(0)
            assert not pending.done()
            current = chat_dao.get_chat_settings("session")
            updated = dict(current.tool_permissions)
            updated["local:screenshot"] = ToolPolicy.ALLOW
            chat_dao.update_chat_settings("session", current.model_copy(update={
                "model_name": "new-model", "tool_permissions": updated,
            }))
        # 2. 获锁后合并权限，其他入口刚保存的字段必须保留。
        await asyncio.wait_for(pending, 2)
        saved = chat_dao.get_chat_settings("session")
        assert saved.model_name == "new-model"
        assert saved.tool_permissions["local:screenshot"] == ToolPolicy.ALLOW
        assert saved.tool_permissions["local:read_file"] == ToolPolicy.DENY
    finally:
        # 3. 断言失败也回收当前请求，不能留下占用锁或继续写配置的测试任务。
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_authorization_observes_permission_service_updates(
    chat_dao: ChatSettingsDao, permission_service: tuple[ToolPermissionService, SettingsMutationCoordinator],
) -> None:
    """权限服务保存后，已有运行时授权对象在下一次判断中使用新权限。

    Args:
        chat_dao (ChatSettingsDao): 服务与运行时共享的配置 DAO。
        permission_service (tuple): 真实权限服务及共用配置写入锁。
    """
    service, _ = permission_service
    authorization = ToolAuthorization(chat_dao.get_chat_settings)
    identity = ToolIdentity.local("read_file")
    invocation = ToolInvocation("session", "call", ToolDescriptor(
        identity, "read_file", "读取文件", "读取文件", {}, "test-contract",
    ), {})
    assert isinstance(authorization.authorize(invocation), AuthorizationRequired)
    await service.set_policy("session", identity, ToolPolicy.ALLOW)
    assert isinstance(authorization.authorize(invocation), Granted)
    await service.set_policy("session", identity, ToolPolicy.DENY)
    result = authorization.authorize(invocation)
    assert isinstance(result, ToolResult) and "不允许" in result.text


@pytest.mark.asyncio
async def test_response_query_failure_preserves_saved_permissions(
    chat_dao: ChatSettingsDao, permission_service: tuple[ToolPermissionService, SettingsMutationCoordinator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存后的目录响应查询失败时，权限修改仍已持久保存。

    Args:
        chat_dao (ChatSettingsDao): 本用例隔离的配置存储。
        permission_service (tuple): 真实权限服务及共用配置写入锁。
        monkeypatch (pytest.MonkeyPatch): 只让保存后的响应查询报告失败。
    """
    def fail_query(session_id: str) -> list:
        """模拟已保存配置后，设置页所需的最新目录无法查询。

        Args:
            session_id (str): 正在查询的会话编号。

        Raises:
            RuntimeError: 模拟目录响应查询失败。
        """
        raise RuntimeError("目录查询失败")

    service, _ = permission_service
    monkeypatch.setattr(service, "list_policies", fail_query)
    with pytest.raises(RuntimeError, match="目录查询失败"):
        await service.set_policies("session", {"local:read_file": ToolPolicy.DENY})
    assert chat_dao.get_chat_settings("session").tool_permissions["local:read_file"] == ToolPolicy.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "reconnect", "update", "disable", "disconnect_failure", "delete"])
async def test_mcp_settings_changes_preserve_memory_cache(
    chat_dao: ChatSettingsDao, tmp_path: Path, operation: str,
) -> None:
    """MCP 修改后会话权限立即更新，各会话已有记忆实例继续复用。

    Args:
        chat_dao (ChatSettingsDao): 本用例隔离的会话配置存储。
        tmp_path (Path): MCP 配置文件的隔离目录。
        operation (str): 创建、重连、身份变化、禁用、删除或保存后断连失败。
    """
    server = StdioMCPServerConfig(id="server", name="测试服务", command="old-command", enabled=True)
    settings = make_settings().model_copy(update={
        "tool_permissions": {"mcp:server:read": ToolPolicy.ALLOW},
        "mcp": MCPModelSettings(servers={"server": {
            "enabled": True, "identity_fingerprint": server_identity_fingerprint(server),
        }}),
    })
    chat_dao.update_chat_settings("session", settings)
    chat_dao.add_chat_settings(make_settings("other-session"))
    mcp_dao = MCPSettingsDao(tmp_path / "mcp_servers.yaml")
    mcp_dao.save(MCPServersConfig(servers=[] if operation == "create" else [server]))
    manager = ConnectionProbe()
    service = MCPService(mcp_dao, chat_dao, manager)
    assert chat_dao.get_chat_settings("session").tool_permissions["mcp:server:read"] == ToolPolicy.ALLOW
    previous = memory_module.get_memory_manager("session")
    other_previous = memory_module.get_memory_manager("other-session")

    if operation == "create":
        await service.create_server(server)
    elif operation == "reconnect":
        await service.reconnect("server")
    elif operation in {"update", "disable"}:
        manager.fail_connect = operation == "update"
        await service.update_server("server", server.model_copy(update={
            "command": "new-command", "enabled": operation != "disable",
        }))
    elif operation == "disconnect_failure":
        manager.fail_disconnect = True
        with pytest.raises(MCPConnectionError, match="断连失败"):
            await service.delete_server("server")
    else:
        await service.delete_server("server")

    assert memory_module.get_memory_manager("session") is previous
    assert memory_module.get_memory_manager("other-session") is other_previous
    saved = chat_dao.get_chat_settings("session")
    if operation in {"update", "disable"}:
        assert saved.tool_permissions["mcp:server:read"] == ToolPolicy.ASK
    elif operation in {"delete", "disconnect_failure"}:
        assert saved.mcp.servers == {}
        assert "mcp:server:read" not in saved.tool_permissions
    else:
        assert saved.mcp.servers["server"].identity_fingerprint == server_identity_fingerprint(server)
        assert saved.tool_permissions["mcp:server:read"] == ToolPolicy.ALLOW


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reconnect", "delete", "connect_failure"])
async def test_mcp_without_saved_chat_changes_preserves_memory_cache(
    chat_dao: ChatSettingsDao, tmp_path: Path, operation: str,
) -> None:
    """没有会话引用或连接失败而未修改会话配置时，不使原记忆实例失效。

    Args:
        chat_dao (ChatSettingsDao): 本用例隔离的会话配置存储。
        tmp_path (Path): MCP 配置文件的隔离目录。
        operation (str): 无引用时的重连、删除或连接失败。
    """
    server = StdioMCPServerConfig(id="server", name="测试服务", command="test", enabled=True)
    mcp_dao = MCPSettingsDao(tmp_path / "mcp_servers.yaml")
    mcp_dao.save(MCPServersConfig(servers=[server]))
    manager = ConnectionProbe()
    service = MCPService(mcp_dao, chat_dao, manager)
    previous = memory_module.get_memory_manager("session")
    if operation == "connect_failure":
        manager.fail_connect = True
        with pytest.raises(MCPConnectionError, match="连接失败"):
            await service.reconnect("server")
    elif operation == "reconnect":
        await service.reconnect("server")
    else:
        await service.delete_server("server")
    assert memory_module.get_memory_manager("session") is previous


@pytest.mark.asyncio
@pytest.mark.parametrize("write_fails", [False, True])
async def test_wired_tool_permissions_preserve_memory_cache(
    chat_dao: ChatSettingsDao, monkeypatch: pytest.MonkeyPatch, write_fails: bool,
) -> None:
    """通过生产装配的服务修改权限，校验新权限生效且保留原记忆实例。

    Args:
        chat_dao (ChatSettingsDao): 本用例隔离的会话配置存储。
        monkeypatch (pytest.MonkeyPatch): 替换生产装配依赖并按需注入写入失败。
        write_fails (bool): 是否拒绝配置文件写入。
    """
    def fail_save(data: dict) -> None:
        """在磁盘写入前失败，保证原配置仍然有效。

        Args:
            data (dict): 本次试图保存的配置内容。

        Raises:
            OSError: 模拟磁盘写入失败。
        """
        raise OSError("保存失败")

    mcp_dao = MCPSettingsDao(chat_dao.config_file.with_name("mcp_servers.yaml"))
    mcp_dao.save(MCPServersConfig())
    previous = memory_module.get_memory_manager("session")
    if write_fails:
        monkeypatch.setattr(chat_dao, "_save_chat_settings_file", fail_save)
    # 1. 使用生产工厂装配服务，读取和保存全部指向本用例的隔离配置。
    service = dependencies.get_tool_permission_service(
        chat_dao, mcp_dao, ConnectionProbe(), SettingsMutationCoordinator(),
    )
    if write_fails:
        with pytest.raises(OSError, match="保存失败"):
            await service.set_policies("session", {"local:read_file": ToolPolicy.DENY})
        assert memory_module.get_memory_manager("session") is previous
    else:
        await service.set_policies("session", {"local:read_file": ToolPolicy.DENY})
        assert chat_dao.get_chat_settings("session").tool_permissions["local:read_file"] == ToolPolicy.DENY
        assert ToolAuthorization(chat_dao.get_chat_settings).get_policy(
            "session", ToolIdentity.local("read_file"),
        ) == ToolPolicy.DENY
        assert memory_module.get_memory_manager("session") is previous
