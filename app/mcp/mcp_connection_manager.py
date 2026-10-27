from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncContextManager, Callable

import anyio
import httpx
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED

from app.schemas.mcp import MCPServerStatus
from app.mcp.identity import server_identity_fingerprint, tool_contract_fingerprint
from app.mcp.process_env import build_mcp_process_env
from app.mcp.limits import limit_instructions, validate_tool_catalog
from app.mcp.types import MCPToolDescriptor, PluginMCPKey
from app.schemas.mcp import (
    MCPServerConfig,
    MCPManagedConfig,
    MCPServersConfig,
    StdioMCPServerConfig,
    StreamableHttpMCPServerConfig,
    MCPConnectionConfig,
    StdioMCPConnectionConfig,
    StreamableHttpMCPConnectionConfig,
)

logger = logging.getLogger(__name__)

SessionFactory = Callable[[MCPManagedConfig], AsyncContextManager[Any]]
ExecutionGuard = Callable[[], str | None]


class MCPConnectionError(RuntimeError):
    """表示 MCP 连接、生命周期或工具调用错误。"""

    pass


class MCPServerDisabledError(MCPConnectionError):
    """表示调用方试图连接已全局禁用的 MCP Server。"""

    pass


def _idle_event() -> asyncio.Event:
    """创建初始状态为已空闲的异步事件。

    Returns:
        asyncio.Event: 已设置的异步事件。
    """
    event = asyncio.Event()
    event.set()
    return event


@dataclass
class _Generation:
    """保存一次可排空的 MCP 连接代及其调用租约状态。"""

    config: MCPManagedConfig
    session: Any
    context_manager: AsyncContextManager[Any]
    accepting_calls: bool = True
    active_calls: int = 0
    lease_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    idle: asyncio.Event = field(default_factory=_idle_event)


@dataclass
class _Connection:
    """保存一个 MCP Server 的进程级连接状态。"""

    config: MCPManagedConfig
    status: MCPServerStatus = MCPServerStatus.DISCONNECTED
    generation: _Generation | None = None
    tools: list[MCPToolDescriptor] = field(default_factory=list)
    instructions: str | None = None
    last_error: str | None = None
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MCPConnectionManager:
    """管理 MCP Server 连接复用、工具目录和安全调用。"""

    @staticmethod
    def _enabled(config: MCPManagedConfig) -> bool:
        """读取独立服务开关，插件连接参数始终随所属插件启用。

        Args:
            config (MCPManagedConfig): 当前服务的连接参数。

        Returns:
            bool: 独立服务开关或插件连接的启用状态。
        """
        return config.enabled if isinstance(config, (StdioMCPServerConfig, StreamableHttpMCPServerConfig)) else True

    async def disconnect_plugin(self, session_id: str, plugin_name: str | None = None) -> None:
        """关闭会话下的插件连接，保留其他会话及独立服务。

        Args:
            session_id (str): 参数变化、停用或被删除的会话。
            plugin_name (str | None): 指定插件；为空时关闭本会话全部插件连接。
        """
        # 1. 保存记录快照，等待某个服务退出时，其他清理请求仍可处理剩余服务。
        for key, state in list(self._connections.items()):
            if isinstance(key, PluginMCPKey) and key.session_id == session_id and (plugin_name is None or key.plugin_name == plugin_name):
                async with state.lifecycle_lock:
                    # 2. 先确认仍是原记录，已经被其他请求移除的记录由该请求负责清理。
                    if self._connections.get(key) is not state:
                        continue
                    # 3. 等待实际连接退出后再删除记录，排队的建连请求会重新取得有效记录。
                    await self._disconnect_state_locked(state)
                    self._connections.pop(key, None)

    def __init__(
        self,
        config_loader: Callable[[], MCPServersConfig],
        *,
        session_factory: SessionFactory | None = None,
        plugin_config_loader: Callable[[PluginMCPKey], MCPConnectionConfig] | None = None,
    ):
        """初始化 MCP 连接管理器。

        Args:
            config_loader (Callable[[], MCPServersConfig]): 按需读取最新 MCP Server 配置的函数。
            session_factory (SessionFactory | None): 可选的 MCP ClientSession 上下文工厂。
            plugin_config_loader (Callable | None): 应用注入的插件连接参数解析函数。
        """
        self._config_loader = config_loader
        self._plugin_config_loader = plugin_config_loader
        self._session_factory = session_factory or self._default_session_factory
        self._connections: dict[str | PluginMCPKey, _Connection] = {}

    def _find_config(self, server_id: str | PluginMCPKey) -> MCPManagedConfig:
        """按 ID 读取最新 MCP Server 配置。

        Args:
            server_id (str | PluginMCPKey): MCP Server 的稳定标识。

        Returns:
            MCPManagedConfig: 与 ID 对应的配置。

        Raises:
            MCPConnectionError: 指定 Server 不存在。
        """
        if isinstance(server_id, PluginMCPKey):
            if self._plugin_config_loader is None:
                raise MCPConnectionError("插件连接参数解析入口未配置")
            try:
                return self._plugin_config_loader(server_id)
            except (KeyError, ValueError) as exc:
                raise MCPConnectionError(f"插件 MCP '{server_id}' 不可用: {exc}") from exc
        for config in self._config_loader().servers:
            if config.id == server_id:
                return config
        raise MCPConnectionError(f"MCP Server '{server_id}' 不存在")

    def get_server_config(self, server_id: str | PluginMCPKey) -> MCPManagedConfig:
        """读取指定 MCP Server 的最新全局配置。

        Args:
            server_id (str | PluginMCPKey): MCP Server 的稳定标识。

        Returns:
            MCPManagedConfig: 与 ID 对应的只读 Pydantic 配置对象。

        Raises:
            MCPConnectionError: 指定 Server 不存在。
        """
        return self._find_config(server_id)

    async def connect(self, server_id: str | PluginMCPKey) -> list[MCPToolDescriptor]:
        """建立或复用 MCP 连接并刷新工具目录。

        Args:
            server_id (str | PluginMCPKey): 要连接的 MCP Server ID。

        Returns:
            list[MCPToolDescriptor]: Server 当前暴露的工具描述符副本。

        Raises:
            MCPConnectionError: 连接、初始化或工具目录校验失败。
        """
        # 1. 按完整服务身份取得记录；等待期间被移除时，重新取得当前记录再处理。
        while True:
            config = self._find_config(server_id)
            state = self._connections.setdefault(server_id, _Connection(config=config))
            async with state.lifecycle_lock:
                if self._connections.get(server_id) is not state:
                    continue

                # 2. 等待锁期间参数或插件开关可能变化，以当前应用配置决定是否连接。
                config = self._find_config(server_id)
                if state.generation is not None and (
                    state.config != config
                    or not self._enabled(config)
                    or state.status != MCPServerStatus.AVAILABLE
                ):
                    # 2.1. 关闭旧连接也会异步等待，退出完成后再次读取最新参数。
                    await self._disconnect_state_locked(state)
                    config = self._find_config(server_id)
                state.config = config

                # 3. 当前服务已禁用则结束请求；相同参数的可用连接由多个请求共用。
                if not self._enabled(config):
                    state.status = MCPServerStatus.DISABLED
                    state.last_error = None
                    raise MCPServerDisabledError(f"MCP Server '{server_id}' 已禁用")
                if state.status == MCPServerStatus.AVAILABLE and state.generation is not None:
                    return list(state.tools)

                # 4. 在仍受管理的记录上完成握手、目录校验和连接登记。
                state.status = MCPServerStatus.CONNECTING
                state.last_error = None
                context_manager = self._session_factory(config)
                try:
                    session = await asyncio.wait_for(
                        context_manager.__aenter__(), timeout=config.connect_timeout_seconds
                    )
                    initialize_result = await asyncio.wait_for(
                        session.initialize(), timeout=config.connect_timeout_seconds
                    )
                    tools_result = await asyncio.wait_for(
                        session.list_tools(), timeout=config.connect_timeout_seconds
                    )
                    raw_tools = validate_tool_catalog(tools_result.tools)
                    tools = [self._to_descriptor(tool) for tool in raw_tools]
                    state.generation = _Generation(
                        config=config,
                        session=session,
                        context_manager=context_manager,
                    )
                    state.instructions = limit_instructions(getattr(initialize_result, "instructions", None))
                    state.tools = tools
                    state.status = MCPServerStatus.AVAILABLE
                    return list(tools)
                except Exception as exc:
                    # 5. 握手或目录校验失败时释放本次资源，记录错误供界面查询。
                    try:
                        await context_manager.__aexit__(type(exc), exc, exc.__traceback__)
                    except Exception:
                        pass
                    state.generation = None
                    state.tools = []
                    state.status = MCPServerStatus.ERROR
                    state.last_error = self._sanitize_error(config, exc)
                    logger.error("MCP '%s' 连接失败: %s", server_id, state.last_error)
                    raise MCPConnectionError(f"MCP '{server_id}' 连接失败: {state.last_error}") from exc

    async def disconnect(self, server_id: str | PluginMCPKey) -> None:
        """等待活动调用结束后断开指定 MCP Server。

        Args:
            server_id (str | PluginMCPKey): 要断开的 MCP Server ID。
        """
        state = self._connections.get(server_id)
        if state is not None:
            async with state.lifecycle_lock:
                await self._disconnect_state_locked(state)
                try:
                    latest = self._find_config(server_id)
                except MCPConnectionError:
                    latest = None
                if latest is not None:
                    state.config = latest
                    state.status = MCPServerStatus.DISABLED if not self._enabled(latest) else MCPServerStatus.DISCONNECTED

    async def reconnect(self, server_id: str | PluginMCPKey) -> list[MCPToolDescriptor]:
        """断开并重新连接指定 MCP Server。

        Args:
            server_id (str | PluginMCPKey): 要重连的 MCP Server ID。

        Returns:
            list[MCPToolDescriptor]: 重连后获取的工具描述符。
        """
        # 1. 重连前检查最新配置；已禁用则先断开旧连接，记录禁用状态后拒绝重连。
        config = self._find_config(server_id)
        if not self._enabled(config):
            await self.disconnect(server_id)
            state = self._connections.setdefault(server_id, _Connection(config=config))
            state.config = config
            state.status = MCPServerStatus.DISABLED
            raise MCPServerDisabledError(f"MCP Server '{server_id}' 已禁用")
        await self.disconnect(server_id)
        return await self.connect(server_id)

    async def close(self) -> None:
        """关闭管理器持有的全部 MCP 连接。"""
        for state in list(self._connections.values()):
            async with state.lifecycle_lock:
                await self._disconnect_state_locked(state)

    async def preconnect_enabled(self) -> None:
        """尝试预连接所有全局启用的 MCP Server。

        单个 Server 连接失败不会阻止其他 Server 继续预连接。
        """
        for config in self._config_loader().servers:
            if not config.enabled:
                continue
            try:
                await self.connect(config.id)
            except MCPConnectionError:
                continue

    async def test_config(self, config: MCPManagedConfig) -> dict[str, Any]:
        """使用独立临时会话测试一份未持久化的 Server 配置。

        Args:
            config (MCPManagedConfig): 待测试的 MCP Server 配置。

        Returns:
            dict[str, Any]: 包含状态、Server instructions 和工具目录的诊断结果。

        Raises:
            MCPConnectionError: 连接、初始化或工具目录读取失败。
        """
        context_manager = self._session_factory(config)
        entered = False
        try:
            session = await asyncio.wait_for(
                context_manager.__aenter__(), timeout=config.connect_timeout_seconds
            )
            entered = True
            initialized = await asyncio.wait_for(
                session.initialize(), timeout=config.connect_timeout_seconds
            )
            listed = await asyncio.wait_for(
                session.list_tools(), timeout=config.connect_timeout_seconds
            )
            raw_tools = validate_tool_catalog(listed.tools)
            tools = [self._to_descriptor(tool) for tool in raw_tools]
            return {
                "status": MCPServerStatus.AVAILABLE,
                "instructions": limit_instructions(getattr(initialized, "instructions", None)),
                "tools": [self._descriptor_view(tool) for tool in tools],
            }
        except Exception as exc:
            sanitized = self._sanitize_error(config, exc)
            raise MCPConnectionError(f"MCP '{config.id}' 测试连接失败: {sanitized}") from exc
        finally:
            if entered:
                await context_manager.__aexit__(None, None, None)

    def get_tools(self, server_id: str | PluginMCPKey) -> list[MCPToolDescriptor]:
        """获取已缓存的工具目录副本。

        Args:
            server_id (str | PluginMCPKey): MCP Server ID。

        Returns:
            list[MCPToolDescriptor]: 当前缓存的工具描述符；尚未连接时返回空列表。
        """
        state = self._connections.get(server_id)
        return list(state.tools) if state else []

    def get_tool_contract_fingerprint(self, server_id: str | PluginMCPKey, tool_name: str) -> str | None:
        """获取当前工具契约指纹。

        Args:
            server_id (str | PluginMCPKey): MCP Server ID。
            tool_name (str): Server 中的原始工具名称。

        Returns:
            str | None: 当前工具契约指纹；工具不存在时返回 ``None``。
        """
        descriptor = next(
            (tool for tool in self.get_tools(server_id) if tool.name == tool_name),
            None,
        )
        return tool_contract_fingerprint(descriptor) if descriptor is not None else None

    def get_status(self, server_id: str | PluginMCPKey) -> dict[str, Any]:
        """获取 MCP Server 的可展示运行状态。

        Args:
            server_id (str | PluginMCPKey): MCP Server ID。

        Returns:
            dict[str, Any]: 状态、工具数量、最近错误和 instructions。
        """
        config = self._find_config(server_id)
        if not self._enabled(config):
            return {
                "status": MCPServerStatus.DISABLED,
                "tool_count": 0,
                "last_error": None,
                "instructions": None,
            }
        state = self._connections.get(server_id)
        if state is None:
            return {
                "status": MCPServerStatus.DISCONNECTED,
                "tool_count": 0,
                "last_error": None,
                "instructions": None,
            }
        return {
            "status": state.status,
            "tool_count": len(state.tools),
            "last_error": state.last_error,
            "instructions": state.instructions,
        }

    def get_server_identity(self, server_id: str | PluginMCPKey) -> str:
        """计算指定 MCP Server 的最新身份指纹。

        Args:
            server_id (str | PluginMCPKey): MCP Server ID。

        Returns:
            str: 最新配置对应的服务身份指纹。
        """
        return server_identity_fingerprint(self._find_config(server_id))

    def get_available_identity(self, server_id: str | PluginMCPKey) -> str | None:
        """获取当前可用连接绑定的服务身份。

        Args:
            server_id (str | PluginMCPKey): MCP Server ID。

        Returns:
            str | None: 可用且配置未漂移时的身份指纹，否则返回 ``None``。
        """
        state = self._connections.get(server_id)
        if state is None or state.status != MCPServerStatus.AVAILABLE or state.generation is None:
            return None
        current = self._find_config(server_id)
        if state.config != current:
            return None
        return server_identity_fingerprint(current)

    async def call_tool(
        self,
        server_id: str | PluginMCPKey,
        tool_name: str,
        arguments: dict,
        *,
        execution_guard: ExecutionGuard | None = None,
    ) -> Any:
        """在可用连接代上安全调用 MCP 工具。

        Args:
            server_id (str | PluginMCPKey): MCP Server ID。
            tool_name (str): Server 中的原始工具名称。
            arguments (dict): 传递给 MCP 工具的参数。
            execution_guard (ExecutionGuard | None): 获得调用租约前执行的最终安全校验。

        Returns:
            Any: MCP SDK 返回的原始调用结果。

        Raises:
            MCPConnectionError: Server 不可用、校验失败、调用超时或执行失败。
        """
        # 1. 调用前读取当前配置并确认启用；需要连接时等待连接完成，关闭中的连接拒绝新调用。
        config = self._find_config(server_id)
        if not self._enabled(config):
            raise MCPConnectionError(f"MCP Server '{server_id}' 已禁用")
        state = self._connections.get(server_id)
        if (
            state is None
            or state.config != config
            or state.status != MCPServerStatus.AVAILABLE
            or state.generation is None
        ):
            if state is not None and state.generation is not None:
                async with state.generation.lease_lock:
                    if not state.generation.accepting_calls:
                        raise MCPConnectionError(
                            f"MCP_CONNECTION_DRAINING: MCP Server '{server_id}' 正在关闭或重连"
                        )
            await self.connect(server_id)
            state = self._connections[server_id]
        generation = state.generation
        if generation is None:
            raise MCPConnectionError(f"MCP Server '{server_id}' 当前不可用")
        # 2. 在连接调用登记锁内重新核对工具、身份和最终授权，然后登记进行中调用。
        async with generation.lease_lock:
            if (
                state.generation is not generation
                or not generation.accepting_calls
                or state.config != config
                or state.status != MCPServerStatus.AVAILABLE
            ):
                raise MCPConnectionError(
                    f"MCP_CONNECTION_DRAINING: MCP Server '{server_id}' 正在关闭或重连"
                )
            if tool_name not in {tool.name for tool in state.tools}:
                raise MCPConnectionError(f"MCP 工具 '{tool_name}' 已不存在，请重新连接服务器")
            if execution_guard is not None:
                guard_error = execution_guard()
                if guard_error:
                    raise MCPConnectionError(guard_error)
            generation.active_calls += 1
            generation.idle.clear()

        # 3. 释放登记锁后等待真实服务结果，同一服务的其他已登记调用可以并行执行。
        failure: Exception | None = None
        failure_message: str | None = None
        retire_generation = False
        try:
            return await asyncio.wait_for(
                generation.session.call_tool(tool_name, arguments),
                timeout=generation.config.call_timeout_seconds,
            )
        except Exception as exc:
            failure = exc
            failure_message = self._sanitize_error(generation.config, exc)
            retire_generation = self._should_retire_generation(exc)
            # 3.1. 只有致命连接错误才停止接收新调用，参数错误和普通超时不会重放本次操作。
            if retire_generation:
                async with generation.lease_lock:
                    generation.accepting_calls = False
                if state.generation is generation:
                    state.status = MCPServerStatus.ERROR
                    state.last_error = failure_message
        finally:
            # 4. 成功、失败或取消都减少进行中数量；最后一项结束后通知连接清理流程。
            async with generation.lease_lock:
                generation.active_calls -= 1
                if generation.active_calls == 0:
                    generation.idle.set()

        if failure is not None:
            if retire_generation:
                await self._retire_failed_generation(state, generation)
            raise MCPConnectionError(
                f"MCP 工具调用失败且未自动重试: {failure_message}"
            ) from failure
        raise AssertionError("MCP 调用结束时缺少结果")

    @staticmethod
    def _should_retire_generation(exc: Exception) -> bool:
        """判断一次调用异常是否明确表示当前连接代已经失效。

        未知异常、请求级 MCP 错误和超时默认保留连接代；只有明确的连接关闭、
        数据流损坏或非超时传输错误才触发排空和回收。

        Args:
            exc (Exception): ``session.call_tool()`` 或外层超时控制抛出的异常。

        Returns:
            bool: 当前 generation 是否必须停止接收新调用并被回收。
        """
        if isinstance(exc, BaseExceptionGroup):
            return any(
                MCPConnectionManager._should_retire_generation(nested)
                for nested in exc.exceptions
                if isinstance(nested, Exception)
            )
        if isinstance(exc, McpError):
            return exc.error.code == CONNECTION_CLOSED
        if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            return False
        return isinstance(
            exc,
            (
                anyio.ClosedResourceError,
                anyio.BrokenResourceError,
                anyio.EndOfStream,
                httpx.TransportError,
                ConnectionError,
                EOFError,
            ),
        )

    async def _retire_failed_generation(
        self,
        state: _Connection,
        generation: _Generation,
    ) -> None:
        """在生命周期锁内回收失败的连接代。

        Args:
            state (_Connection): Server 的连接状态。
            generation (_Generation): 发生失败的连接代。
        """
        async with state.lifecycle_lock:
            if state.generation is generation:
                await self._disconnect_state_locked(state, status=MCPServerStatus.ERROR)

    async def _disconnect_state_locked(
        self,
        state: _Connection,
        *,
        status: MCPServerStatus | None = None,
    ) -> None:
        """在持有生命周期锁时排空并关闭当前连接代。

        Args:
            state (_Connection): 要关闭的 Server 连接状态。
            status (MCPServerStatus | None): 关闭后覆盖写入的状态。
        """
        generation = state.generation
        if generation is not None:
            async with generation.lease_lock:
                generation.accepting_calls = False
            await generation.idle.wait()
            state.generation = None
            try:
                await generation.context_manager.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning(
                    "关闭 MCP '%s' 失败: %s",
                    state.config.id,
                    self._sanitize_error(state.config, exc),
                )
        state.tools = []
        state.instructions = None
        state.status = status or (MCPServerStatus.DISABLED if not self._enabled(state.config) else MCPServerStatus.DISCONNECTED)

    @staticmethod
    def _to_descriptor(tool: Any) -> MCPToolDescriptor:
        """把 MCP SDK 工具对象转换为内部描述符。

        Args:
            tool (Any): MCP SDK 返回的工具对象。

        Returns:
            MCPToolDescriptor: 与 SDK 类型解耦的不可变工具描述符。
        """
        annotations = getattr(tool, "annotations", None)
        if hasattr(annotations, "model_dump"):
            annotations = annotations.model_dump(mode="json")
        return MCPToolDescriptor(
            name=tool.name,
            description=getattr(tool, "description", "") or "",
            input_schema=getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}},
            annotations=annotations or {},
        )

    @staticmethod
    def _descriptor_view(tool: MCPToolDescriptor) -> dict[str, Any]:
        """把工具描述符转换为 API 可序列化字典。

        Args:
            tool (MCPToolDescriptor): 内部工具描述符。

        Returns:
            dict[str, Any]: 面向 API 的工具信息字典。
        """
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "annotations": tool.annotations,
        }

    @staticmethod
    def _sanitize_error(config: MCPManagedConfig, exc: Exception) -> str:
        """从错误消息中移除 MCP 凭据和 URL 敏感部分。

        Args:
            config (MCPManagedConfig): 产生错误的 MCP Server 配置。
            exc (Exception): 原始异常。

        Returns:
            str: 已脱敏并限制长度的错误文本。
        """
        value = str(exc)
        secrets = config.env.values() if isinstance(config, StdioMCPConnectionConfig) else config.headers.values()
        for secret in secrets:
            if secret:
                value = value.replace(secret, "***")
        if isinstance(config, StreamableHttpMCPConnectionConfig):
            parsed = urlsplit(config.url)
            safe_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            value = value.replace(config.url, safe_url)
            if parsed.query:
                value = value.replace(parsed.query, "[query omitted]")
            if parsed.fragment:
                value = value.replace(parsed.fragment, "[fragment omitted]")
        return value[:1000]

    @staticmethod
    @asynccontextmanager
    async def _default_session_factory(config: MCPManagedConfig):
        """创建与传输类型匹配的 MCP ClientSession 上下文。

        Args:
            config (MCPManagedConfig): MCP Server 配置。

        Yields:
            已进入但尚未执行 ``initialize`` 的 MCP ClientSession。

        Raises:
            MCPConnectionError: 配置使用不受支持的传输类型。
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        if isinstance(config, StdioMCPConnectionConfig):
            params = StdioServerParameters(
                command=config.command,
                args=config.args,
                cwd=config.cwd,
                env=build_mcp_process_env(config.env),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    yield session
            return

        if isinstance(config, StreamableHttpMCPConnectionConfig):
            async with httpx.AsyncClient(
                headers=config.headers or None,
                timeout=httpx.Timeout(
                    config.call_timeout_seconds,
                    connect=config.connect_timeout_seconds,
                ),
            ) as http_client:
                async with streamable_http_client(
                    config.url,
                    http_client=http_client,
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        yield session
            return

        raise MCPConnectionError("不支持的 MCP transport")
