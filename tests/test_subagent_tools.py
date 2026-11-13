from tests.runtime_support import make_execution_control
"""验证工具持久受理后的立即回执和异步执行归属。"""
from tests.runtime_support import collect_agent_events
from app.schemas.runtime_types import CommandReceipt, CommandStatus
from app.crud.sqlite_factory import SqliteFactory

from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator

import asyncio
from pathlib import Path
from tests.runtime_support import configured_settings
from types import SimpleNamespace

import pytest

from app.agent.runtime.subagent_requests import CreateSubagentRequest
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.core.tool_manager import ToolManager
from app.agent.mcp.adapter import MCPToolAdapter
from app.agent.models.contracts import StreamChunk
from app.agent.plugins.tool_adapter import PluginToolAdapter
from app.agent.runtime.turn_control import TurnControl
from app.agent.state.model import AgentState
from app.agent.state.messages import ToolCall
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.core.runtime import ToolRuntime
from app.agent.tools.create_subagent import CreateSubagentTool
from app.agent.tools.interact_subagent import InteractSubagentTool
from app.agent.tools.get_subagent_details import GetSubagentDetailsTool
from app.agent.tools._subagent_support import MessageParentTool
from app.schemas.chat_settings import ChatSettings, AgentPluginSettings
from app.schemas.mcp import MCPModelSettings, MCPServerPolicy
from app.schemas.tool_identity import ToolIdentity
from app.schemas.tool_permissions import ToolPolicy
from app.mcp.types import MCPToolDescriptor


class OperationTool(BaseTool):
    """提供可被父模型选入白名单的普通任务工具。"""
    name, description = "operation", "执行测试操作"
    parameters_schema = {"type": "object", "properties": {}}

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """返回实际执行结果，白名单选择本身不会调用此操作。

        Args:
            args (dict): 工具调用参数。
            context (ToolContext): 本次调用的框架上下文。

        Returns:
            ToolResult: 操作成功的测试结果。
        """
        return ToolResult.success("操作成功")


def test_parent_model_public_names_become_source_specific_whitelist(tmp_path: Path) -> None:
    """父模型按实际工具目录填写四类公开名称，真实 Pipeline 将其解析为稳定身份。

    Args:
        tmp_path (Path): 本次测试独占的数据库目录。
    """
    async def scenario() -> None:
        """仅替换模型和请求消费者，保留 Agent 装配、权限过滤及工具执行。"""
        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(str(tmp_path / "tasks.sqlite3"))))
        settings = ChatSettings(session_id="parent", model_name="test", openai_api_key="test",
            openai_base_url="http://unused", temperature=0, system_prompt="完成任务",
            context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
            tool_permissions={"local:create_subagent": ToolPolicy.ALLOW, "local:operation": ToolPolicy.ALLOW,
                              "mcp:server:operation": ToolPolicy.ASK},
            mcp=MCPModelSettings(servers={"server": MCPServerPolicy(enabled=True, identity_fingerprint="f" * 64)}),
            agent_plugins={"sample": AgentPluginSettings(enabled=True,
                tool_permissions={"local:operation": ToolPolicy.ALLOW, "mcp:server:operation": ToolPolicy.ASK})})
        authorization = ToolAuthorization(lambda _: settings)
        connection = SimpleNamespace(get_server_identity=lambda _: "f" * 64)
        descriptor = MCPToolDescriptor("operation", "远端测试操作", {"type": "object", "properties": {}})
        targets = (
            OperationTool(), PluginToolAdapter("sample", OperationTool()),
            MCPToolAdapter(session_id="parent", server_id="server", server_name="测试服务",
                descriptor=descriptor, connection_manager=connection, authorization=authorization),
            MCPToolAdapter(session_id="parent", server_id="server", server_name="插件服务",
                descriptor=descriptor, connection_manager=connection, authorization=authorization, plugin_name="sample"),
        )
        public_names = {"operation", "plugin__sample__operation", "mcp__server__operation",
                        "plugin__sample__mcp__server__operation"}
        expected_identities = frozenset({ToolIdentity.local("operation"),
            ToolIdentity.local("operation", plugin_name="sample"), ToolIdentity.mcp("server", "operation"),
            ToolIdentity.mcp("server", "operation", plugin_name="sample")})

        class Model:
            """从实际模型目录选择公开名称，不手工提供内部身份或权限键。"""
            def __init__(self) -> None:
                """记录是否已经发起创建工具。"""
                self.created = False

            async def astream(self, messages, *, tools=None):
                """首次从目录选取四类任务工具，下一步返回最终文字。"""
                if not self.created:
                    self.created = True
                    selected = sorted(item.name for item in tools if item.name in public_names)
                    assert set(selected) == public_names
                    yield StreamChunk(tool_call=ToolCall("create", "create_subagent", {
                        "name": "助手", "system_prompt": "完成委派", "prompt": "执行任务",
                        "tool_whitelist": selected,
                    }))
                else:
                    yield StreamChunk(content="已委派")

            async def close(self) -> None:
                """测试模型不持有外部连接。"""

        async def receive_request() -> CreateSubagentRequest:
            """返回持久受理回执，检查跨队列传递的稳定工具身份。"""
            request = await asyncio.wait_for(queue.get(), 3)
            assert isinstance(request, CreateSubagentRequest)
            assert request.tool_whitelist == expected_identities
            request.reply.set_result(CommandReceipt(CommandStatus.ACCEPTED, "child"))
            return request

        agent = Agent(AgentConfig(session_id="parent", model_name="test", api_key="test",
            system_prompt="完成任务", context_strategy=settings.context_strategy), llm_client=Model(),
            sqlite_factory=SqliteFactory(str(tmp_path / "state.sqlite3")), tool_authorization=authorization,
            tool_runtime_factory=lambda name, run, call: ToolRuntime("parent", run, name, call, supervisor, queue))
        for tool in targets:
            agent.register_tool(tool)
        agent.register_tool(CreateSubagentTool(agent.tool_manager.get_visible_tool_identities()))
        consumer = asyncio.create_task(receive_request())
        try:
            await asyncio.wait_for(_run_parent(agent), 5)
            request = await consumer
            # 1. 子工具目录按稳定身份过滤，四类工具保持原始来源且可实际调用。
            child_tools = ToolManager(tool_whitelist=request.tool_whitelist)
            for tool in targets:
                child_tools.register(tool)
            assert {item.name for item in child_tools.get_tools()} == public_names
            assert child_tools.get("plugin__sample__operation") is targets[1]
            result = await targets[1].execute({}, ToolContext("parent", AgentState.create_new("child")))
            assert result.text == "操作成功"
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            await agent.close()
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


async def _run_parent(agent: Agent) -> None:
    """驱动父 Agent 的真实模型和工具循环。

    Args:
        agent (Agent): 使用可控模型和隔离存储的父 Agent。
    """
    async def receive_agent_event(_event):
        """在原事件位置执行此场景的观察和控制。"""
        pass
    await agent.run('开始委派', turn_control=make_execution_control('parent-run')[1], emit=receive_agent_event)


@pytest.mark.parametrize("name", ["missing", "local:operation", "operation"])
def test_unavailable_public_name_is_rejected_before_creation(tmp_path: Path, name: str) -> None:
    """未知名称、内部权限键或已禁用工具不能通过白名单创建产生副作用。

    Args:
        tmp_path (Path): 隔离任务记录目录。
        name (str): 模型填写的非法或当前不可见名称。
    """
    async def scenario() -> None:
        """使用真实权限过滤产生目录，再检查创建工具没有投递请求。"""
        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        settings = ChatSettings(session_id="parent", model_name="test", openai_api_key="test",
            openai_base_url="http://unused", temperature=0, system_prompt="测试", context_strategy={},
            tool_permissions={"local:operation": ToolPolicy.DENY})
        tools = ToolManager(session_id="parent", authorization=ToolAuthorization(lambda _: settings))
        tools.register(OperationTool())
        runtime = ToolRuntime("parent", "run", "create_subagent", "call", supervisor, queue)
        try:
            with pytest.raises(ValueError, match="当前可见目录"):
                await CreateSubagentTool(tools.get_visible_tool_identities()).execute({"name": "助手", "system_prompt": "test", "prompt": "任务",
                    "tool_whitelist": [name]}, ToolContext("parent", AgentState.create_new("parent"), runtime=runtime))
            assert queue.empty()
        finally:
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("tool,args", [
    (CreateSubagentTool({}), {"name": "助手", "system_prompt": "test", "prompt": "任务"}),
    (InteractSubagentTool(), {"subagent_id": "child", "action": "command", "text": "继续"}),
    (InteractSubagentTool(), {"subagent_id": "child", "action": "stop"}),
    (InteractSubagentTool(), {"subagent_id": "child", "action": "destroy"}),
])
def test_subagent_tool_returns_receipt_after_acceptance(
    tmp_path: Path, tool: BaseTool, args: dict,
) -> None:
    """创建及交互持久受理后立即返回回执，后续结果由异步通知交付。

    Args:
        tmp_path (Path): 本次测试独占的任务数据库目录。
        tool (BaseTool): 发起创建或交互的工具实例。
        args (dict): 模型提交的业务参数。
    """
    async def scenario() -> None:
        """只消费受理请求，验证工具无需再查询名单或等待执行。"""
        import json
        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        runtime = ToolRuntime("parent", "run", tool.name, "call", supervisor, queue)
        invocation = asyncio.create_task(tool.execute(args, ToolContext("parent", AgentState.create_new("parent"), runtime=runtime)))
        try:
            request = await asyncio.wait_for(queue.get(), 1)
            assert not invocation.done()
            request.reply.set_result(CommandReceipt(CommandStatus.ACCEPTED, "child"))
            result = await asyncio.wait_for(invocation, 1)
            assert json.loads(result.text) == {"context_id": "child", "accepted": True, "status": "accepted", "run_id": None, "detail": None}
            assert queue.empty()
        finally:
            invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("tool,args", [
    (CreateSubagentTool({}), {"name": "助手", "system_prompt": "test", "prompt": "任务"}),
    (InteractSubagentTool(), {"subagent_id": "child", "action": "command", "text": "继续"}),
    (InteractSubagentTool(), {"subagent_id": "child", "action": "stop"}),
    (InteractSubagentTool(), {"subagent_id": "child", "action": "destroy"}),
    (GetSubagentDetailsTool(), {"subagent_id": "child"}),
    (MessageParentTool(), {"text": "已处理", "expect_reply": False}),
])
def test_cancelled_tool_wait_cancels_unneeded_receipt(
    tmp_path: Path, tool: BaseTool, args: dict,
) -> None:
    """工具调用取消后结束本次回执 Future，队列中的请求仍由消费者持有。

    Args:
        tmp_path (Path): 本次测试独占的任务数据库目录。
        tool (BaseTool): 发起独立请求并等待本次回执的工具。
        args (dict): 本次工具调用参数。
    """
    async def scenario() -> None:
        """取得真实工具投递的请求，取消等待后核对已失去接收者的回执。"""
        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        runtime = ToolRuntime("parent", "run", tool.name, "call", supervisor, queue,
                              subagent_id="child" if tool.name == "message_parent" else None)
        invocation = asyncio.create_task(tool.execute(
            args, ToolContext("parent", AgentState.create_new("parent"), runtime=runtime)))
        try:
            request = await queue.get()
            invocation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await invocation
            assert request.reply.cancelled()
        finally:
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["registered_later", "disabled", "removed", "replaced", "cleared"])
def test_creation_uses_current_tool_identities_after_catalog_changes(tmp_path: Path, change: str) -> None:
    """创建工具使用装配后更新的目录，避免权限或热插拔变化后沿用旧白名单。

    Args:
        tmp_path (Path): 本次测试独占的任务数据库目录。
        change (str): 工具装配后发生的权限、登记、替换或清空操作。
    """
    async def scenario() -> None:
        """通过实际工具查找和队列请求观察最新身份是否参与持久受理。"""
        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        settings = ChatSettings(session_id="parent", model_name="test", openai_api_key="test",
            openai_base_url="http://unused", temperature=0, system_prompt="测试", context_strategy={},
            tool_permissions={"local:create_subagent": ToolPolicy.ALLOW, "local:operation": ToolPolicy.ALLOW})
        manager = ToolManager(session_id="parent", authorization=ToolAuthorization(lambda _: settings))
        create = CreateSubagentTool(manager.get_visible_tool_identities())
        manager.register(create)
        if change != "registered_later":
            manager.register(OperationTool())
        manager.get_tools()

        # 1. 创建工具已持有目录引用，再改变工具集合或权限。
        expected = ToolIdentity.local("operation")
        if change == "registered_later":
            manager.register(OperationTool())
        elif change == "disabled":
            settings.tool_permissions["local:operation"] = ToolPolicy.DENY
        elif change == "removed":
            manager.unregister("operation")
        elif change == "replaced":
            manager.unregister("operation")
            replacement = OperationTool()
            expected = ToolIdentity.mcp("server", "operation")
            replacement.identity = expected
            settings.mcp = MCPModelSettings(servers={"server": MCPServerPolicy(enabled=True)})
            settings.tool_permissions["mcp:server:operation"] = ToolPolicy.ALLOW
            manager.register(replacement)
        else:
            manager.clear()
            manager.register(create)

        # 2. 按 Pipeline 的实际查找入口执行；目录无效时拒绝投递，有效时保存最新身份。
        runtime = ToolRuntime("parent", "run", "create_subagent", "call", supervisor, queue)
        context = ToolContext("parent", AgentState.create_new("parent"), runtime=runtime)
        args = {"name": "助手", "system_prompt": "执行任务", "prompt": "开始", "tool_whitelist": ["operation"]}
        tool = manager.get("create_subagent")
        assert tool is not None
        invocation = None
        try:
            if change in {"disabled", "removed", "cleared"}:
                with pytest.raises(ValueError, match="当前可见目录"):
                    await tool.execute(args, context)
                assert queue.empty()
            else:
                invocation = asyncio.create_task(tool.execute(args, context))
                request = await asyncio.wait_for(queue.get(), 1)
                assert request.tool_whitelist == frozenset({expected})
                request.reply.set_result(CommandReceipt(CommandStatus.ACCEPTED, "child"))
                assert (await asyncio.wait_for(invocation, 1)).text
        finally:
            if invocation is not None:
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_factory_wires_creation_tool_to_its_agent_catalog(tmp_path: Path) -> None:
    """应用工厂装配创建工具后，实际调用能解析随后注册的工具身份。

    Args:
        tmp_path (Path): 本次测试独占的任务数据库目录。
    """
    async def scenario() -> None:
        """保留真实工厂和工具初始化，通过共享队列确认白名单解析结果。"""
        from app.agent.runtime import agent_factory

        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        settings = ChatSettings(session_id="parent", model_name="test", openai_api_key="test",
            openai_base_url="http://unused", temperature=0, system_prompt="测试", context_strategy={},
            agent_plugins={"memory": AgentPluginSettings(enabled=False)},
            tool_permissions={"local:create_subagent": ToolPolicy.ALLOW, "local:operation": ToolPolicy.ALLOW})
        agent = agent_factory.build_agent(settings, tool_authorization=ToolAuthorization(lambda _: settings), runtime_queue=queue,
            tool_runtime_factory=lambda name, run, call: ToolRuntime("parent", run, name, call, supervisor, queue), sqlite_factory=SqliteFactory())
        invocation = None
        try:
            agent.register_tool(OperationTool())
            await agent.initialize()
            tool = agent.tool_manager.get("create_subagent")
            assert tool is not None
            context = ToolContext("parent", AgentState.create_new("parent"),
                runtime=ToolRuntime("parent", "run", tool.name, "call", supervisor, queue))
            invocation = asyncio.create_task(tool.execute({"name": "助手", "system_prompt": "完成任务",
                "prompt": "开始", "tool_whitelist": ["operation"]}, context))
            request = await asyncio.wait_for(queue.get(), 1)
            assert request.tool_whitelist == frozenset({ToolIdentity.local("operation")})
            request.reply.set_result(CommandReceipt(CommandStatus.ACCEPTED, "child"))
            assert (await asyncio.wait_for(invocation, 1)).text
        finally:
            if invocation is not None:
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)
            await agent.close()
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())
