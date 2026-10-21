"""从公共输入输出接口验证单向事件读取与多连接分发。"""

import asyncio
from functools import partial
from tests.runtime_support import finish_test_turn
import pytest

from app.agent.core.event_router import AgentEvent, EventType
from app.agent.state.manager import SessionStateManager
from app.agent.state.model import AgentState
from app.agent.state.messages import ToolCall
from app.interaction.interaction_contract import ChannelCommand
from app.interaction.interaction_event_hub import InteractionEventHub
from app.orchestration.agent_runtime_scheduler import AgentRuntimeScheduler
from app.services.channel_subscription_service import ChannelSubscriptionService


def test_receipt_does_not_reserve_run_and_hub_fans_out(tmp_path) -> None:
    """受理不预分配编号，创建 Agent 后产生开始事件并交付两个连接。

    Args:
        tmp_path (Path): 测试专用数据库目录。
    """
    async def scenario() -> None:
        """用可控 Agent 保持本轮运行，比较两个连接看到的真实事件。"""
        path = str(tmp_path / "state.sqlite3")
        release = asyncio.Event()

        class Agent:
            """等待测试放行后结束的执行替身。"""

            async def run(self, message, **kwargs):
                """产生文字并等待结束。

                Args:
                    message (str): 用户正文。
                    **kwargs (dict): 本轮控制及提交参数。

                Yields:
                    AgentEvent: 文字和本轮完成事件。
                """
                yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": kwargs.get("origin_channel", "desktop"), "resumed": False})
                yield AgentEvent(EventType.TEXT_CHUNK, message)
                await release.wait()
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """释放测试执行对象，无外部资源。"""

        def factory(session_id, _manager):
            """创建测试 Agent。

            Args:
                _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
                session_id (str): 当前会话编号。

            Returns:
                Agent: 当前轮次执行替身。
            """
            return Agent()

        subscriptions = ChannelSubscriptionService(path)
        scheduler = AgentRuntimeScheduler(
            agent_factory=factory, subscriptions=subscriptions,
            session_state_manager_factory=partial(SessionStateManager, db_path=path),
        )
        hub = InteractionEventHub(scheduler, subscriptions=subscriptions)
        await scheduler.start()
        await hub.start()
        first = hub.subscribe("s", "desktop")
        second = hub.subscribe("s", "desktop")
        try:
            assert (await anext(first)).payload["phase"] == "idle"
            await anext(second)
            receipt = await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "你好"}))
            assert receipt.accepted and receipt.run_id is None
            assert not (await scheduler.submit(ChannelCommand("s", "desktop", "stop_turn"))).accepted
            left = await asyncio.wait_for(anext(first), 1)
            right = await asyncio.wait_for(anext(second), 1)
            assert left == right and left.kind == "run_started" and left.run_id
            assert await anext(first) == await anext(second)
            assert (await scheduler.snapshot("s")).payload["run_id"] == left.run_id
            assert (await scheduler.submit(ChannelCommand(
                "s", "desktop", "stop_turn", target_run_id=left.run_id,
            ))).accepted
        finally:
            release.set()
            await first.aclose()
            await second.aclose()
            await hub.close()
            await scheduler.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure,expected", [
    ("factory", ["error"]),
    ("stream", ["run_started", "text_chunk", "error"]),
    ("reported", ["run_started", "error"]),
    ("cleanup", ["run_started", "run_completed"]),
])
def test_failed_turn_returns_idle_without_duplicate_errors(tmp_path, failure, expected) -> None:
    """创建、消费或清理失败不会留下活动轮次或重复通知。

    Args:
        tmp_path (Path): 隔离数据库目录。
        failure (str): 注入错误的位置。
        expected (list[str]): 该失败的预期事件顺序。
    """
    async def scenario() -> None:
        """只从输出、状态和停止回执观察执行结果。"""
        path = str(tmp_path / "state.sqlite3")
        cleaned = asyncio.Event()

        class Agent:
            """产生已知输出，在指定阶段注入异常。"""

            async def run(self, message, **kwargs):
                """产生片段错误或正常结束。

                Args:
                    message (str): 本轮输入。
                    **kwargs (dict): 控制与读取参数。

                Yields:
                    AgentEvent: 当前测试阶段的事件。
                """
                yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": kwargs.get("origin_channel", "desktop"), "resumed": False})
                await finish_test_turn(path, (kwargs["inbox_input"],), message=message)
                if failure == "stream":
                    yield AgentEvent(EventType.TEXT_CHUNK, "片段")
                    raise RuntimeError("输出失败")
                if failure == "reported":
                    yield AgentEvent(EventType.ERROR, "已报告错误")
                    raise RuntimeError("输出结束时又失败")
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """标记清理完成，按需模拟关闭失败。"""
                cleaned.set()
                if failure == "cleanup":
                    raise RuntimeError("清理失败")

        def factory(session_id, _manager):
            """按指定位置失败或创建 Agent。

            Args:
                _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
                session_id (str): 会话编号。

            Returns:
                Agent: 本轮对象。

            Raises:
                RuntimeError: 指定工厂失败。
            """
            if failure == "factory":
                cleaned.set()
                raise RuntimeError("工厂失败")
            return Agent()

        scheduler = AgentRuntimeScheduler(
            agent_factory=factory, session_state_manager_factory=partial(SessionStateManager, db_path=path),
        )
        await scheduler.start()
        waiting = None
        try:
            assert (await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "执行"}))).accepted
            observed = [await asyncio.wait_for(scheduler.next_event(), 1) for _ in expected]
            assert [event.kind for event in observed] == expected
            await asyncio.wait_for(cleaned.wait(), 1)
            snapshot = await scheduler.snapshot("s")
            assert snapshot.payload["phase"] == "idle" and snapshot.payload["run_id"] is None
            assert not (await scheduler.submit(ChannelCommand("s", "desktop", "stop_turn"))).accepted
            waiting = asyncio.create_task(scheduler.next_event())
            await asyncio.sleep(0)
            assert not waiting.done()
        finally:
            if waiting is not None:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            await scheduler.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["steer", "approve", "approve_before_cleanup"])
def test_pending_approval_steer_or_decision_wakes_worker_immediately(tmp_path, action) -> None:
    """有效引导取消审批，批准在查询或清理交错处到达仍沿用原轮次。

    Args:
        tmp_path (Path): 隔离数据库目录。
        action (str): 有效引导，或在空查询、旧输出清理期间批准。
    """
    async def scenario() -> None:
        """保存真实审批与输入，使用数据库查询屏障控制唤醒时序。"""
        path = str(tmp_path / "state.sqlite3")
        query_entered = asyncio.Event()
        release_query = asyncio.Event()
        processed = asyncio.Event()
        approval_cleanup_entered = asyncio.Event()
        release_approval_cleanup = asyncio.Event()
        should_block = False

        class Inbox(SessionStateManager):
            """在审批等待的空查询处暂停，所有读取仍来自 SQLite。"""

            async def read_activation_batch(self, **kwargs):
                """真实查询后等待测试插入审批操作。

                Args:
                    **kwargs (dict): 读取过滤条件。

                Returns:
                    tuple: 本次已读取记录。
                """
                result = await super().read_activation_batch(**kwargs)
                if not result and should_block and not query_entered.is_set():
                    query_entered.set()
                    await release_query.wait()
                return result

        class ApprovalAgent:
            """首次产生审批，后续交付新指令或恢复持久决定。"""

            async def run(self, message, *, inbox_input, **kwargs):
                """保存已读取真人输入和对应的审批或完成状态。

                Args:
                    message (str): 真人输入。
                    inbox_input (InboxEnvelope): 原输入读取。
                    **kwargs (dict): 其余执行参数。

                Yields:
                    AgentEvent: 审批或正常完成。
                """
                yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": kwargs.get("origin_channel", "desktop"), "resumed": False})
                nonlocal should_block
                manager = SessionStateManager("s", path)
                try:
                    state = await manager.load()
                    if message == "旧指令":
                        state.add_user_message(message)
                        call = ToolCall("call", "test_tool", {})
                        state.record_assistant_step("", (call,))
                        state.clear_pending_tool_calls()
                        state.set_interrupt({
                            "type": "authorization_request", "request_id": "request",
                            "tool_call_id": call.id, "tool_name": call.name,
                            "tool_args": {}, "origin_channel": "desktop", "message": "批准？",
                        })
                        await manager.save(state, checkpoint_type="intermediate", inbox_inputs=(inbox_input,))
                        should_block = action == "approve"
                        yield AgentEvent(EventType.INTERRUPT, state.interrupt_data)
                        # 1. 审批已经可见时，旧输出仍可能在异步释放资源；允许此时提交决定。
                        if action == "approve_before_cleanup":
                            approval_cleanup_entered.set()
                            await release_approval_cleanup.wait()
                    else:
                        state.close_unfinished_tools("已取消", "已取消", "未知")
                        state.add_user_message(message)
                        await manager.save(state, checkpoint_type="completed", inbox_inputs=(inbox_input,), release_queued=True)
                        processed.set()
                        yield AgentEvent(EventType.DONE, None)
                finally:
                    await manager.close()

            async def resume(self, request_id, **kwargs):
                """检查 Scheduler 从 checkpoint 取得决定，避免依赖内存副本。

                Args:
                    decision (dict): 已保存决定。
                    initial_state (AgentState): 同一次读取的审批状态。
                    **kwargs (dict): 控制和模型前通知参数。

                Yields:
                    AgentEvent: 恢复后完成。
                """
                yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": "desktop", "resumed": True})
                manager = SessionStateManager("s", path)
                try:
                    restored = await manager.load_for_resume(request_id)
                    assert restored.interrupt_data["runtime_decision"] == {"kind": "approve"}
                finally:
                    await manager.close()
                processed.set()
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """本替身不拥有外部资源。"""

        def factory(session_id, _manager):
            """创建审批测试执行对象。

            Args:
                _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
                session_id (str): 当前会话。

            Returns:
                ApprovalAgent: 本次执行对象。
            """
            return ApprovalAgent()

        scheduler = AgentRuntimeScheduler(
            agent_factory=factory, session_state_manager_factory=partial(Inbox, db_path=path),
        )
        await scheduler.start()
        operation = None
        try:
            # 1. 通过真实受理入口建立当前轮次和持久审批，后续操作针对此轮编号。
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "旧指令"}))
            started = await asyncio.wait_for(scheduler.next_event(), 1)
            assert (await asyncio.wait_for(scheduler.next_event(), 1)).kind == "interrupt"
            if action == "steer":
                # 2. 有效引导必须取消旧审批；迟到批准不能覆盖，输入也不能留在排队列表。
                receipt = await scheduler.submit(ChannelCommand("s", "desktop", "human_steer",
                    {"message": "新指令"}, target_run_id=started.run_id))
                assert receipt.accepted
                assert not (await scheduler.submit(ChannelCommand("s", "desktop", "resolve_authorization",
                    {"kind": "approve"}, request_id="request"))).accepted
                assert (await scheduler.snapshot("s")).payload["queued_inputs"] == []
            elif action == "approve":
                # 2. worker 此时在会话锁内等待查询放行；批准任务只能先排队等锁。
                await asyncio.wait_for(query_entered.wait(), 1)
                operation = asyncio.create_task(scheduler.submit(ChannelCommand(
                    "s", "desktop", "resolve_authorization", {"kind": "approve"}, request_id="request")))
                # 2.1. 先放行查询释放会话锁，再等待批准；反过来会让测试自己互相等待。
                release_query.set()
                assert (await asyncio.wait_for(operation, 1)).accepted
            else:
                # 2. 批准先保存，旧输出后清理；清理只能移除控制对象，不能丢掉待恢复轮次。
                await asyncio.wait_for(approval_cleanup_entered.wait(), 1)
                receipt = await scheduler.submit(ChannelCommand(
                    "s", "desktop", "resolve_authorization", {"kind": "approve"}, request_id="request"))
                assert receipt.accepted and receipt.run_id == started.run_id
                release_approval_cleanup.set()
            await asyncio.wait_for(processed.wait(), 1)
            if action != "steer":
                # 3. 恢复产生开始事件，但仍属于同一回答，不能重新分配回答编号。
                while True:
                    event = await asyncio.wait_for(scheduler.next_event(), 1)
                    if event.kind == "run_started":
                        assert event.run_id == started.run_id and event.payload["resumed"]
                        break
        finally:
            # 4. 失败时放行所有查询和清理屏障，取消尚未完成的命令后关闭会话 worker。
            release_query.set()
            release_approval_cleanup.set()
            if operation is not None and not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            await scheduler.close()
    asyncio.run(scenario())
