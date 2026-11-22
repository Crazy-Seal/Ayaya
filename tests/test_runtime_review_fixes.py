"""验证批次 B 审查发现的准备、控制、审批及关闭边界。"""

import asyncio
from pathlib import Path

import pytest

from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.models.contracts import StreamChunk
from app.agent.state.manager import RequiredSaveError, SessionStateManager
from app.agent.state.messages import ToolCall
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.core.base import BaseTool, ToolResult
from app.crud.sqlite_factory import SqliteFactory
from app.crud.task_record_dao import TaskRecordDao
from app.interaction.interaction_contract import InteractionCommand
from app.schemas.chat_settings import ChatSettings
from app.schemas.runtime_types import InteractionEventKind, SessionState
from app.schemas.subagent_types import SubagentAction
from app.schemas.task_types import PluginRequest, PluginRequestKind
from tests.runtime_support import build_test_scheduler, close_test_runtime, configured_settings, next_runtime_event
from tests.subagent_support import SubagentHarness, wait_for_subagent_worker


async def next_kind(runtime, kind):
    """等待实际 Hub 发布指定业务事件，忽略场景无关的输出。"""
    while True:
        event = await asyncio.wait_for(next_runtime_event(runtime), 3)
        if event.kind == kind:
            return event


class ProtectedTool(BaseTool):
    """用于验证失效审批不会继续调用工具。"""

    name, description = "protected_review", "需要审批的操作"
    parameters_schema = {"type": "object", "properties": {}}

    async def execute(self, args, context) -> ToolResult:
        """本组测试从未批准执行，调用即为生命周期错误。"""
        raise AssertionError("失效审批不应调用工具")


def approval_settings(session_id: str) -> ChatSettings:
    """为隔离测试提供默认询问工具权限的配置。"""
    return ChatSettings(session_id=session_id, model_name="test", openai_api_key="test",
        openai_base_url="http://unused", temperature=0, system_prompt="test", context_strategy=ContextStrategyConfig())


@pytest.mark.parametrize("action", [SubagentAction.STOP, SubagentAction.DESTROY])
def test_child_stop_during_result_preparation_never_starts_model(tmp_path: Path, monkeypatch, action):
    """结果记录保存期间接受停止或销毁，保存确认后只交付停止报告。"""
    async def scenario():
        """阻塞真实 DAO 调用之前，让命令与准备阶段发生交错。"""
        entered, release = asyncio.Event(), asyncio.Event()
        original = TaskRecordDao.start_subagent_run
        calls = []

        async def prepare(dao, *args):
            """保留真实保存，只延迟第一次准备的完成。"""
            entered.set()
            await release.wait()
            return await original(dao, *args)

        async def script(messages, tools):
            """记录任何不应发生的模型调用。"""
            calls.append(messages)
            yield StreamChunk(content="不应运行")

        monkeypatch.setattr(TaskRecordDao, "start_subagent_run", prepare)
        async with SubagentHarness(tmp_path / "child.sqlite3", script) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(entered.wait(), 3)
            try:
                assert (await harness.scheduler.interact_subagent("parent", receipt.context_id, action)).accepted
            finally:
                release.set()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            details = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
            assert not calls
            assert [item["status"] for item in details["results"]] == ["stopped"]
            await harness.queue.join()
            assert (receipt.context_id in harness.scheduler._sessions) == (action == SubagentAction.STOP)
    asyncio.run(scenario())


@pytest.mark.parametrize("delete", [False, True])
def test_parent_stop_during_input_selection_prevents_execution(tmp_path: Path, monkeypatch, delete):
    """父输入读取尚未返回时停止或删除，读取完成后不得开始模型。"""
    async def scenario():
        """通过真实输入存储和公开命令观察选择与交付的边界。"""
        entered, release = asyncio.Event(), asyncio.Event()
        factory = SqliteFactory(tmp_path / "parent.sqlite3")
        settings = configured_settings("s")
        original = SessionStateManager.read_activation_batch
        calls = []

        async def read(manager, *args, **kwargs):
            """让父会话读取输入时保持挂起。"""
            entered.set()
            await release.wait()
            return await original(manager, *args, **kwargs)

        def agent(context, manager):
            """停止后的输入交付连 Agent 资源都不应创建。"""
            calls.append(context)
            raise AssertionError("停止后不应创建 Agent")

        runtime = build_test_scheduler(runtime_queue=asyncio.Queue(), agent_factory=agent,
            session_state_manager_factory=lambda context: SessionStateManager(context, factory), chat_settings_dao=settings)
        try:
            await runtime.start()
            assert not (await runtime.submit(InteractionCommand("s", "stop_turn"))).accepted
            monkeypatch.setattr(SessionStateManager, "read_activation_batch", read)
            await runtime.submit(InteractionCommand("s", "human_turn", {"message": "开始"}))
            await asyncio.wait_for(entered.wait(), 3)
            session = runtime._sessions["s"]
            if delete:
                settings.delete_chat_settings("s")
                runtime.schedule_parent_cleanup("s")
                cleanup = runtime._parent_cleanup_workers["s"]
            else:
                assert (await runtime.submit(InteractionCommand("s", "stop_turn"))).accepted
            release.set()
            if delete:
                await asyncio.wait_for(cleanup, 3)
                await runtime.runtime_queue.join()
                assert "s" not in runtime._sessions
                assert "s" not in runtime._parent_cleanup_workers
            else:
                await session.close("测试结束")
            assert not calls
        finally:
            release.set()
            await close_test_runtime(runtime)
    asyncio.run(scenario())


def test_faulted_approval_does_not_block_next_child(tmp_path: Path, monkeypatch):
    """停止审批的保存失败后旧展示请求失效，下一子审批仍能成为队首。"""
    async def scenario():
        """只在取消保存处注入最终失败，保留真实审批和队列调用链。"""
        settings = approval_settings("parent")
        original = SessionStateManager.cancel_pending_interrupt
        broken_context = None

        async def cancel(manager):
            """模拟旧子审批取消经过持久化策略后的最终失败。"""
            if manager.session_id == broken_context:
                raise RequiredSaveError("审批取消保存失败")
            return await original(manager)

        async def script(messages, tools):
            """每个新子首次执行均请求审批。"""
            yield StreamChunk(tool_call=ToolCall("review", ProtectedTool.name, {}))

        async with SubagentHarness(tmp_path / "fault.sqlite3", script, tools=(ProtectedTool(),),
                authorization=ToolAuthorization(lambda _: settings)) as harness:
            first = await harness.create()
            await next_kind(harness.scheduler, InteractionEventKind.SUBAGENT_INTERRUPT)
            broken_context = first.context_id
            monkeypatch.setattr(SessionStateManager, "cancel_pending_interrupt", cancel)
            await harness.scheduler.interact_subagent("parent", first.context_id, SubagentAction.STOP)
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, first.context_id), 3)
            await harness.queue.join()
            assert (await harness.scheduler.snapshot("parent")).payload["approval_prompt"] is None
            second = await harness.create("第二个任务")
            event = await next_kind(harness.scheduler, InteractionEventKind.SUBAGENT_INTERRUPT)
            assert event.payload["subagent_id"] == second.context_id
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", [PluginRequestKind.STOP_TURN, PluginRequestKind.STEER_TURN])
def test_plugin_control_advances_waiting_parent(tmp_path: Path, kind):
    """插件停止或引导审批等待均可推进，插件停止后父会话仍保持启用。"""
    async def scenario():
        """真实 Agent 先等待工具审批，再消费插件控制及通知。"""
        factory = SqliteFactory(tmp_path / "plugin.sqlite3")
        settings = approval_settings("s")
        continued = asyncio.Event()
        calls = 0

        class Model:
            """首次请求审批，控制处理后的下一轮正常结束。"""
            async def astream(self, messages, tools=None):
                """记录实际进入模型的轮次。"""
                nonlocal calls
                calls += 1
                if calls == 1:
                    yield StreamChunk(tool_call=ToolCall("review", ProtectedTool.name, {}))
                else:
                    continued.set()
                    yield StreamChunk(content="新消息已处理")

            async def close(self):
                """脚本模型没有外部资源。"""

        def agent(context, manager):
            """装配真实 Agent 和受审批保护的工具。"""
            result = Agent(AgentConfig(context, "test", "test", ContextStrategyConfig()), factory,
                session_state_manager=manager, llm_client=Model(), tool_authorization=ToolAuthorization(lambda _: settings))
            result.register_tool(ProtectedTool())
            return result

        runtime = build_test_scheduler(runtime_queue=asyncio.Queue(), agent_factory=agent,
            session_state_manager_factory=lambda context: SessionStateManager(context, factory), chat_settings_dao=configured_settings("s"))
        try:
            await runtime.start()
            await runtime.submit(InteractionCommand("s", "human_turn", {"message": "开始"}))
            event = await next_kind(runtime, InteractionEventKind.INTERRUPT)
            await runtime.runtime_queue.put(PluginRequest(kind=kind, session_id="s", plugin_name="test", name="control", text="改做新工作"))
            await next_kind(runtime, InteractionEventKind.AUTHORIZATION_RESOLVED)
            if kind == PluginRequestKind.STOP_TURN:
                await runtime.runtime_queue.put(PluginRequest(kind=PluginRequestKind.NOTIFY, session_id="s",
                    plugin_name="test", name="notice", text="后续工作"))
            await asyncio.wait_for(continued.wait(), 3)
            assert (await runtime.snapshot("s")).payload["session_state"] == SessionState.ENABLED
            assert calls == 2
        finally:
            await close_test_runtime(runtime)
    asyncio.run(scenario())


def test_parent_delete_stops_children_before_parent_close_returns(tmp_path: Path, monkeypatch):
    """父关闭被阻塞时，子模型仍立即收到停止并结束自己的执行。"""
    from app.agent.runtime.parent_session import ParentSession

    async def scenario():
        """阻塞父 close 的等待，观察真实子模型收到取消及会话登记释放。"""
        entered, stopped = asyncio.Event(), asyncio.Event()
        closing, release = asyncio.Event(), asyncio.Event()
        original = ParentSession.close

        async def close(parent, reason):
            """模拟父资源仍在等待关闭确认。"""
            closing.set()
            await release.wait()
            await original(parent, reason)

        async def script(messages, tools):
            """子模型一直等待，停止时通过 finally 通知测试。"""
            entered.set()
            try:
                await asyncio.Event().wait()
                yield StreamChunk(content="不应完成")
            finally:
                stopped.set()

        async with SubagentHarness(tmp_path / "delete.sqlite3", script) as harness:
            child = await harness.create()
            await asyncio.wait_for(entered.wait(), 3)
            monkeypatch.setattr(ParentSession, "close", close)
            harness.supervisor_settings.delete_chat_settings("parent")
            harness.scheduler.schedule_parent_cleanup("parent")
            cleanup = harness.scheduler._parent_cleanup_workers["parent"]
            try:
                await asyncio.wait_for(closing.wait(), 3)
                await asyncio.wait_for(stopped.wait(), 3)
                assert not cleanup.done()
            finally:
                release.set()
            await asyncio.wait_for(cleanup, 3)
            await harness.queue.join()
            assert child.context_id not in harness.scheduler._sessions
            assert child.context_id not in harness.scheduler._subagent_records
            assert "parent" not in harness.scheduler._sessions
            assert "parent" not in harness.scheduler._parent_cleanup_workers
    asyncio.run(scenario())


def test_completed_one_shot_releases_runtime_registration(tmp_path: Path):
    """一次性子正常交付结果并关闭后释放运行对象，持久报告继续可查询。"""
    async def scenario():
        """等待实际工作结束，再分别核验运行登记与持久结果。"""
        async def script(messages, tools):
            """正常返回子任务结果。"""
            yield StreamChunk(content="任务完成")

        async with SubagentHarness(tmp_path / "one-shot.sqlite3", script, retain=False) as harness:
            child = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, child.context_id), 3)
            await harness.queue.join()
            assert child.context_id not in harness.scheduler._sessions
            assert child.context_id not in harness.scheduler._subagent_records
            details = await harness.scheduler.get_subagent_details("parent", child.context_id)
            assert details["results"][0]["status"] == "completed"
    asyncio.run(scenario())


def test_parent_delete_closes_loaded_children_when_identity_query_fails(tmp_path: Path, monkeypatch):
    """子身份枚举失败仍关闭已加载的空闲保留子，释放其运行对象。"""
    async def scenario():
        """先形成空闲保留子，再让删除时的持久枚举失败。"""
        async def script(messages, tools):
            """完成一次工作，留下可复用的会话。"""
            yield StreamChunk(content="完成")

        async def fail_listing(session_id):
            """模拟持久查询返回最终错误。"""
            raise OSError("身份列表读取失败")

        async with SubagentHarness(tmp_path / "list-failure.sqlite3", script) as harness:
            child = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, child.context_id), 3)
            monkeypatch.setattr(harness.scheduler._subagent_dao, "list_records", fail_listing)
            harness.supervisor_settings.delete_chat_settings("parent")
            harness.scheduler.schedule_parent_cleanup("parent")
            await asyncio.wait_for(harness.scheduler._parent_cleanup_workers["parent"], 3)
            await harness.queue.join()
            assert child.context_id not in harness.scheduler._sessions
            assert child.context_id not in harness.scheduler._subagent_records
    asyncio.run(scenario())


def test_parent_delete_unregisters_recovered_unloaded_children(tmp_path: Path):
    """重启后尚未加载 Session 的子身份，也在父删除后释放内存登记。"""
    async def scenario():
        """使用同一隔离数据库重建运行时，观察持久恢复和随后删除。"""
        async def script(messages, tools):
            """首次运行正常完成，重启后不会自动执行。"""
            yield StreamChunk(content="完成")

        path = tmp_path / "restart.sqlite3"
        async with SubagentHarness(path, script) as first:
            child = await first.create()
            await asyncio.wait_for(wait_for_subagent_worker(first.scheduler, child.context_id), 3)
        async with SubagentHarness(path, script) as second:
            assert child.context_id in second.scheduler._subagent_records
            assert child.context_id not in second.scheduler._sessions
            second.supervisor_settings.delete_chat_settings("parent")
            second.scheduler.schedule_parent_cleanup("parent")
            await asyncio.wait_for(second.scheduler._parent_cleanup_workers["parent"], 3)
            assert child.context_id not in second.scheduler._subagent_records
            details = await second.scheduler.get_subagent_details("parent", child.context_id)
            assert not details["context_available"]
    asyncio.run(scenario())
