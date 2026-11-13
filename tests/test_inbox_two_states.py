from tests.runtime_support import make_execution_control
"""从输入查询和保存接口验证两阶段交付及排队撤销。"""
from tests.runtime_support import collect_agent_events
from tests.runtime_support import runtime_hub, close_test_runtime
from app.routes.agent import observe_runtime
from app.schemas.runtime_types import AgentRunOutcome, AgentRunOutcomeKind
from tests.runtime_support import next_runtime_event
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory

from tests.runtime_support import finish_task_record
from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator
from tests.runtime_support import RuntimeAgentStub

import asyncio
from functools import partial

import pytest

from app.agent.state.manager import SessionStateManager
from app.agent.state.model import AgentState
from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.core.events import AgentEvent, EventType
from app.interaction.interaction_contract import ChannelCommand
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from tests.runtime_support import configured_settings, finish_test_turn


def test_read_does_not_claim_and_cancelled_input_rolls_back(tmp_path) -> None:
    """读取后仍可撤销，失效输入不能随 checkpoint 保存。

    Args:
        tmp_path (Path): 测试专用 SQLite 目录。
    """
    async def scenario():
        """通过查询、取消和真实事务验证未交付输入的行为。"""
        path = str(tmp_path / "state.sqlite3")
        inbox = SessionStateManager("s", SqliteFactory(path))
        manager = SessionStateManager("s", SqliteFactory(path))
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

        class NoticeAgent(RuntimeAgentStub):
            """真人轮次正常保存，纯通知轮次总在保存之前失败。"""

            async def run(self, message, *, inbox_input, emit, **kwargs):
                """保存真人和同批通知，模拟正常完成。

                Args:
                    message (str): 用户正文。
                    inbox_input (InboxEnvelope): 当前真人输入。
                    **kwargs (dict): 同批任务通知及控制参数。

                Yields:
                    AgentEvent: 完成事件。
                """
                outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED)
                await emit(AgentEvent(EventType.RUN_STARTED, {"origin_channel": kwargs.get("origin_channel", "desktop"), "resumed": False}))
                await finish_test_turn(path, kwargs.get("inbox_inputs", (inbox_input,)), message=message,
                    runtime_inputs=kwargs.get("runtime_inputs", ()), release_queued=True)
                await emit(AgentEvent(EventType.DONE, None))

                return outcome

            async def run_runtime_inputs(self, messages=None, *, emit, **kwargs):
                """计数并注入交付失败，不调用模型。

                Args:
                    messages (tuple): 本次任务事实。
                    **kwargs (dict): 通知交付参数。

                Yields:
                    AgentEvent: 本用例在交付前失败，不产生执行事件。
                """
                outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED)
                await emit(AgentEvent(EventType.RUN_STARTED, {"origin_channel": "desktop", "resumed": False}))
                nonlocal attempts
                attempts += 1
                raise RuntimeError("通知保存失败")




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
                event = await asyncio.wait_for(next_runtime_event(scheduler), 2)
                if event.kind == kind:
                    return event

        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(path)))
        scheduler = build_test_scheduler(runtime_queue=supervisor.runtime_queue, agent_factory=factory, task_supervisor=supervisor,
            session_state_manager_factory=partial(SessionStateManager, sqlite_factory=SqliteFactory(path)),
         chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        records, inbox = supervisor.records, SessionStateManager("s", SqliteFactory(path))
        await scheduler.start()
        try:
            # 1. 本次真人输入先取得激活资格，随后任务终态才允许自动交付。
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "启动"}))
            await until("run_completed")
            await records.insert_running("task", "s", "任务", 1, str(tmp_path / "output"))
            await finish_task_record(supervisor, "task", "completed")
            error = await until("error")
            assert attempts == 1 and "failed_input" not in error.payload
            assert len(await inbox.read_task_terminals()) == 1
            # 2. 耗尽后后台通知不重新激活；新的真人输入则一并提交保留的任务事实。
            await records.insert_running("later", "s", "后续任务", 1, str(tmp_path / "later"))
            await finish_task_record(supervisor, "later", "completed")
            await until("task_status")
            assert attempts == 1
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "继续"}))
            await until("run_completed")
            assert await inbox.read_task_terminals() == ()
        finally:
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
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

            async def close(self):
                """替身没有外部资源。"""

        agent = WaitingAgent(AgentConfig(session_id="s", model_name="test", api_key="test", context_strategy=ContextStrategyConfig()),
                             sqlite_factory=SqliteFactory(path), llm_client=Model())
        inbox = SessionStateManager("s", SqliteFactory(path))

        async def consume():
            """完整读取取消轮次，返回供断言的公开事件。

            Returns:
                list[AgentEvent]: 本轮输出。
            """
            return [event for event in await collect_agent_events(agent.run, '取消我', inbox_input=item)]

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
def test_delivery_retries_only_transaction_and_never_replays_agent(tmp_path, monkeypatch, failures) -> None:
    """输入具名事务最多四次尝试，模型和实际 Agent 始终只执行一次。"""
    async def scenario():
        """通过真实 Agent 与 SQLite 临时锁故障验证事务重试及耗尽后的暂停。"""
        import sqlite3
        from app.crud.agent_state_dao import AgentStateDao
        from app.agent.models.contracts import StreamChunk
        path = str(tmp_path / "state.sqlite3")
        factory = SqliteFactory(path)
        attempts = 0
        model_calls = 0
        agent_calls = 0
        original = AgentStateDao._write_state_in_transaction

        async def save(dao, db, state, **kwargs):
            """只在输入进展写入时模拟 BUSY，最终保存使用真实事务。"""
            nonlocal attempts
            if dao.session_id == "s" and kwargs.get("checkpoint_type") == "intermediate":
                attempts += 1
                if attempts <= failures:
                    error = sqlite3.OperationalError("输入保存暂时占用")
                    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise error
            return await original(dao, db, state, **kwargs)

        class Model:
            """只有输入已经交付才产生最终回答。"""

            async def astream(self, messages, tools=None):
                """记录模型业务实际执行的次数。"""
                nonlocal model_calls
                model_calls += 1
                yield StreamChunk(content="已完成")

            async def close(self):
                """测试模型没有独立资源。"""

        def create(context, manager):
            """每次业务创建实际 Agent，事务重试始终复用原执行。"""
            nonlocal agent_calls
            agent_calls += 1
            return Agent(AgentConfig(context, "test", "test", ContextStrategyConfig()), factory,
                         llm_client=Model(), session_state_manager=manager)

        scheduler = build_test_scheduler(runtime_queue=asyncio.Queue(), agent_factory=create,
            session_state_manager_factory=lambda context: SessionStateManager(context, factory), chat_settings_dao=configured_settings())
        inbox = SessionStateManager("s", factory)
        try:
            await scheduler.start()
            monkeypatch.setattr(AgentStateDao, "_write_state_in_transaction", save)
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "原始输入"}))
            events = []
            ending = "history_restored" if failures == 4 else "run_completed"
            while not events or events[-1].kind != ending:
                events.append(await asyncio.wait_for(next_runtime_event(scheduler), 2))
            assert attempts == min(failures + 1, 4)
            assert agent_calls == 1 and model_calls == (0 if failures == 4 else 1)
            assert [event.kind for event in events].count("run_started") == 1
            if failures == 4:
                assert any(event.kind == "error" and event.payload["save_failed"] for event in events)
                assert (await scheduler.snapshot("s")).payload["session_state"] == "paused"
                assert [item.payload["message"] for item in await inbox.pending_humans()] == ["原始输入"]
                await scheduler.task_supervisor.records.insert_running("later", "s", title="后续任务", background=True)
                await finish_task_record(scheduler.task_supervisor, "later", "completed")
                await scheduler.runtime_queue.join()
                assert attempts == 4 and agent_calls == 1 and model_calls == 0
            else:
                assert [event.kind for event in events] == ["run_started", "human_message", "text_chunk", "run_completed"]
                assert not await inbox.pending_humans()
        finally:
            await inbox.close()
            await close_test_runtime(scheduler)
    asyncio.run(scenario())
