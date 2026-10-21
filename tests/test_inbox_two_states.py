"""从输入查询和保存接口验证两阶段交付及排队撤销。"""

import asyncio
from functools import partial

import pytest

from app.agent.state.manager import SessionStateManager
from app.agent.state.model import AgentState
from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.core.events import AgentEvent, EventType
from app.interaction.interaction_contract import ChannelCommand
from app.orchestration.agent_runtime_scheduler import AgentRuntimeScheduler
from app.orchestration.task_supervisor import TaskSupervisor
from tests.runtime_support import finish_test_turn


def test_read_does_not_claim_and_cancelled_input_rolls_back(tmp_path) -> None:
    """读取后仍可撤销，失效输入不能随 checkpoint 保存。

    Args:
        tmp_path (Path): 测试专用 SQLite 目录。
    """
    async def scenario():
        """通过查询、取消和真实事务验证未交付输入的行为。"""
        path = str(tmp_path / "state.sqlite3")
        inbox = SessionStateManager("s", path)
        manager = SessionStateManager("s", path)
        try:
            # 1. 放行排队输入后只读取，不占用它，也不禁止撤销。
            item = await inbox.submit_human("desktop", "排队", queued=True)
            await inbox.release_queued()
            batch = await inbox.read_activation_batch()
            assert batch[0].id == item.id
            assert await inbox.status(item.id) == "pending"
            assert await inbox.cancel_queued(item.id, "desktop")
            # 2. 保存时输入已经删除，整个事务回滚，聊天历史不接受该输入。
            state = AgentState.create_new("s")
            state.add_user_message("排队")
            with pytest.raises(RuntimeError, match="输入已失效"):
                await manager.save(state, checkpoint_type="intermediate", inbox_inputs=batch)
            assert not (await manager.load()).messages
        finally:
            await manager.close()
            await inbox.close()
    asyncio.run(scenario())


def test_task_notice_failure_stops_until_human_reactivates(tmp_path) -> None:
    """任务通知交付失败保留事实，后台唤醒不能重新启动，真人输入可以恢复。

    Args:
        tmp_path (Path): 隔离存储目录。
    """
    async def scenario():
        """通过任务终态事务、公开通知和真人命令观察 worker 生命周期。"""
        path = str(tmp_path / "state.sqlite3")
        attempts = 0

        class NoticeAgent:
            """真人轮次正常保存，纯通知轮次总在保存之前失败。"""

            async def run(self, message, *, inbox_input, **kwargs):
                """保存真人和同批通知，模拟正常完成。

                Args:
                    message (str): 用户正文。
                    inbox_input (InboxEnvelope): 当前真人输入。
                    **kwargs (dict): 同批任务通知及控制参数。

                Yields:
                    AgentEvent: 完成事件。
                """
                yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": kwargs.get("origin_channel", "desktop"), "resumed": False})
                await finish_test_turn(path, kwargs.get("inbox_inputs", (inbox_input,)), message=message,
                    runtime_inputs=kwargs.get("runtime_inputs", ()), release_queued=True)
                yield AgentEvent(EventType.DONE, None)

            async def run_runtime_inputs(self, messages=None, **kwargs):
                """计数并注入交付失败，不调用模型。

                Args:
                    messages (tuple): 本次任务事实。
                    **kwargs (dict): 通知交付参数。

                Yields:
                    AgentEvent: 本用例在交付前失败，不产生执行事件。
                """
                yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": "desktop", "resumed": False})
                nonlocal attempts
                attempts += 1
                raise RuntimeError("通知保存失败")
                yield

            async def close(self):
                """当前替身没有额外资源。"""

        def factory(session_id, _manager):
            """为每次输入处理创建替身。

            Args:
                _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
                session_id (str): 当前会话。

            Returns:
                NoticeAgent: 新执行对象。
            """
            return NoticeAgent()

        async def until(kind):
            """等待公开事件到达指定阶段。

            Args:
                kind (str): 要观察的事件类型。

            Returns:
                InteractionEvent: 对应阶段的实际事件。
            """
            while True:
                event = await asyncio.wait_for(scheduler.next_event(), 2)
                if event.kind == kind:
                    return event

        supervisor = TaskSupervisor(db_path=path, spool_dir=tmp_path / "tasks")
        scheduler = AgentRuntimeScheduler(agent_factory=factory, task_supervisor=supervisor,
            session_state_manager_factory=partial(SessionStateManager, db_path=path),
        )
        records, inbox = supervisor.records, SessionStateManager("s", path)
        await scheduler.start()
        try:
            # 1. 本次真人输入先取得激活资格，随后任务终态才允许自动交付。
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "启动"}))
            await until("run_completed")
            await records.insert_running("task", "s", "任务", 1, str(tmp_path / "output"))
            await supervisor.finish_once("task", "completed", 0)
            error = await until("error")
            assert attempts == 4 and "failed_input" not in error.payload
            assert len(await inbox.read_task_terminals()) == 1
            # 2. 耗尽后后台通知不重新激活；新的真人输入则一并提交保留的任务事实。
            await records.insert_running("later", "s", "后续任务", 1, str(tmp_path / "later"))
            await supervisor.finish_once("later", "completed", 0)
            await until("task_status")
            assert attempts == 4
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "继续"}))
            await until("run_completed")
            assert await inbox.read_task_terminals() == ()
        finally:
            await scheduler.close()
            await supervisor.close()
            await inbox.close()
    asyncio.run(scenario())


def test_cancel_during_agent_initialization_finishes_without_model(tmp_path) -> None:
    """初始化期间撤销排队输入，真实 Agent 正常结束且不调用模型。

    Args:
        tmp_path (Path): 隔离存储目录。
    """
    async def scenario():
        """在初始化屏障处撤销，随后消费真实 Agent 的结果。"""
        path = str(tmp_path / "state.sqlite3")
        entered, release = asyncio.Event(), asyncio.Event()

        class WaitingAgent(Agent):
            """仅用屏障替代初始化，保留真实输入保存与执行入口。"""

            async def initialize(self):
                """通知测试已开始初始化，等撤销提交后继续。"""
                entered.set()
                await release.wait()

        class Model:
            """禁止失效输入进入模型。"""

            async def astream(self, *args, **kwargs):
                """任何调用都说明取消未在保存阶段生效。

                Args:
                    *args (tuple): 模型输入。
                    **kwargs (dict): 模型配置。

                Yields:
                    None: 本测试禁止模型执行。
                """
                raise AssertionError("已撤销输入不能调用模型")
                yield

            async def close(self):
                """替身没有外部资源。"""

        agent = WaitingAgent(AgentConfig(session_id="s", model_name="test", api_key="test", context_strategy=ContextStrategyConfig()),
                             db_path=path, llm_client=Model())
        inbox = SessionStateManager("s", path)

        async def consume():
            """完整读取取消轮次，返回供断言的公开事件。

            Returns:
                list[AgentEvent]: 本轮输出。
            """
            return [event async for event in agent.run("取消我", inbox_input=item)]

        work = None
        try:
            # 1. 排队输入已经被读取，Agent 在初始化屏障处等待，此时仍允许撤销。
            item = await inbox.submit_human("desktop", "取消我", queued=True)
            await inbox.release_queued()
            item = await inbox.read_next()
            work = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 1)
            assert await inbox.cancel_queued(item.id, "desktop")
            # 2. 撤销提交后放行，真实保存事务拒绝失效输入，只产生正常结束事件。
            release.set()
            events = await asyncio.wait_for(work, 1)
            assert [event.type for event in events] == [EventType.RUN_STARTED, EventType.DONE]
            assert not (await agent.session_state_manager.load()).messages
        finally:
            # 3. 断言失败时也放行初始化并等待执行退出，随后关闭隔离资源。
            release.set()
            if work is not None and not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            await agent.close()
            await inbox.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failures", [2, 4])
def test_delivery_retries_then_succeeds_or_returns_failed_input(tmp_path, failures) -> None:
    """未交付输入至多尝试四次，耗尽后删除并返回可重发内容。

    Args:
        tmp_path (Path): 隔离数据库目录。
        failures (int): 保存输入前连续失败的次数。
    """
    async def scenario():
        """从命令、事件、快照和 Inbox 结果验证有限重试。"""
        path = str(tmp_path / "state.sqlite3")
        attempts = 0
        agents, controls, closed = [], [], []

        class TestAgent:
            """按次数失败，成功时执行真实输入保存事务。"""

            async def run(self, message, *, inbox_input, **kwargs):
                """保存前按次数失败，成功时交付输入并完成。

                Args:
                    message (str): 真人正文。
                    inbox_input (InboxEnvelope): 待读取输入。
                    **kwargs (dict): 本轮其他参数。

                Yields:
                    AgentEvent: 正常完成事件。
                """
                yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": kwargs.get("origin_channel", "desktop"), "resumed": False})
                nonlocal attempts
                attempts += 1
                controls.append(kwargs["turn_control"])
                if attempts <= failures:
                    raise RuntimeError("保存前失败")
                manager = SessionStateManager("s", path)
                try:
                    state = await manager.load()
                    human = state.add_user_message(message)
                    await manager.save(state, checkpoint_type="intermediate", inbox_inputs=(inbox_input,))
                    yield AgentEvent(EventType.HUMAN_INPUT_COMMITTED, {"message": human, "input_sequence": inbox_input.id})
                    await manager.save(state, checkpoint_type="completed", release_queued=True)
                finally:
                    await manager.close()
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """每次尝试都关闭，没有额外资源。"""
                closed.append(self)

        def factory(session_id, _manager):
            """每次尝试创建独立 Agent。

            Args:
                _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
                session_id (str): 当前会话编号。

            Returns:
                TestAgent: 本次尝试对象。
            """
            agent = TestAgent()
            agents.append(agent)
            return agent

        supervisor = TaskSupervisor(db_path=path, spool_dir=tmp_path / "tasks")
        scheduler = AgentRuntimeScheduler(agent_factory=factory, task_supervisor=supervisor,
            session_state_manager_factory=partial(SessionStateManager, db_path=path),
        )
        inbox = SessionStateManager("s", path)
        await scheduler.start()
        try:
            # 1. 提交真人输入，通过真实输出观察全部尝试，只允许一个轮次开始事件。
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "原始输入"}))
            events = []
            while True:
                event = await asyncio.wait_for(scheduler.next_event(), 2)
                events.append(event)
                if event.kind in {"error", "run_completed"}:
                    break
            assert attempts == min(failures + 1, 4)
            assert len(agents) == attempts and len({id(agent) for agent in agents}) == attempts
            assert all(control is controls[0] for control in controls)
            assert [event.kind for event in events].count("run_started") == 1
            assert (await scheduler.snapshot("s")).payload["phase"] == "idle"
            if failures == 4:
                # 2. 耗尽后原输入被删除；后台通知不能重启这批失败尝试。
                assert events[-1].payload["failed_input"]["message"] == "原始输入"
                assert await inbox.read_next() is None
                await supervisor.records.insert_running("later", "s", "后续任务", 1, str(tmp_path / "out"))
                await supervisor.finish_once("later", "completed", 0)
                event = await asyncio.wait_for(scheduler.next_event(), 1)
                assert event.kind == "task_status"
                assert attempts == 4
            else:
                # 2. 保存最终成功，只发布一次真人消息和完成事件，Inbox 不再查询到它。
                assert [event.kind for event in events] == ["run_started", "human_message", "run_completed"]
                assert (await inbox.pending_humans()) == []
        finally:
            await inbox.close()
            await scheduler.close()
            await supervisor.close()
    asyncio.run(scenario())
