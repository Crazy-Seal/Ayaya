
from typing import cast
from tests.model_capture import capture_input
from pathlib import Path

import yaml

from app.agent.tools.authorization.manager import ToolAuthorization, descriptor_for_tool
from app.agent.tools.authorization.catalog import (
    LocalToolCatalogAdapter,
    MCPToolCatalogAdapter,
    UnifiedToolCatalog,
)
from app.agent.tools.authorization.models import (
    AuthorizationRequired,
    Granted,
    ToolDescriptor,
    ToolIdentity,
    ToolInvocation,
)
from app.agent.tools.core.base import InterruptEvent, ToolResult
from app.agent.core.tool_manager import ToolManager
from app.agent.context import ContextStrategyConfig, ModelInputCoordinator
from app.agent.core.events import EventType
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.core.plugin_manager import PluginManager
from app.agent.plugins.base import PluginHook
from app.agent.models.llm_client import StreamChunk
from app.agent.state import AgentState
from app.agent.state.messages import ToolCall, ToolMessage
from app.agent.tools.core.base import BaseTool, ToolContext
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.mcp.identity import server_identity_fingerprint
from app.mcp.types import MCPToolDescriptor
from app.schemas.chat_settings import ChatSettings
from app.schemas.mcp import MCPServersConfig, StdioMCPServerConfig
from app.schemas.tool_permissions import ToolPolicy
from app.services.settings_mutation import SettingsMutationCoordinator
from app.services.tool_permission_service import ToolPermissionService


def _configured_model() -> dict:
    """构造带本用例工具权限的会话设置。

    Returns:
        dict: 供外层用例断言使用的预设结果。
    """
    return {
        "session_id": "configured-session",
        "model_name": "test-model",
        "openai_api_key": "test-key",
        "openai_base_url": "http://127.0.0.1:1/v1",
        "temperature": 0.1,
        "system_prompt": "test",
        "tool_permissions": {
            "local:read_file": "allow",
            "local:screenshot": "deny",
            "mcp:filesystem:read": "allow",
        },
        "tool_permissions_schema_version": 1,
        "context_strategy": {"compression_trigger_tokens_k": 100},
        "mcp": {
            "servers": {
                "filesystem": {
                    "enabled": True,
                    "identity_fingerprint": "a" * 64,
                }
            }
        },
    }


def test_tool_identity_uses_source_specific_stable_keys() -> None:
    """验证工具身份按本地和 MCP 来源使用独立稳定键。"""
    assert ToolIdentity.local("read_file").stable_key == "local:read_file"
    assert (
        ToolIdentity.mcp("filesystem", "read").stable_key
        == "mcp:filesystem:read"
    )


def test_new_local_tool_defaults_to_ask_without_overwriting_saved_permissions(
    tmp_path: Path, monkeypatch,
) -> None:
    """验证新工具默认 ask 且不覆盖已有权限。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    from app.agent.tools.core.registry import ToolRegistry
    from app.services.chat_settings_service import ChatSettingsService

    monkeypatch.setattr(
        ToolRegistry,
        "list_tools",
        lambda: ["read_file", "screenshot", "exec_command"],
    )
    config_file = tmp_path / "chat_settings.yaml"
    config_file.write_text(
        yaml.safe_dump({"chat_models": [_configured_model()]}, sort_keys=False),
        encoding="utf-8",
    )
    dao = ChatSettingsDao(config_file=config_file)
    service = ChatSettingsService(dao)
    settings = service.get_chat_settings_by_session("configured-session")

    assert settings.tool_permissions["local:read_file"] == ToolPolicy.ALLOW
    assert settings.tool_permissions["local:screenshot"] == ToolPolicy.DENY
    assert settings.tool_permissions["local:exec_command"] == ToolPolicy.ASK
    assert settings.tool_permissions["mcp:filesystem:read"] == ToolPolicy.ALLOW

    import asyncio
    asyncio.run(service.update_chat_settings(settings))
    stored = yaml.safe_load(config_file.read_text(encoding="utf-8"))["chat_models"][0]
    assert stored["tool_permissions_schema_version"] == 1
    assert stored["tool_permissions"]["local:exec_command"] == "ask"
    before = config_file.read_bytes()
    ChatSettingsDao(config_file=config_file).get_chat_settings("configured-session")
    assert config_file.read_bytes() == before


def test_sample_permissions_keep_memory_queries_under_memory_plugin() -> None:
    """示例把记忆查询权限保存在插件中，独立工具仅预先允许联网。"""
    sample_path = Path(__file__).parents[1] / "config" / "chat_settings.sample.yaml"
    model = yaml.safe_load(sample_path.read_text(encoding="utf-8"))["chat_models"][0]
    permissions = model["tool_permissions"]
    assert {name for name, policy in permissions.items() if policy == "allow"} == {
        "local:access_the_internet",
    }
    assert {name for name, policy in permissions.items() if policy == "ask"} == set()
    assert all(policy == "deny" for name, policy in permissions.items()
               if name != "local:access_the_internet")
    assert model["agent_plugins"]["memory"]["tool_permissions"] == {
        "local:search_memory": "allow", "local:search_diary": "allow",
    }
    assert "local:search_memory" not in permissions and "local:search_diary" not in permissions


def test_permission_service_groups_local_and_mcp_tools_and_marks_unavailable(
    tmp_path: Path,
) -> None:
    """验证权限服务合并本地和 MCP 目录并显示不可用原因。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    import asyncio

    async def scenario() -> None:
        """由外层测试调用，完成“权限服务合并本地和 MCP 目录并显示不可用原因”的异步步骤。"""
        chat_file = tmp_path / "chat_settings.yaml"
        chat_file.write_text("chat_models: []\n", encoding="utf-8")
        mcp_file = tmp_path / "mcp_servers.yaml"
        server = StdioMCPServerConfig(
            id="filesystem",
            name="Filesystem",
            enabled=True,
            command="python",
        )
        mcp_dao = MCPSettingsDao(mcp_file)
        mcp_dao.save(MCPServersConfig(servers=[server]))
        chat_dao = ChatSettingsDao(chat_file)
        chat_dao.add_chat_settings(ChatSettings(
            session_id="session",
            model_name="model",
            openai_api_key="key",
            openai_base_url="http://127.0.0.1:1/v1",
            temperature=0.1,
            system_prompt="test",
            context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
            tool_permissions={
                "local:read_file": ToolPolicy.ALLOW,
                "mcp:filesystem:read": ToolPolicy.ASK,
            },
            mcp={
                "servers": {
                    "filesystem": {
                        "enabled": True,
                        "identity_fingerprint": server_identity_fingerprint(server),
                    }
                }
            },
        ))

        class Manager:
            """提供当前用例所需的 Manager 测试替身，执行记录仅供外层断言使用。

            该定义仅用于外层用例：验证权限服务合并本地和 MCP 目录并显示不可用原因。
            """
            available = True

            def get_available_identity(self, _server_id: str) -> str | None:
                """返回测试连接当前可用的服务身份。

                该定义仅用于外层用例：验证权限服务合并本地和 MCP 目录并显示不可用原因。

                Args:
                    _server_id (str): MCP 服务的稳定编号。

                Returns:
                    str | None: 供外层用例断言使用的预设结果。
                """
                return server_identity_fingerprint(server) if self.available else None

            def get_tools(self, _server_id: str) -> list[MCPToolDescriptor]:
                """返回本次连接的测试工具目录。

                该定义仅用于外层用例：验证权限服务合并本地和 MCP 目录并显示不可用原因。

                Args:
                    _server_id (str): MCP 服务的稳定编号。

                Returns:
                    list[MCPToolDescriptor]: 当前用例预设的回调时机或工具目录。
                """
                return [MCPToolDescriptor("read", "读取远端文件", {"type": "object"})]

        manager = Manager()
        service = ToolPermissionService(
            chat_dao,
            UnifiedToolCatalog(
                LocalToolCatalogAdapter(),
                MCPToolCatalogAdapter(mcp_dao, manager),
            ),
            SettingsMutationCoordinator(),
        )

        policies = service.list_policies("session")
        by_key = {item.stable_key: item for item in policies}
        assert by_key["local:read_file"].source == "local"
        assert by_key["mcp:filesystem:read"].server_name == "Filesystem"
        assert by_key["mcp:filesystem:read"].available is True

        manager.available = False
        unavailable = {
            item.stable_key: item for item in service.list_policies("session")
        }["mcp:filesystem:read"]
        assert unavailable.available is False
        assert unavailable.unavailable_reason

        await service.set_policies(
            "session",
            {"local:read_file": ToolPolicy.DENY},
        )
        assert (
            chat_dao.get_chat_settings("session").tool_permissions["local:read_file"]
            == ToolPolicy.DENY
        )
        await service.set_policy(
            "session",
            ToolIdentity.local("read_file"),
            ToolPolicy.ALLOW,
        )
        assert (
            chat_dao.get_chat_settings("session").tool_permissions["local:read_file"]
            == ToolPolicy.ALLOW
        )

    asyncio.run(scenario())


def _descriptor(name: str = "read_file") -> ToolDescriptor:
    """构造用于批准绑定检查的工具身份及定义。

    Args:
        name (str): 当前对象的名称。

    Returns:
        ToolDescriptor: 供外层用例断言使用的预设结果。
    """
    return ToolDescriptor(
        identity=ToolIdentity.local(name),
        exposed_name=name,
        display_name=name,
        description="读取文件",
        input_schema={"type": "object"},
        contract_fingerprint="contract-v1",
    )


def _authorization(policy: ToolPolicy) -> ToolAuthorization:
    """装配使用测试设置读取函数的工具授权对象。

    Args:
        policy (ToolPolicy): 本用例指定的权限或压缩策略。

    Returns:
        ToolAuthorization: 供外层用例断言使用的预设结果。
    """
    class Settings:
        """提供当前用例所需的 Settings 测试替身，执行记录仅供外层断言使用。"""
        tool_permissions = {"local:read_file": policy}
        mcp = type("Mcp", (), {"servers": {}})()

    return ToolAuthorization(lambda _session_id: Settings())


def test_ask_policy_returns_one_generic_authorization_request() -> None:
    """验证ask 权限生成一份通用审批请求。"""
    authorization = _authorization(ToolPolicy.ASK)
    invocation = ToolInvocation(
        session_id="session",
        tool_call_id="call-1",
        descriptor=_descriptor(),
        arguments={"path": "notes.txt"},
    )

    outcome = authorization.authorize(invocation)

    assert isinstance(outcome, AuthorizationRequired)
    assert outcome.interrupt.type == "authorization_request"
    assert outcome.interrupt.data["tool_call_id"] == "call-1"
    assert outcome.interrupt.data["tool_identity"] == "local:read_file"
    assert outcome.interrupt.data["arguments"] == {"path": "notes.txt"}
    assert outcome.interrupt.data["contract_fingerprint"] == "contract-v1"


def test_approval_is_bound_to_the_original_arguments() -> None:
    """验证批准绑定原调用参数不能用于变化后的参数。"""
    authorization = _authorization(ToolPolicy.ASK)
    invocation = ToolInvocation(
        session_id="session",
        tool_call_id="call-1",
        descriptor=_descriptor(),
        arguments={"path": "notes.txt"},
    )
    pending = authorization.authorize(invocation)
    assert isinstance(pending, AuthorizationRequired)

    changed = ToolInvocation(
        session_id="session",
        tool_call_id="call-1",
        descriptor=_descriptor(),
        arguments={"path": "secrets.txt"},
    )
    outcome = authorization.resolve_authorization(
        changed,
        decision={"kind": "approve"},
        authorization_snapshot=pending.resume_state,
    )

    assert isinstance(outcome, ToolResult)
    assert "已过期" in outcome.text


def test_explicit_rejection_wins_if_policy_changes_to_allow() -> None:
    """验证明确拒绝不因权限改为 allow 被覆盖。"""
    class Settings:
        """提供当前用例所需的 Settings 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证明确拒绝不因权限改为 allow 被覆盖。
        """
        def __init__(self) -> None:
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证明确拒绝不因权限改为 allow 被覆盖。
            """
            self.tool_permissions = {"local:read_file": ToolPolicy.ASK}
            self.mcp = type("Mcp", (), {"servers": {}})()

    settings = Settings()
    authorization = ToolAuthorization(lambda _session_id: settings)
    invocation = ToolInvocation(
        session_id="session",
        tool_call_id="call-1",
        descriptor=_descriptor(),
        arguments={"path": "notes.txt"},
    )
    pending = authorization.authorize(invocation)
    assert isinstance(pending, AuthorizationRequired)

    settings.tool_permissions["local:read_file"] = ToolPolicy.ALLOW
    outcome = authorization.resolve_authorization(
        invocation,
        decision={"kind": "reject"},
        authorization_snapshot=pending.resume_state,
    )

    assert isinstance(outcome, ToolResult)
    assert "拒绝" in outcome.text


def test_local_description_and_authorization_timing_are_part_of_approval_contract() -> None:
    """验证本地工具说明和授权时机纳入审批约束。"""
    class Tool:
        """提供当前用例所需的 Tool 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证本地工具说明和授权时机纳入审批约束。
        """
        name = "read_file"
        display_name = "读取文件"
        description = "读取一个文件"
        parameters_schema = {"type": "object"}
        authorization_timing = "before_execute"

    original = descriptor_for_tool(cast(BaseTool, Tool()))
    Tool.description = "读取多个文件"
    changed_description = descriptor_for_tool(cast(BaseTool, Tool()))
    Tool.description = "读取一个文件"
    Tool.authorization_timing = "after_prepare"
    changed_timing = descriptor_for_tool(cast(BaseTool, Tool()))

    assert original.contract_fingerprint != changed_description.contract_fingerprint
    assert original.contract_fingerprint != changed_timing.contract_fingerprint


def test_allow_and_valid_approval_produce_granted_outcomes() -> None:
    """验证allow 或有效批准产生可执行结果。"""
    invocation = ToolInvocation(
        session_id="session",
        tool_call_id="call-1",
        descriptor=_descriptor(),
        arguments={"path": "notes.txt"},
    )
    assert isinstance(_authorization(ToolPolicy.ALLOW).authorize(invocation), Granted)

    asking = _authorization(ToolPolicy.ASK)
    pending = asking.authorize(invocation)
    assert isinstance(pending, AuthorizationRequired)
    approved = asking.resolve_authorization(
        invocation,
        decision={"kind": "approve"},
        authorization_snapshot=pending.resume_state,
    )
    assert isinstance(approved, Granted)
    assert approved.grant is not None


def test_mcp_policy_reads_live_unified_settings_and_fails_closed() -> None:
    """验证MCP 权限读取实时设置且缺失时拒绝执行。"""
    identity = ToolIdentity.mcp("filesystem", "read")
    settings = ChatSettings(
        session_id="session",
        model_name="model",
        openai_api_key="key",
        openai_base_url="http://127.0.0.1:1/v1",
        temperature=0.1,
        system_prompt="test",
        context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        tool_permissions={identity.stable_key: ToolPolicy.ALLOW},
        mcp={"servers": {"filesystem": {"enabled": True}}},
    )
    authorization = ToolAuthorization(lambda _session_id: settings)

    assert authorization.get_policy("session", identity) == ToolPolicy.ALLOW
    settings.mcp.servers["filesystem"].enabled = False
    assert authorization.get_policy("session", identity) == ToolPolicy.DENY

    def broken_loader(_session_id: str):
        """主动制造设置读取失败，验证授权无法确认时拒绝执行。

        该定义仅用于外层用例：验证MCP 权限读取实时设置且缺失时拒绝执行。

        Args:
            _session_id (str): 当前操作所属的会话编号。
        """
        raise RuntimeError("配置损坏")

    assert ToolAuthorization(broken_loader).get_policy("session", identity) == ToolPolicy.DENY


def test_denied_tools_are_filtered_from_model_projection() -> None:
    """验证deny 工具不公开给模型。"""
    class Tool:
        """提供当前用例所需的 Tool 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证deny 工具不公开给模型。
        """
        name = "read_file"
        description = "读取文件"
        parameters_schema = {"type": "object", "properties": {}}

        def definition(self):
            """提供用例使用的模型工具定义。

            该定义仅用于外层用例：验证deny 工具不公开给模型。

            Returns:
                dict: 供外层用例断言使用的预设结果。
            """
            return {"type": "function", "function": {"name": self.name}}

    manager = ToolManager(
        session_id="session",
        authorization=_authorization(ToolPolicy.DENY),
    )
    manager.register(Tool())

    assert manager.get_tools() == []


class _CountingTool(BaseTool):
    """提供用例指定的工具响应、中断或异常，验证执行管道的结果保存与恢复。"""
    name = "read_file"
    description = "读取文件"
    parameters_schema = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。"""
        self.calls = 0

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            args (dict): 当前工具调用或被包装函数的参数。
            context (ToolContext): 执行管道提供的工具运行上下文。

        Returns:
            ToolResult: 供外层用例断言使用的预设结果。
        """
        self.calls += 1
        return ToolResult.success("ok")


class _UnsafePrepareTool(_CountingTool):
    """提供用例指定的工具响应、中断或异常，验证执行管道的结果保存与恢复。"""
    authorization_timing = "after_prepare"

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            args (dict): 当前工具调用或被包装函数的参数。
            context (ToolContext): 执行管道提供的工具运行上下文。

        Returns:
            ToolResult: 供外层用例断言使用的预设结果。
        """
        self.calls += 1
        return ToolResult(
            content="",
            interrupt=InterruptEvent.create("unsupported_execution", "不应在授权前执行",
                data={"action": "click", "x": 1, "y": 2}),
        )


class _PreparedTool(_CountingTool):
    """提供用例指定的工具响应、中断或异常，验证执行管道的结果保存与恢复。"""
    authorization_timing = "after_prepare"

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            args (dict): 当前工具调用或被包装函数的参数。
            context (ToolContext): 执行管道提供的工具运行上下文。

        Returns:
            ToolResult: 供外层用例断言使用的预设结果。
        """
        self.calls += 1
        if context.resume_data == {"kind": "authorization_granted"}:
            return ToolResult.success("performed")
        return ToolResult.prepared(
            {"action": "click", "x": 1, "y": 2},
            resume_state={"prepared": True},
        )


class _RecordingPlugins:
    """提供当前用例所需的 _RecordingPlugins 测试替身，执行记录仅供外层断言使用。"""
    def __init__(self) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。"""
        self.hooks: list[PluginHook] = []

    async def run_hooks(self, hook: PluginHook, *_args, **_kwargs) -> None:
        """记录工具执行前后钩子触发，供检查审批等待与拒绝边界。

        Args:
            hook (PluginHook): 当前触发的插件执行时机。
            _args (dict[str, Any]): 当前工具调用或被包装函数的参数。
            _kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
        """
        self.hooks.append(hook)


class _ScriptedLlm:
    """提供按用例脚本输出的模型响应，验证流式处理、摘要或失败恢复，不调用真实模型。"""
    def __init__(self) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。"""
        self.scripts = [
            [StreamChunk(tool_call=ToolCall(id="call-1", name="read_file", args={}))],
            [StreamChunk(content="done")],
        ]

    async def astream(self, messages, tools=None):
        """模型管道调用时按测试脚本顺序产出片段或异常，供外层检查消息和用量处理。

        Args:
            messages (list[dict]): 按对话顺序排列的消息集合。
            tools (list[dict] | None): 当前向模型公开的工具定义。
        """
        messages = capture_input(messages)
        for chunk in self.scripts.pop(0):
            yield chunk


def _pipeline(policy: ToolPolicy) -> tuple[ExecutionPipeline, _CountingTool]:
    """装配显式测试依赖的执行管道，不调用真实模型。

    Args:
        policy (ToolPolicy): 本用例指定的权限或压缩策略。

    Returns:
        tuple[ExecutionPipeline, _CountingTool]: 供外层用例断言使用的预设结果。
    """
    llm = _ScriptedLlm()
    authorization = _authorization(policy)
    manager = ToolManager(session_id="session", authorization=authorization)
    tool = _CountingTool()
    manager.register(tool)
    pipeline = ExecutionPipeline(
        prompt_source=type("Prompt", (), {"system_prompt": "test"})(),
        llm_client=llm,
        tool_manager=manager,
        plugin_manager=PluginManager(),
        model_input=ModelInputCoordinator(
            ContextStrategyConfig(compression_trigger_tokens_k=100),
            llm,
            accounting_key="test",
        ),
        tool_authorization=authorization,
    )
    return pipeline, tool


async def _events(stream) -> list:
    """完整消费模型工具管道事件，供检查审批和结果顺序。

    Args:
        stream (Any): 需要消费的 Agent 或模型事件流。

    Returns:
        list: 供外层用例断言使用的预设结果。
    """
    return [event async for event in stream]


def test_pipeline_waits_for_approval_before_executing_ask_tool() -> None:
    """验证ask 工具先等待批准再执行。"""
    import asyncio

    async def scenario() -> None:
        """由外层测试调用，完成“ask 工具先等待批准再执行”的异步步骤。"""
        pipeline, tool = _pipeline(ToolPolicy.ASK)
        state = AgentState(session_id="session")
        state.add_user_message("read")

        events = await _events(pipeline.execute(state))

        assert tool.calls == 0
        assert events[-1].type == EventType.INTERRUPT
        assert events[-1].data["type"] == "authorization_request"

        await _events(pipeline.resume_tools(state, {"kind": "approve"}))
        assert tool.calls == 1

    asyncio.run(scenario())


def test_pipeline_rechecks_deny_for_a_stale_model_tool_call() -> None:
    """验证旧模型调用执行前重新核对 deny 权限。"""
    import asyncio

    async def scenario() -> None:
        """由外层测试调用，完成“旧模型调用执行前重新核对 deny 权限”的异步步骤。"""
        pipeline, tool = _pipeline(ToolPolicy.DENY)
        state = AgentState(session_id="session")
        state.add_user_message("read")

        await _events(pipeline.execute(state))

        assert tool.calls == 0
        tool_messages = [message for message in state.messages if isinstance(message, ToolMessage)]
        assert "不允许" in tool_messages[0].text

    asyncio.run(scenario())


def test_after_prepare_tool_cannot_request_protected_effect_before_authorization() -> None:
    """验证准备后授权工具不能在批准前请求受保护动作。"""
    import asyncio

    async def scenario() -> None:
        """由外层测试调用，完成“准备后授权工具不能在批准前请求受保护动作”的异步步骤。"""
        llm = _ScriptedLlm()
        authorization = _authorization(ToolPolicy.ASK)
        manager = ToolManager(session_id="session", authorization=authorization)
        tool = _UnsafePrepareTool()
        manager.register(tool)
        pipeline = ExecutionPipeline(
            prompt_source=type("Prompt", (), {"system_prompt": "test"})(),
            llm_client=llm,
            tool_manager=manager,
            plugin_manager=PluginManager(),
            model_input=ModelInputCoordinator(
                ContextStrategyConfig(compression_trigger_tokens_k=100),
                llm,
                accounting_key="test",
            ),
            tool_authorization=authorization,
        )
        state = AgentState(session_id="session")
        state.add_user_message("act")

        events = await _events(pipeline.execute(state))

        assert tool.calls == 1
        assert all(event.type != EventType.INTERRUPT for event in events)
        tool_messages = [message for message in state.messages if isinstance(message, ToolMessage)]
        assert "只能请求只读准备数据" in tool_messages[0].text

    asyncio.run(scenario())


def test_after_prepare_hooks_span_the_logical_call_and_rejection_has_no_after_hook() -> None:
    """验证准备后授权钩子围绕完整调用且拒绝无执行后钩子。"""
    import asyncio

    async def build_case():
        """装配当前授权测试所需的工具、插件和调用参数。

        该定义仅用于外层用例：验证准备后授权钩子围绕完整调用且拒绝无执行后钩子。
        """
        llm = _ScriptedLlm()
        authorization = _authorization(ToolPolicy.ASK)
        manager = ToolManager(session_id="session", authorization=authorization)
        tool = _PreparedTool()
        manager.register(tool)
        plugins = _RecordingPlugins()
        pipeline = ExecutionPipeline(
            prompt_source=type("Prompt", (), {"system_prompt": "test"})(),
            llm_client=llm,
            tool_manager=manager,
            plugin_manager=plugins,
            model_input=ModelInputCoordinator(
                ContextStrategyConfig(compression_trigger_tokens_k=100),
                llm,
                accounting_key="test",
            ),
            tool_authorization=authorization,
        )
        state = AgentState(session_id="session")
        call = ToolCall(id="call-1", name="read_file", args={})
        pending = await pipeline._execute_tool(state, call)
        return pipeline, state, call, pending, plugins

    async def scenario() -> None:
        """由外层测试调用，完成“准备后授权钩子围绕完整调用且拒绝无执行后钩子”的异步步骤。"""
        rejected_pipeline, rejected_state, rejected_call, pending, rejected_plugins = (
            await build_case()
        )
        assert pending.interrupt and pending.interrupt.type == "authorization_request"
        rejected = await rejected_pipeline._execute_tool(
            rejected_state,
            rejected_call,
            resume_data={"kind": "reject"},
            resume_state=pending.resume_state,
        )
        assert "拒绝" in rejected.text
        assert rejected_plugins.hooks == [PluginHook.BEFORE_TOOL]

        approved_pipeline, approved_state, approved_call, pending, approved_plugins = (
            await build_case()
        )
        approved = await approved_pipeline._execute_tool(
            approved_state,
            approved_call,
            resume_data={"kind": "approve"},
            resume_state=pending.resume_state,
        )
        assert approved.text == "performed"
        assert approved_plugins.hooks == [PluginHook.BEFORE_TOOL, PluginHook.AFTER_TOOL]

    asyncio.run(scenario())
