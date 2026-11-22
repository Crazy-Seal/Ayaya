"""通过持久输入、快照与生命周期入口验证退出后的执行意图作废。"""
from tests.runtime_support import runtime_hub, close_test_runtime
from tests.runtime_event_source import observe_session
from app.schemas.runtime_types import AgentRunOutcome, AgentRunOutcomeKind, CheckpointType
from tests.runtime_support import next_runtime_event
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory

from tests.runtime_support import finish_task_record
from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator
from tests.runtime_support import RuntimeAgentStub
from app.agent.state.manager import task_result_messages
from tests.runtime_support import configured_settings
from app.agent.core.events import AgentEvent, EventType
from tests.model_capture import result_parts
from tests.model_capture import capture_input

import asyncio

import pytest

from app.agent.agent import Agent
from app.agent.state.manager import SessionStateManager
from app.agent.state import AgentState
from app.agent.state.messages import HumanMessage, TaskResultMessage, ToolCall, ToolMessage
from app.agent.state.messages import AssistantMessage, ImageContent, ToolMessage
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.interaction.interaction_contract import InteractionCommand
from app.interaction.interaction_event_hub import InteractionEventHub


def test_shutdown_retries_parent_resource_still_owned_by_session(tmp_path) -> None:
    """退出首次关闭父资源失败时，会话保留句柄并在共享存储关闭前继续释放。"""
    async def scenario() -> None:
        """通过退出后的资源释放结果检查所有权交接。"""
        path = str(tmp_path / "state.sqlite3")
        entered, released = asyncio.Event(), asyncio.Event()
        failure_injected = False

        class TestAgent(RuntimeAgentStub):
            """执行保持等待，关闭资源在退出期间暂时失败一次。"""

            def __init__(self, manager: SessionStateManager) -> None:
                """借用原 worker 的状态对象。

                Args:
                    manager (SessionStateManager): 当前上下文的持久状态业务对象。
                """
                self.manager = manager

            async def run(self, message, *, emit, **kwargs):
                """交付输入后等待应用退出取消执行。"""
                outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED)
                state = await self.manager.load()
                self.manager.append_human_input(state, message, None, inputs=kwargs["inbox_inputs"])
                await self.manager.save(state, checkpoint_type=CheckpointType.INTERMEDIATE, inbox_inputs=kwargs["inbox_inputs"])
                await emit(AgentEvent(EventType.RUN_STARTED, {"resumed": False}))
                entered.set()
                await asyncio.Event().wait()

                return outcome

            async def close(self) -> None:
                """资源所有者在自身关闭方法内完成有限重试。"""
                import logging
                from app.agent.utils.infra.bounded_operation import close_resources

                async def release() -> None:
                    """第一次关闭暂时失败，第二次释放实际资源。"""
                    nonlocal failure_injected
                    if not failure_injected:
                        failure_injected = True
                        raise OSError("暂时无法关闭父资源")
                    released.set()

                await close_resources((("测试资源", release),), logger=logging.getLogger(__name__))

        scheduler = build_test_scheduler(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(),
            agent_factory=lambda _session, manager: TestAgent(manager),
            session_state_manager_factory=lambda context: SessionStateManager(context, SqliteFactory(path)))
        try:
            await scheduler.start()
            await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "执行任务"}))
            await asyncio.wait_for(entered.wait(), 2)
            await close_test_runtime(scheduler)
            assert released.is_set()
        finally:
            await close_test_runtime(scheduler)

    asyncio.run(scenario())


def test_restart_discards_humans_and_preserves_pending_notices(tmp_path) -> None:
    """验证重启清理删除所有旧意图，保留未交付任务通知。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """安排各种交付状态并核对清理事务的持久结果。"""
        path = str(tmp_path / "state.sqlite3")
        inbox = SessionStateManager("s", SqliteFactory(path))
        records = TaskRecordDao(SqliteFactory(path))
        manager = SessionStateManager("s", SqliteFactory(path))
        try:
            # 1. 已交付真人输入保留，其他真人输入覆盖读取和两种排队放行状态。
            await inbox.submit_human("已交付")
            delivered = await inbox.read_next()
            state = AgentState.create_new("s")
            state.add_user_message("已交付")
            await manager.save(state, checkpoint_type="completed", inbox_inputs=(delivered,))
            await inbox.submit_human("已读取")
            claimed_human = await inbox.read_next()
            await inbox.submit_human("已放行", queued=True)
            await inbox.release_queued()
            await inbox.submit_human("未放行", queued=True)
            await inbox.submit_human("立即输入")
            # 2. 遗留任务通知已读取但尚未交付，重启后仍然保留待交付状态。
            await records.insert_running("task", "s", "测试任务", 1, str(tmp_path / "out"))
            await records.finish_once("task", "completed", 0)
            terminal = (await inbox.read_task_terminals())[0]
            await inbox.reset_after_restart()
            await inbox.reset_after_restart()
            assert await inbox.pending_humans() == []
            assert await inbox.status(delivered.id) == "delivered"
            assert await inbox.status(claimed_human.id) is None
            restored = (await inbox.read_task_terminals())[0]
            assert restored.payload == terminal.payload
            assert restored.id == terminal.id and restored.status == "pending"
            # 3. 查询没有令牌；原始通知仍可交付，重复交付必须整笔回滚。
            await manager.save(state, checkpoint_type="completed", inbox_inputs=(terminal,))
            with pytest.raises(RuntimeError, match="输入已失效"):
                await manager.save(state, checkpoint_type="intermediate", inbox_inputs=(restored,))
            assert (await manager.load()).messages == state.messages
        finally:
            await inbox.close()
            await records.close()
            await manager.close()
    asyncio.run(scenario())


def test_restart_reset_preserves_humans_when_deletion_fails(tmp_path) -> None:
    """验证真人输入删除失败保留原输入和任务通知，修复后可重新清理。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """用 SQLite 触发器注入第二步失败，检查整笔事务回滚。"""
        path = str(tmp_path / "state.sqlite3")
        inbox = SessionStateManager("s", SqliteFactory(path))
        records = TaskRecordDao(SqliteFactory(path))
        try:
            await inbox.submit_human("必须原子作废")
            await records.insert_running("task", "s", "任务", 1, str(tmp_path / "out"))
            await records.finish_once("task", "completed", 0)
            notice = (await inbox.read_task_terminals())[0]
            db = await inbox._state_dao._get_db()
            await db.execute("CREATE TRIGGER reject_reset BEFORE DELETE ON session_inbox "
                             "WHEN OLD.kind = 'human' BEGIN SELECT RAISE(ABORT, 'injected'); END")
            await db.commit()
            with pytest.raises(Exception, match="injected"):
                await inbox.reset_after_restart()
            assert len(await inbox.pending_humans()) == 1
            assert await inbox.status(notice.id) == "pending"
            await db.execute("DROP TRIGGER reject_reset")
            await db.commit()
            await inbox.reset_after_restart()
            assert await inbox.pending_humans() == []
            assert await inbox.status(notice.id) == "pending"
        finally:
            await inbox.close()
            await records.close()
    asyncio.run(scenario())


def test_abandoned_result_preserves_media_and_commit_failure_is_retryable(tmp_path) -> None:
    """验证已有回报和已完成工具图片保留，收尾事务失败可重试且不重复结果。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """以已有工具媒体和桌面回报组成未完成批次，验证原子收尾。"""
        path = str(tmp_path / "state.sqlite3")
        manager = SessionStateManager("s", SqliteFactory(path))
        try:
            state = AgentState.create_new("s")
            calls = [ToolCall(id=str(index), name="draw", args={}) for index in range(3)]
            state.record_assistant_step("正文", calls)
            image = "data:image/png;base64,iVBORw0KGgpwYXlsb2Fk"
            first = state.record_tool_result(calls[0], result_parts('已完成', (image,), 'tool'))
            state.clear_pending_tool_calls()
            state.set_interrupt({
                "type": "authorization_request", "request_id": "r",
                "tool_call_id": calls[1].id, "tool_name": calls[1].name,
                "resume_state": {},
            }, (calls[2],))
            await manager.save(state, checkpoint_type="intermediate")
            db = await manager._state_dao._get_db()
            await db.execute("CREATE TRIGGER reject_checkpoint BEFORE INSERT ON checkpoints "
                             "WHEN NEW.checkpoint_type = 'completed' BEGIN SELECT RAISE(ABORT, 'injected'); END")
            await db.commit()
            with pytest.raises(Exception, match="injected"):
                await manager.finalize_abandoned_turn()
            assert (await manager.load()).interrupt_data is not None
            await db.execute("DROP TRIGGER reject_checkpoint")
            await db.commit()
            assert await manager.finalize_abandoned_turn()
            closed = await manager.load()
            results = [item for item in closed.messages if isinstance(item, ToolMessage)]
            media = [item for item in closed.messages if isinstance(item, ToolMessage) and any(isinstance(part, ImageContent) for part in item.content)]
            assert len(results) == 3 and results[0].message_id == first.message_id
            assert len(media) == 1
            assert all(item.tool_call_id in {result.tool_call_id for result in results} for item in media)
            assert not await manager.finalize_abandoned_turn()
            assert (await manager.load()).messages == closed.messages
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("persisted", [False, True])
def test_shutdown_preserves_partial_once_and_keeps_message_identity(tmp_path, persisted) -> None:
    """验证关闭时的真实片段保存一次，已持久片段只更新原消息。

    Args:
        tmp_path (Path): 隔离数据库目录。
        persisted (bool): 片段是否已进入 checkpoint。
    """
    async def scenario() -> None:
        """调用无执行收尾入口检查消息身份和终止后缀。"""
        path = str(tmp_path / "state.sqlite3")
        manager = SessionStateManager("s", SqliteFactory(path))
        state = AgentState.create_new("s")
        state.add_user_message("任务")
        message = state.add_assistant_message("可见片段") if persisted else None
        try:
            await manager.save(state, checkpoint_type="intermediate")
            assert await manager.finalize_abandoned_turn( partial="可见片段", partial_persisted=persisted)
            closed = await manager.load()
            assistants = [item for item in closed.messages if isinstance(item, AssistantMessage)]
            assert len(assistants) == 1
            assert assistants[0].text == "可见片段……（应用退出，本轮已终止）"
            if message is not None:
                assert assistants[0].message_id == message.message_id
            assert not await manager.finalize_abandoned_turn( partial="可见片段", partial_persisted=persisted)
            assert (await manager.load()).messages == closed.messages
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["approval", "approved", "ordinary", "human"])
def test_start_finalizes_abandoned_checkpoint_without_execution(tmp_path, case) -> None:
    """验证各种遗留 checkpoint 启动后闭合，快照及重复启动不执行工具。

    Args:
        tmp_path (Path): 隔离数据库目录。
        case (str): 遗留执行阶段。
    """
    async def scenario() -> None:
        """持久保存遗留状态，通过正式启动入口检查结果。"""
        path = str(tmp_path / "state.sqlite3")
        state = AgentState.create_new("s")
        human = state.add_user_message("旧任务")
        if case != "human":
            first = ToolCall(id="first", name="read_file", args={})
            later = ToolCall(id="later", name="write_file", args={})
            state.record_assistant_step("已保存正文", (first, later))
            if case != "ordinary":
                state.clear_pending_tool_calls()
                interrupt = {
                    "type": "authorization_request",
                    "request_id": "old-request", "tool_call_id": first.id,
                    "tool_name": first.name, "tool_args": {}, "resume_state": {},
                    "data": {"arguments": {}},
                }
                if case == "approved":
                    interrupt["runtime_decision"] = {"kind": "approve"}
                state.set_interrupt(interrupt, (later,))
        manager = SessionStateManager("s", SqliteFactory(path))
        await manager.save(state, checkpoint_type="intermediate")
        await manager.close()

        def unexpected_agent(_session, _manager):
            """任何启动期间的执行对象构造都使测试失败。

            Args:
                _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
                _session (str): 请求执行的会话。
            """
            raise AssertionError("启动和快照不能构造可执行 Agent")

        scheduler = build_test_scheduler(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(),
           agent_factory=unexpected_agent,
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)),
        )
        try:
            await scheduler.start()
            await scheduler.start()
            assert ((await scheduler.snapshot("s")).payload)["phase"] == "idle"
            manager = SessionStateManager("s", SqliteFactory(path))
            try:
                closed = await manager.load()
                assert manager.last_loaded_checkpoint_type == "completed"
                assert closed.messages[0] == human
                assert closed.interrupt_data is None and not closed.pending_tool_calls
                results = [item for item in closed.messages if isinstance(item, ToolMessage)]
                assert len(results) == (0 if case == "human" else 2)
                original = closed.to_checkpoint()
                assert not await manager.finalize_abandoned_turn()
                assert (await manager.load()).to_checkpoint() == original
            finally:
                await manager.close()
        finally:
            await close_test_runtime(scheduler)
    asyncio.run(scenario())


def test_terminal_waits_for_new_human_and_shutdown_cannot_reactivate(tmp_path) -> None:
    """验证通知留存到新输入，运行内终态可激活，关闭期间的记录不能再执行。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """使用真实 Inbox 交付和隔离执行替身验证生命周期规则。"""
        path = str(tmp_path / "state.sqlite3")
        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(path)))
        records = supervisor.records
        await records.insert_running("old", "s", "旧任务", 1, str(tmp_path / "out"))
        await finish_task_record(supervisor, "old", "completed")
        calls = []
        executed = asyncio.Event()

        class RecordingAgent(RuntimeAgentStub):
            """通过真实持久交付记录激活时的输入顺序。"""
            async def run(self, message, *, inbox_input=None, inbox_inputs=None, runtime_inputs=(), emit, **kwargs):
                """保存通知和真人消息，并结束测试轮次。

                Args:
                    message (str): 新真人正文。
                    inbox_input (InboxEnvelope | None): 真人读取。
                    inbox_inputs (tuple | None): 合并读取。
                    runtime_inputs (tuple): 先交付的任务事实。
                    **kwargs (dict): 其余运行参数。
                Yields:
                    AgentEvent: 轮次完成事件。
                """
                outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED)
                await emit(AgentEvent(EventType.RUN_STARTED, {"resumed": False}))
                manager = SessionStateManager("s", SqliteFactory(path))
                try:
                    state = await manager.load()
                    for item in task_result_messages(tuple(item for item in inbox_inputs or () if item.kind == "task_terminal")):
                        state.append_input(item)
                    state.add_user_message(message)
                    await manager.save(state, checkpoint_type="completed", inbox_inputs=inbox_inputs or (inbox_input,))
                finally:
                    await manager.close()
                calls.append(message)
                executed.set()
                await emit(AgentEvent(EventType.DONE, None))

                return outcome

            async def run_runtime_inputs(self, messages=None, *, inbox_inputs, emit, **kwargs):
                """记录本次运行内通知启动的内部轮次。

                Args:
                    messages (tuple): 任务通知消息。
                    inbox_inputs (tuple): 通知读取。
                    **kwargs (dict): 其余运行参数。
                Yields:
                    AgentEvent: 轮次完成事件。
                """
                outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED)
                await emit(AgentEvent(EventType.RUN_STARTED, {"resumed": False}))
                manager = SessionStateManager("s", SqliteFactory(path))
                try:
                    state = await manager.load()
                    for item in messages if messages is not None else task_result_messages(inbox_inputs):
                        state.append_input(item)
                    await manager.save(state, checkpoint_type="completed", inbox_inputs=inbox_inputs)
                finally:
                    await manager.close()
                calls.append("内部通知")
                executed.set()
                await emit(AgentEvent(EventType.DONE, None))

                return outcome

            async def close(self):
                """替身不拥有外部资源。"""

        scheduler = build_test_scheduler(runtime_queue=supervisor.runtime_queue,
            task_supervisor=supervisor,
            agent_factory=lambda _, _manager: RecordingAgent(),
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)),
         chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        try:
            assert not (await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "启动前"}))).accepted
            await scheduler.start()
            event = await asyncio.wait_for(next_runtime_event(scheduler), 1)
            assert event.kind == "task_status"
            assert calls == []
            assert (await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "新意图"}))).accepted
            await asyncio.wait_for(executed.wait(), 2)
            manager = SessionStateManager("s", SqliteFactory(path))
            try:
                messages = (await manager.load()).messages
                assert isinstance(messages[0], TaskResultMessage)
                assert isinstance(messages[1], HumanMessage)
            finally:
                await manager.close()
            executed.clear()
            await records.insert_running("new", "s", "新任务", 1, str(tmp_path / "out"))
            await finish_task_record(supervisor, "new", "completed")
            await asyncio.wait_for(executed.wait(), 2)
            await scheduler.begin_shutdown()
            await records.insert_running("closing", "s", "关闭任务", 1, str(tmp_path / "out"))
            await finish_task_record(supervisor, "closing", "stopped")
            assert not (await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "关闭后"}))).accepted
            await close_test_runtime(scheduler)
            await close_test_runtime(scheduler)
            assert calls == ["新意图", "内部通知"]
            inbox = SessionStateManager("s", SqliteFactory(path))
            try:
                assert len(await inbox.read_task_terminals()) == 1
            finally:
                await inbox.close()
        finally:
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_start_failure_closes_instance_and_new_instance_finishes_cleanup(tmp_path, monkeypatch) -> None:
    """验证启动收尾失败拒绝命令，重试完成后仍不激活旧任务。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (MonkeyPatch): 收尾故障注入。
    """
    async def scenario() -> None:
        """在启动步骤之间注入失败，再通过相同入口重试。"""
        path = str(tmp_path / "state.sqlite3")
        manager = SessionStateManager("s", SqliteFactory(path))
        state = AgentState.create_new("s")
        state.add_user_message("已交付旧意图")
        await manager.save(state, checkpoint_type="intermediate")
        await manager.close()
        original = SessionStateManager.finalize_abandoned_turn

        async def fail_finalize(state_manager, **kwargs):
            """注入收尾失败，确保执行开放步骤未被绕过。

            Args:
                state_manager (SessionStateManager): 当前会话管理器。
                **kwargs (dict): 正常关闭片段参数。
            Raises:
                RuntimeError: 固定测试失败。
            """
            raise RuntimeError("injected")

        def unexpected_agent(_session, _manager):
            """确保启动故障及重试都不构造执行对象。

            Args:
                _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
                _session (str): 请求激活的会话。
            """
            raise AssertionError("启动不能执行")

        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(path)))
        scheduler = build_test_scheduler(runtime_queue=supervisor.runtime_queue,
            task_supervisor=supervisor,
            agent_factory=unexpected_agent,
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)),
         chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        try:
            monkeypatch.setattr(SessionStateManager, "finalize_abandoned_turn", fail_finalize)
            with pytest.raises(RuntimeError, match="injected"):
                await scheduler.start()
            assert scheduler.phase == "stopped"
            assert scheduler._task_reader is None
            assert not (await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "失败后"}))).accepted
            monkeypatch.setattr(SessionStateManager, "finalize_abandoned_turn", original)
            with pytest.raises(RuntimeError, match="只能启动一次"):
                await scheduler.start()
            await close_test_runtime(scheduler)
            scheduler = build_test_scheduler(runtime_queue=asyncio.Queue(), agent_factory=unexpected_agent,
                session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)),
                chat_settings_dao=supervisor_settings)
            await scheduler.start()
            assert scheduler.phase == "running"
            assert scheduler._task_reader is not None
            assert ((await scheduler.snapshot("s")).payload)["phase"] == "idle"
        finally:
            monkeypatch.setattr(SessionStateManager, "finalize_abandoned_turn", original)
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())




@pytest.mark.parametrize("committing", [False, True])
def test_shutdown_captures_real_agent_partial_and_closes_subscription(tmp_path, committing) -> None:
    """验证真实执行中的模型片段在生命周期关闭后持久保存且订阅结束。

    Args:
        tmp_path (Path): 隔离数据库目录。
        committing (bool): 是否在内存正文已记录但最终提交尚未完成时关闭。
    """
    async def scenario() -> None:
        """通过真实 Agent 的流式输出和公共订阅安排关闭时机。"""
        from app.agent.agent import AgentConfig
        from app.agent.context import ContextStrategyConfig
        from app.agent.models.llm_client import StreamChunk
        path = str(tmp_path / "state.sqlite3")
        commit_entered = asyncio.Event()
        commit_released = asyncio.Event()


        class WaitingLLM:
            """产生一段真实可见输出后保持模型读取等待。"""
            async def astream(self, messages, tools=None):
                """只提供一段输出，其余等待由生命周期取消。

                Args:
                    messages (list): 输入消息。
                    tools (list | None): 工具定义。
                Yields:
                    StreamChunk: 真实可见正文。
                """
                messages = capture_input(messages)
                yield StreamChunk(content="真实片段")
                if not committing:
                    await asyncio.Event().wait()

            async def close(self):
                """模型替身不访问外部网络。"""

        def make_agent(session, _manager):
            """注入隔离模型和存储，保留真实 Agent 行为。

            Args:
                _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
                session (str): 共享会话。
            Returns:
                Agent: 真实执行对象。
            """
            agent = Agent(AgentConfig(session_id=session, model_name="test", api_key="test",
                                     context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)),
                         sqlite_factory=SqliteFactory(path), llm_client=WaitingLLM(), session_state_manager=_manager)
            if committing:
                original_save = _manager.save

                async def delayed_save(state, **kwargs):
                    """只阻塞借用对象的正常完成提交，保持 worker 的关闭所有权。

                    Args:
                        state (AgentState): 本次执行状态。
                        **kwargs (dict): 原保存参数。

                    Returns:
                        int: 已保存的 checkpoint 编号。
                    """
                    if kwargs.get("checkpoint_type") == "completed":
                        commit_entered.set()
                        await commit_released.wait()
                    return await original_save(state, **kwargs)

                _manager.save = delayed_save
            return agent

        scheduler = build_test_scheduler(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), agent_factory=make_agent,
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)),
        )
        hub = runtime_hub(scheduler)
        pass
        await scheduler.start()
        stream = observe_session("s", hub, scheduler)
        await anext(stream)
        try:
            await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "新任务"}))
            while (await asyncio.wait_for(anext(stream), 2)).kind != "text_chunk":
                pass
            if committing:
                await asyncio.wait_for(commit_entered.wait(), 2)
            await asyncio.wait_for(scheduler.begin_shutdown(), 2)
            await hub.close()
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            await hub.close()
            if committing:
                # 最终提交已经开始，退出仍先关闭订阅，再等待原保存完成。
                assert scheduler._sessions["s"]._worker is not None
                assert not scheduler._sessions["s"]._worker.done()
            commit_released.set()
            await close_test_runtime(scheduler)
            manager = SessionStateManager("s", SqliteFactory(path))
            try:
                state = await manager.load()
                expected = "真实片段" if committing else "真实片段……（应用退出，本轮已终止）"
                assert state.messages[-1].text.startswith("真实片段")
                if not committing:
                    assert "终止" in state.messages[-1].text or "打断" in state.messages[-1].text
                assert manager.last_loaded_checkpoint_type == "completed"
            finally:
                await manager.close()
        finally:
            commit_released.set()
            await hub.close()
            await close_test_runtime(scheduler)
            await stream.aclose()
    asyncio.run(scenario())
