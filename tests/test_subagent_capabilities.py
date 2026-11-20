"""子工具白名单、动态父授权和插件钩子开关的公开接口回归。"""
from app.crud.sqlite_factory import SqliteFactory

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.context import ContextStrategyConfig
from app.agent.core.plugin_manager import PluginManager
from app.agent.core.tool_manager import ToolManager
from app.agent.plugins.base import BasePlugin, PluginContext, PluginHook
from app.agent.plugins.tool_adapter import PluginToolAdapter
from app.agent.state.model import AgentState
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.core.decorator import tool
from app.agent.tools._subagent_support import MessageParentTool
from app.schemas.chat_settings import AgentPluginSettings, ChatSettings
from app.schemas.tool_identity import ToolIdentity
from app.schemas.tool_permissions import ToolPolicy


@pytest.mark.parametrize("definition", ["class", "decorator"])
@pytest.mark.parametrize("plugin", [False, True])
@pytest.mark.parametrize("parent_only", [False, True])
def test_tool_scope_controls_exposure_lookup_and_delegation(
    definition: str, plugin: bool, parent_only: bool,
) -> None:
    """类工具和装饰器工具沿用同一父子使用范围，插件包装后继续生效。

    Args:
        definition (str): 使用类定义或函数装饰器创建工具。
        plugin (bool): 是否经过插件工具适配器包装。
        parent_only (bool): 是否将工具限定为仅主 Agent 使用。
    """
    class Operation(BaseTool):
        """提供默认可由父子 Agent 使用的普通工具。"""
        name, description = "operation", "执行操作"
        parameters_schema = {"type": "object", "properties": {}}

        async def execute(self, args: dict, context: ToolContext) -> ToolResult:
            """返回当前操作的执行结果。

            Args:
                args (dict): 本次模型调用的参数。
                context (ToolContext): 本次工具调用的运行上下文。

            Returns:
                ToolResult: 当前操作的文字结果。
            """
            return ToolResult.success("完成")

    async def operation() -> str:
        """返回装饰器工具的执行结果。

        Returns:
            str: 当前操作的文字结果。
        """
        return "完成"

    if definition == "class":
        instance = Operation()
        if parent_only:
            instance.parent_only = True
    else:
        tool_class = (tool(description="执行操作", parent_only=True)(operation) if parent_only
                      else tool(description="执行操作")(operation))
        instance = tool_class()
    instance = PluginToolAdapter("sample", instance) if plugin else instance
    identity = ToolIdentity.local("operation", plugin_name="sample" if plugin else None)
    parent = ToolManager()
    child = ToolManager(tool_whitelist=frozenset({identity}))
    parent.register(instance)
    child.register(instance)
    assert parent.get(instance.name) is instance
    assert [item.name for item in parent.get_tools()] == [instance.name]
    assert (instance.name in parent.get_delegatable_tool_identities()) is (not parent_only)
    assert (child.get(instance.name) is instance) is (not parent_only)
    assert [item.name for item in child.get_tools()] == ([] if parent_only else [instance.name])
    assert "parent_only" not in instance.parameters_schema["properties"]


@pytest.mark.parametrize("name", ["python", "python_reset", "create_subagent", "interact_subagent", "get_subagent_details"])
def test_builtin_parent_tools_are_hidden_from_child_models(name: str) -> None:
    """真实内置工具的使用范围声明同时约束子模型目录和调用查找。

    Args:
        name (str): 仅主 Agent 可使用的内置工具名称。
    """
    from app.agent.tools.core.registry import ToolRegistry

    tool_class = ToolRegistry.get(name)
    assert tool_class is not None
    # 1. 本用例仅检查公开契约，省略工具执行时才使用的应用依赖。
    instance = object.__new__(tool_class)
    parent = ToolManager()
    child = ToolManager(tool_whitelist=frozenset({ToolIdentity.local(name)}))
    parent.register(instance)
    child.register(instance)
    assert [item.name for item in parent.get_tools()] == [name]
    assert parent.get(name) is instance
    assert name not in parent.get_delegatable_tool_identities()
    assert child.get_tools() == []
    assert child.get(name) is None


def test_parent_management_tools_are_configurable_in_the_existing_permission_catalog() -> None:
    """实际权限目录公开三项父管理工具，子专用通信不会误公开成父工具。"""
    from app.agent.tools.authorization.catalog import LocalToolCatalogAdapter
    settings = ChatSettings(session_id="parent", model_name="test", openai_api_key="test", openai_base_url="http://unused",
        temperature=0, system_prompt="parent", context_strategy=ContextStrategyConfig(),
        tool_permissions={"local:create_subagent": ToolPolicy.ALLOW})
    entries = {entry.stable_key: entry for entry in LocalToolCatalogAdapter().list_entries(settings)}
    assert entries["local:create_subagent"].policy == ToolPolicy.ALLOW
    assert entries["local:interact_subagent"].policy == ToolPolicy.ASK
    assert entries["local:get_subagent_details"].policy == ToolPolicy.ASK
    assert "local:message_parent" not in entries


@pytest.mark.parametrize("identity", [ToolIdentity.local("operation"), ToolIdentity.local("operation", plugin_name="test"),
    ToolIdentity.mcp("server", "operation"), ToolIdentity.mcp("server", "operation", plugin_name="test")])
def test_whitelist_filters_all_tool_origins_and_forged_lookup(identity: ToolIdentity) -> None:
    """四种来源同时受可见范围限制，普通查找遵守完整工具身份和白名单。"""
    class Operation(BaseTool):
        """提供可被真实查询和调用的普通工具。"""
        name, description = "operation", "测试操作"
        parameters_schema = {"type": "object", "properties": {}}

        async def execute(self, args, context) -> ToolResult:
            """返回实际执行结果。"""
            return ToolResult.success("已执行")

    tool = Operation()
    tool.identity = identity
    denied = ToolManager(tool_whitelist=frozenset())
    denied.register(tool)
    assert denied.get_tools() == []
    assert denied.get(tool.name) is None
    allowed = ToolManager(tool_whitelist=frozenset({identity}))
    allowed.register(tool)
    assert allowed.get(tool.name) is tool
    assert [item.name for item in allowed.get_tools()] == [tool.name]
    tool.name = "create_subagent"
    allowed.register(tool)
    assert allowed.get("create_subagent") is tool
    denied.register_framework_tool(MessageParentTool())
    assert [item.name for item in denied.get_tools()] == ["message_parent"]


def test_parent_permission_changes_are_read_for_child_tool_visibility() -> None:
    """白名单不会授权调用，父会话运行中禁用工具后子模型也不再看见它。"""
    settings = ChatSettings(session_id="parent", model_name="test", openai_api_key="test", openai_base_url="http://unused",
        temperature=0, system_prompt="test", context_strategy=ContextStrategyConfig(), tool_permissions={"local:operation": ToolPolicy.ALLOW})

    class Operation(BaseTool):
        """提供父会话可以动态禁用的工具。"""
        name, description = "operation", "测试操作"
        parameters_schema = {"type": "object", "properties": {}}

        async def execute(self, args, context) -> ToolResult:
            """返回获准执行的结果。"""
            return ToolResult.success("执行")

    def load(session_id: str) -> ChatSettings:
        """检查读取的是父身份，子身份不应持有另一份权限。"""
        assert session_id == "parent"
        return settings

    manager = ToolManager(session_id="parent", authorization=ToolAuthorization(load), tool_whitelist=frozenset({ToolIdentity.local("operation")}))
    manager.register(Operation())
    assert len(manager.get_tools()) == 1
    settings.tool_permissions["local:operation"] = ToolPolicy.DENY
    assert manager.get_tools() == []


def test_disabling_automatic_hooks_keeps_plugin_registered() -> None:
    """关闭自动业务钩子时仍可登记插件，重新开启后执行已登记的钩子。"""
    async def scenario() -> None:
        """通过实际钩子调用验证登记、开关和清空操作。"""
        events: list[str] = []

        class Plugin(BasePlugin):
            """记录模型请求前的业务钩子调用。"""
            name = "test"

            @property
            def hooks(self):
                """订阅常规模型前钩子。"""
                return [PluginHook.BEFORE_LLM]

            async def execute(self, context) -> None:
                """记录自动业务钩子执行。"""
                events.append("hook")

        manager = PluginManager(enable_plugin_hooks=False)
        manager.register(Plugin(context=PluginContext(SqliteFactory())))
        await manager.run_hooks(PluginHook.BEFORE_LLM, AgentState.create_new("child"))
        assert manager.get_plugin("test") is not None
        assert events == []
        manager.if_enable_plugin_hooks = True
        await manager.run_hooks(PluginHook.BEFORE_LLM, AgentState.create_new("child"))
        assert events == ["hook"]
        manager.clear()
        assert manager.list_plugins() == []
        await manager.run_hooks(PluginHook.BEFORE_LLM, AgentState.create_new("child"))
        assert events == ["hook"]
    asyncio.run(scenario())


def test_factory_assembles_child_mcp_with_parent_identity_and_current_permissions(tmp_path: Path, repl_service) -> None:
    """通过真实工厂和 MCP 适配器调用，验证父身份贯穿目录、身份绑定和调用前复核。

    Args:
        repl_service (ReplService): 测试 fixture 提供的懒启动原生 Python 资源。
    """
    async def scenario() -> None:
        """只替换远程 MCP 连接，子装配和适配器执行保留生产流程。"""
        from mcp.types import CallToolResult, TextContent
        from app.agent.runtime.agent_factory import build_agent
        from app.agent.state.manager import SessionStateManager
        from app.mcp.identity import tool_contract_fingerprint
        from app.mcp.types import MCPToolDescriptor
        from app.schemas.agent_storage import AgentStorageScope
        from app.schemas.mcp import MCPModelSettings, MCPServerPolicy
        from app.schemas.subagent_types import SubagentDefinition

        descriptor = MCPToolDescriptor("operation", "远程操作", {"type": "object", "properties": {}})
        calls: list[str] = []

        class Connection:
            """模拟稳定的应用共享连接，真实执行适配器提供的权限检查。"""
            def get_server_config(self, server):
                """提供已启用的父服务配置。"""
                return SimpleNamespace(enabled=True, name="测试服务")

            async def connect(self, server):
                """返回远端实际工具目录。"""
                return [descriptor]

            def get_server_identity(self, server):
                """返回父已确认的远端身份。"""
                return "f" * 64

            def get_tool_contract_fingerprint(self, server, name):
                """保持请求与实际远端契约一致。"""
                return tool_contract_fingerprint(descriptor)

            async def call_tool(self, server, name, args, *, execution_guard):
                """在调用前执行实际防护，避免测试略过适配器的最终检查。"""
                error = execution_guard()
                if error:
                    raise ValueError(error)
                calls.append(name)
                return CallToolResult(content=[TextContent(type="text", text="远端执行成功")])

        settings = ChatSettings(session_id="parent", model_name="test", openai_api_key="test", openai_base_url="http://unused",
            temperature=0, system_prompt="parent", context_strategy=ContextStrategyConfig(),
            agent_plugins={"memory": AgentPluginSettings(enabled=False)}, tool_permissions={"mcp:server:operation": ToolPolicy.ALLOW},
            mcp=MCPModelSettings(servers={"server": MCPServerPolicy(enabled=True, identity_fingerprint="f" * 64)}))
        definition = SubagentDefinition("child", "parent", "助手", "child", frozenset({ToolIdentity.mcp("server", "operation")}),
            True, datetime.now(timezone.utc))
        manager = SessionStateManager("child", SqliteFactory(str(tmp_path / "state.sqlite3")), storage_scope=AgentStorageScope.SUBAGENT)
        agent = build_agent(settings, mcp_connection_manager=Connection(), tool_authorization=ToolAuthorization(lambda sid: settings if sid == "parent" else None),
            session_state_manager=manager, subagent=definition, sqlite_factory=SqliteFactory(), repl_service=repl_service)
        try:
            await agent.initialize()
            available = agent.tool_manager.get_tools()
            assert len(available) == 1
            tool = agent.tool_manager.get(available[0].name)
            result = await tool.execute({}, ToolContext("parent", AgentState.create_new("child")))
            assert result.text == "远端执行成功"
            settings.tool_permissions["mcp:server:operation"] = ToolPolicy.DENY
            await tool.execute({}, ToolContext("parent", AgentState.create_new("child")))
            assert calls == ["operation"]
        finally:
            await agent.close()
            await manager.close()
    asyncio.run(scenario())
