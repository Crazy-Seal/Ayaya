"""通过受控的连接退出等待，验证配置变更和插件连接清理可以安全并发。"""

from app.mcp.mcp_connection_manager import SessionFactory
from typing import cast

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp.types import Tool

from app.mcp.mcp_connection_manager import MCPConnectionError, MCPConnectionManager
from app.mcp.types import MCPToolDescriptor, PluginMCPKey
from app.schemas.mcp import MCPServersConfig, StdioMCPConnectionConfig


class ControlledServer:
    """记录实际建连和关闭，并允许测试暂停旧连接的退出过程。

    Attributes:
        config (StdioMCPConnectionConfig | None): 应用当前提供的参数；为空表示插件服务已不可用。
        opened (list[str]): 已经建立连接的程序名称。
        closed (list[str]): 已经完成资源清理的程序名称。
        closing (asyncio.Event): 旧连接开始退出时通知测试安排并发请求。
        finish_close (asyncio.Event): 测试允许旧连接结束退出的信号。
        config_read (asyncio.Event): 连接请求已经读取参数、即将取得生命周期锁的信号。
    """

    def __init__(self) -> None:
        """创建完全由测试控制的参数、连接记录和异步等待信号。"""
        self.config: StdioMCPConnectionConfig | None = StdioMCPConnectionConfig(
            id="game", name="测试服务", command="first",
        )
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.closing = asyncio.Event()
        self.finish_close = asyncio.Event()
        self.config_read = asyncio.Event()

    def load_config(self, key: PluginMCPKey) -> StdioMCPConnectionConfig:
        """提供当前应用参数，通知测试连接请求已经开始。

        Args:
            key (PluginMCPKey): 管理器正在连接的插件服务。

        Returns:
            StdioMCPConnectionConfig: 当前可用的连接参数。

        Raises:
            KeyError: 插件已禁用、服务已移除或所属会话已删除。
        """
        self.config_read.set()
        if self.config is None:
            raise KeyError(f"插件服务已不可用: {key}")
        return self.config

    @asynccontextmanager
    async def session_factory(self, config: StdioMCPConnectionConfig) -> AsyncIterator["FakeSession"]:
        """模拟外部连接，旧连接退出时等待测试放行。

        Args:
            config (StdioMCPConnectionConfig): 管理器本次实际使用的连接参数。

        Yields:
            FakeSession: 提供握手与工具目录的外部服务替身。
        """
        self.opened.append(config.command)
        try:
            yield FakeSession()
        finally:
            if config.command == "first":
                self.closing.set()
                await self.finish_close.wait()
            self.closed.append(config.command)


class FakeSession:
    """只替代外部服务响应，连接记录和锁均使用真实管理器。"""

    async def initialize(self) -> SimpleNamespace:
        """完成模拟握手。

        Returns:
            SimpleNamespace: 不包含额外说明的初始化结果。
        """
        return SimpleNamespace(instructions=None)

    async def list_tools(self) -> SimpleNamespace:
        """返回一个工具，供测试通过公开查询确认新连接仍受管理。

        Returns:
            SimpleNamespace: 含读取工具的服务目录。
        """
        return SimpleNamespace(tools=[Tool(name="read", inputSchema={"type": "object"})])


async def queue_connect(
    manager: MCPConnectionManager, server: ControlledServer, key: PluginMCPKey,
) -> asyncio.Task[list[MCPToolDescriptor]]:
    """让新请求读完配置并排到旧连接的生命周期锁之后。

    Args:
        manager (MCPConnectionManager): 正在等待旧服务退出的真实管理器。
        server (ControlledServer): 记录参数读取时机的服务替身。
        key (PluginMCPKey): 需要重新连接的完整插件身份。

    Returns:
        asyncio.Task[list[MCPToolDescriptor]]: 等待旧连接清理结束的建连任务。
    """
    server.config_read.clear()
    task = asyncio.create_task(manager.connect(key))
    await asyncio.wait_for(server.config_read.wait(), timeout=5)
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("request_count", [1, 3])
async def test_plugin_connect_waiting_for_removal_stays_managed(request_count: int) -> None:
    """旧记录移除后，排队请求共用一个可查询且能统一关闭的新连接。

    Args:
        request_count (int): 旧连接退出期间同时排队的建连请求数。
    """
    server = ControlledServer()
    manager = MCPConnectionManager(
        MCPServersConfig, session_factory=cast(SessionFactory, server.session_factory), plugin_config_loader=server.load_config,
    )
    key = PluginMCPKey("chat", "plugin", "game")
    tasks = []
    try:
        await manager.connect(key)
        # 1. 暂停旧连接退出，使新请求确定排在即将删除的记录上。
        removal = asyncio.create_task(manager.disconnect_plugin(key.session_id, key.plugin_name))
        tasks.append(removal)
        await asyncio.wait_for(server.closing.wait(), timeout=5)
        server.config = server.config.model_copy(update={"command": "second"})
        for _ in range(request_count):
            tasks.append(await queue_connect(manager, server, key))

        # 2. 允许删除完成，再通过公开目录和退出结果检查新连接的归属。
        server.finish_close.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        assert server.opened == ["first", "second"]
        assert [tool.name for tool in manager.get_tools(key)] == ["read"]
        assert manager.get_status(key)["status"] == "available"
        await manager.close()
        assert server.closed == ["first", "second"]
    finally:
        server.finish_close.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_connect_reads_latest_config_after_waiting_for_disconnect() -> None:
    """普通断开保留记录时，排队请求也必须使用等待期间更新的参数。"""
    server = ControlledServer()
    manager = MCPConnectionManager(
        MCPServersConfig, session_factory=cast(SessionFactory, server.session_factory), plugin_config_loader=server.load_config,
    )
    key = PluginMCPKey("chat", "plugin", "game")
    tasks = []
    try:
        await manager.connect(key)
        removal = asyncio.create_task(manager.disconnect(key))
        tasks.append(removal)
        await asyncio.wait_for(server.closing.wait(), timeout=5)
        tasks.append(await queue_connect(manager, server, key))
        # 1. 请求读到旧参数后再修改，验证拿锁后会重新读取。
        server.config = server.config.model_copy(update={"command": "second"})
        server.finish_close.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        assert server.opened == ["first", "second"]
        assert manager.get_status(key)["status"] == "available"
    finally:
        server.finish_close.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()
    assert server.closed == ["first", "second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_record", [False, True])
async def test_queued_connect_rejects_unavailable_plugin_config(remove_record: bool) -> None:
    """等待期间插件服务变为不可用时，旧请求不能重新建立连接。

    Args:
        remove_record (bool): 清理流程是否同时删除旧连接记录。
    """
    server = ControlledServer()
    manager = MCPConnectionManager(
        MCPServersConfig, session_factory=cast(SessionFactory, server.session_factory), plugin_config_loader=server.load_config,
    )
    key = PluginMCPKey("chat", "plugin", "game")
    tasks = []
    try:
        await manager.connect(key)
        removal = asyncio.create_task(
            manager.disconnect_plugin(key.session_id, key.plugin_name) if remove_record else manager.disconnect(key),
        )
        tasks.append(removal)
        await asyncio.wait_for(server.closing.wait(), timeout=5)
        connecting = await queue_connect(manager, server, key)
        tasks.append(connecting)
        # 1. 应用在禁用插件、删除服务或删除会话后，不再提供这份连接参数。
        server.config = None
        server.finish_close.set()
        await asyncio.wait_for(removal, timeout=5)
        with pytest.raises(MCPConnectionError, match="插件服务已不可用"):
            await asyncio.wait_for(connecting, timeout=5)
        assert server.opened == ["first"]
        assert manager.get_tools(key) == []
    finally:
        server.finish_close.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()
    assert server.closed == ["first"]


@pytest.mark.asyncio
async def test_connect_reads_latest_config_after_closing_changed_connection() -> None:
    """建连请求自己等待旧服务退出时，也采用退出期间再次更新的参数。"""
    server = ControlledServer()
    manager = MCPConnectionManager(
        MCPServersConfig, session_factory=cast(SessionFactory, server.session_factory), plugin_config_loader=server.load_config,
    )
    key = PluginMCPKey("chat", "plugin", "game")
    tasks = []
    try:
        await manager.connect(key)
        server.config = server.config.model_copy(update={"command": "second"})
        connecting = asyncio.create_task(manager.connect(key))
        tasks.append(connecting)
        await asyncio.wait_for(server.closing.wait(), timeout=5)
        # 1. 本次请求拿锁后仍可能等待旧资源退出，第三份参数应取代第二份。
        server.config = server.config.model_copy(update={"command": "third"})
        server.finish_close.set()
        await asyncio.wait_for(connecting, timeout=5)
        assert server.opened == ["first", "third"]
        assert manager.get_status(key)["status"] == "available"
    finally:
        server.finish_close.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()
    assert server.closed == ["first", "third"]


@pytest.mark.asyncio
async def test_session_cleanup_tolerates_concurrent_plugin_cleanup() -> None:
    """全会话清理等待第一个插件时，另一个插件可以独立完成清理。"""
    server = ControlledServer()
    first = PluginMCPKey("chat", "plugin", "game")
    second = PluginMCPKey("chat", "other", "game")
    other_config = StdioMCPConnectionConfig(id="game", name="另一个服务", command="second")

    def load_config(key: PluginMCPKey) -> StdioMCPConnectionConfig:
        """为两个插件提供独立参数，第二个服务无需等待退出。

        Args:
            key (PluginMCPKey): 当前查询的插件服务身份。

        Returns:
            StdioMCPConnectionConfig: 对应插件的连接参数。
        """
        return other_config if key == second else server.load_config(key)

    manager = MCPConnectionManager(
        MCPServersConfig, session_factory=cast(SessionFactory, server.session_factory), plugin_config_loader=load_config,
    )
    tasks = []
    try:
        await manager.connect(first)
        await manager.connect(second)
        # 1. 全会话清理停在第一个服务退出时，另一个请求先移除第二个服务。
        removal = asyncio.create_task(manager.disconnect_plugin(first.session_id))
        tasks.append(removal)
        await asyncio.wait_for(server.closing.wait(), timeout=5)
        await manager.disconnect_plugin(second.session_id, second.plugin_name)
        assert server.closed == ["second"]

        # 2. 全会话清理恢复后跳过已经清理的记录，全部资源只关闭一次。
        server.finish_close.set()
        await asyncio.wait_for(removal, timeout=5)
        assert manager.get_tools(first) == []
        assert manager.get_tools(second) == []
    finally:
        server.finish_close.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()
    assert server.closed == ["second", "first"]
