"""删除提交与后台清理通过公开受理、名单及历史查询验证。"""
from tests.runtime_support import runtime_hub, close_test_runtime
from app.routes.agent import observe_runtime
from tests.runtime_support import next_runtime_event
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory

from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator

import asyncio
from pathlib import Path
from contextlib import closing
import sqlite3

import pytest

from app.agent.models.contracts import StreamChunk
from app.agent.state.messages import AssistantMessage, ToolCall, ToolMessage
from app.agent.state.manager import SessionStateManager
from app.agent.tools.core.base import BaseTool, ToolResult
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.subagent_types import SubagentAction, SubagentRecord
from tests.subagent_support import SubagentHarness, wait_for_subagent_worker, subagent_worker_running
from app.services.chat_settings_service import ChatSettingsService
from app.interaction.interaction_contract import InteractionCommand
from app.agent.tools.core.runtime import ToolRuntime
from app.agent.runtime.tasks.base import BaseTask
from app.agent.runtime.tasks.runtime import TaskRuntime
from app.schemas.runtime_types import CheckpointType, InteractionEventKind, TaskStatus
from app.routes.chat_settings import delete_chat_settings


@pytest.mark.parametrize("retain", [False, True])
@pytest.mark.parametrize("waiting_tool", [False, True])
@pytest.mark.parametrize("fail_save", [False, True])
def test_destroy_preserves_partial_response_and_finishes_tool_history(
    tmp_path: Path, monkeypatch, retain: bool, waiting_tool: bool, fail_save: bool,
) -> None:
    """销毁流式回答或挂起工具时立即废弃，后台保存正文和完整工具记录。"""
    async def scenario() -> None:
        """在正文生成后或工具执行中销毁，再从独立数据库连接核对留档。"""
        entered, cancelled = asyncio.Event(), asyncio.Event()
        partial = "销毁前已生成的回答"

        class WaitingTool(BaseTool):
            """在销毁取消时释放等待，供检查原工具调用的终止记录。"""
            name, description = "waiting_tool", "等待销毁"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args, context) -> ToolResult:
                """通知工具已经开始，并在执行被取消后归还资源。"""
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
                return ToolResult.success("工具完成")

        async def script(messages, tools):
            """先输出正文，再停在模型等待或实际工具内部。"""
            yield StreamChunk(content=partial)
            if waiting_tool:
                yield StreamChunk(tool_call=ToolCall(id="waiting", name="waiting_tool", args={}))
            else:
                entered.set()
                await asyncio.Event().wait()

        async with SubagentHarness(tmp_path / "state.sqlite3", script, retain=retain,
                                   tools=(WaitingTool(),) if waiting_tool else ()) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(entered.wait(), 3)
            saves = 0
            if fail_save:
                manager = harness.scheduler._sessions[receipt.context_id]._manager
                from app.crud.agent_state_dao import AgentStateDao
                original_save = AgentStateDao._write_state_in_transaction

                async def save(dao, db, state, **kwargs):
                    """在真实保存事务内部模拟一次 BUSY，保存相同正文和工具取消结果。"""
                    nonlocal saves
                    if dao is manager._state_dao:
                        saves += 1
                        if saves == 1:
                            error = sqlite3.OperationalError("首次保存暂时失败")
                            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                            raise error
                    return await original_save(dao, db, state, **kwargs)

                monkeypatch.setattr(AgentStateDao, "_write_state_in_transaction", save)
            destroyed = await asyncio.wait_for(harness.scheduler.interact_subagent(
                "parent", receipt.context_id, SubagentAction.DESTROY), 0.5)
            assert destroyed.accepted
            assert not await harness.scheduler.snapshot_subagents("parent")
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            reader = SessionStateManager(receipt.context_id, SqliteFactory(harness.path),
                                         storage_scope=AgentStorageScope.SUBAGENT)
            try:
                state = await reader.load()
                assert reader.last_loaded_checkpoint_type == CheckpointType.COMPLETED
                assistants = [item for item in state.messages if isinstance(item, AssistantMessage)]
                assert len(assistants) == 1 and assistants[0].text == partial + "……（被用户打断）"
                assert not state.pending_tool_calls and not state.interrupt_data
                if waiting_tool:
                    assert cancelled.is_set()
                    results = [item for item in state.messages if isinstance(item, ToolMessage)]
                    assert len(results) == 1 and results[0].tool_call_id == "waiting"
            finally:
                await reader.close()
            details = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
            assert len(details["results"]) == 1
            assert details["results"][0]["status"] == "stopped"
            assert details["results"][0]["result"] == partial
            assert len([item for item in details["recent_interactions"] if item["kind"] == "final"]) == 1
            assert harness.received.empty()
            if fail_save:
                assert saves >= 2
    asyncio.run(scenario())


@pytest.mark.parametrize("shutdown", [False, True])
def test_destroy_during_final_commit_keeps_original_response(tmp_path: Path, monkeypatch, shutdown: bool) -> None:
    """最终提交已开始时销毁立即返回，原提交完成后保留正常正文及结果。"""
    async def scenario() -> None:
        """阻塞最终保存，验证销毁不会取消已经开始的正常提交。"""
        saving, released = asyncio.Event(), asyncio.Event()
        original = SessionStateManager.complete_turn
        final = "已经生成的完整回答"

        async def save(manager, state, **kwargs):
            """子 Agent 进入最终提交后等待，父 Agent 沿用正常保存。"""
            if manager.storage_scope == AgentStorageScope.SUBAGENT:
                saving.set()
                await released.wait()
            return await original(manager, state, **kwargs)

        async def script(messages, tools):
            """生成完整正文并开始最终提交。"""
            yield StreamChunk(content=final)

        monkeypatch.setattr(SessionStateManager, "complete_turn", save)
        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(saving.wait(), 3)
            try:
                destroyed = await asyncio.wait_for(harness.scheduler.interact_subagent(
                    "parent", receipt.context_id, SubagentAction.DESTROY), 0.5)
                assert destroyed.accepted
                assert subagent_worker_running(harness.scheduler, receipt.context_id)
                assert not await harness.scheduler.snapshot_subagents("parent")
                if shutdown:
                    await asyncio.wait_for(harness.scheduler.begin_shutdown(), 0.5)
            finally:
                released.set()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            reader = SessionStateManager(receipt.context_id, SqliteFactory(harness.path),
                                         storage_scope=AgentStorageScope.SUBAGENT)
            try:
                state = await reader.load()
                assert reader.last_loaded_checkpoint_type == CheckpointType.COMPLETED
                assert [item.text for item in state.messages if isinstance(item, AssistantMessage)] == [final]
            finally:
                await reader.close()
            details = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
            assert len(details["results"]) == 1
            assert details["results"][0]["status"] == "completed"
            assert details["results"][0]["result"] == final
            assert harness.received.empty()
    asyncio.run(scenario())


def test_shutdown_waits_for_destroy_history_save_after_repeated_cancellation(tmp_path: Path, monkeypatch) -> None:
    """销毁保存历史期间应用再次取消执行时，先完成原保存再关闭连接。"""
    async def scenario() -> None:
        """用保存屏障让销毁与退出重叠，检查正文只保存一次。"""
        running, saving, released = asyncio.Event(), asyncio.Event(), asyncio.Event()
        partial = "退出时也要保留的正文"

        async def script(messages, tools):
            """生成正文后停在下一次模型读取。"""
            yield StreamChunk(content=partial)
            running.set()
            await asyncio.Event().wait()

        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(running.wait(), 3)
            manager = harness.scheduler._sessions[receipt.context_id]._manager
            original = manager.finish_interrupted_turn

            async def save(*args, **kwargs):
                """取得中断保存工作后等待退出请求，再执行真实数据库事务。"""
                saving.set()
                await released.wait()
                await original(*args, **kwargs)

            monkeypatch.setattr(manager, "finish_interrupted_turn", save)
            try:
                await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.DESTROY)
                await asyncio.wait_for(saving.wait(), 3)
                await harness.scheduler.begin_shutdown()
                assert subagent_worker_running(harness.scheduler, receipt.context_id)
            finally:
                released.set()
            await asyncio.wait_for(harness.scheduler.wait_for_workers(), 3)
            reader = SessionStateManager(receipt.context_id, SqliteFactory(harness.path),
                                         storage_scope=AgentStorageScope.SUBAGENT)
            try:
                state = await reader.load()
                assert reader.last_loaded_checkpoint_type == CheckpointType.COMPLETED
                assistants = [item for item in state.messages if isinstance(item, AssistantMessage)]
                assert len(assistants) == 1 and partial in assistants[0].text
            finally:
                await reader.close()
            details = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
            assert len(details["results"]) == 1 and details["results"][0]["result"] == partial
    asyncio.run(scenario())


@pytest.mark.parametrize("retain", [False, True])
def test_normal_report_timing_preserves_reuse_and_does_not_wait_for_one_shot_cleanup(tmp_path: Path, retain: bool) -> None:
    """父收到正常报告后，保留型子可在资源关闭期间受理下一条消息。"""
    async def scenario() -> None:
        """阻塞实际 Agent 关闭，观察父 Inbox 和只读最终结果。"""
        cleaning, released = asyncio.Event(), asyncio.Event()

        class Harness(SubagentHarness):
            """仅为实际资源关闭设置屏障，保留全部业务执行。"""
            def _child(self, definition, manager):
                """给真实子 Agent 的关闭操作设置一次事件等待。"""
                agent = super()._child(definition, manager)
                original = agent.close

                async def close(**kwargs):
                    """在真实资源释放前等待测试观察正常报告。"""
                    cleaning.set()
                    await released.wait()
                    await original(**kwargs)

                agent.close = close
                return agent

        async def script(messages, tools):
            """正常完成当前委派。"""
            yield StreamChunk(content="正常完成")

        async with Harness(tmp_path / "state.sqlite3", script, retain=retain) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(cleaning.wait(), 3)
            try:
                details = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
                assert details["results"][0]["result"] == "正常完成"
                assert await asyncio.wait_for(harness.received.get(), 3)
                assert bool(await harness.scheduler.snapshot_subagents("parent")) == retain
                next_task = await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.COMMAND, "清理期间受理的下一项")
                assert next_task.accepted == retain
            finally:
                released.set()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            if retain:
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, next_task.context_id), 3)
    asyncio.run(scenario())


def test_destroy_retires_before_blocked_cleanup_and_never_notifies_parent(tmp_path: Path, monkeypatch) -> None:
    """销毁立即废弃，清理被阻塞时重复销毁仍成功，最终报告只留档。"""
    async def scenario() -> None:
        """让模型和资源释放分别停在事件上，观察公开语义。"""
        running, cleaning, released = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def script(messages, tools):
            """等待 destroy 停止当前模型调用。"""
            running.set()
            await asyncio.Event().wait()
            yield StreamChunk(content="不会执行")

        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(running.wait(), 3)
            original = harness.scheduler._sessions[receipt.context_id]._execution._agent.close

            async def close(**kwargs):
                """只阻塞资源释放，不改变生产执行逻辑。"""
                cleaning.set()
                await released.wait()
                await original(**kwargs)

            monkeypatch.setattr(harness.scheduler._sessions[receipt.context_id]._execution._agent, "close", close)
            try:
                destroyed = await asyncio.wait_for(harness.scheduler.interact_subagent(
                    "parent", receipt.context_id, SubagentAction.DESTROY), 0.5)
                assert destroyed.accepted
                assert not await harness.scheduler.snapshot_subagents("parent")
                assert not (await harness.scheduler.get_subagent_details("parent", receipt.context_id))["context_available"]
                repeated = await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.DESTROY)
                assert repeated.accepted
                rejected = await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.COMMAND, "再次执行")
                assert not rejected.accepted
                await asyncio.wait_for(cleaning.wait(), 3)
                assert harness.received.empty()
            finally:
                released.set()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            detail = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
            assert len(detail["results"]) == 1
            assert len([item for item in detail["recent_interactions"] if item["kind"] == "final"]) == 1
            manager = SessionStateManager("parent", SqliteFactory(harness.path))
            try:
                assert not await manager.read_task_terminals()
                assert harness.received.empty()
            finally:
                await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("committing,shutdown", [(False, False), (False, True), (True, False), (True, True)])
def test_parent_delete_preserves_response_and_waits_for_final_commit(
    tmp_path: Path, committing: bool, shutdown: bool,
) -> None:
    """删除父配置立即封闭入口，后台停止生成或等待已经开始的最终保存。"""
    async def scenario() -> None:
        """用真实父执行和保存屏障验证正文留档及共享连接的释放顺序。"""
        from app.agent.agent import Agent, AgentConfig
        from app.agent.context import ContextStrategyConfig
        from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
        from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
        from tests.runtime_support import configured_settings

        entered, released, cleaning, disconnected = (asyncio.Event() for _ in range(4))
        save_cancelled = asyncio.Event()
        closed = False
        response = "删除父角色前已生成的回答"
        path = str(tmp_path / "state.sqlite3")
        queue = asyncio.Queue()
        supervisor_settings = configured_settings("parent")
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(path)))

        class Manager(SessionStateManager):
            """最终提交开始后等待测试放行，其他事务保持原样。"""
            async def complete_turn(self, state, **kwargs):
                """父执行进入最终提交时记录屏障，并观察保存是否被取消。"""
                if committing:
                    entered.set()
                    try:
                        await released.wait()
                    except asyncio.CancelledError:
                        save_cancelled.set()
                        raise
                return await super().complete_turn(state, **kwargs)

        class Model:
            """生成固定正文，按场景等待停止或自然进入最终保存。"""
            async def astream(self, messages, *, tools=None):
                """保留完整 Agent 流程，只替换网络模型返回。"""
                yield StreamChunk(content=response)
                if not committing:
                    entered.set()
                    await asyncio.Event().wait()

            async def close(self) -> None:
                """记录模型已释放，验证共享连接在执行关闭后处理。"""
                nonlocal closed
                closed = True

        class Scheduler(AgentRuntimeScheduler):
            """为后台父清理开始设置屏障，不改变实际清理步骤。"""
            async def _cleanup_parent(self, session_id: str) -> None:
                """通知测试清理已经取得执行，再运行生产清理逻辑。"""
                cleaning.set()
                await super()._cleanup_parent(session_id)

        async def disconnect(session_id: str) -> None:
            """原模型关闭后才允许释放会话共享连接。"""
            assert closed
            disconnected.set()

        scheduler = build_test_scheduler(scheduler_type=Scheduler, runtime_queue=queue, task_supervisor=supervisor,
            chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations,
            agent_factory=lambda session_id, manager: Agent(
                AgentConfig(session_id=session_id, model_name="test", api_key="test",
                    context_strategy=ContextStrategyConfig()), llm_client=Model(),
                session_state_manager=manager, enable_plugin_hooks=False, sqlite_factory=SqliteFactory()),
            session_state_manager_factory=lambda session_id: Manager(session_id, SqliteFactory(path)),
            cleanup_session_connections=disconnect)
        service = ChatSettingsService(supervisor_settings, mutation_coordinator=supervisor_mutations)
        try:
            await scheduler.start()
            assert (await scheduler.submit(InteractionCommand("parent", "human_turn", {"message": "开始"}))).accepted
            await asyncio.wait_for(entered.wait(), 3)
            if shutdown:
                # 删除刚提交、后台清理尚未开始时退出，退出捕获的正文仍由原清理流程保存。
                assert (await delete_chat_settings("parent", service, scheduler)).code == 200
                await asyncio.wait_for(scheduler.begin_shutdown(), 0.5)
            else:
                assert (await asyncio.wait_for(delete_chat_settings("parent", service, scheduler), 0.5)).code == 200
            await asyncio.wait_for(cleaning.wait(), 3)
            assert not supervisor_settings.exists("parent")
            assert not (await scheduler.submit(InteractionCommand("parent", "human_turn", {"message": "迟到消息"}))).accepted
            if committing:
                assert not save_cancelled.is_set()
                assert not disconnected.is_set()
            released.set()
            await asyncio.wait_for(disconnected.wait(), 3)
            reader = SessionStateManager("parent", SqliteFactory(path))
            try:
                state = await reader.load()
                assert reader.last_loaded_checkpoint_type == CheckpointType.COMPLETED
                assistants = [item.text for item in state.messages if isinstance(item, AssistantMessage)]
                suffix = "……（被用户打断）"
                assert assistants == [response if committing else response + suffix]
                assert not state.pending_tool_calls and not state.interrupt_data
            finally:
                await reader.close()
        finally:
            released.set()
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_deleted_parent_retries_failed_agent_close_before_shared_connections(tmp_path: Path) -> None:
    """父 Agent 在关闭方法内重试客户端关闭，完成后释放共享连接。"""
    async def scenario() -> None:
        """实际父模型保持执行，删除触发关闭，客户端首次关闭失败后再次成功。"""
        from app.agent.agent import Agent, AgentConfig
        from app.agent.context import ContextStrategyConfig
        from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
        from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
        from tests.runtime_support import configured_settings

        running, failed, disconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()
        closes = 0
        queue = asyncio.Queue()
        supervisor_settings = configured_settings("parent")
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))

        class Model:
            """关闭故障可恢复的真实 Agent 模型替身。"""
            async def astream(self, messages, *, tools=None):
                """保持模型等待，让删除中断当前执行。"""
                running.set()
                await asyncio.Event().wait()
                yield StreamChunk(content="不会执行")

            async def close(self) -> None:
                """第一次关闭失败，后续释放成功。"""
                nonlocal closes
                closes += 1
                if closes == 1:
                    failed.set()
                    raise OSError("测试父模型关闭失败")

        def factory(session_id, manager):
            """保留完整父 Agent 取消和关闭流程。"""
            return Agent(AgentConfig(session_id=session_id, model_name="test", api_key="test",
                context_strategy=ContextStrategyConfig()),
                session_state_manager=manager, llm_client=Model(), enable_plugin_hooks=False, sqlite_factory=SqliteFactory())

        async def disconnect(session_id: str) -> None:
            """所有 Agent 资源成功关闭后才释放共享连接。"""
            assert closes == 2
            disconnected.set()

        scheduler = build_test_scheduler(runtime_queue=queue, agent_factory=factory, task_supervisor=supervisor,
            session_state_manager_factory=lambda context: SessionStateManager(context, SqliteFactory(supervisor.records.db_path)),
            cleanup_session_connections=disconnect, chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        service = ChatSettingsService(supervisor_settings, mutation_coordinator=supervisor_mutations)
        try:
            await scheduler.start()
            assert (await scheduler.submit(InteractionCommand("parent", "human_turn", {"message": "开始"}))).accepted
            await asyncio.wait_for(running.wait(), 3)
            assert (await delete_chat_settings("parent", service, scheduler)).code == 200
            await asyncio.wait_for(failed.wait(), 3)
            assert not disconnected.is_set()
            await asyncio.wait_for(disconnected.wait(), 3)
            assert closes == 2
        finally:
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("delete_parent", [False, True])
def test_parent_retries_interrupted_save_before_closing_agent(tmp_path: Path, monkeypatch, delete_parent: bool) -> None:
    """父停止或删除时保存暂时失败，原执行保留正文并在保存成功后才关闭。"""
    async def scenario() -> None:
        """在中断事务首次失败后阻塞重试，检查实际资源和完成通知顺序。"""
        from app.agent.agent import Agent, AgentConfig
        from app.agent.context import ContextStrategyConfig
        from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
        from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
        from tests.runtime_support import configured_settings

        running, retrying, released, closed = (asyncio.Event() for _ in range(4))
        attempts = 0
        calls = 0
        partial = "保存恢复后仍然保留的父回答"
        path = str(tmp_path / "state.sqlite3")
        from app.crud.agent_state_dao import AgentStateDao
        original = AgentStateDao._write_state_in_transaction

        async def save(dao, db, state, **kwargs):
            """首次事务内部遇到 BUSY，第二次保存原先准备的中断材料。"""
            nonlocal attempts
            if kwargs.get("checkpoint_type") == CheckpointType.COMPLETED:
                attempts += 1
                if attempts == 1:
                    error = sqlite3.OperationalError("测试首次中断保存失败")
                    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise error
                retrying.set()
                await released.wait()
            return await original(dao, db, state, **kwargs)

        class Model:
            """产生部分正文后等待停止，关闭时只记录资源已释放。"""
            async def astream(self, messages, *, tools=None):
                """记录业务只执行一次，重试只重新提交保存。"""
                nonlocal calls
                calls += 1
                yield StreamChunk(content=partial)
                running.set()
                await asyncio.Event().wait()

            async def close(self) -> None:
                """供测试检查正文保存前不会释放实际模型实例。"""
                closed.set()

        queue = asyncio.Queue()
        supervisor_settings = configured_settings("parent")
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(path)))
        scheduler = build_test_scheduler(runtime_queue=queue, task_supervisor=supervisor,
            agent_factory=lambda session_id, manager: Agent(
                AgentConfig(session_id=session_id, model_name="test", api_key="test",
                    context_strategy=ContextStrategyConfig()), llm_client=Model(),
                session_state_manager=manager, enable_plugin_hooks=False, sqlite_factory=SqliteFactory()),
            session_state_manager_factory=lambda session_id: SessionStateManager(session_id, SqliteFactory(path)), chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        service = ChatSettingsService(supervisor_settings, mutation_coordinator=supervisor_mutations)
        monkeypatch.setattr(AgentStateDao, "_write_state_in_transaction", save)
        try:
            await scheduler.start()
            await scheduler.submit(InteractionCommand("parent", "human_turn", {"message": "开始"}))
            await asyncio.wait_for(running.wait(), 3)
            if delete_parent:
                assert (await asyncio.wait_for(delete_chat_settings("parent", service, scheduler), 0.5)).code == 200
            else:
                assert (await scheduler.submit(InteractionCommand("parent", "stop_turn"))).accepted
            await asyncio.wait_for(retrying.wait(), 3)
            assert not closed.is_set()
            released.set()
            await asyncio.wait_for(closed.wait(), 3)
            reader = SessionStateManager("parent", SqliteFactory(path))
            try:
                state = await reader.load()
                assert reader.last_loaded_checkpoint_type == CheckpointType.COMPLETED
                assert [item.text for item in state.messages if isinstance(item, AssistantMessage)] == [
                    partial + "……（被用户打断）"]
            finally:
                await reader.close()
            assert attempts == 2 and calls == 1
            if not delete_parent:
                events = []
                while True:
                    try:
                        events.append(await asyncio.wait_for(next_runtime_event(scheduler), 0.05))
                    except asyncio.TimeoutError:
                        break
                assert not any(item.kind == "error" for item in events)
                assert len([item for item in events if item.kind == "run_completed"]) == 1
        finally:
            released.set()
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("delete_parent", [False, True])
def test_parent_shutdown_closes_resources_when_interrupted_save_keeps_failing(
    tmp_path: Path, monkeypatch, delete_parent: bool,
) -> None:
    """父中断保存失败后退出仍关闭模型，数据库保留上次成功的检查点。"""
    async def scenario() -> None:
        """让中断保存明确失败，检查关闭资源后的数据库未包含失败正文。"""
        from app.agent.agent import Agent, AgentConfig
        from app.agent.context import ContextStrategyConfig
        from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
        from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
        from tests.runtime_support import configured_settings

        running, failed, closed = (asyncio.Event() for _ in range(3))
        calls = 0
        partial = "退出时保存失败的父回答"
        path = str(tmp_path / "state.sqlite3")

        async def fail(manager, *args, **kwargs):
            """模拟中断保存接口返回最终失败。"""
            failed.set()
            raise sqlite3.OperationalError("测试持续中断保存失败")

        class Model:
            """输出正文后等待停止，记录实际模型资源是否关闭。"""
            async def astream(self, messages, *, tools=None):
                """检查保存错误恢复过程没有重新调用模型。"""
                nonlocal calls
                calls += 1
                yield StreamChunk(content=partial)
                running.set()
                await asyncio.Event().wait()

            async def close(self) -> None:
                """记录退出释放实际客户端。"""
                closed.set()

        queue = asyncio.Queue()
        supervisor_settings = configured_settings("parent")
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(path)))
        scheduler = build_test_scheduler(runtime_queue=queue, task_supervisor=supervisor,
            agent_factory=lambda session_id, manager: Agent(
                AgentConfig(session_id=session_id, model_name="test", api_key="test",
                    context_strategy=ContextStrategyConfig()), llm_client=Model(),
                session_state_manager=manager, enable_plugin_hooks=False, sqlite_factory=SqliteFactory()),
            session_state_manager_factory=lambda session_id: SessionStateManager(session_id, SqliteFactory(path)), chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        service = ChatSettingsService(supervisor_settings, mutation_coordinator=supervisor_mutations)
        monkeypatch.setattr(SessionStateManager, "finish_interrupted_turn", fail)
        try:
            await scheduler.start()
            await scheduler.submit(InteractionCommand("parent", "human_turn", {"message": "开始"}))
            await asyncio.wait_for(running.wait(), 3)
            await scheduler.submit(InteractionCommand("parent", "stop_turn"))
            await asyncio.wait_for(failed.wait(), 3)
            if delete_parent:
                assert (await delete_chat_settings("parent", service, scheduler)).code == 200
            await scheduler.begin_shutdown()
            await asyncio.wait_for(scheduler.wait_for_workers(), 3)
            assert closed.is_set() and calls == 1
            await close_test_runtime(scheduler)
            reader = SessionStateManager("parent", SqliteFactory(path))
            try:
                state = await reader.load()
                assert reader.last_loaded_checkpoint_type == CheckpointType.INTERMEDIATE
                assert [item.text for item in state.messages if isinstance(item, AssistantMessage)] == []
            finally:
                await reader.close()
        finally:
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("retain", [False, True])
def test_child_shutdown_closes_resources_when_interrupted_save_keeps_failing(
    tmp_path: Path, monkeypatch, retain: bool,
) -> None:
    """子中断保存失败后退出仍关闭模型，数据库保留上次成功的检查点。"""
    async def scenario() -> None:
        """使用真实子执行和数据库，检查保存失败不阻断资源关闭。"""
        running, failed, closed = (asyncio.Event() for _ in range(3))
        calls = 0
        partial = "退出时保存失败的子回答"

        async def script(messages, tools):
            """产生待保存正文后等待停止。"""
            nonlocal calls
            calls += 1
            yield StreamChunk(content=partial)
            running.set()
            await asyncio.Event().wait()

        async def fail(*args, **kwargs):
            """让子 Agent 的实时中断保存一直失败。"""
            failed.set()
            raise sqlite3.OperationalError("测试持续子中断保存失败")

        async def close() -> None:
            """记录真实 Agent 关闭模型的调用。"""
            closed.set()

        async with SubagentHarness(tmp_path / "state.sqlite3", script, retain=retain) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(running.wait(), 3)
            execution = harness.scheduler._sessions[receipt.context_id]
            monkeypatch.setattr(execution._manager, "finish_interrupted_turn", fail)
            monkeypatch.setattr(execution._execution._agent.llm_client, "close", close)
            await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.STOP)
            await asyncio.wait_for(failed.wait(), 3)
            await harness.scheduler.begin_shutdown()
            await asyncio.wait_for(harness.scheduler.wait_for_workers(), 3)
            assert closed.is_set() and calls == 1
            await close_test_runtime(harness.scheduler)
            reader = SessionStateManager(receipt.context_id, SqliteFactory(harness.path),
                                         storage_scope=AgentStorageScope.SUBAGENT)
            try:
                state = await reader.load()
                assert reader.last_loaded_checkpoint_type == CheckpointType.INTERMEDIATE
                assert [item.text for item in state.messages if isinstance(item, AssistantMessage)] == []
            finally:
                await reader.close()
    asyncio.run(scenario())


def test_parent_delete_returns_before_cleanup_and_allows_late_task(tmp_path: Path) -> None:
    """父配置删除先拒绝 Agent 输入，旧来源的普通任务仍可登记结束。"""
    async def scenario() -> None:
        """使用同一真实配置 DAO 和短锁，只有最后连接释放设置屏障。"""
        entered, released = asyncio.Event(), asyncio.Event()

        async def script(messages, tools):
            """保持一个活跃的子执行，供删除停止。"""
            yield StreamChunk(content="子任务完成")

        async def connections(session_id: str) -> None:
            """等待测试放行实际资源释放。"""
            entered.set()
            await released.wait()

        class Work(BaseTask):
            """用于验证已删除身份仍可提交独立普通任务。"""
            async def run(self, runtime) -> str:
                """返回一次迟到任务结果。"""
                return "迟到任务完成"

        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            scheduler = harness.scheduler
            scheduler._cleanup_session_connections = connections
            service = ChatSettingsService(harness.supervisor_settings,
                mutation_coordinator=harness.supervisor_mutations)
            runtime = ToolRuntime("parent", "old-run", "work", "call", harness.supervisor, harness.queue,
                subagent_id=receipt.context_id)
            result = await asyncio.wait_for(delete_chat_settings("parent", service, scheduler), 0.5)
            assert result.code == 200
            try:
                assert not await scheduler.snapshot_subagents("parent")
                assert not (await scheduler.submit(InteractionCommand("parent", "human_turn", {"message": "迟到消息"}))).accepted
                await asyncio.wait_for(entered.wait(), 3)
                task_id = await runtime.submit_task(Work(), name="late", title="迟到任务", notify_agent=True)
                assert (await harness.supervisor.wait(task_id, timeout_ms=1000)).result == "迟到任务完成"
                assert not (await scheduler.get_subagent_details("parent", receipt.context_id))["context_available"]
            finally:
                released.set()
    asyncio.run(scenario())


def test_failed_config_replace_keeps_parent_callable(tmp_path: Path, monkeypatch) -> None:
    """文件替换失败时原父配置保留，接口失败也不启动资源清理。"""
    async def scenario() -> None:
        """在真实原子替换处失败，随后通过原入口再次受理输入。"""
        async def script(messages, tools):
            """完成由仍有效的父身份创建的委派。"""
            yield StreamChunk(content="已完成")

        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            dao = harness.supervisor_settings
            service = ChatSettingsService(dao, mutation_coordinator=harness.supervisor_mutations)
            before = dao.config_file.read_bytes()
            original = Path.replace

            def replace(path: Path, target):
                """只拒绝当前父配置的最终替换，其他文件照常处理。"""
                if target == dao.config_file:
                    raise OSError("测试文件替换失败")
                return original(path, target)

            monkeypatch.setattr(Path, "replace", replace)
            with pytest.raises(OSError, match="替换失败"):
                await service.delete_chat_settings("parent")
            assert dao.config_file.read_bytes() == before
            assert dao.exists("parent")
            receipt = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
    asyncio.run(scenario())


def test_retire_transaction_failure_keeps_identity_and_pending_input(tmp_path: Path) -> None:
    """未交付输入删除失败时，废弃时间与输入一起回滚，身份仍可以受派。"""
    async def scenario() -> None:
        """通过真实 SQLite 触发器拒绝废弃事务的输入删除。"""
        entered = asyncio.Event()

        async def script(messages, tools):
            """保持活动执行，随后写入尚未交付的引导输入。"""
            entered.set()
            await asyncio.Event().wait()
            yield StreamChunk(content="不会执行")

        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(entered.wait(), 3)
            # 1. 直接保留一次尚未交付的真实命令，令删除 SQL 的失败可观测。
            with closing(sqlite3.connect(harness.path)) as db:
                db.execute("INSERT INTO subagent_session_inbox(session_id,kind,payload_json,created_at) "
                    "VALUES(?,'agent_command','{}','now')", (receipt.context_id,))
                db.execute("CREATE TRIGGER refuse_discard BEFORE DELETE ON subagent_session_inbox "
                    "BEGIN SELECT RAISE(ABORT,'discard failed'); END")
                db.commit()
            try:
                failed = await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.DESTROY)
                assert not failed.accepted and "discard failed" in failed.detail
                assert (await harness.scheduler.get_subagent_details("parent", receipt.context_id))["context_available"]
                assert len(await harness.scheduler.snapshot_subagents("parent")) == 1
                with closing(sqlite3.connect(harness.path)) as db:
                    assert db.execute("SELECT count(*) FROM subagent_session_inbox WHERE status='pending'").fetchone() == (1,)
            finally:
                with closing(sqlite3.connect(harness.path)) as db:
                    db.execute("DROP TRIGGER refuse_discard")
                    db.commit()
                await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.DESTROY)
    asyncio.run(scenario())


@pytest.mark.parametrize("parent_delete", [False, True])
@pytest.mark.parametrize("work_first", [False, True])
def test_registration_and_retirement_keep_task_ownership(tmp_path: Path, parent_delete: bool, work_first: bool) -> None:
    """删除前登记的任务按范围停止，删除后迟到登记的任务可以继续结束。"""
    async def scenario() -> None:
        """用事件排定登记和删除顺序，分别验证范围停止和迟到登记完成。"""
        cleaned, started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def script(messages, tools):
            """先完成保留型委派，让普通任务的独立生命周期可观察。"""
            yield StreamChunk(content="可以继续委派")

        class Work(BaseTask):
            """已登记的工作等待删除，清理时报告完成。"""
            async def run(self, runtime) -> str:
                """任务一直等待停止。"""
                started.set()
                await release.wait()
                return "迟到任务完成"

            async def cleanup(self) -> None:
                """确认已登记的任务确实由后台停止并清理。"""
                cleaned.set()

        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            first = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, first.context_id), 3)
            supervisor = harness.supervisor
            service = ChatSettingsService(harness.supervisor_settings, mutation_coordinator=harness.supervisor_mutations)
            runtime = ToolRuntime("parent", "old-run", "work", "call", supervisor, harness.queue, subagent_id=first.context_id)

            async def register() -> str:
                """通过普通工具入口登记独立任务并记录固定来源。"""
                return await runtime.submit_task(Work(), name="work", title="竞争任务",
                    notify_agent=True)

            async def delete() -> None:
                """分别经过父配置删除入口或子销毁入口。"""
                if parent_delete:
                    await service.delete_chat_settings("parent")
                    harness.scheduler.schedule_parent_cleanup("parent")
                else:
                    await harness.scheduler.interact_subagent("parent", first.context_id, SubagentAction.DESTROY)

            if work_first:
                task_id = await register()
                await asyncio.wait_for(started.wait(), 3)
                await delete()
                await asyncio.wait_for(cleaned.wait(), 3)
                record = await supervisor.wait(task_id, timeout_ms=3000)
                assert record.status == "stopped"
            else:
                await delete()
                if parent_delete:
                    await asyncio.wait_for(harness.scheduler._parent_cleanup_workers["parent"], 3)
                else:
                    await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, first.context_id), 3)
                task_id = await register()
                await asyncio.wait_for(started.wait(), 3)
                release.set()
                record = await supervisor.wait(task_id, timeout_ms=3000)
                assert record.status == "completed" and record.result == "迟到任务完成"
            assert not await harness.scheduler.snapshot_subagents("parent")
    asyncio.run(scenario())




def test_new_input_arriving_during_previous_worker_close_runs_without_another_message(tmp_path: Path) -> None:
    """暂停 worker 释放连接期间受理的新输入，关闭完成后自动交接给新 worker。"""
    async def scenario() -> None:
        """仅阻塞旧 worker 的 Manager.close，第二条消息仍走公开提交入口。"""
        from app.agent.agent import Agent, AgentConfig
        from app.agent.context import ContextStrategyConfig
        from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
        from tests.runtime_support import configured_settings
        first, second, closing, released = (asyncio.Event() for _ in range(4))
        held = False

        class Model:
            """根据真实历史确认第二条输入已经自动执行。"""
            async def astream(self, messages, *, tools=None):
                """记录每次明确输入带来的模型调用。"""
                (second if "第二条" in str(messages) else first).set()
                yield StreamChunk(content="收到")

            async def close(self) -> None:
                """阻塞第一次实际 Agent 的资源关闭，输入仍可独立受理。"""
                nonlocal held
                if not held:
                    held = True
                    closing.set()
                    await released.wait()

        scheduler = build_test_scheduler(runtime_queue=asyncio.Queue(), chat_settings_dao=configured_settings("s"),
            session_state_manager_factory=lambda context: SessionStateManager(context, SqliteFactory(str(tmp_path / "state.sqlite3"))),
            agent_factory=lambda context, manager: Agent(AgentConfig(session_id=context, model_name="test", api_key="test",
                context_strategy=ContextStrategyConfig()), session_state_manager=manager, llm_client=Model(), enable_plugin_hooks=False, sqlite_factory=SqliteFactory()))
        try:
            await scheduler.start()
            assert (await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "第一条"}))).accepted
            await asyncio.wait_for(first.wait(), 3)
            assert (await scheduler.submit(InteractionCommand("s", "stop_turn"))).accepted
            await asyncio.wait_for(closing.wait(), 3)
            assert (await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "第二条"}))).accepted
            released.set()
            await asyncio.wait_for(second.wait(), 3)
        finally:
            released.set()
            await close_test_runtime(scheduler)
    asyncio.run(scenario())


def test_task_result_read_before_destroy_is_shown_without_waking_retired_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务通知查询与销毁交错时，界面收到结果，子会话保持关闭。

    Args:
        tmp_path (Path): 本场景独占的数据库目录。
        monkeypatch (pytest.MonkeyPatch): 在消费者的身份查询返回前设置屏障。
    """
    async def scenario() -> None:
        """任务完成后挂起通知查询，销毁原子提交并关闭资源后继续投递。"""
        entered, released, finish_work = asyncio.Event(), asyncio.Event(), asyncio.Event()
        model_calls = 0
        wakeups = 0

        async def script(messages, tools):
            """先完成保留型委派，普通后台工作单独等待。"""
            nonlocal model_calls
            model_calls += 1
            yield StreamChunk(content="委派完成")

        class Work(BaseTask):
            """在测试放行后正常完成，让消费者处理已保存的终态通知。"""
            async def run(self, runtime: TaskRuntime) -> str:
                """等待测试安排完成时机，再返回业务结果。

                Args:
                    runtime (TaskRuntime): Supervisor 提供的任务来源和运行能力。
                Returns:
                    str: 需要保存并展示的后台工作结果。
                """
                await finish_work.wait()
                return "后台工作完成"

        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            await asyncio.wait_for(harness.received.get(), 3)
            while (await harness.scheduler.snapshot("parent")).payload["phase"] != "idle":
                await asyncio.sleep(0)
            original = harness.scheduler._subagent_dao.get
            blocked = False

            async def get(session_id: str, subagent_id: str) -> SubagentRecord | None:
                """暂存消费者读到的可用身份，让销毁先完成再返回这份旧记录。

                Args:
                    session_id (str): 后台任务所属父上下文。
                    subagent_id (str): 本次通知的子接收者。
                Returns:
                    SubagentRecord | None: 查询当时的身份记录。
                """
                nonlocal blocked
                record = await original(session_id, subagent_id)
                if not blocked and asyncio.current_task() is harness.scheduler._task_reader:
                    assert record is not None and record.retired_at is None
                    blocked = True
                    entered.set()
                    await released.wait()
                return record

            def notify_inbox_changed() -> None:
                """记录消费者是否错误地唤醒已经关闭的原来的子会话。"""
                nonlocal wakeups
                wakeups += 1

            monkeypatch.setattr(harness.scheduler._subagent_dao, "get", get)
            child = harness.scheduler._sessions[receipt.context_id]
            monkeypatch.setattr(child, "notify_inbox_changed", notify_inbox_changed)
            runtime = ToolRuntime("parent", "run", "work", "call", harness.supervisor, harness.queue,
                subagent_id=receipt.context_id)
            task_id = await runtime.submit_task(Work(), name="work", title="后台工作",
                notify_agent=True)
            try:
                # 1. 任务正常结束，消费者在唤醒子 Agent 前读到仍可用的身份。
                finish_work.set()
                await asyncio.wait_for(entered.wait(), 3)
                assert (await harness.supervisor.wait(task_id, timeout_ms=3000)).status == TaskStatus.COMPLETED
                inbox = SessionStateManager(receipt.context_id, SqliteFactory(harness.path),
                    storage_scope=AgentStorageScope.SUBAGENT, parent_session_id="parent")
                try:
                    notices = await inbox.read_task_terminals()
                    assert [item.payload["task_id"] for item in notices] == [task_id]
                finally:
                    await inbox.close()
                # 2. 让销毁完成资源关闭；消费者恢复时拿到的仍是销毁前的旧记录。
                destroyed = await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.DESTROY)
                assert destroyed.accepted
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            finally:
                finish_work.set()
                released.set()

            # 3. 界面继续展示真实结果，销毁清除未交付通知，子会话保持关闭。
            await asyncio.wait_for(harness.queue.join(), 3)
            statuses = []
            while True:
                try:
                    event = await asyncio.wait_for(next_runtime_event(harness.scheduler), 0.05)
                except asyncio.TimeoutError:
                    break
                if event.kind == InteractionEventKind.TASK_STATUS and event.task_id == task_id:
                    statuses.append(event.payload["status"])
            assert statuses == [TaskStatus.RUNNING, TaskStatus.COMPLETED]
            assert wakeups == 0 and model_calls == 1
            assert receipt.context_id not in harness.scheduler._sessions
            assert not await harness.scheduler.snapshot_subagents("parent")
            inbox = SessionStateManager(receipt.context_id, SqliteFactory(harness.path),
                storage_scope=AgentStorageScope.SUBAGENT, parent_session_id="parent")
            try:
                assert not await inbox.read_task_terminals()
            finally:
                await inbox.close()
    asyncio.run(scenario())
