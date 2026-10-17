from tests.model_capture import image_urls, image_kind
from pathlib import Path
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED, INVALID_PARAMS, ErrorData
from pydantic import BaseModel, ValidationError

from app.agent.context import ContextStrategyConfig
from app.agent.agent import Agent, AgentConfig
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.results.normalizer import ToolResultNormalizer
from app.agent.core.event_router import AgentEvent, EventType
from app.agent.state import AgentState
from app.agent.mcp.adapter import MCPToolAdapter, build_exposed_tool_name
from app.mcp.identity import server_identity_fingerprint, tool_contract_fingerprint
from app.mcp.types import MCPToolDescriptor
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.crud.chat_settings_dao import ChatSettingsDao
from app.mcp.mcp_connection_manager import (
    MCPConnectionError,
    MCPConnectionManager,
    MCPServerDisabledError,
)
from app.services.mcp_service import MCPService
from app.services.chat_settings_service import ChatSettingsService
from app.services.settings_mutation import SettingsMutationCoordinator
from app.schemas.chat_settings import ChatSettings
from app.schemas.tool_permissions import ToolPolicy
from app.schemas.mcp import (
    MCPModelSettings,
    MCPServerPolicy,
    MCPServersConfig,
    StdioMCPServerConfig,
    StreamableHttpMCPServerConfig,
)


def _chat_settings(**overrides) -> ChatSettings:
    """构造当前 MCP 或记忆测试所需的会话配置。

    Args:
        overrides (Any): 覆盖当前测试配置或工具参数默认值的字段。

    Returns:
        ChatSettings: 供外层用例断言使用的预设结果。
    """
    values = {
        "session_id": "session-a",
        "model_name": "model",
        "openai_api_key": "key",
        "openai_base_url": "http://127.0.0.1:1/v1",
        "temperature": 0.1,
        "system_prompt": "prompt",
        "context_strategy": ContextStrategyConfig(compression_trigger_tokens_k=100),
    }
    values.update(overrides)
    return ChatSettings(**values)


def test_mcp_server_config_supports_stdio_and_streamable_http() -> None:
    """验证MCP 服务配置支持 stdio 和流式 HTTP。"""
    config = MCPServersConfig(
        servers=[
            {
                "id": "filesystem",
                "name": "Filesystem",
                "enabled": True,
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "server-filesystem"],
                "cwd": "E:/workspace",
                "env": {"TOKEN": "plain-secret"},
            },
            {
                "id": "remote-search",
                "name": "Remote Search",
                "enabled": True,
                "transport": "streamable_http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer plain-secret"},
            },
        ]
    )

    assert isinstance(config.servers[0], StdioMCPServerConfig)
    assert isinstance(config.servers[1], StreamableHttpMCPServerConfig)
    assert config.servers[0].connect_timeout_seconds == 15
    assert config.servers[1].call_timeout_seconds == 60


@pytest.mark.parametrize("server_id", ["", "contains space", "a" * 33])
def test_mcp_server_id_is_stable_model_tool_name_component(server_id: str) -> None:
    """验证服务编号作为模型工具名称的稳定组成部分。

    Args:
        server_id (str): MCP 服务的稳定编号。
    """
    with pytest.raises(ValidationError):
        StdioMCPServerConfig(
            id=server_id,
            name="Invalid",
            transport="stdio",
            command="python",
        )


def test_mcp_settings_dao_round_trips_plaintext_credentials(tmp_path: Path) -> None:
    """验证MCP 配置保存读取保留声明的凭据。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    config_file = tmp_path / "config" / "mcp_servers.yaml"
    dao = MCPSettingsDao(config_file=config_file)
    saved = MCPServersConfig(
        servers=[
            StdioMCPServerConfig(
                id="github",
                name="GitHub",
                transport="stdio",
                command="npx",
                env={"GITHUB_TOKEN": "plain-secret"},
            )
        ]
    )

    dao.save(saved)
    loaded = MCPSettingsDao(config_file=config_file).load()

    assert loaded == saved
    assert "plain-secret" in config_file.read_text(encoding="utf-8")
    assert not config_file.with_suffix(".yaml.tmp").exists()


def test_mcp_error_sanitization_removes_headers_and_url_query() -> None:
    """验证MCP 错误清理移除请求头和 URL 查询敏感值。"""
    config = StreamableHttpMCPServerConfig(
        id="remote",
        name="Remote",
        url="http://example.test/mcp?token=query-secret",
        headers={"Authorization": "Bearer header-secret"},
    )
    sanitized = MCPConnectionManager._sanitize_error(
        config,
        RuntimeError(
            "failed http://example.test/mcp?token=query-secret with Bearer header-secret"
        ),
    )

    assert "query-secret" not in sanitized
    assert "header-secret" not in sanitized
    assert "http://example.test/mcp" in sanitized


def test_chat_settings_defaults_to_no_mcp_access() -> None:
    """验证新会话默认无 MCP 服务访问权限。"""
    settings = _chat_settings()

    assert settings.mcp == MCPModelSettings()
    assert settings.mcp.servers == {}


def test_chat_settings_yaml_without_mcp_uses_default(tmp_path: Path) -> None:
    """验证旧 YAML 缺少 MCP 字段时使用默认值。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    config_file = tmp_path / "chat_settings.yaml"
    settings = _chat_settings().model_dump(mode="json")
    settings.pop("mcp")
    import yaml

    config_file.write_text(
        yaml.safe_dump({"chat_models": [settings]}, allow_unicode=True),
        encoding="utf-8",
    )

    loaded = ChatSettingsDao(config_file).get_chat_settings("session-a")

    assert loaded.mcp.servers == {}


def test_chat_settings_round_trips_stable_key_mcp_policies() -> None:
    """验证稳定键形式的 MCP 权限经保存读取仍完整。"""
    settings = _chat_settings(
        tool_permissions={
            "mcp:filesystem:read_file": ToolPolicy.ALLOW,
            "mcp:filesystem:write_file": ToolPolicy.ASK,
            "mcp:filesystem:delete_file": ToolPolicy.DENY,
        },
        mcp={
            "servers": {
                "filesystem": {
                    "enabled": True,
                }
            }
        }
    )

    assert settings.tool_permissions == {
        "mcp:filesystem:read_file": ToolPolicy.ALLOW,
        "mcp:filesystem:write_file": ToolPolicy.ASK,
        "mcp:filesystem:delete_file": ToolPolicy.DENY,
    }

    persisted = settings.model_dump()
    restored = ChatSettings.model_validate(persisted)
    assert restored.mcp.model_dump() == settings.mcp.model_dump()
    assert restored.tool_permissions == settings.tool_permissions


def test_mcp_tool_alias_is_namespaced_stable_and_model_safe() -> None:
    """验证MCP 工具别名稳定且符合模型命名规则。"""
    short = build_exposed_tool_name("filesystem", "read_file")
    long = build_exposed_tool_name("remote", "包含空格/" + "x" * 100)

    assert short == "mcp__filesystem__read_file"
    assert len(long) <= 64
    assert long == build_exposed_tool_name("remote", "包含空格/" + "x" * 100)
    assert all(character.isalnum() or character in "_-" for character in long)


class _FakeSession:
    """提供当前用例所需的 _FakeSession 测试替身，执行记录仅供外层断言使用。"""
    def __init__(self) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。"""
        self.initialize_count = 0
        self.calls: list[tuple[str, dict]] = []

    async def initialize(self):
        """模拟 MCP 会话初始化，避免连接真实服务。"""
        self.initialize_count += 1
        return SimpleNamespace(instructions="仅用于诊断")

    async def list_tools(self):
        """返回当前测试服务的工具目录。"""
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="read_file",
                    description="读取文件",
                    inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
                    annotations=None,
                ),
                SimpleNamespace(
                    name="write_file",
                    description="写入文件",
                    inputSchema={"type": "object", "properties": {}},
                    annotations=None,
                ),
                SimpleNamespace(
                    name="new_tool",
                    description="新增工具",
                    inputSchema={"type": "object", "properties": {}},
                    annotations=None,
                ),
            ]
        )

    async def call_tool(self, name: str, arguments: dict):
        """记录 MCP 调用并按用例返回结果或制造异常。

        Args:
            name (str): 当前对象的名称。
            arguments (dict): 模型工具调用的参数文本或字典。
        """
        self.calls.append((name, arguments))
        return SimpleNamespace(
            isError=False,
            structuredContent={"ok": True},
            content=[
                SimpleNamespace(type="text", text="完成"),
                SimpleNamespace(type="image", data="iVBORw0KGgo=", mimeType="image/png"),
            ],
        )


@pytest.mark.asyncio
async def test_connection_manager_reuses_connection_and_returns_raw_tool_catalog() -> None:
    """验证连接管理器复用连接并返回原始工具目录。"""
    session = _FakeSession()
    closed = 0

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证连接管理器复用连接并返回原始工具目录。

        Args:
            _config (Any): 调用方提供的配置。
        """
        nonlocal closed
        try:
            yield session
        finally:
            closed += 1

    config = MCPServersConfig(
        servers=[
            StdioMCPServerConfig(
                id="filesystem",
                name="Filesystem",
                enabled=True,
                transport="stdio",
                command="python",
            )
        ]
    )
    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)
    first = await manager.connect("filesystem")
    second = await manager.connect("filesystem")

    assert [tool.name for tool in first] == ["read_file", "write_file", "new_tool"]
    assert [tool.name for tool in second] == ["read_file", "write_file", "new_tool"]
    assert session.initialize_count == 1
    await manager.close()
    assert closed == 1


@pytest.mark.asyncio
async def test_connection_manager_allows_concurrent_calls_per_server() -> None:
    """验证同一服务允许并发工具调用。"""
    import asyncio

    class _SerialSession(_FakeSession):
        """提供当前用例所需的 _SerialSession 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证同一服务允许并发工具调用。
        """
        active = 0
        max_active = 0

        async def call_tool(self, name: str, arguments: dict):
            """记录 MCP 调用并按用例返回结果或制造异常。

            该定义仅用于外层用例：验证同一服务允许并发工具调用。

            Args:
                name (str): 当前对象的名称。
                arguments (dict): 模型工具调用的参数文本或字典。
            """
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                return await super().call_tool(name, arguments)
            finally:
                self.active -= 1

    session = _SerialSession()

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证同一服务允许并发工具调用。

        Args:
            _config (Any): 调用方提供的配置。
        """
        yield session

    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="serial", name="Serial", enabled=True, command="python"
    )])
    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)
    await manager.connect("serial")

    await asyncio.gather(
        manager.call_tool("serial", "read_file", {"index": 1}),
        manager.call_tool("serial", "read_file", {"index": 2}),
    )

    assert session.max_active == 2
    await manager.close()


@pytest.mark.asyncio
async def test_unknown_call_error_keeps_generation_for_next_call() -> None:
    """验证未知调用错误不更换后续调用连接。"""
    close_order: list[int] = []
    session = _FakeSession()
    original_call = session.call_tool
    failed = False

    async def fail_once(name: str, arguments: dict):
        """首次调用主动失败，随后恢复脚本响应，用于验证失败边界和重试。

        该定义仅用于外层用例：验证未知调用错误不更换后续调用连接。

        Args:
            name (str): 当前对象的名称。
            arguments (dict): 模型工具调用的参数文本或字典。
        """
        nonlocal failed
        if not failed:
            failed = True
            session.calls.append((name, arguments))
            raise RuntimeError("unknown call failure with plain-secret")
        return await original_call(name, arguments)

    session.call_tool = fail_once
    created = 0

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证未知调用错误不更换后续调用连接。

        Args:
            _config (Any): 调用方提供的配置。
        """
        nonlocal created
        current = created
        created += 1
        try:
            yield session
        finally:
            close_order.append(current)

    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="lifecycle",
        name="Lifecycle",
        enabled=True,
        command="python",
        env={"TOKEN": "plain-secret"},
    )])
    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)

    with pytest.raises(Exception, match="未自动重试") as error:
        await manager.call_tool("lifecycle", "read_file", {})
    assert "plain-secret" not in str(error.value)
    assert close_order == []
    assert manager.get_status("lifecycle")["status"] == "available"
    assert manager.get_status("lifecycle")["last_error"] is None

    result = await manager.call_tool("lifecycle", "read_file", {})
    assert result.isError is False
    assert created == 1
    await manager.close()
    assert close_order == [0]


class _InvalidCallResult(BaseModel):
    """提供当前用例所需的 _InvalidCallResult 测试替身，执行记录仅供外层断言使用。"""
    value: int


def _validation_error() -> ValidationError:
    """构造本用例需要的 MCP 参数校验异常。

    Returns:
        ValidationError: 供外层用例断言使用的预设结果。
    """
    with pytest.raises(ValidationError) as raised:
        _InvalidCallResult(value="invalid")
    return raised.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        McpError(ErrorData(code=INVALID_PARAMS, message="参数不合法")),
        RuntimeError("Invalid structured content returned by tool read_file"),
        _validation_error(),
        ValueError("本地参数错误"),
        httpx.ReadTimeout("调用超时"),
        ExceptionGroup("unknown", [RuntimeError("未知异常")]),
    ],
    ids=[
        "mcp-request-error",
        "output-schema-error",
        "response-validation-error",
        "local-value-error",
        "httpx-timeout",
        "unknown-exception-group",
    ],
)
async def test_nonfatal_call_errors_keep_generation(failure: Exception) -> None:
    """验证非致命调用错误保留连接。

    Args:
        failure (Exception): 当前用例要求抛出的调用异常。
    """
    session = _FakeSession()
    original_call = session.call_tool
    failed = False
    closed = 0

    async def fail_once(name: str, arguments: dict):
        """首次调用主动失败，随后恢复脚本响应，用于验证失败边界和重试。

        该定义仅用于外层用例：验证非致命调用错误保留连接。

        Args:
            name (str): 当前对象的名称。
            arguments (dict): 模型工具调用的参数文本或字典。
        """
        nonlocal failed
        if not failed:
            failed = True
            raise failure
        return await original_call(name, arguments)

    session.call_tool = fail_once

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证非致命调用错误保留连接。

        Args:
            _config (Any): 调用方提供的配置。
        """
        nonlocal closed
        try:
            yield session
        finally:
            closed += 1

    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="nonfatal", name="Nonfatal", enabled=True, command="python"
    )])
    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)

    with pytest.raises(MCPConnectionError, match="未自动重试"):
        await manager.call_tool("nonfatal", "read_file", {})

    assert closed == 0
    assert manager.get_status("nonfatal")["status"] == "available"
    assert manager.get_status("nonfatal")["last_error"] is None
    assert (await manager.call_tool("nonfatal", "read_file", {})).isError is False
    await manager.close()
    assert closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        McpError(ErrorData(code=CONNECTION_CLOSED, message="连接已关闭")),
        anyio.ClosedResourceError(),
        anyio.BrokenResourceError(),
        anyio.EndOfStream(),
        httpx.ReadError("读取连接失败"),
        ConnectionResetError("连接被重置"),
        EOFError("连接已结束"),
        ExceptionGroup("transport", [RuntimeError("上下文"), EOFError("连接已结束")]),
    ],
    ids=[
        "mcp-connection-closed",
        "anyio-closed-resource",
        "anyio-broken-resource",
        "anyio-end-of-stream",
        "httpx-read-error",
        "connection-reset",
        "eof",
        "fatal-exception-group",
    ],
)
async def test_fatal_call_errors_retire_generation(failure: Exception) -> None:
    """验证致命调用错误移除失效连接。

    Args:
        failure (Exception): 当前用例要求抛出的调用异常。
    """
    sessions = [_FakeSession(), _FakeSession()]
    original_call = sessions[0].call_tool
    closed: list[int] = []
    created = 0
    failed = False

    async def fail_once(name: str, arguments: dict):
        """首次调用主动失败，随后恢复脚本响应，用于验证失败边界和重试。

        该定义仅用于外层用例：验证致命调用错误移除失效连接。

        Args:
            name (str): 当前对象的名称。
            arguments (dict): 模型工具调用的参数文本或字典。
        """
        nonlocal failed
        if not failed:
            failed = True
            raise failure
        return await original_call(name, arguments)

    sessions[0].call_tool = fail_once

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证致命调用错误移除失效连接。

        Args:
            _config (Any): 调用方提供的配置。
        """
        nonlocal created
        index = created
        created += 1
        try:
            yield sessions[index]
        finally:
            closed.append(index)

    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="fatal", name="Fatal", enabled=True, command="python"
    )])
    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)

    with pytest.raises(MCPConnectionError, match="未自动重试"):
        await manager.call_tool("fatal", "read_file", {})

    assert closed == [0]
    assert manager.get_status("fatal")["status"] == "error"
    assert (await manager.call_tool("fatal", "read_file", {})).isError is False
    assert created == 2
    await manager.close()
    assert closed == [0, 1]


@pytest.mark.asyncio
async def test_one_failed_concurrent_call_does_not_cancel_other_inflight_call() -> None:
    """验证一项并发调用失败不取消其他进行中调用。"""
    import asyncio

    release_success = asyncio.Event()
    success_started = asyncio.Event()
    closed = 0

    class _ConcurrentSession(_FakeSession):
        """提供当前用例所需的 _ConcurrentSession 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证一项并发调用失败不取消其他进行中调用。
        """
        async def call_tool(self, name: str, arguments: dict):
            """记录 MCP 调用并按用例返回结果或制造异常。

            该定义仅用于外层用例：验证一项并发调用失败不取消其他进行中调用。

            Args:
                name (str): 当前对象的名称。
                arguments (dict): 模型工具调用的参数文本或字典。
            """
            if arguments["kind"] == "fail":
                await success_started.wait()
                raise ConnectionResetError("transport failed")
            success_started.set()
            await release_success.wait()
            return await super().call_tool(name, arguments)

    session = _ConcurrentSession()

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证一项并发调用失败不取消其他进行中调用。

        Args:
            _config (Any): 调用方提供的配置。
        """
        nonlocal closed
        try:
            yield session
        finally:
            closed += 1

    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="concurrent", name="Concurrent", enabled=True, command="python"
    )])
    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)
    await manager.connect("concurrent")

    success_task = asyncio.create_task(
        manager.call_tool("concurrent", "read_file", {"kind": "success"})
    )
    failure_task = asyncio.create_task(
        manager.call_tool("concurrent", "read_file", {"kind": "fail"})
    )
    await success_started.wait()
    await asyncio.sleep(0)
    assert not success_task.done()
    release_success.set()

    success = await success_task
    assert success.isError is False
    with pytest.raises(MCPConnectionError, match="未自动重试"):
        await failure_task
    assert closed == 1


def _tool_context(
    resume_data: dict | None = None,
    resume_state: dict | None = None,
) -> ToolContext:
    """为 MCP 调用构造工具上下文和事件回调。

    Args:
        resume_data (dict | None): 本次模拟审批或桌面回报的数据。
        resume_state (dict | None): 工具中断时保存的恢复材料。

    Returns:
        ToolContext: 供外层用例断言使用的预设结果。
    """
    return ToolContext(
        session_id="session-a",
        state=SimpleNamespace(),
        emit_event=lambda *_args: None,
        get_checkpoint=lambda: {},
        set_checkpoint=lambda _data: None,
        resume_data=resume_data,
        resume_state=resume_state,
    )


class _MutablePolicyResolver:
    """为 Adapter 安全测试提供可变的实时权限与身份。"""

    def __init__(self, policy: ToolPolicy, identity: str | None) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        该定义仅用于外层用例：为 Adapter 安全测试提供可变的实时权限与身份。

        Args:
            policy (ToolPolicy): 本用例指定的权限或压缩策略。
            identity (str | None): 当前服务或工具的测试身份摘要。
        """
        self.policy = policy
        self.identity = identity

    def get_policy(self, _session_id: str, _server_id: str, _tool_name: str) -> ToolPolicy:
        """读取用例提供的实时工具权限，验证执行前会重新检查。

        该定义仅用于外层用例：为 Adapter 安全测试提供可变的实时权限与身份。

        Args:
            _session_id (str): 当前操作所属的会话编号。
            _server_id (str): MCP 服务的稳定编号。
            _tool_name (str): 注册表中使用的工具名称。

        Returns:
            ToolPolicy: 供外层用例断言使用的预设结果。
        """
        return self.policy

    def get_bound_identity(self, _session_id: str, _server_id: str) -> str | None:
        """返回当前 MCP 权限绑定的服务身份。

        该定义仅用于外层用例：为 Adapter 安全测试提供可变的实时权限与身份。

        Args:
            _session_id (str): 当前操作所属的会话编号。
            _server_id (str): MCP 服务的稳定编号。

        Returns:
            str | None: 供外层用例断言使用的预设结果。
        """
        return self.identity

    def is_exposed(self, _session_id: str, _tool: BaseTool) -> bool:
        """按用例权限判断工具是否应出现在模型工具列表中。

        该定义仅用于外层用例：为 Adapter 安全测试提供可变的实时权限与身份。

        Args:
            _session_id (str): 当前操作所属的会话编号。
            _tool (BaseTool): 需要注册或执行的工具对象。

        Returns:
            bool: 供外层用例断言使用的预设结果。
        """
        return self.policy != ToolPolicy.DENY

    def get_bound_mcp_identity(self, _session_id: str, _server_id: str) -> str | None:
        """返回当前会话绑定的 MCP 服务身份，供最终执行检查。

        该定义仅用于外层用例：为 Adapter 安全测试提供可变的实时权限与身份。

        Args:
            _session_id (str): 当前操作所属的会话编号。
            _server_id (str): MCP 服务的稳定编号。

        Returns:
            str | None: 供外层用例断言使用的预设结果。
        """
        return self.identity


@pytest.mark.asyncio
async def test_adapter_executes_authorized_call_and_converts_results() -> None:
    """验证适配器执行已授权调用并转换结果。"""
    session = _FakeSession()

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证适配器执行已授权调用并转换结果。

        Args:
            _config (Any): 调用方提供的配置。
        """
        yield session

    config = MCPServersConfig(
        servers=[
            StdioMCPServerConfig(
                id="filesystem",
                name="Filesystem",
                enabled=True,
                transport="stdio",
                command="python",
            )
        ]
    )
    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)
    await manager.connect("filesystem")
    descriptor = manager.get_tools("filesystem")[1]
    resolver = _MutablePolicyResolver(
        ToolPolicy.ALLOW,
        server_identity_fingerprint(config.servers[0]),
    )
    adapter = MCPToolAdapter(
        session_id="session-a",
        server_id="filesystem",
        server_name="Filesystem",
        descriptor=descriptor,
        connection_manager=manager,
        authorization=resolver,
    )

    result = await adapter.execute({"path": "README.md"}, _tool_context())

    assert result.interrupt is None
    assert "完成" in result.text
    assert '"ok":true' in result.text
    assert image_urls(result) == ["data:image/png;base64,iVBORw0KGgo="]
    assert session.calls == [("write_file", {"path": "README.md"})]
    await manager.close()


@pytest.mark.asyncio
async def test_adapter_does_not_own_user_approval_interrupts() -> None:
    """验证适配器不自行拥有用户审批中断。"""
    session = _FakeSession()
    resolver = _MutablePolicyResolver(ToolPolicy.ASK, None)

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证适配器不自行拥有用户审批中断。

        Args:
            _config (Any): 调用方提供的配置。
        """
        yield session

    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="filesystem", name="Filesystem", enabled=True, command="python"
    )])
    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)
    resolver.identity = server_identity_fingerprint(config.servers[0])
    await manager.connect("filesystem")
    descriptor = manager.get_tools("filesystem")[0]
    adapter = MCPToolAdapter(
        session_id="session-a",
        server_id="filesystem",
        server_name="Filesystem",
        descriptor=descriptor,
        connection_manager=manager,
        authorization=resolver,
    )
    result = await adapter.execute({}, _tool_context())

    assert result.interrupt is None
    assert session.calls == [("read_file", {})]
    await manager.close()


@pytest.mark.asyncio
async def test_adapter_rejects_changed_tool_contract_at_final_guard() -> None:
    """验证最后权限检查拒绝变化后的工具定义。"""
    session = _FakeSession()

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证最后权限检查拒绝变化后的工具定义。

        Args:
            _config (Any): 调用方提供的配置。
        """
        yield session

    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="filesystem", name="Filesystem", enabled=True, command="python"
    )])
    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)
    await manager.connect("filesystem")
    descriptor = manager.get_tools("filesystem")[0]
    resolver = _MutablePolicyResolver(
        ToolPolicy.ALLOW,
        server_identity_fingerprint(config.servers[0]),
    )
    adapter = MCPToolAdapter(
        session_id="session-a",
        server_id="filesystem",
        server_name="Filesystem",
        descriptor=descriptor,
        connection_manager=manager,
        authorization=resolver,
    )
    manager.get_tool_contract_fingerprint = lambda *_args: "changed-contract"
    result = await adapter.execute({}, _tool_context())

    assert "MCP_APPROVAL_STALE" in result.text
    assert session.calls == []
    await manager.close()


@pytest.mark.asyncio
async def test_adapter_rejects_server_identity_changed_after_construction() -> None:
    """验证适配器构造后服务身份变化会被拒绝。"""
    session = _FakeSession()

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证适配器构造后服务身份变化会被拒绝。

        Args:
            _config (Any): 调用方提供的配置。
        """
        yield session

    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="filesystem", name="Filesystem", enabled=True, command="python"
    )])
    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)
    await manager.connect("filesystem")
    original_identity = server_identity_fingerprint(config.servers[0])
    resolver = _MutablePolicyResolver(ToolPolicy.ALLOW, original_identity)
    adapter = MCPToolAdapter(
        session_id="session-a",
        server_id="filesystem",
        server_name="Filesystem",
        descriptor=manager.get_tools("filesystem")[0],
        connection_manager=manager,
        authorization=resolver,
    )
    manager.get_server_identity = lambda _server_id: "changed-identity"
    resolver.identity = "changed-identity"

    result = await adapter.execute({}, _tool_context())

    assert "MCP_IDENTITY_UNBOUND" in result.text
    assert session.calls == []
    await manager.close()


def test_identity_fingerprints_only_cover_identity_and_tool_contract() -> None:
    """验证身份摘要只覆盖服务身份与工具定义。"""
    base = StdioMCPServerConfig(
        id="filesystem",
        name="Filesystem",
        enabled=True,
        command="python",
        args=["server.py"],
        env={"TOKEN": "secret", "MODE": "local"},
    )
    renamed = base.model_copy(update={
        "name": "Renamed",
        "enabled": False,
        "connect_timeout_seconds": 30,
        "call_timeout_seconds": 120,
    })
    changed = base.model_copy(update={"args": ["other.py"]})
    descriptor = MCPToolDescriptor(
        name="read_file",
        description="读取文件",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        annotations={"readOnlyHint": True},
    )

    assert server_identity_fingerprint(base) == server_identity_fingerprint(renamed)
    assert server_identity_fingerprint(base) != server_identity_fingerprint(changed)
    assert tool_contract_fingerprint(descriptor) != tool_contract_fingerprint(
        MCPToolDescriptor(
            name=descriptor.name,
            description="新的描述",
            input_schema=descriptor.input_schema,
            annotations=descriptor.annotations,
        )
    )


@pytest.mark.asyncio
async def test_adapter_rechecks_current_policy_before_execution() -> None:
    """验证执行前重新读取当前权限。"""
    session = _FakeSession()
    resolver = _MutablePolicyResolver(ToolPolicy.ALLOW, None)

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证执行前重新读取当前权限。

        Args:
            _config (Any): 调用方提供的配置。
        """
        yield session

    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="filesystem", name="Filesystem", enabled=True, command="python"
    )])
    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)
    resolver.identity = server_identity_fingerprint(config.servers[0])
    await manager.connect("filesystem")
    adapter = MCPToolAdapter(
        session_id="session-a",
        server_id="filesystem",
        server_name="Filesystem",
        descriptor=manager.get_tools("filesystem")[0],
        connection_manager=manager,
        authorization=resolver,
    )

    resolver.policy = ToolPolicy.DENY
    result = await adapter.execute({}, _tool_context())

    assert "未获准" in result.text
    assert session.calls == []
    await manager.close()


@pytest.mark.asyncio
async def test_adapter_rechecks_policy_inside_connection_lease() -> None:
    """验证取得连接使用权后仍重新核对权限。"""
    session = _FakeSession()
    resolver = _MutablePolicyResolver(ToolPolicy.ALLOW, None)

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证取得连接使用权后仍重新核对权限。

        Args:
            _config (Any): 调用方提供的配置。
        """
        yield session

    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="filesystem", name="Filesystem", enabled=True, command="python"
    )])

    class _PolicyChangingManager(MCPConnectionManager):
        """提供当前用例所需的 _PolicyChangingManager 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证取得连接使用权后仍重新核对权限。
        """
        async def call_tool(self, *args, **kwargs):
            """记录 MCP 调用并按用例返回结果或制造异常。

            该定义仅用于外层用例：验证取得连接使用权后仍重新核对权限。

            Args:
                args (dict[str, Any]): 当前工具调用或被包装函数的参数。
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            resolver.policy = ToolPolicy.DENY
            return await super().call_tool(*args, **kwargs)

    manager = _PolicyChangingManager(lambda: config, session_factory=session_factory)
    resolver.identity = server_identity_fingerprint(config.servers[0])
    await manager.connect("filesystem")
    adapter = MCPToolAdapter(
        session_id="session-a",
        server_id="filesystem",
        server_name="Filesystem",
        descriptor=manager.get_tools("filesystem")[0],
        connection_manager=manager,
        authorization=resolver,
    )

    result = await adapter.execute({}, _tool_context())

    assert "未获准" in result.text
    assert session.calls == []
    assert manager.get_status("filesystem")["status"] == "available"
    await manager.close()


@pytest.mark.asyncio
async def test_adapter_rejects_changed_policy_binding_identity() -> None:
    """验证权限绑定身份变化后拒绝调用。"""
    session = _FakeSession()

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证权限绑定身份变化后拒绝调用。

        Args:
            _config (Any): 调用方提供的配置。
        """
        yield session

    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="filesystem", name="Filesystem", enabled=True, command="python"
    )])
    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)
    await manager.connect("filesystem")
    resolver = _MutablePolicyResolver(ToolPolicy.ALLOW, "旧身份")
    adapter = MCPToolAdapter(
        session_id="session-a",
        server_id="filesystem",
        server_name="Filesystem",
        descriptor=manager.get_tools("filesystem")[0],
        connection_manager=manager,
        authorization=resolver,
    )

    result = await adapter.execute({}, _tool_context())

    assert "MCP_IDENTITY_UNBOUND" in result.text
    assert session.calls == []
    await manager.close()


@pytest.mark.asyncio
async def test_call_timeout_is_not_replayed_keeps_generation_and_redacts_secret() -> None:
    """验证调用超时不重放且保留连接并清理敏感值。"""
    class _SlowSession(_FakeSession):
        """提供当前用例所需的 _SlowSession 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证调用超时不重放且保留连接并清理敏感值。
        """
        timed_out = False

        async def call_tool(self, name: str, arguments: dict):
            """记录 MCP 调用并按用例返回结果或制造异常。

            该定义仅用于外层用例：验证调用超时不重放且保留连接并清理敏感值。

            Args:
                name (str): 当前对象的名称。
                arguments (dict): 模型工具调用的参数文本或字典。
            """
            self.calls.append((name, arguments))
            if not self.timed_out:
                self.timed_out = True
                await __import__("asyncio").sleep(0.05)
            return await super().call_tool(name, arguments)

    session = _SlowSession()
    closed = 0

    @asynccontextmanager
    async def session_factory(_config):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：验证调用超时不重放且保留连接并清理敏感值。

        Args:
            _config (Any): 调用方提供的配置。
        """
        nonlocal closed
        try:
            yield session
        finally:
            closed += 1

    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="slow",
        name="Slow",
        enabled=True,
        command="python",
        call_timeout_seconds=0.01,
        env={"TOKEN": "plain-secret"},
    )])
    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)
    await manager.connect("slow")

    with pytest.raises(Exception, match="未自动重试") as error:
        await manager.call_tool("slow", "read_file", {"token": "plain-secret"})

    assert len(session.calls) == 1
    assert "plain-secret" not in str(error.value)
    assert closed == 0
    assert manager.get_status("slow")["status"] == "available"
    assert manager.get_status("slow")["last_error"] is None
    result = await manager.call_tool("slow", "read_file", {"token": "plain-secret"})
    assert result.isError is False
    assert len(session.calls) == 3
    await manager.close()
    assert closed == 1


def test_adapter_converts_errors_multiple_images_and_unsupported_content() -> None:
    """验证适配器转换错误、多图片和不支持的内容。"""
    error = MCPToolAdapter._convert_result(SimpleNamespace(
        isError=True,
        content=[SimpleNamespace(type="text", text="remote failed")],
    ))
    assert "remote failed" in error.text

    converted = MCPToolAdapter._convert_result(SimpleNamespace(
        isError=False,
        structuredContent={"large": "x" * 60_000},
        content=[
            SimpleNamespace(type="image", data="iVBORw0KGgo=", mimeType="image/png"),
            SimpleNamespace(type="image", data="/9j/", mimeType="image/jpeg"),
            SimpleNamespace(type="audio"),
            SimpleNamespace(type="resource_link"),
            SimpleNamespace(type="resource"),
        ],
    ))
    assert image_urls(converted) == [
        "data:image/png;base64,iVBORw0KGgo=",
        "data:image/jpeg;base64,/9j/",
    ]
    assert image_kind(converted) == "mcp"
    assert "已忽略 3 个不支持的 MCP 内容类型" in converted.text
    assert "x" * 60_000 in converted.text


def test_adapter_preserves_large_error_for_unified_normalization() -> None:
    """验证较大错误正文保留给统一结果规范化。"""
    huge_error = MCPToolAdapter._convert_result(SimpleNamespace(
        isError=True,
        content=[SimpleNamespace(type="text", text="x" * 100_000)],
    ))

    assert huge_error.text == "错误: " + "x" * 100_000


def test_adapter_applies_image_quotas() -> None:
    """图片数量、Base64 和 MIME 均受配额约束。"""

    images = [
        SimpleNamespace(type="image", data="iVBORw0KGgo=", mimeType="image/png")
        for _ in range(6)
    ]
    images.extend([
        SimpleNamespace(type="image", data="%%%", mimeType="image/png"),
        SimpleNamespace(type="image", data="iVBORw0KGgo=", mimeType="image/gif"),
    ])
    converted = MCPToolAdapter._convert_result(SimpleNamespace(isError=False, content=images))
    normalized = ToolResultNormalizer().normalize(converted)
    assert len(image_urls(normalized)) == 5
    assert "已忽略 3 张工具图片：图片数量超过限制" in normalized.text


def test_adapter_deduplicates_exact_structured_serialization() -> None:
    """验证结构化内容已有完全一致正文时不重复追加。"""
    structured = {"items": [1, 2], "message": "你好"}
    serialized = '{"items":[1,2],"message":"你好"}'

    converted = MCPToolAdapter._convert_result(SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(type="text", text=f"结果如下：\n{serialized}")],
        structuredContent=structured,
    ))

    assert converted.text == f"结果如下：\n{serialized}"


def test_adapter_appends_structured_content_when_exact_serialization_is_absent() -> None:
    """验证缺少完全一致正文时追加结构化内容。"""
    converted = MCPToolAdapter._convert_result(SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(type="text", text='结果如下： {"value": 1}')],
        structuredContent={"value": 1},
    ))

    assert converted.text == (
        '结果如下： {"value": 1}\n\n[structuredContent]\n{"value":1}'
    )


def test_adapter_accepts_structured_result_larger_than_schema_limit() -> None:
    """验证结构化结果不受工具 Schema 大小限制误截断。"""
    converted = MCPToolAdapter._convert_result(SimpleNamespace(
        isError=False,
        content=[],
        structuredContent={"value": "x" * (300 * 1024)},
    ))

    assert converted.text.startswith('{"value":"')
    assert "已忽略 structured content" not in converted.text


def test_adapter_and_normalizer_limit_content_parts_and_summarize_images(monkeypatch) -> None:
    """无效图片不得造成无界解码、原因列表或二次遍历。

    Args:
        monkeypatch (Any): Pytest 的依赖替换工具，用例结束后自动恢复。
    """
    class _RepeatedInvalidImages:
        """提供当前用例所需的 _RepeatedInvalidImages 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：无效图片不得造成无界解码、原因列表或二次遍历。
        """
        def __len__(self) -> int:
            """返回测试载荷的模拟长度，供检查结果大小限制。

            该定义仅用于外层用例：无效图片不得造成无界解码、原因列表或二次遍历。

            Returns:
                int: 供外层用例断言使用的预设结果。
            """
            return 100_000

        def __iter__(self):
            """按用例提供测试载荷的迭代结果，供检查内容转换。

            该定义仅用于外层用例：无效图片不得造成无界解码、原因列表或二次遍历。
            """
            for _ in range(100_000):
                yield SimpleNamespace(type="image", data="%%%", mimeType="image/png")

    decode_calls = 0
    original_decode = __import__(
        "app.agent.tools.results.normalizer", fromlist=["base64"]
    ).base64.b64decode

    def counted_decode(*args, **kwargs):
        """记录 Base64 解码次数后使用原解码器，验证不会重复解码图片。

        该定义仅用于外层用例：无效图片不得造成无界解码、原因列表或二次遍历。

        Args:
            args (dict[str, Any]): 当前工具调用或被包装函数的参数。
            kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
        """
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(*args, **kwargs)

    monkeypatch.setattr("app.agent.tools.results.normalizer.base64.b64decode", counted_decode)
    converted = MCPToolAdapter._convert_result(SimpleNamespace(
        isError=False,
        content=_RepeatedInvalidImages(),
    ))
    normalized = ToolResultNormalizer().normalize(converted)

    assert decode_calls == 1_000
    assert normalized.text.count("Base64 非法") == 1
    assert "已忽略 1000 张工具图片" in normalized.text
    assert "已忽略 99000 个 MCP content parts" in normalized.text


def test_adapter_does_not_preserve_untrusted_content_type_or_mime_values() -> None:
    """汇总原因必须使用固定类别，不能保存远端构造的任意字符串。"""
    parts = [
        SimpleNamespace(type="image", data="", mimeType=f"image/attacker-{index}")
        for index in range(500)
    ]
    parts.extend(SimpleNamespace(type=f"attacker-{index}") for index in range(500))

    converted = MCPToolAdapter._convert_result(SimpleNamespace(isError=False, content=parts))
    normalized = ToolResultNormalizer().normalize(converted)

    assert "image/attacker-" not in normalized.text
    assert "attacker-" not in normalized.text
    assert normalized.text.count("MIME 不支持") == 1
    assert normalized.text.count("不支持的 MCP 内容类型") == 1


@pytest.mark.parametrize("part_type", ["text", "audio", "resource", "unknown"])
def test_adapter_applies_content_part_limit_to_every_part_type(part_type: str) -> None:
    """文本、音频、资源和未知类型均必须受同一总数量限制。

    Args:
        part_type (str): 当前参数化用例要验证的 MCP 内容类别。
    """
    class _RepeatedParts:
        """提供当前用例所需的 _RepeatedParts 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：文本、音频、资源和未知类型均必须受同一总数量限制。
        """
        def __len__(self) -> int:
            """返回测试载荷的模拟长度，供检查结果大小限制。

            该定义仅用于外层用例：文本、音频、资源和未知类型均必须受同一总数量限制。

            Returns:
                int: 供外层用例断言使用的预设结果。
            """
            return 100_000

        def __iter__(self):
            """按用例提供测试载荷的迭代结果，供检查内容转换。

            该定义仅用于外层用例：文本、音频、资源和未知类型均必须受同一总数量限制。
            """
            for _ in range(100_000):
                yield SimpleNamespace(type=part_type, text="x")

    converted = MCPToolAdapter._convert_result(SimpleNamespace(
        isError=False,
        content=_RepeatedParts(),
    ))

    assert "已忽略 99000 个 MCP content parts" in converted.text
    if part_type != "text":
        assert "已忽略 1000 个不支持的 MCP 内容类型" in converted.text


def test_unified_image_normalizer_stops_at_mcp_content_part_limit(monkeypatch) -> None:
    """达到 content parts 上限后不得继续执行图片签名校验。

    Args:
        monkeypatch (Any): Pytest 的依赖替换工具，用例结束后自动恢复。
    """
    class _RepeatedImages:
        """提供当前用例所需的 _RepeatedImages 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：达到 content parts 上限后不得继续执行图片签名校验。
        """
        def __len__(self) -> int:
            """返回测试载荷的模拟长度，供检查结果大小限制。

            该定义仅用于外层用例：达到 content parts 上限后不得继续执行图片签名校验。

            Returns:
                int: 供外层用例断言使用的预设结果。
            """
            return 100_000

        def __iter__(self):
            """按用例提供测试载荷的迭代结果，供检查内容转换。

            该定义仅用于外层用例：达到 content parts 上限后不得继续执行图片签名校验。
            """
            for _ in range(100_000):
                yield SimpleNamespace(type="image", data="AA==", mimeType="image/png")

    signature_calls = 0

    def counted_signature(_mime_type: str, _data: bytes) -> bool:
        """记录图片文件头检查次数后调用原校验器。

        该定义仅用于外层用例：达到 content parts 上限后不得继续执行图片签名校验。

        Args:
            _mime_type (str): 图片载荷声明的媒体类型。
            _data (bytes): 需要解析或保存的结构化数据。

        Returns:
            bool: 供外层用例断言使用的预设结果。
        """
        nonlocal signature_calls
        signature_calls += 1
        return False

    monkeypatch.setattr(
        "app.agent.tools.results.normalizer._has_valid_signature",
        counted_signature,
    )
    converted = MCPToolAdapter._convert_result(SimpleNamespace(
        isError=False,
        content=_RepeatedImages(),
    ))
    normalized = ToolResultNormalizer().normalize(converted)

    assert signature_calls == 1_000
    assert "已忽略 1000 张工具图片：文件签名不匹配" in normalized.text
    assert "已忽略 99000 个 MCP content parts" in normalized.text


def test_tool_catalog_and_instructions_quotas() -> None:
    """超大工具目录在进入缓存前失败，instructions 则安全截断。"""
    from app.mcp.limits import limit_instructions, validate_tool_catalog

    tools = [
        SimpleNamespace(name=f"tool_{index}", description="", inputSchema={}, annotations={})
        for index in range(201)
    ]
    with pytest.raises(ValueError, match="工具数量"):
        validate_tool_catalog(tools)

    oversized_schema = SimpleNamespace(
        name="large",
        description="",
        inputSchema={"value": "x" * (256 * 1024 + 1)},
        annotations={},
    )
    with pytest.raises(ValueError, match="Schema"):
        validate_tool_catalog([oversized_schema])

    limited = limit_instructions("x" * 30_000)
    assert limited is not None and len(limited) < 21_000
    assert "instructions 已截断" in limited


@pytest.mark.asyncio
async def test_disabled_server_never_creates_session_on_connect_or_reconnect() -> None:
    """disabled 是 Manager 底层硬门禁，不得触发 session factory。"""
    calls: list[str] = []
    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="disabled", name="Disabled", enabled=False, command="python"
    )])

    @asynccontextmanager
    async def session_factory(server):
        """为连接管理器创建用例控制的 MCP 会话，记录连接及资源释放。

        该定义仅用于外层用例：disabled 是 Manager 底层硬门禁，不得触发 session factory。

        Args:
            server (Any): 用例提供的 MCP 服务配置。
        """
        calls.append(server.id)
        yield _FakeSession()

    manager = MCPConnectionManager(lambda: config, session_factory=session_factory)
    with pytest.raises(MCPServerDisabledError):
        await manager.connect("disabled")
    with pytest.raises(MCPServerDisabledError):
        await manager.reconnect("disabled")

    assert calls == []
    assert manager.get_status("disabled")["status"] == "disabled"


class _BoundTool(BaseTool):
    """提供用例指定的工具响应、中断或异常，验证执行管道的结果保存与恢复。"""
    name = "mcp__filesystem__read_file"
    description = "bound"
    parameters_schema = {"type": "object", "properties": {}}

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            args (dict): 当前工具调用或被包装函数的参数。
            context (ToolContext): 执行管道提供的工具运行上下文。

        Returns:
            ToolResult: 供外层用例断言使用的预设结果。
        """
        return ToolResult.success("ok")


@pytest.mark.asyncio
async def test_agent_builds_adapters_without_owning_connections(tmp_path: Path) -> None:
    """验证Agent 创建适配器但不接管连接生命周期。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    class _Manager:
        """提供当前用例所需的 _Manager 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证Agent 创建适配器但不接管连接生命周期。
        """
        def __init__(self) -> None:
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证Agent 创建适配器但不接管连接生命周期。
            """
            self.calls: list[str] = []
            self.config = StdioMCPServerConfig(
                id="filesystem", name="Filesystem", enabled=True, command="python"
            )

        def get_server_config(self, server_id: str):
            """读取测试目录中的 MCP 服务配置。

            该定义仅用于外层用例：验证Agent 创建适配器但不接管连接生命周期。

            Args:
                server_id (str): MCP 服务的稳定编号。
            """
            assert server_id == "filesystem"
            return self.config

        def get_server_identity(self, server_id: str) -> str:
            """返回测试指定的服务身份摘要。

            该定义仅用于外层用例：验证Agent 创建适配器但不接管连接生命周期。

            Args:
                server_id (str): MCP 服务的稳定编号。

            Returns:
                str: 供外层用例断言使用的预设结果。
            """
            assert server_id == "filesystem"
            return server_identity_fingerprint(self.config)

        async def connect(self, server_id: str):
            """记录服务连接请求，并提供用例指定的工具目录或失败。

            该定义仅用于外层用例：验证Agent 创建适配器但不接管连接生命周期。

            Args:
                server_id (str): MCP 服务的稳定编号。
            """
            self.calls.append(server_id)
            return [MCPToolDescriptor("read_file", "读取", {"type": "object"})]

    manager = _Manager()
    model_settings = MCPModelSettings(
        servers={"filesystem": MCPServerPolicy(
            enabled=True,
            identity_fingerprint=server_identity_fingerprint(manager.config),
        )}
    )
    settings = _chat_settings(
        mcp=model_settings,
        tool_permissions={"mcp:filesystem:read_file": "allow"},
    )
    agent = Agent(
        AgentConfig(
            session_id="mcp-agent",
            model_name="model",
            api_key="key",
            context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
            mcp=model_settings,
        ),
        db_path=str(tmp_path / "state.sqlite3"),
        mcp_connection_manager=manager,
        tool_authorization=ToolAuthorization(lambda _session_id: settings),
    )

    await agent.initialize()
    assert agent.tool_manager.has("mcp__filesystem__read_file")
    assert manager.calls == ["filesystem"]
    await agent.close()


@pytest.mark.asyncio
async def test_mcp_service_crud_exposes_runtime_status_and_cascades_policies(tmp_path: Path) -> None:
    """验证MCP 服务增删改查暴露运行状态并同步权限。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    mcp_file = tmp_path / "mcp_servers.yaml"
    chat_file = tmp_path / "chat_settings.yaml"
    chat_file.write_text("chat_models: []\n", encoding="utf-8")
    chat_dao = ChatSettingsDao(config_file=chat_file)
    chat_dao.add_chat_settings(
        _chat_settings(
            tool_permissions={"mcp:github:search": "ask"},
            mcp={
                "servers": {
                    "github": {
                        "enabled": True,
                    }
                }
            }
        )
    )

    class _Manager:
        """提供当前用例所需的 _Manager 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证MCP 服务增删改查暴露运行状态并同步权限。
        """
        def __init__(self) -> None:
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证MCP 服务增删改查暴露运行状态并同步权限。
            """
            self.disconnected: list[str] = []

        def get_status(self, _server_id: str):
            """返回脚本连接状态，替代真实 MCP 连接管理器。

            该定义仅用于外层用例：验证MCP 服务增删改查暴露运行状态并同步权限。

            Args:
                _server_id (str): MCP 服务的稳定编号。
            """
            return {"status": "available", "tool_count": 1, "last_error": None, "instructions": None}

        async def connect(self, _server_id: str):
            """记录服务连接请求，并提供用例指定的工具目录或失败。

            该定义仅用于外层用例：验证MCP 服务增删改查暴露运行状态并同步权限。

            Args:
                _server_id (str): MCP 服务的稳定编号。
            """
            return []

        async def disconnect(self, server_id: str):
            """记录服务断开和资源释放，用于检查连接生命周期。

            该定义仅用于外层用例：验证MCP 服务增删改查暴露运行状态并同步权限。

            Args:
                server_id (str): MCP 服务的稳定编号。
            """
            self.disconnected.append(server_id)

        async def test_config(self, _config):
            """返回用例指定的服务配置测试结果，避免连接真实 MCP 服务。

            该定义仅用于外层用例：验证MCP 服务增删改查暴露运行状态并同步权限。

            Args:
                _config (Any): 调用方提供的配置。
            """
            return {"status": "available", "tools": []}

        def get_tools(self, _server_id: str):
            """返回本次连接的测试工具目录。

            该定义仅用于外层用例：验证MCP 服务增删改查暴露运行状态并同步权限。

            Args:
                _server_id (str): MCP 服务的稳定编号。
            """
            return []

    manager = _Manager()
    service = MCPService(MCPSettingsDao(mcp_file), chat_dao, manager)
    server = StdioMCPServerConfig(
        id="github",
        name="GitHub",
        enabled=True,
        transport="stdio",
        command="npx",
        env={"GITHUB_TOKEN": "plain-secret"},
    )

    created = await service.create_server(server)
    assert created["config"]["env"]["GITHUB_TOKEN"] == "plain-secret"
    assert created["runtime"]["status"] == "available"
    assert created["affected_model_count"] == 1
    assert len(service.list_servers()) == 1

    affected = await service.delete_server("github")
    assert affected == ["session-a"]
    assert manager.disconnected == ["github"]
    assert MCPSettingsDao(mcp_file).load().servers == []
    assert ChatSettingsDao(chat_file).get_chat_settings("session-a").mcp.servers == {}


@pytest.mark.asyncio
async def test_identity_change_rebinds_all_current_tools_to_ask(tmp_path: Path) -> None:
    """验证服务身份变化将当前工具权限重新绑定为 ask。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    mcp_file = tmp_path / "mcp_servers.yaml"
    chat_file = tmp_path / "chat_settings.yaml"
    old_server = StdioMCPServerConfig(
        id="filesystem", name="Filesystem", enabled=True, command="old-command"
    )
    MCPSettingsDao(mcp_file).save(MCPServersConfig(servers=[old_server]))
    chat_file.write_text("chat_models: []\n", encoding="utf-8")
    chat_dao = ChatSettingsDao(chat_file)
    chat_dao.add_chat_settings(_chat_settings(
        tool_permissions={"mcp:filesystem:read_file": "allow", "mcp:filesystem:old_tool": "allow"},
        mcp={"servers": {
        "filesystem": {
            "enabled": True,
            "identity_fingerprint": server_identity_fingerprint(old_server),
        }
    }}))

    class _Manager:
        """提供当前用例所需的 _Manager 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证服务身份变化将当前工具权限重新绑定为 ask。
        """
        async def reconnect(self, _server_id: str):
            """模拟服务重新连接，供检查路由是否调用正确服务。

            该定义仅用于外层用例：验证服务身份变化将当前工具权限重新绑定为 ask。

            Args:
                _server_id (str): MCP 服务的稳定编号。
            """
            return [
                MCPToolDescriptor("read_file", "read", {"type": "object"}),
                MCPToolDescriptor("new_tool", "new", {"type": "object"}),
            ]

        def get_status(self, _server_id: str):
            """返回脚本连接状态，替代真实 MCP 连接管理器。

            该定义仅用于外层用例：验证服务身份变化将当前工具权限重新绑定为 ask。

            Args:
                _server_id (str): MCP 服务的稳定编号。
            """
            return {"status": "available", "tool_count": 2, "last_error": None, "instructions": None}

        async def disconnect(self, _server_id: str):
            """记录服务断开和资源释放，用于检查连接生命周期。

            该定义仅用于外层用例：验证服务身份变化将当前工具权限重新绑定为 ask。

            Args:
                _server_id (str): MCP 服务的稳定编号。
            """
            pass

    service = MCPService(MCPSettingsDao(mcp_file), chat_dao, _Manager())
    new_server = old_server.model_copy(update={"command": "new-command"})

    await service.update_server("filesystem", new_server)

    saved = ChatSettingsDao(chat_file).get_chat_settings("session-a")
    policy = saved.mcp.servers["filesystem"]
    assert policy.enabled is True
    assert {
        key: value
        for key, value in saved.tool_permissions.items()
        if key.startswith("mcp:filesystem:")
    } == {
        "mcp:filesystem:read_file": ToolPolicy.ASK,
        "mcp:filesystem:new_tool": ToolPolicy.ASK,
    }
    assert policy.identity_fingerprint == server_identity_fingerprint(new_server)


@pytest.mark.asyncio
async def test_chat_settings_save_filters_deleted_servers_and_downgrades_unbound_allow(
    tmp_path: Path,
) -> None:
    """验证保存会话设置时过滤已删除服务并降级未绑定许可。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    mcp_file = tmp_path / "mcp_servers.yaml"
    chat_file = tmp_path / "chat_settings.yaml"
    server = StdioMCPServerConfig(
        id="filesystem", name="Filesystem", enabled=True, command="python"
    )
    MCPSettingsDao(mcp_file).save(MCPServersConfig(servers=[server]))
    chat_file.write_text("chat_models: []\n", encoding="utf-8")
    chat_dao = ChatSettingsDao(chat_file)
    chat_dao.add_chat_settings(_chat_settings())

    class _Manager:
        """提供当前用例所需的 _Manager 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证保存会话设置时过滤已删除服务并降级未绑定许可。
        """
        tool_names = ["read_file"]

        def get_available_identity(self, _server_id: str):
            """返回测试连接当前可用的服务身份。

            该定义仅用于外层用例：验证保存会话设置时过滤已删除服务并降级未绑定许可。

            Args:
                _server_id (str): MCP 服务的稳定编号。
            """
            return server_identity_fingerprint(server)

        def get_tools(self, _server_id: str):
            """返回本次连接的测试工具目录。

            该定义仅用于外层用例：验证保存会话设置时过滤已删除服务并降级未绑定许可。

            Args:
                _server_id (str): MCP 服务的稳定编号。
            """
            return [
                MCPToolDescriptor(name, name, {"type": "object"})
                for name in self.tool_names
            ]

    manager = _Manager()
    service = ChatSettingsService(
        chat_dao,
        MCPSettingsDao(mcp_file),
        manager,
    )
    incoming = _chat_settings(
        tool_permissions={"mcp:filesystem:read_file": "allow", "mcp:deleted:danger": "allow"},
        mcp={"servers": {"filesystem": {"enabled": True}, "deleted": {"enabled": True}}},
    )

    await service.update_chat_settings(incoming)

    saved = ChatSettingsDao(chat_file).get_chat_settings("session-a")
    assert set(saved.mcp.servers) == {"filesystem"}
    assert saved.tool_permissions["mcp:filesystem:read_file"] == ToolPolicy.ASK
    assert "mcp:deleted:danger" not in saved.tool_permissions
    assert (
        saved.mcp.servers["filesystem"].identity_fingerprint
        == server_identity_fingerprint(server)
    )

    manager.tool_names = ["read_file", "new_tool"]
    bound_update = _chat_settings(
        tool_permissions={"mcp:filesystem:read_file": "allow", "mcp:filesystem:new_tool": "allow"},
        mcp={"servers": {"filesystem": {
            "enabled": True,
            "identity_fingerprint": server_identity_fingerprint(server),
        }}},
    )
    await service.update_chat_settings(bound_update)
    rebound = ChatSettingsDao(chat_file).get_chat_settings("session-a")
    assert {
        key: value
        for key, value in rebound.tool_permissions.items()
        if key.startswith("mcp:filesystem:")
    } == {
        "mcp:filesystem:read_file": ToolPolicy.ALLOW,
        "mcp:filesystem:new_tool": ToolPolicy.ASK,
    }

    MCPSettingsDao(mcp_file).save(MCPServersConfig())
    await service.update_chat_settings(bound_update)
    assert ChatSettingsDao(chat_file).get_chat_settings("session-a").mcp.servers == {}


@pytest.mark.asyncio
async def test_concurrent_server_delete_and_stale_chat_settings_put_cannot_restore_policy(
    tmp_path: Path,
) -> None:
    """验证并发删除服务与旧设置保存不能恢复已删除权限。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    import asyncio

    mcp_file = tmp_path / "mcp_servers.yaml"
    chat_file = tmp_path / "chat_settings.yaml"
    server = StdioMCPServerConfig(
        id="filesystem", name="Filesystem", enabled=True, command="python"
    )
    MCPSettingsDao(mcp_file).save(MCPServersConfig(servers=[server]))
    chat_file.write_text("chat_models: []\n", encoding="utf-8")
    chat_dao = ChatSettingsDao(chat_file)
    original = _chat_settings(tool_permissions={"mcp:filesystem:read_file": "allow"}, mcp={"servers": {"filesystem": {
        "enabled": True,
        "identity_fingerprint": server_identity_fingerprint(server),
    }}})
    chat_dao.add_chat_settings(original)

    class _Manager:
        """提供当前用例所需的 _Manager 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证并发删除服务与旧设置保存不能恢复已删除权限。
        """
        async def disconnect(self, _server_id: str):
            """记录服务断开和资源释放，用于检查连接生命周期。

            该定义仅用于外层用例：验证并发删除服务与旧设置保存不能恢复已删除权限。

            Args:
                _server_id (str): MCP 服务的稳定编号。
            """
            await asyncio.sleep(0)

        def get_available_identity(self, _server_id: str):
            """返回测试连接当前可用的服务身份。

            该定义仅用于外层用例：验证并发删除服务与旧设置保存不能恢复已删除权限。

            Args:
                _server_id (str): MCP 服务的稳定编号。
            """
            return server_identity_fingerprint(server)

        def get_tools(self, _server_id: str):
            """返回本次连接的测试工具目录。

            该定义仅用于外层用例：验证并发删除服务与旧设置保存不能恢复已删除权限。

            Args:
                _server_id (str): MCP 服务的稳定编号。
            """
            return [MCPToolDescriptor("read_file", "read", {"type": "object"})]

    coordinator = SettingsMutationCoordinator()
    manager = _Manager()
    mcp_service = MCPService(
        MCPSettingsDao(mcp_file), chat_dao, manager, coordinator
    )
    chat_service = ChatSettingsService(
        chat_dao, MCPSettingsDao(mcp_file), manager, coordinator
    )

    await asyncio.gather(
        mcp_service.delete_server("filesystem"),
        chat_service.update_chat_settings(original),
    )

    assert ChatSettingsDao(chat_file).get_chat_settings("session-a").mcp.servers == {}
