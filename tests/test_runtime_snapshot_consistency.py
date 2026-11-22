"""用公开事件流验证状态查询和增量交付的并发顺序。"""
from tests.runtime_support import runtime_hub, close_test_runtime
from tests.runtime_event_source import observe_session
from app.schemas.runtime_types import AgentRunOutcome, AgentRunOutcomeKind, CheckpointType
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory
from tests.runtime_support import RuntimeAgentStub

import asyncio
from functools import partial
from tests.runtime_support import configured_settings
from pathlib import Path

import pytest

from app.agent.core.events import AgentEvent, EventType
from app.agent.state.manager import SessionStateManager
from app.interaction.interaction_contract import ChannelCommand, InteractionEvent
from app.interaction.interaction_event_hub import InteractionEventHub
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.services.channel_subscription_service import ChannelSubscriptionService
from tests.runtime_event_source import EventSource


@pytest.mark.parametrize("cancelled", [False, True])
def test_snapshot_failure_or_cancellation_allows_reconnection(cancelled: bool) -> None:
    """查询失败或取消后，后续连接仍可查询和接收真实增量。

    Args:
        cancelled (bool): 是否取消等待，否则模拟查询异常。
    """
    async def scenario() -> None:
        """反复终止首次查询，再验证新连接和正常输出。"""
        source = EventSource(InteractionEventHub(subscriptions=ChannelSubscriptionService(SqliteFactory())))
        source.fail_snapshot = True
        if cancelled:
            source.release_snapshot = asyncio.Event()
        hub = source.hub
        pass
        try:
            for _ in range(3):
                source.snapshot_entered.clear()
                stream = observe_session("s", "desktop", hub, source)
                waiting = asyncio.create_task(anext(stream))
                await asyncio.wait_for(source.snapshot_entered.wait(), 1)
                if cancelled:
                    waiting.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await waiting
                else:
                    with pytest.raises(RuntimeError, match="模拟快照"):
                        await waiting
                await stream.aclose()
            source.fail_snapshot = False
            source.release_snapshot = None
            recovered = observe_session("s", "desktop", hub, source)
            try:
                assert (await anext(recovered)).payload["phase"] == "idle"
                await source.publish(InteractionEvent("s", "text_chunk", {"value": "恢复后输出"}))
                assert (await asyncio.wait_for(anext(recovered), 1)).payload["value"] == "恢复后输出"
            finally:
                await recovered.aclose()
        finally:
            await hub.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("event_type,data,expected_phase", [
    (EventType.TEXT_CHUNK, "天气", "running"),
    (EventType.DONE, None, "idle"),
    (EventType.INTERRUPT, {"type": "authorization_request", "request_id": "approval"}, "pending_interrupt"),
    (EventType.ERROR, "模拟失败", "idle"),
])
def test_event_during_inbox_query_reaches_old_and_new_connection(
    tmp_path: Path, event_type: EventType, data: object, expected_phase: str,
) -> None:
    """查询期间不阻塞执行，新连接先收到旧状态再收到对应增量。

    Args:
        tmp_path (Path): 隔离数据库目录。
        event_type (EventType): 查询期间产生的事件类别。
        data (object): 该事件的正文。
        expected_phase (str): 本轮事件后的阶段。
    """
    async def scenario() -> None:
        """保持真实 Inbox 查询等待，同时从 Agent 公开输出推进运行。"""
        path = str(tmp_path / "state.sqlite3")
        query_entered = asyncio.Event()
        release_query = asyncio.Event()
        release_agent = asyncio.Event()
        block_query = False

        class WaitingInbox(SessionStateManager):
            """只暂停状态查询，领取和保存仍使用真实 SQLite。"""

            async def pending_humans(self, *, origin_channel=None):
                """让新连接停在数据库查询之前。

                Args:
                    origin_channel (str | None): 原渠道过滤条件。

                Returns:
                    list[InboxEnvelope]: 真实待处理输入。
                """
                if block_query:
                    query_entered.set()
                    await release_query.wait()
                return await super().pending_humans(origin_channel=origin_channel)

        class Agent(RuntimeAgentStub):
            """按测试控制的时机输出一条状态变化。"""

            def __init__(self, manager: SessionStateManager) -> None:
                """使用 worker 的状态对象，输出前通过真实事务确认输入交付。

                Args:
                    manager (SessionStateManager): worker 借给此轮执行的状态业务对象。
                """
                self.manager = manager

            async def run(self, message, *, emit, **kwargs):
                """先产生片段，再等待查询交错后继续。

                Args:
                    message (str): 触发本轮的真人输入。
                    **kwargs (dict): 当前轮次控制和领取材料。

                Yields:
                    AgentEvent: 已知片段及指定的并发事件。
                """
                outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED)
                await emit(AgentEvent(EventType.RUN_STARTED, {"origin_channel": kwargs.get("origin_channel", "desktop"), "resumed": False}))
                state = await self.manager.load()
                self.manager.append_human_input(state, message, None, inputs=kwargs["inbox_inputs"])
                await self.manager.save(state, checkpoint_type=CheckpointType.INTERMEDIATE, inbox_inputs=kwargs["inbox_inputs"])
                await emit(AgentEvent(EventType.TEXT_CHUNK, "今天"))
                await release_agent.wait()
                control = kwargs["turn_control"]
                if event_type == EventType.INTERRUPT:
                    from tests.runtime_support import pause_test_turn
                    from app.agent.state.messages import ToolCall
                    state.record_assistant_step("", (ToolCall("approval-call", "test_tool", {}),))
                    state.clear_pending_tool_calls()
                    state.set_interrupt({**data, "tool_call_id": "approval-call", "tool_name": "test_tool"})
                    await self.manager.save_progress(state)
                    pause_test_turn(control, data["request_id"])
                    outcome = AgentRunOutcome(AgentRunOutcomeKind.INTERRUPTED)
                elif event_type in {EventType.DONE, EventType.ERROR}:
                    if event_type == EventType.ERROR:
                        outcome = AgentRunOutcome(AgentRunOutcomeKind.FAILED, error=str(data))
                await emit(AgentEvent(event_type, data))
                if event_type == EventType.TEXT_CHUNK:
                    await asyncio.Event().wait()

                return outcome

            async def close(self):
                """测试执行对象没有外部资源。"""

        def factory(session_id, _manager):
            """创建本次隔离执行。

            Args:
                _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
                session_id (str): 当前会话。

            Returns:
                Agent: 本次 Agent 替身。
            """
            return Agent(_manager)

        subscriptions = ChannelSubscriptionService(SqliteFactory(path))
        scheduler = build_test_scheduler(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(),
            agent_factory=factory, subscriptions=subscriptions,
            session_state_manager_factory=partial(WaitingInbox, sqlite_factory=SqliteFactory(path)),
        )
        hub = runtime_hub(scheduler)
        await scheduler.start()
        pass
        old = observe_session("s", "desktop", hub, scheduler)
        new = observe_session("s", "desktop", hub, scheduler)
        waiting = None
        try:
            await anext(old)
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "开始"}))
            await asyncio.wait_for(anext(old), 1)
            previous = await asyncio.wait_for(anext(old), 1)
            assert previous.payload["value"] == "今天"
            block_query = True
            waiting = asyncio.create_task(anext(new))
            await asyncio.wait_for(query_entered.wait(), 1)
            release_agent.set()
            # 1. 查询仍未返回时，已有连接已经收到事件，证明查询没有锁住执行和分发。
            old_event = await asyncio.wait_for(anext(old), 1)
            assert not waiting.done()
            release_query.set()
            snapshot = await asyncio.wait_for(waiting, 1)
            assert snapshot.payload["phase"] == "running"
            assert snapshot.event_sequence == previous.event_sequence
            new_event = await asyncio.wait_for(anext(new), 1)
            assert old_event == new_event
            assert new_event.event_sequence == snapshot.event_sequence + 1
            block_query = False
            assert (await scheduler.snapshot("s")).payload["phase"] == expected_phase
        finally:
            release_query.set()
            release_agent.set()
            if waiting is not None and not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            await old.aclose()
            await new.aclose()
            await hub.close()
            await close_test_runtime(scheduler)
    asyncio.run(scenario())


def test_event_already_reflected_in_snapshot_is_not_delivered_again() -> None:
    """状态已包含但尚未被 Hub 领取的事件不会向新连接重复交付。"""
    async def scenario() -> None:
        """先产生事件，随后同时启动领取和首次状态查询。"""
        source = EventSource(InteractionEventHub(subscriptions=ChannelSubscriptionService(SqliteFactory())))
        await source.publish(InteractionEvent("s", "text_chunk", {"value": "状态之前"}))
        hub = source.hub
        stream = observe_session("s", "desktop", hub, source)
        try:
            assert (await anext(stream)).event_sequence == 1
            pass
            await source.publish(InteractionEvent("s", "text_chunk", {"value": "状态之后"}))
            event = await asyncio.wait_for(anext(stream), 1)
            assert event.event_sequence == 2 and event.payload["value"] == "状态之后"
        finally:
            await stream.aclose()
            await hub.close()
    asyncio.run(scenario())
