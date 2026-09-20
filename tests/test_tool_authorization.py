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
from app.agent.tools.base import ToolResult
from app.agent.core.tool_manager import ToolManager
from app.agent.context import ContextStrategyConfig, ModelInputCoordinator
from app.agent.core.event_router import EventType
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.core.plugin_manager import PluginManager
from app.agent.plugins.base import PluginHook
from app.agent.models.llm_client import StreamChunk
from app.agent.state import AgentState
from app.agent.state.messages import ToolCall, ToolMessage
from app.agent.tools.base import BaseTool, ToolContext
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.mcp.identity import server_identity_fingerprint
from app.mcp.types import MCPToolDescriptor
from app.schemas.chat_settings import ChatSettings
from app.schemas.mcp import MCPServersConfig, StdioMCPServerConfig
from app.schemas.tool_permissions import ToolPolicy
from app.services.settings_mutation import SettingsMutationCoordinator
from app.services.tool_permission_service import ToolPermissionService


def _legacy_model() -> dict:
    return {
        "session_id": "legacy-session",
        "model_name": "test-model",
        "openai_api_key": "test-key",
        "openai_base_url": "http://127.0.0.1:1/v1",
        "temperature": 0.1,
        "system_prompt": "test",
        "tools_list": ["read_file", "screenshot"],
        "context_strategy": {"compression_trigger_tokens_k": 100},
        "mcp": {
            "servers": {
                "filesystem": {
                    "enabled": True,
                    "identity_fingerprint": "a" * 64,
                    "tools": {"read": "allow", "write": "deny"},
                }
            }
        },
    }


def test_tool_identity_uses_source_specific_stable_keys() -> None:
    assert ToolIdentity.local("read_file").stable_key == "local:read_file"
    assert (
        ToolIdentity.mcp("filesystem", "read").stable_key
        == "mcp:filesystem:read"
    )


def test_legacy_permissions_are_migrated_and_persisted_in_one_structure(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "chat_settings.yaml"
    config_file.write_text(
        yaml.safe_dump({"chat_models": [_legacy_model()]}, sort_keys=False),
        encoding="utf-8",
    )

    settings = ChatSettingsDao(config_file=config_file).get_chat_settings("legacy-session")

    assert settings.tool_permissions["local:read_file"] == ToolPolicy.ALLOW
    assert settings.tool_permissions["local:screenshot"] == ToolPolicy.ASK
    assert settings.tool_permissions["mcp:filesystem:read"] == ToolPolicy.ALLOW
    assert settings.tool_permissions["mcp:filesystem:write"] == ToolPolicy.DENY

    stored = yaml.safe_load(config_file.read_text(encoding="utf-8"))["chat_models"][0]
    assert stored["tool_permissions_schema_version"] == 1
    assert "tools_list" not in stored
    assert "tools" not in stored["mcp"]["servers"]["filesystem"]
    assert stored["tool_permissions"]["local:screenshot"] == "ask"


def test_known_but_disabled_legacy_local_tools_migrate_to_deny(tmp_path: Path) -> None:
    config_file = tmp_path / "chat_settings.yaml"
    config_file.write_text(
        yaml.safe_dump({"chat_models": [_legacy_model()]}, sort_keys=False),
        encoding="utf-8",
    )

    settings = ChatSettingsDao(config_file=config_file).get_chat_settings("legacy-session")

    assert settings.tool_permissions["local:run_ps"] == ToolPolicy.DENY


def test_permission_service_groups_local_and_mcp_tools_and_marks_unavailable(
    tmp_path: Path,
) -> None:
    import asyncio

    async def scenario() -> None:
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
            available = True

            def get_available_identity(self, _server_id: str) -> str | None:
                return server_identity_fingerprint(server) if self.available else None

            def get_tools(self, _server_id: str) -> list[MCPToolDescriptor]:
                return [MCPToolDescriptor("read", "读取远端文件", {"type": "object"})]

        manager = Manager()
        authorization = ToolAuthorization(
            chat_dao.get_chat_settings,
            catalog=UnifiedToolCatalog(
                LocalToolCatalogAdapter(),
                MCPToolCatalogAdapter(mcp_dao, manager),
            ),
            settings_writer=chat_dao.update_chat_settings,
            mutation_coordinator=SettingsMutationCoordinator(),
        )
        service = ToolPermissionService(authorization)

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
        await authorization.set_policy(
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
    return ToolDescriptor(
        identity=ToolIdentity.local(name),
        exposed_name=name,
        display_name=name,
        description="读取文件",
        input_schema={"type": "object"},
        contract_fingerprint="contract-v1",
    )


def _authorization(policy: ToolPolicy) -> ToolAuthorization:
    class Settings:
        tool_permissions = {"local:read_file": policy}
        mcp = type("Mcp", (), {"servers": {}})()

    return ToolAuthorization(lambda _session_id: Settings())


def test_ask_policy_returns_one_generic_authorization_request() -> None:
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
    assert "已过期" in outcome.content


def test_explicit_rejection_wins_if_policy_changes_to_allow() -> None:
    class Settings:
        def __init__(self) -> None:
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
    assert "拒绝" in outcome.content


def test_local_description_and_authorization_timing_are_part_of_approval_contract() -> None:
    class Tool:
        name = "read_file"
        display_name = "读取文件"
        description = "读取一个文件"
        parameters_schema = {"type": "object"}
        authorization_timing = "before_execute"

    original = descriptor_for_tool(Tool())
    Tool.description = "读取多个文件"
    changed_description = descriptor_for_tool(Tool())
    Tool.description = "读取一个文件"
    Tool.authorization_timing = "after_prepare"
    changed_timing = descriptor_for_tool(Tool())

    assert original.contract_fingerprint != changed_description.contract_fingerprint
    assert original.contract_fingerprint != changed_timing.contract_fingerprint


def test_allow_and_valid_approval_produce_granted_outcomes() -> None:
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
        raise RuntimeError("配置损坏")

    assert ToolAuthorization(broken_loader).get_policy("session", identity) == ToolPolicy.DENY


def test_denied_tools_are_filtered_from_model_projection() -> None:
    class Tool:
        name = "read_file"
        description = "读取文件"
        parameters_schema = {"type": "object", "properties": {}}

        def to_openai_tool(self) -> dict:
            return {"type": "function", "function": {"name": self.name}}

    manager = ToolManager(
        session_id="session",
        authorization=_authorization(ToolPolicy.DENY),
    )
    manager.register(Tool())

    assert manager.get_openai_tools() == []


class _CountingTool(BaseTool):
    name = "read_file"
    description = "读取文件"
    parameters_schema = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult.success("ok")


class _UnsafePrepareTool(_CountingTool):
    authorization_timing = "after_prepare"

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult.continuation(
            "perform_screen_action",
            "不应在授权前执行",
            payload={"action": "click", "x": 1, "y": 2},
        )


class _PreparedTool(_CountingTool):
    authorization_timing = "after_prepare"

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        self.calls += 1
        if context.resume_data == {"kind": "authorization_granted"}:
            return ToolResult.success("performed")
        return ToolResult.prepared(
            {"action": "click", "x": 1, "y": 2},
            resume_state={"prepared": True},
        )


class _RecordingPlugins:
    def __init__(self) -> None:
        self.hooks: list[PluginHook] = []

    async def run_hooks(self, hook: PluginHook, *_args, **_kwargs) -> None:
        self.hooks.append(hook)


class _ScriptedLlm:
    def __init__(self) -> None:
        self.scripts = [
            [StreamChunk(tool_call=ToolCall(id="call-1", name="read_file", args={}))],
            [StreamChunk(content="done")],
        ]

    async def astream(self, messages, tools=None):
        for chunk in self.scripts.pop(0):
            yield chunk


def _pipeline(policy: ToolPolicy) -> tuple[ExecutionPipeline, _CountingTool]:
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
        emit_event=lambda _event, _data: None,
        tool_authorization=authorization,
    )
    return pipeline, tool


async def _events(stream) -> list:
    return [event async for event in stream]


def test_pipeline_waits_for_approval_before_executing_ask_tool() -> None:
    import asyncio

    async def scenario() -> None:
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
    import asyncio

    async def scenario() -> None:
        pipeline, tool = _pipeline(ToolPolicy.DENY)
        state = AgentState(session_id="session")
        state.add_user_message("read")

        await _events(pipeline.execute(state))

        assert tool.calls == 0
        tool_messages = [message for message in state.messages if isinstance(message, ToolMessage)]
        assert "不允许" in tool_messages[0].content

    asyncio.run(scenario())


def test_after_prepare_tool_cannot_request_protected_effect_before_authorization() -> None:
    import asyncio

    async def scenario() -> None:
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
            emit_event=lambda _event, _data: None,
            tool_authorization=authorization,
        )
        state = AgentState(session_id="session")
        state.add_user_message("act")

        events = await _events(pipeline.execute(state))

        assert tool.calls == 1
        assert all(event.type != EventType.INTERRUPT for event in events)
        tool_messages = [message for message in state.messages if isinstance(message, ToolMessage)]
        assert "只能请求只读准备数据" in tool_messages[0].content

    asyncio.run(scenario())


def test_after_prepare_hooks_span_the_logical_call_and_rejection_has_no_after_hook() -> None:
    import asyncio

    async def build_case():
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
            emit_event=lambda _event, _data: None,
            tool_authorization=authorization,
        )
        state = AgentState(session_id="session")
        call = ToolCall(id="call-1", name="read_file", args={})
        pending = await pipeline._execute_tool(state, call)
        return pipeline, state, call, pending, plugins

    async def scenario() -> None:
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
        assert "拒绝" in rejected.content
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
        assert approved.content == "performed"
        assert approved_plugins.hooks == [PluginHook.BEFORE_TOOL, PluginHook.AFTER_TOOL]

    asyncio.run(scenario())
