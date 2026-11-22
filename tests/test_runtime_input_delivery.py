from tests.memory_support import memory_count, set_memory_count
"""验证输入提交事实、原子交付，以及生成器关闭和取消时的执行顺序。"""
from tests.runtime_support import collect_agent_events
from tests.runtime_support import runtime_hub, close_test_runtime
from tests.runtime_event_source import observe_session
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory

import asyncio
from base64 import b64encode
from tests.runtime_support import configured_settings
from contextlib import closing
from functools import partial
from io import BytesIO
import sqlite3

import pytest
from PIL import Image

from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.core.events import EventType
from app.agent.models.llm_client import StreamChunk
from app.agent.plugins.base import BasePlugin, PluginHook

from app.agent.state.manager import SessionStateManager, RequiredSaveError
from app.agent.state.messages import ImageContent, TaskResultMessage
from app.crud.task_record_dao import TaskRecordDao
from app.interaction.interaction_contract import InteractionCommand
from app.interaction.interaction_event_hub import InteractionEventHub
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler


class ProbeModel:
    """记录请求次数并提供固定回复，不访问外部模型。

    Attributes:
        calls (int): 管道实际开始模型请求的次数。
        closed (bool): Agent 是否释放了模型资源。
    """

    def __init__(self):
        """初始化当前测试独立的请求计数和关闭标记。"""
        self.calls = 0
        self.closed = False

    async def astream(self, messages, tools=None):
        """计数并返回一段正常回复。

        Args:
            messages (list): 管道准备的当前模型输入。
            tools (list | None): 当前可用工具定义。

        Yields:
            StreamChunk: 固定的完成正文。
        """
        self.calls += 1
        yield StreamChunk(content="完成")

    async def close(self):
        """记录 Agent 已关闭借用的测试模型。"""
        self.closed = True


class BlockingInputHooks(BasePlugin):
    """在 ON_INVOKE 钩子暂停，让测试观察此前已经输出的提交事实。

    Attributes:
        calls (list[PluginHook]): 按调用顺序记录的激活钩子。
        entered (asyncio.Event): ON_INVOKE 钩子已开始等待的信号。
        release (asyncio.Event): 测试允许 ON_INVOKE 钩子结束等待的信号。
    """

    name = "blocking_input_hooks"

    def __init__(self):
        """创建本测试的钩子记录和控制事件。"""
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def hooks(self):
        """只订阅一轮输入激活的提交前和执行开始钩子。

        Returns:
            list[PluginHook]: 输入保存前与执行开始的两个钩子。
        """
        return [PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, PluginHook.ON_INVOKE]

    async def execute(self, context):
        """记录激活步骤，在 ON_INVOKE 钩子等待测试放行。

        Args:
            context (HookContext): Agent 当前分发的激活步骤。
        """
        self.calls.append(context.hook)
        if context.hook == PluginHook.ON_INVOKE:
            self.entered.set()
            await self.release.wait()


def make_agent(tmp_path, model):
    """创建使用测试目录和外部模型替身的真实 Agent。

    Args:
        tmp_path (Path): 本测试独立的数据库目录。
        model (ProbeModel): 用于观察模型调用和资源释放的替身。

    Returns:
        Agent: 尚未开始执行的真实 Agent。
    """
    return Agent(AgentConfig(session_id="s", model_name="test", api_key="test",
                             context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)),
                 sqlite_factory=SqliteFactory(str(tmp_path / "state.sqlite3")), llm_client=model)


def test_human_commit_event_precedes_on_invoke_and_model(tmp_path) -> None:
    """提交事件先于 ON_INVOKE 钩子和模型，真实消息身份与交付状态已经可查询。

    Args:
        tmp_path (Path): 隔离存储目录。
    """
    async def scenario():
        """逐次读取真实生成器，控制 ON_INVOKE 钩子并验证事件和数据库顺序。"""
        model = ProbeModel()
        agent = make_agent(tmp_path, model)
        hooks = BlockingInputHooks()
        agent.register_plugin(hooks)
        inbox = SessionStateManager("s", SqliteFactory(str(tmp_path / "state.sqlite3")))
        human = await inbox.submit_human("你好")
        events = asyncio.Queue()
        release_commit = asyncio.Event()

        async def receive(event):
            """记录事件，在输入已提交处暂停，单独核对后续钩子尚未运行。"""
            events.put_nowait(event)
            if event.type == EventType.HUMAN_INPUT_COMMITTED:
                await release_commit.wait()

        pending = asyncio.create_task(agent.run("你好", inbox_input=human, receive_task_notifications=True, emit=receive))
        try:
            assert (await asyncio.wait_for(events.get(), 2)).type == EventType.RUN_STARTED
            event = await asyncio.wait_for(events.get(), 2)
            assert event.type == EventType.HUMAN_INPUT_COMMITTED
            assert event.data["input_sequence"] == human.id
            state = await agent.session_state_manager.load()
            assert event.data["message"] == state.messages[-1]
            assert await inbox.status(human.id) == "delivered"
            assert hooks.calls == [PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT]
            assert model.calls == 0
            release_commit.set()
            await asyncio.wait_for(hooks.entered.wait(), 1)
            assert not pending.done() and model.calls == 0
            hooks.release.set()
            assert (await asyncio.wait_for(events.get(), 2)).type == EventType.TEXT_CHUNK
            assert (await asyncio.wait_for(events.get(), 2)).type == EventType.DONE
            assert (await pending).kind == "completed"
            assert model.calls == 1
            assert hooks.calls == [PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, PluginHook.ON_INVOKE]
            assert memory_count(await agent.session_state_manager.load()) == 0
        finally:
            hooks.release.set()
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await agent.close()
            await inbox.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_during_on_invoke", [False, True])
def test_commit_stream_close_or_cancel_preserves_saved_fact(tmp_path, cancel_during_on_invoke) -> None:
    """提交后关闭或取消生成器不重复保存、不补跑 ON_INVOKE 钩子，也不开始模型请求。

    Args:
        tmp_path (Path): 隔离存储目录。
        cancel_during_on_invoke (bool): 是否继续到 ON_INVOKE 钩子等待处再取消。
    """
    async def scenario():
        """在提交事实输出处关闭，或在 ON_INVOKE 钩子中取消真实 Agent 流。"""
        model = ProbeModel()
        agent = make_agent(tmp_path, model)
        hooks = BlockingInputHooks()
        agent.register_plugin(hooks)
        inbox = SessionStateManager("s", SqliteFactory(str(tmp_path / "state.sqlite3")))
        human = await inbox.submit_human("保存后停止")
        events = asyncio.Queue()
        release_commit = asyncio.Event()

        async def receive(event):
            """输入提交后停在发布位置，供调用方明确取消当前调用。"""
            events.put_nowait(event)
            if event.type == EventType.HUMAN_INPUT_COMMITTED:
                await release_commit.wait()

        pending = asyncio.create_task(agent.run(human.payload["message"], inbox_input=human,
            receive_task_notifications=True, emit=receive))
        try:
            assert (await events.get()).type == EventType.RUN_STARTED
            assert (await events.get()).type == EventType.HUMAN_INPUT_COMMITTED
            if cancel_during_on_invoke:
                release_commit.set()
                await asyncio.wait_for(hooks.entered.wait(), 1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert model.calls == 0
            assert len(hooks.calls) == (2 if cancel_during_on_invoke else 1)
            with closing(sqlite3.connect(tmp_path / "state.sqlite3")) as db:
                assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 1
            await agent.close()
            assert model.closed
            # 1. Agent 只关闭自己的 SessionStateManager；调用方仍能读取其拥有的 Inbox。
            assert await inbox.status(human.id) == "delivered"
        finally:
            hooks.release.set()
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await agent.close()
            await inbox.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("entry", ["human", "task"])
@pytest.mark.parametrize("failure_table", ["checkpoints", "conversation_messages", "session_inbox"])
def test_input_commit_failure_rolls_back_all_persistent_writes(tmp_path, entry, failure_table) -> None:
    """三处写入任一失败都回滚消息、checkpoint 和 delivered，修复后只交付一次。

    Args:
        tmp_path (Path): 隔离存储目录。
        entry (str): 验证真人提交事件或任务通知交付入口。
        failure_table (str): 用真实触发器拒绝写入的表。
    """
    async def scenario():
        """在原子事务的不同位置注入 SQLite 错误，检查完整回滚和重试结果。"""
        path = str(tmp_path / "state.sqlite3")
        model = ProbeModel()
        agent = make_agent(tmp_path, model)
        inbox = SessionStateManager("s", SqliteFactory(path))
        records = TaskRecordDao(SqliteFactory(path))
        delivery = agent.session_state_manager
        events = []
        try:
            await agent.session_state_manager.load()
            if entry == "human":
                item = await inbox.submit_human("原子保存")
            else:
                await records.insert_running("task", "s", "任务", 1, str(tmp_path / "out"))
                await records.finish_once("task", "completed", 0)
                item = (await inbox.read_task_terminals())[0]
            action = "UPDATE" if failure_table == "session_inbox" else "INSERT"
            with closing(sqlite3.connect(path)) as db:
                db.execute(f"CREATE TRIGGER reject_input BEFORE {action} ON {failure_table} "
                           "BEGIN SELECT RAISE(ABORT, 'reject_input'); END")
            if entry == "human":
                async def receive_agent_event(event):
                    """观察输入事务失败前实际已发布的事件。"""
                    events.append(event)
                outcome = await agent.run('原子保存', inbox_input=item, receive_task_notifications=True, emit=receive_agent_event)
                assert outcome.kind == "save_failed" and "reject_input" in outcome.error
            else:
                with pytest.raises(RequiredSaveError, match="reject_input") as failure:
                    await delivery.deliver_pending(await agent.session_state_manager.load())
                assert isinstance(failure.value.__cause__, sqlite3.IntegrityError)
            assert all(event.type == EventType.RUN_STARTED for event in events) and model.calls == 0
            assert await inbox.status(item.id) == "pending"
            assert not (await agent.session_state_manager.load()).messages
            with closing(sqlite3.connect(path)) as db:
                assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
                assert db.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
                db.execute("DROP TRIGGER reject_input")
            if entry == "human":
                events = [event for event in await collect_agent_events(agent.run, '原子保存', inbox_input=item, receive_task_notifications=True)]
                assert events[0].type == EventType.RUN_STARTED
                assert events[1].type == EventType.HUMAN_INPUT_COMMITTED
                assert events[-1].type == EventType.DONE and model.calls == 1
            else:
                state = await agent.session_state_manager.load()
                await delivery.deliver_pending(state)
                await delivery.deliver_pending(state)
                assert len(state.messages) == 1 and isinstance(state.messages[0], TaskResultMessage)
                assert memory_count(state) == 0
            assert await inbox.status(item.id) == "delivered"
        finally:
            await agent.close()
            await inbox.close()
            await records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("provide_inbox", [False, True])
def test_first_model_boundary_reads_only_explicitly_provided_inbox(tmp_path, provide_inbox) -> None:
    """独立调用可不读取动态通知，提供 Inbox 时首个模型请求前实际交付且不重复。

    Args:
        tmp_path (Path): 隔离存储目录。
        provide_inbox (bool): 是否显式传入任务通知的读取来源。
    """
    async def scenario():
        """预先保存任务终态，不提供初始任务批次，验证真实 Pipeline 的首次检查。"""
        path = str(tmp_path / "state.sqlite3")
        model = ProbeModel()
        agent = make_agent(tmp_path, model)
        inbox = SessionStateManager("s", SqliteFactory(path))
        records = TaskRecordDao(SqliteFactory(path))
        try:
            await records.insert_running("task", "s", "任务", 1, str(tmp_path / "out"))
            await records.finish_once("task", "completed", 0)
            events = [event for event in await collect_agent_events(agent.run, '你好', receive_task_notifications=provide_inbox)]
            assert events[0].type == EventType.RUN_STARTED
            assert events[1].type == EventType.HUMAN_INPUT_COMMITTED
            assert events[1].data["input_sequence"] is None
            assert events[-1].type == EventType.DONE and model.calls == 1
            state = await agent.session_state_manager.load()
            assert sum(isinstance(message, TaskResultMessage) for message in state.messages) == int(provide_inbox)
            assert len(await inbox.read_task_terminals()) == int(not provide_inbox)
            await agent.close()
            await inbox.pending_humans()
        finally:
            await agent.close()
            await inbox.close()
            await records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("with_image", [False, True])
def test_hub_preserves_human_payload_before_blocked_on_invoke(tmp_path, with_image) -> None:
    """新提交事实映射为原界面事件，阻塞 ON_INVOKE 钩子时正文、附件和去重身份已经可见。

    Args:
        tmp_path (Path): 隔离会话和消息数据库目录。
        with_image (bool): 是否同时验证已保存真人附件的文件引用。
    """
    async def scenario():
        """用真实 Agent、Scheduler 和 Hub 接通提交事件，模型与 ON_INVOKE 钩子由测试控制。"""
        path = str(tmp_path / "state.sqlite3")
        model = ProbeModel()
        agent = make_agent(tmp_path, model)
        hooks = BlockingInputHooks()
        agent.register_plugin(hooks)
        images = []
        if with_image:
            with BytesIO() as buffer:
                Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
                images.append("data:image/png;base64," + b64encode(buffer.getvalue()).decode("ascii"))

        def factory(session_id, _manager):
            """为唯一测试轮次提供已装配钩子和模型的真实 Agent。

            Args:
                _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
                session_id (str): 本测试提交命令的会话。

            Returns:
                Agent: 本轮使用的隔离执行对象。
            """
            assert session_id == "s"
            return agent

        scheduler = build_test_scheduler(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(),
            agent_factory=factory,
            session_state_manager_factory=partial(SessionStateManager, sqlite_factory=SqliteFactory(path)),
        )
        hub = runtime_hub(scheduler)
        await scheduler.start()
        pass
        stream = observe_session("s", hub, scheduler)
        observer = SessionStateManager("s", SqliteFactory(path))
        try:
            await anext(stream)
            receipt = await scheduler.submit(InteractionCommand("s", "human_turn", {
                "message": "立即显示", "images": images,
            }))
            assert receipt.accepted
            assert (await asyncio.wait_for(anext(stream), 2)).kind == "run_started"
            human = await asyncio.wait_for(anext(stream), 2)
            await asyncio.wait_for(hooks.entered.wait(), 1)
            assert human.kind == "human_message"
            assert human.run_id is None
            saved_message = (await agent.session_state_manager.load()).messages[-1]
            references = [part.storage_ref for part in saved_message.content if isinstance(part, ImageContent)]
            assert human.payload == {
                "message": "立即显示", "images": references,
                "message_id": saved_message.message_id,
                "input_sequence": human.payload["input_sequence"],
            }
            if with_image:
                from app.agent.utils.domain.images import load_image_data_url
                assert load_image_data_url(references[0]) == images[0]
            assert await observer.status(human.payload["input_sequence"]) == "delivered"
            assert model.calls == 0
            hooks.release.set()
            kinds = []
            while not kinds or kinds[-1] != "run_completed":
                kinds.append((await asyncio.wait_for(anext(stream), 2)).kind)
            assert kinds == ["text_chunk", "run_completed"]
        finally:
            hooks.release.set()
            await stream.aclose()
            await hub.close()
            await close_test_runtime(scheduler)
            await agent.close()
            await observer.close()
    asyncio.run(scenario())
