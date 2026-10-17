"""通过真实事件中心和隔离 Inbox，验证快照与回答事件的交付边界。"""

import asyncio
from functools import partial
from pathlib import Path

import pytest

from app.agent.core.event_router import AgentEvent, EventType
from app.agent.runtime.session_inbox import InboxEnvelope, SessionInbox
from app.agent.state.manager import StateManager
from app.schemas.runtime_types import InteractionEventKind, SessionPhase
from app.orchestration.agent_runtime_scheduler import AgentRuntimeScheduler
from app.services.channel_subscription_service import ChannelSubscriptionService
from app.interaction.interaction_contract import InteractionEvent
from app.interaction.interaction_event_hub import InteractionEventHub


@pytest.mark.parametrize("cancelled", [False, True])
def test_snapshot_failure_or_cancellation_removes_registered_queue(cancelled: bool) -> None:
    """快照失败或等待期间取消时，旧队列移除，新的订阅仍能建立。

    Args:
        cancelled (bool): 是否取消等待；否则让快照直接抛错。
    """
    async def scenario() -> None:
        """设置失败屏障，并检查连续三次失败后的队列及后续公开订阅。"""
        entered = asyncio.Event()

        async def failing_snapshot(session_id: str, channel: str) -> dict:
            """模拟快照读取失败或长期等待。

            Args:
                session_id (str): 测试会话。
                channel (str): 测试渠道。

            Returns:
                dict: 本次不会成功返回。

            Raises:
                RuntimeError: 查询失败的测试异常。
            """
            entered.set()
            if cancelled:
                await asyncio.Event().wait()
            raise RuntimeError("模拟快照查询失败")

        def ready_snapshot(session_id: str, channel: str) -> dict:
            """返回后续重连需要的正常快照。

            Args:
                session_id (str): 测试会话。
                channel (str): 测试渠道。

            Returns:
                dict: 空闲界面状态。
            """
            return {"phase": "idle"}

        hub = InteractionEventHub(failing_snapshot)
        try:
            # 1. 在登记后的查询阶段退出，不能把队列留给已经结束的连接。
            for _ in range(3):
                entered.clear()
                stream = hub.subscribe("s", "desktop")
                waiting = asyncio.create_task(anext(stream))
                await asyncio.wait_for(entered.wait(), 1)
                if cancelled:
                    waiting.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await waiting
                else:
                    with pytest.raises(RuntimeError, match="模拟快照"):
                        await waiting
                await stream.aclose()
                assert not hub._subscribers[("s", "desktop")]
            # 2. 后续连接得到正常快照，正常关闭也移除队列。
            hub.set_snapshot_provider(ready_snapshot)
            recovered = hub.subscribe("s", "desktop")
            assert (await anext(recovered)).payload == {"phase": "idle"}
            await recovered.aclose()
            assert not hub._subscribers[("s", "desktop")]
        finally:
            await hub.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("event_type,data,expected_phase", [
    (EventType.TEXT_CHUNK, "天气", SessionPhase.RUNNING),
    (EventType.DONE, None, SessionPhase.IDLE),
    (EventType.INTERRUPT, {"type": "authorization_request", "request_id": "approval"}, SessionPhase.PENDING_INTERRUPT),
    (EventType.ERROR, "模拟失败", SessionPhase.ERROR),
])
def test_event_arriving_during_inbox_query_follows_snapshot(
    tmp_path: Path, event_type: EventType, data: object, expected_phase: SessionPhase,
) -> None:
    """查询排队消息期间产生的文字或状态，只在快照之后交付给新旧连接。

    Args:
        tmp_path (Path): 隔离数据库目录。
        event_type (EventType): 交错到达的 Agent 事件。
        data (object): 事件正文或审批内容。
        expected_phase (SessionPhase): 事件发布后应显示的阶段。
    """
    async def scenario() -> None:
        """使用真实 Inbox 查询屏障，同时观察已有连接和新连接。"""
        path = str(tmp_path / "state.sqlite3")
        query_entered = asyncio.Event()
        release_query = asyncio.Event()
        produced = asyncio.Event()
        block_query = False

        class WaitingInbox(SessionInbox):
            """让新连接查询排队列表时暂停，真实数据仍来自测试 SQLite。"""

            async def pending_humans(self, *, origin_channel: str | None = None) -> list[InboxEnvelope]:
                """查询前等待测试放行，让 Agent 的下一事件恰好到达。

                Args:
                    origin_channel (str | None): 原渠道过滤条件。

                Returns:
                    list[InboxEnvelope]: 测试数据库里的待处理真人消息。
                """
                if block_query:
                    query_entered.set()
                    await release_query.wait()
                return await super().pending_humans(origin_channel=origin_channel)

        def unused_agent(session_id: str):
            """拒绝启动模型；本测试只验证 Scheduler 的展示事件。

            Args:
                session_id (str): 测试会话。

            Raises:
                AssertionError: 测试意外进入模型执行。
            """
            raise AssertionError("展示测试不应启动 Agent")

        async def stream():
            """产生屏障期间到达的 Agent 事件。

            Yields:
                AgentEvent: 测试指定的单条回答或状态事件。
            """
            produced.set()
            yield AgentEvent(event_type, data)

        hub = InteractionEventHub(subscriptions=ChannelSubscriptionService(path))
        scheduler = AgentRuntimeScheduler(
            hub, agent_factory=unused_agent,
            inbox_factory=partial(WaitingInbox, db_path=path),
            state_manager_factory=partial(StateManager, db_path=path),
        )
        old = hub.subscribe("s", "desktop")
        new = hub.subscribe("s", "desktop")
        tasks = []
        try:
            # 1. 先发布已有回答，再让新连接持发布锁查询 Inbox。
            await scheduler._publish_runtime_event(InteractionEvent(
                "s", InteractionEventKind.RUN_STARTED, {}, origin_channel="desktop", run_id="run",
            ))
            await scheduler._publish_runtime_event(InteractionEvent(
                "s", InteractionEventKind.TEXT_CHUNK, {"value": "今天"}, run_id="run",
            ))
            previous = await anext(old)
            block_query = True
            snapshot_task = asyncio.create_task(anext(new))
            tasks.append(snapshot_task)
            await asyncio.wait_for(query_entered.wait(), 1)
            event_task = asyncio.create_task(scheduler._consume_events("s", "desktop", "run", stream()))
            tasks.append(event_task)
            await asyncio.wait_for(produced.wait(), 1)
            # 2. 事件正在等待发布，不能提前修改完整状态，让同一文字或终态进入旧水位。
            assert not event_task.done()
            assert scheduler._phases["s"] == SessionPhase.RUNNING
            release_query.set()
            snapshot = await asyncio.wait_for(snapshot_task, 1)
            await asyncio.wait_for(event_task, 1)
            assert "partial" not in snapshot.payload
            assert snapshot.payload["phase"] == SessionPhase.RUNNING
            assert snapshot.payload["run_id"] == "run"
            assert snapshot.event_sequence == previous.event_sequence
            old_event = await asyncio.wait_for(anext(old), 1)
            new_event = await asyncio.wait_for(anext(new), 1)
            assert old_event == new_event
            assert new_event.event_sequence == snapshot.event_sequence + 1
            # 3. 新旧连接仍收到相同增量；快照不含正文，完成编号失效、审批可恢复。
            current = await scheduler.snapshot("s", "desktop")
            assert current["phase"] == expected_phase
            if event_type == EventType.TEXT_CHUNK:
                assert old_event.payload["value"] == "天气"
                assert "partial" not in previous.payload
                assert "partial" not in current
            elif event_type in {EventType.DONE, EventType.ERROR}:
                assert current["run_id"] is None
            else:
                assert current["interrupt"]["request_id"] == "approval"
                await scheduler._publish_runtime_event(InteractionEvent(
                    "s", InteractionEventKind.AUTHORIZATION_RESOLVED, {}, request_id="approval",
                ))
                assert (await scheduler.snapshot("s", "desktop"))["interrupt"] is None
        finally:
            release_query.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await old.aclose()
            await new.aclose()
            await hub.close()

    asyncio.run(scenario())
