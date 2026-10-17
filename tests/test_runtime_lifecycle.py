from tests.model_capture import result_parts
"""通过持久输入、快照与生命周期入口验证退出后的执行意图作废。"""
from tests.model_capture import capture_input

import asyncio

import pytest

from app.agent.agent import Agent
from app.agent.runtime.session_inbox import SessionInbox
from app.agent.state import AgentState
from app.agent.state.manager import StateManager
from app.agent.state.messages import HumanMessage, TaskResultMessage, ToolCall, ToolMessage
from app.agent.state.messages import AssistantMessage, ImageContent, ToolMessage
from app.crud.task_record_dao import TaskRecordDao
from app.orchestration.agent_runtime_scheduler import AgentRuntimeScheduler
from app.interaction.interaction_contract import ChannelCommand
from app.interaction.interaction_event_hub import InteractionEventHub


def test_restart_discards_humans_and_releases_terminal_claims(tmp_path) -> None:
    """验证重启清理删除所有旧意图，保留通知内容并使旧领取失效。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """安排各种交付状态并核对清理事务的持久结果。"""
        path = str(tmp_path / "state.sqlite3")
        inbox = SessionInbox("s", path)
        records = TaskRecordDao(path)
        manager = StateManager("s", path)
        try:
            # 1. 已交付真人输入保留，其他真人输入覆盖领取和两种排队放行状态。
            await inbox.submit_human("desktop", "已交付")
            delivered = await inbox.claim_next()
            state = AgentState.create_new("s")
            state.add_user_message("已交付")
            await manager.save(state, checkpoint_type="completed", inbox_claims=(delivered,))
            await inbox.submit_human("desktop", "已领取")
            claimed_human = await inbox.claim_next()
            await inbox.submit_human("desktop", "已放行", queued=True)
            await inbox.release_queued()
            await inbox.submit_human("desktop", "未放行", queued=True)
            await inbox.submit_human("desktop", "立即输入")
            # 2. 遗留任务通知已领取但尚未交付，重启后无需等待旧租期。
            await records.insert_running("task", "s", "测试任务", 1, str(tmp_path / "out"))
            await records.finish_once("task", "completed", 0, "执行事实")
            terminal = (await inbox.claim_task_terminals())[0]
            await SessionInbox.reset_after_restart(path)
            await SessionInbox.reset_after_restart(path)
            assert await inbox.pending_humans() == []
            assert await inbox.status(delivered.id) == "delivered"
            assert await inbox.status(claimed_human.id) is None
            restored = (await inbox.claim_task_terminals())[0]
            assert restored.payload == terminal.payload
            assert restored.claim_token != terminal.claim_token
            # 3. 旧令牌的交付不能写入 checkpoint，新的领取仍然有效。
            with pytest.raises(RuntimeError, match="claim 已失效"):
                await manager.save(state, checkpoint_type="intermediate", inbox_claims=(terminal,))
            assert manager.last_loaded_checkpoint_type is None
            assert (await manager.load()).messages == state.messages
            await manager.save(state, checkpoint_type="completed", inbox_claims=(restored,))
        finally:
            await inbox.close()
            await records.close()
            await manager.close()
    asyncio.run(scenario())


def test_restart_reset_rolls_back_human_deletion_when_notice_release_fails(tmp_path) -> None:
    """验证通知重置失败不会只删除真人输入，重试可完成全部清理。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """用 SQLite 触发器注入第二步失败，检查整笔事务回滚。"""
        path = str(tmp_path / "state.sqlite3")
        inbox = SessionInbox("s", path)
        records = TaskRecordDao(path)
        try:
            await inbox.submit_human("desktop", "必须原子作废")
            await records.insert_running("task", "s", "任务", 1, str(tmp_path / "out"))
            await records.finish_once("task", "completed", 0, "事实")
            notice = (await inbox.claim_task_terminals())[0]
            db = await inbox._get_db()
            await db.execute("CREATE TRIGGER reject_reset BEFORE UPDATE ON session_inbox "
                             "WHEN OLD.kind = 'task_terminal' BEGIN SELECT RAISE(ABORT, 'injected'); END")
            await db.commit()
            with pytest.raises(Exception, match="injected"):
                await SessionInbox.reset_after_restart(path)
            assert len(await inbox.pending_humans()) == 1
            assert await inbox.status(notice.id) == "claimed"
            await db.execute("DROP TRIGGER reject_reset")
            await db.commit()
            await SessionInbox.reset_after_restart(path)
            assert await inbox.pending_humans() == []
            assert await inbox.status(notice.id) == "pending"
        finally:
            await inbox.close()
            await records.close()
    asyncio.run(scenario())


def test_abandoned_result_preserves_media_and_commit_failure_is_retryable(tmp_path) -> None:
    """验证已有回报和延迟媒体保留，收尾事务失败可重试且不重复结果。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """以已有工具媒体和桌面回报组成未完成批次，验证原子收尾。"""
        path = str(tmp_path / "state.sqlite3")
        manager = StateManager("s", path)
        try:
            state = AgentState.create_new("s")
            calls = [ToolCall(id=str(index), name="screenshot", args={}) for index in range(3)]
            state.record_assistant_step("正文", calls)
            image = "data:image/png;base64,iVBORw0KGgpwYXlsb2Fk"
            first = state.record_tool_result(calls[0], result_parts('已完成', (image,), 'tool'))
            state.clear_pending_tool_calls()
            state.set_interrupt({
                "type": "continuation_request", "request_id": "r",
                "tool_call_id": calls[1].id, "tool_name": calls[1].name,
                "resume_state": {"continuation_claimed": True},
                "desktop_result": {"screenshot_data": image, "width": 800, "height": 600},
            }, (calls[2],))
            await manager.save(state, checkpoint_type="intermediate")
            db = await manager._get_db()
            await db.execute("CREATE TRIGGER reject_checkpoint BEFORE INSERT ON checkpoints "
                             "WHEN NEW.checkpoint_type = 'completed' BEGIN SELECT RAISE(ABORT, 'injected'); END")
            await db.commit()
            with pytest.raises(Exception, match="injected"):
                await Agent.finalize_abandoned_turn(manager)
            assert (await manager.load()).interrupt_data is not None
            await db.execute("DROP TRIGGER reject_checkpoint")
            await db.commit()
            assert await Agent.finalize_abandoned_turn(manager)
            closed = await manager.load()
            results = [item for item in closed.messages if isinstance(item, ToolMessage)]
            media = [item for item in closed.messages if isinstance(item, ToolMessage) and any(isinstance(part, ImageContent) for part in item.content)]
            assert len(results) == 3 and results[0].message_id == first.message_id
            assert len(media) == 2
            assert all(item.tool_call_id in {result.tool_call_id for result in results} for item in media)
            assert '"width": 800' in results[1].text
            assert not await Agent.finalize_abandoned_turn(manager)
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
        manager = StateManager("s", path)
        state = AgentState.create_new("s")
        state.add_user_message("任务")
        message = state.add_assistant_message("可见片段") if persisted else None
        try:
            await manager.save(state, checkpoint_type="intermediate")
            assert await Agent.finalize_abandoned_turn(manager, partial="可见片段", partial_persisted=persisted)
            closed = await manager.load()
            assistants = [item for item in closed.messages if isinstance(item, AssistantMessage)]
            assert len(assistants) == 1
            assert assistants[0].text == "可见片段……（应用退出，本轮已终止）"
            if message is not None:
                assert assistants[0].message_id == message.message_id
            assert not await Agent.finalize_abandoned_turn(manager, partial="可见片段", partial_persisted=persisted)
            assert (await manager.load()).messages == closed.messages
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["approval", "approved", "unclaimed", "unknown", "result", "ordinary", "human"])
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
            first = ToolCall(id="first", name="control_screen", args={})
            later = ToolCall(id="later", name="write_file", args={})
            state.record_assistant_step("已保存正文", (first, later))
            if case != "ordinary":
                state.clear_pending_tool_calls()
                interrupt = {
                    "type": "authorization_request" if case in {"approval", "approved"} else "continuation_request",
                    "request_id": "old-request", "tool_call_id": first.id,
                    "tool_name": first.name, "tool_args": {}, "origin_channel": "desktop",
                    "resume_state": {"continuation_claimed": case in {"unknown", "result"}},
                    "data": {"kind": "perform_screen_action", "payload": {}},
                }
                if case == "approved":
                    interrupt["runtime_decision"] = {"kind": "approve"}
                if case == "result":
                    interrupt["desktop_result"] = {"executed": True, "error": "原始回报"}
                state.set_interrupt(interrupt, (later,))
        manager = StateManager("s", path)
        await manager.save(state, checkpoint_type="intermediate")
        await manager.close()

        def unexpected_agent(_session):
            """任何启动期间的执行对象构造都使测试失败。

            Args:
                _session (str): 请求执行的会话。
            """
            raise AssertionError("启动和快照不能构造可执行 Agent")

        scheduler = AgentRuntimeScheduler(
            InteractionEventHub(), agent_factory=unexpected_agent,
            inbox_factory=lambda session: SessionInbox(session, path),
            state_manager_factory=lambda session: StateManager(session, path),
        )
        try:
            await scheduler.start()
            await scheduler.start()
            assert (await scheduler.snapshot("s", "desktop"))["phase"] == "idle"
            manager = StateManager("s", path)
            try:
                closed = await manager.load()
                assert manager.last_loaded_checkpoint_type == "completed"
                assert closed.messages[0] == human
                assert closed.interrupt_data is None and not closed.pending_tool_calls
                results = [item for item in closed.messages if isinstance(item, ToolMessage)]
                assert len(results) == (0 if case == "human" else 2)
                if case == "unknown":
                    assert "未知" in results[0].text
                if case == "result":
                    assert "原始回报" in results[0].text
                original = closed.to_checkpoint()
                assert not await Agent.finalize_abandoned_turn(manager)
                assert (await manager.load()).to_checkpoint() == original
            finally:
                await manager.close()
        finally:
            await scheduler.close()
    asyncio.run(scenario())


def test_terminal_waits_for_new_human_and_shutdown_cannot_reactivate(tmp_path) -> None:
    """验证通知留存到新输入，运行内终态可激活，关闭回调不能再执行。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """使用真实 Inbox 交付和隔离执行替身验证生命周期规则。"""
        path = str(tmp_path / "state.sqlite3")
        records = TaskRecordDao(path)
        await records.insert_running("old", "s", "旧任务", 1, str(tmp_path / "out"))
        await records.finish_once("old", "completed", 0, "旧事实")
        calls = []
        executed = asyncio.Event()

        class RecordingAgent:
            """通过真实持久交付记录激活时的输入顺序。"""
            async def run(self, message, *, inbox_claim=None, inbox_claims=None, runtime_inputs=(), **kwargs):
                """保存通知和真人消息，并结束测试轮次。

                Args:
                    message (str): 新真人正文。
                    inbox_claim (InboxEnvelope | None): 真人领取。
                    inbox_claims (tuple | None): 合并领取。
                    runtime_inputs (tuple): 先交付的任务事实。
                    **kwargs (dict): 其余运行参数。
                Yields:
                    AgentEvent: 轮次完成事件。
                """
                from app.agent.core.event_router import AgentEvent, EventType
                manager = StateManager("s", path)
                try:
                    state = await manager.load()
                    for item in runtime_inputs:
                        state.append_input(item)
                    state.add_user_message(message)
                    await manager.save(state, checkpoint_type="completed", inbox_claims=inbox_claims or (inbox_claim,))
                finally:
                    await manager.close()
                calls.append(message)
                executed.set()
                yield AgentEvent(EventType.DONE, None)

            async def run_runtime_inputs(self, messages, *, inbox_claims, **kwargs):
                """记录本次运行内通知启动的内部轮次。

                Args:
                    messages (tuple): 任务通知消息。
                    inbox_claims (tuple): 通知领取。
                    **kwargs (dict): 其余运行参数。
                Yields:
                    AgentEvent: 轮次完成事件。
                """
                from app.agent.core.event_router import AgentEvent, EventType
                manager = StateManager("s", path)
                try:
                    state = await manager.load()
                    for item in messages:
                        state.append_input(item)
                    await manager.save(state, checkpoint_type="completed", inbox_claims=inbox_claims)
                finally:
                    await manager.close()
                calls.append("内部通知")
                executed.set()
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """替身不拥有外部资源。"""

        scheduler = AgentRuntimeScheduler(
            InteractionEventHub(), agent_factory=lambda _: RecordingAgent(),
            inbox_factory=lambda session: SessionInbox(session, path),
            state_manager_factory=lambda session: StateManager(session, path),
        )
        try:
            assert not (await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "启动前"}))).accepted
            await scheduler.start()
            await scheduler.notify_task_terminal("s")
            await scheduler.snapshot("s", "desktop")
            assert calls == []
            assert (await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "新意图"}))).accepted
            await asyncio.wait_for(executed.wait(), 2)
            manager = StateManager("s", path)
            try:
                messages = (await manager.load()).messages
                assert isinstance(messages[0], TaskResultMessage)
                assert isinstance(messages[1], HumanMessage)
            finally:
                await manager.close()
            executed.clear()
            await records.insert_running("new", "s", "新任务", 1, str(tmp_path / "out"))
            await records.finish_once("new", "completed", 0, "新事实")
            await scheduler.notify_task_terminal("s")
            await asyncio.wait_for(executed.wait(), 2)
            await scheduler.begin_shutdown()
            await records.insert_running("closing", "s", "关闭任务", 1, str(tmp_path / "out"))
            await records.finish_once("closing", "stopped", None, "关闭事实")
            await scheduler.notify_task_terminal("s")
            assert not (await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "关闭后"}))).accepted
            await scheduler.close()
            await scheduler.close()
            assert calls == ["新意图", "内部通知"]
            inbox = SessionInbox("s", path)
            try:
                assert len(await inbox.claim_task_terminals()) == 1
            finally:
                await inbox.close()
        finally:
            await scheduler.close()
            await records.close()
    asyncio.run(scenario())


def test_start_failure_keeps_execution_closed_and_retry_finishes_cleanup(tmp_path, monkeypatch) -> None:
    """验证启动收尾失败拒绝命令，重试完成后仍不激活旧任务。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (MonkeyPatch): 收尾故障注入。
    """
    async def scenario() -> None:
        """在启动步骤之间注入失败，再通过相同入口重试。"""
        path = str(tmp_path / "state.sqlite3")
        manager = StateManager("s", path)
        state = AgentState.create_new("s")
        state.add_user_message("已交付旧意图")
        await manager.save(state, checkpoint_type="intermediate")
        await manager.close()
        original = Agent.finalize_abandoned_turn

        async def fail_finalize(state_manager, **kwargs):
            """注入收尾失败，确保执行开放步骤未被绕过。

            Args:
                state_manager (StateManager): 当前会话管理器。
                **kwargs (dict): 正常关闭片段参数。
            Raises:
                RuntimeError: 固定测试失败。
            """
            raise RuntimeError("injected")

        def unexpected_agent(_session):
            """确保启动故障及重试都不构造执行对象。

            Args:
                _session (str): 请求激活的会话。
            """
            raise AssertionError("启动不能执行")

        scheduler = AgentRuntimeScheduler(
            InteractionEventHub(), agent_factory=unexpected_agent,
            inbox_factory=lambda session: SessionInbox(session, path),
            state_manager_factory=lambda session: StateManager(session, path),
        )
        try:
            monkeypatch.setattr(Agent, "finalize_abandoned_turn", staticmethod(fail_finalize))
            with pytest.raises(RuntimeError, match="injected"):
                await scheduler.start()
            assert scheduler.phase == "initializing"
            assert not (await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "失败后"}))).accepted
            monkeypatch.setattr(Agent, "finalize_abandoned_turn", staticmethod(original))
            await scheduler.start()
            assert scheduler.phase == "running"
            assert (await scheduler.snapshot("s", "desktop"))["phase"] == "idle"
        finally:
            monkeypatch.setattr(Agent, "finalize_abandoned_turn", staticmethod(original))
            await scheduler.close()
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

        class DelayedCommitManager(StateManager):
            """阻塞正常完成提交，保持最新 checkpoint 尚无正文。"""
            async def save(self, state, **kwargs):
                """只阻塞完成提交，输入交付使用真实事务。

                Args:
                    state (AgentState): 当前状态。
                    **kwargs (dict): 原持久参数。
                Returns:
                    int: 保存后的 checkpoint 编号。
                """
                if kwargs.get("checkpoint_type") == "completed":
                    commit_entered.set()
                    await asyncio.Event().wait()
                return await super().save(state, **kwargs)

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

        def make_agent(session):
            """注入隔离模型和存储，保留真实 Agent 行为。

            Args:
                session (str): 共享会话。
            Returns:
                Agent: 真实执行对象。
            """
            agent = Agent(AgentConfig(session_id=session, model_name="test", api_key="test",
                                     context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)),
                         db_path=path, llm_client=WaitingLLM())
            if committing:
                agent.state_manager = DelayedCommitManager(session, path)
            return agent

        hub = InteractionEventHub()
        scheduler = AgentRuntimeScheduler(
            hub, agent_factory=make_agent,
            inbox_factory=lambda session: SessionInbox(session, path),
            state_manager_factory=lambda session: StateManager(session, path),
        )
        await scheduler.start()
        stream = hub.subscribe("s", "desktop")
        await anext(stream)
        try:
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "新任务"}))
            while (await asyncio.wait_for(anext(stream), 2)).kind != "text_chunk":
                pass
            if committing:
                await asyncio.wait_for(commit_entered.wait(), 2)
            await scheduler.begin_shutdown()
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            await scheduler.close()
            manager = StateManager("s", path)
            try:
                state = await manager.load()
                assert state.messages[-1].text == "真实片段……（应用退出，本轮已终止）"
                assert manager.last_loaded_checkpoint_type == "completed"
            finally:
                await manager.close()
        finally:
            await scheduler.close()
            await stream.aclose()
    asyncio.run(scenario())
