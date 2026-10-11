"""通过真实激活入口与临时 SQLite 验证记忆批次和内部通知的提交时机。"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.core.event_router import AgentEvent, EventType
from app.agent.memory.store.chat_history_store import ChatHistoryStore
from app.agent.models.llm_client import StreamChunk
from app.agent.plugins.base import BasePlugin, HookContext, PluginHook
from app.agent.plugins.memory import MemoryPlugin
from app.agent.runtime.session_inbox import InboxEnvelope, SessionInbox
from app.agent.state import AgentState
from app.agent.state.manager import StateManager
from app.agent.state.messages import BaseMessage, HumanMessage, TaskResultMessage, ToolCall
from app.agent.tools.base import BaseTool, ToolContext, ToolResult
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.runtime_types import InteractionEventKind, InterruptKind, TaskStatus
from app.services.agent_runtime_scheduler import AgentRuntimeScheduler
from app.services.channel_subscription_service import ChannelSubscriptionService
from app.services.interaction_contract import ChannelCommand, InteractionEvent
from app.services.interaction_event_hub import InteractionEventHub
import app.agent.plugins.memory as memory_plugin_module


SESSION_ID = "activation-input-lifecycle"
WAIT_SECONDS = 5


class QueuedModel:
    """让测试在真实模型请求处安排任务结束和回复，不访问外部模型。

    Attributes:
        requests (asyncio.Queue[list[dict]]): Agent 实际发送的模型输入。
        replies (asyncio.Queue[tuple[StreamChunk, ...]]): 测试为下一次请求安排的回复。
    """

    def __init__(self) -> None:
        """创建每个模型替身独立的请求与回复队列。"""
        self.requests: asyncio.Queue[list[dict]] = asyncio.Queue()
        self.replies: asyncio.Queue[tuple[StreamChunk, ...]] = asyncio.Queue()

    async def astream(self, messages: list[dict], tools=None) -> AsyncIterator[StreamChunk]:
        """记录真实输入，等待测试给出回复后按顺序返回分片。

        Args:
            messages (list[dict]): 管道准备好的模型输入。
            tools (list | None): 当前可用工具定义。

        Yields:
            StreamChunk: 测试安排的正文或工具调用。
        """
        await self.requests.put(messages)
        reply = await self.replies.get()
        for chunk in reply:
            yield chunk

    async def close(self) -> None:
        """完成模型替身清理；没有网络连接需要关闭。"""


class RecordingMemory:
    """读取真实协议历史，只替换外部召回和记忆提取。

    Attributes:
        chat_history_store (ChatHistoryStore): 临时数据库中的真实协议日志查询器。
        cutoffs (list[int]): 激活准备时读取的真实历史截止位置。
        extractions (list[tuple[list[BaseMessage], list[BaseMessage]]]): 实际进入提取的当前批次与前情。
        extracted (asyncio.Event): 提取已经收到材料时通知测试结束等待。
    """

    def __init__(self, db_path: Path) -> None:
        """绑定临时日志，创建材料记录和完成通知。

        Args:
            db_path (Path): 测试临时 SQLite 文件。
        """
        self.chat_history_store = ChatHistoryStore(db_path=str(db_path))
        self.cutoffs: list[int] = []
        self.extractions: list[tuple[list[BaseMessage], list[BaseMessage]]] = []
        self.extracted = asyncio.Event()

    async def get_protocol_history_cutoff(self) -> int:
        """读取并记录输入提交前已有协议消息的最后编号。

        Returns:
            int: 当前测试会话的真实协议日志高水位。
        """
        cutoff = await self.chat_history_store.get_latest_message_id(SESSION_ID)
        self.cutoffs.append(cutoff)
        return cutoff

    async def get_context(self, **kwargs) -> str:
        """替换外部记忆召回，让测试只关注提取时机。

        Args:
            kwargs (dict): 插件传入的召回查询和类别开关。

        Returns:
            str: 不追加额外召回材料的空文本。
        """
        return ""

    async def add(self, messages: list[BaseMessage], history: list[BaseMessage], **kwargs) -> None:
        """记录真实批次查询得到的提取材料，随后通知测试。

        Args:
            messages (list[BaseMessage]): 当前待提取批次。
            history (list[BaseMessage]): 只作参考的更早对话。
            kwargs (dict): 情景和语义记忆的启用开关。
        """
        self.extractions.append((list(messages), list(history)))
        self.extracted.set()


class RecordingHooks(BasePlugin):
    """作为真实插件记录调用方可观察的激活提交事件。

    Attributes:
        events (list[PluginHook]): 多个 Agent 激活共用的事件记录。
    """

    name = "record_activation_commit"

    def __init__(self, events: list[PluginHook]) -> None:
        """绑定测试的钩子观察记录。

        Args:
            events (list[PluginHook]): 用于观察激活生命周期的列表。
        """
        self.events = events

    @property
    def hooks(self) -> list[PluginHook]:
        """订阅激活输入的前后提交事件。

        Returns:
            list[PluginHook]: 新激活应各触发一次的两个事件。
        """
        return [PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, PluginHook.AFTER_ACTIVATION_INPUT_COMMIT]

    async def execute(self, context: HookContext) -> None:
        """记录真实插件分发事件，不修改 Agent 状态。

        Args:
            context (HookContext): Agent 提交入口分发的钩子上下文。
        """
        self.events.append(context.hook)


class StepTool(BaseTool):
    """在真实工具管道中完成一个无外部副作用的步骤。"""

    name = "activation_step"
    description = "完成激活生命周期测试步骤"
    parameters_schema = {"type": "object", "properties": {}}

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """完成工具调用，使管道进入下一次模型请求。

        Args:
            args (dict): 不需要额外参数的步骤输入。
            context (ToolContext): 真实管道提供的执行上下文。

        Returns:
            ToolResult: 已完成步骤的工具结果。
        """
        return ToolResult.success("步骤已完成")


async def make_agent(db_path: Path, model: QueuedModel, hooks: list[PluginHook]) -> Agent:
    """通过公开注册入口装配真实 Agent 与内置记忆插件。

    Args:
        db_path (Path): 本测试共用的临时 SQLite 文件。
        model (QueuedModel): 外部模型的队列替身。
        hooks (list[PluginHook]): 真实插件事件的观察记录。

    Returns:
        Agent: 保留真实状态、插件和执行管道的 Agent。
    """
    agent = Agent(
        AgentConfig(
            session_id=SESSION_ID, model_name="test", api_key="test",
            context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        ),
        db_path=str(db_path), llm_client=model,
    )
    agent.register_tool(StepTool())
    await agent.register_plugin(MemoryPlugin(enable_diary=False, enable_semantic=False))
    await agent.register_plugin(RecordingHooks(hooks))
    return agent


async def seed_history(manager: StateManager, completed: int, counter: int) -> None:
    """保存明确编号的完整旧轮次，作为待消费批次的真实材料。

    Args:
        manager (StateManager): 临时数据库中的状态访问对象。
        completed (int): 已经有普通助手回复的旧轮次数。
        counter (int): 尚未消费的真人输入计数。
    """
    state = AgentState.create_new(SESSION_ID)
    for number in range(1, completed + 1):
        state.add_user_message(f"human-{number}")
        state.add_assistant_message(f"reply-{number}")
    state.long_term_memory_turn_counter = counter
    await manager.save(state, checkpoint_type="completed")


async def publish_terminal(records: TaskRecordDao, db_path: Path, task_id: str) -> None:
    """通过真实任务终态事务保存一条 Inbox 通知，不启动外部进程。

    Args:
        records (TaskRecordDao): 测试任务记录访问对象。
        db_path (Path): 用于安排临时输出文件路径。
        task_id (str): 本条通知的独立任务编号。
    """
    await records.insert_running(task_id, SESSION_ID, "测试任务", 1, str(db_path.with_suffix(".out")))
    assert await records.finish_once(task_id, TaskStatus.COMPLETED, 0, "任务已完成")


def bind_memory(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> RecordingMemory:
    """为后续真实 Agent 激活绑定使用临时日志的外部记忆记录器。

    Args:
        db_path (Path): 本测试共用的临时数据库。
        monkeypatch (pytest.MonkeyPatch): 测试结束时恢复记忆入口的替换工具。

    Returns:
        RecordingMemory: 保留真实批次查询的提取记录器。
    """
    memory = RecordingMemory(db_path)

    def locate_memory(session_id: str) -> RecordingMemory:
        """将当前测试会话的记忆请求交给记录器。

        Args:
            session_id (str): 请求记忆的会话编号。

        Returns:
            RecordingMemory: 当前测试的外部记忆记录器。
        """
        assert session_id == SESSION_ID
        return memory

    monkeypatch.setattr(memory_plugin_module, "get_memory_manager", locate_memory)
    return memory


def terminal_message(claim: InboxEnvelope) -> TaskResultMessage:
    """将单条真实完成任务的领取记录投影为 Agent 输入。

    Args:
        claim (InboxEnvelope): 通过真实 Inbox 领取的完成任务通知。

    Returns:
        TaskResultMessage: 保留任务编号和输出的内部输入消息。
    """
    return TaskResultMessage(
        task_id=claim.payload["task_id"], task_name=claim.payload["task_name"],
        status="completed", result=claim.payload["result"],
    )


async def drain(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    """读取真实 Agent 的公开事件流，等待提交或中断完成。

    Args:
        stream (AsyncIterator[AgentEvent]): Agent 激活或恢复入口返回的流。

    Returns:
        list[AgentEvent]: 按实际顺序产生的事件。
    """
    return [event async for event in stream]


async def wait_event(stream: AsyncIterator[InteractionEvent], kind: InteractionEventKind) -> None:
    """消费真实渠道事件，直到当前轮次达到需要观察的状态。

    Args:
        stream (AsyncIterator[InteractionEvent]): EventHub 的公开订阅流。
        kind (InteractionEventKind): 本次需要等待的事件类别。
    """
    while True:
        event = await asyncio.wait_for(anext(stream), WAIT_SECONDS)
        assert event.kind != InteractionEventKind.ERROR, event.payload
        if event.kind == kind:
            return


@pytest.mark.parametrize("completed", [9, 19])
def test_mid_turn_notification_preserves_batch_until_next_activation(tmp_path: Path, monkeypatch, completed: int) -> None:
    """当前轮次收到任务通知后保留计数，下一激活提取正确十轮。

    Args:
        tmp_path (Path): Pytest 提供的隔离目录。
        monkeypatch (pytest.MonkeyPatch): 替换外部记忆入口并在测试后恢复。
        completed (int): 首批或已有旧批次时的完整历史轮数。
    """
    async def scenario() -> None:
        """沿真实 Scheduler、Agent 和通知事务观察两个连续激活。"""
        db_path = tmp_path / "state.sqlite3"
        memory = RecordingMemory(db_path)
        hooks: list[PluginHook] = []

        def locate_memory(session_id: str) -> RecordingMemory:
            """让内置插件使用真实日志和外部提取记录器。

            Args:
                session_id (str): 请求记忆的会话编号。

            Returns:
                RecordingMemory: 本测试会话的提取记录器。
            """
            assert session_id == SESSION_ID
            return memory

        monkeypatch.setattr(memory_plugin_module, "get_memory_manager", locate_memory)
        models = [QueuedModel(), QueuedModel()]
        agents = [await make_agent(db_path, model, hooks) for model in models]
        remaining = list(agents)

        def create_agent(session_id: str) -> Agent:
            """把预先装配的真实 Agent 依次交给生产调度路径。

            Args:
                session_id (str): 本次激活的会话编号。

            Returns:
                Agent: 具有独立外部模型队列的真实 Agent。
            """
            assert session_id == SESSION_ID
            return remaining.pop(0)

        def create_inbox(session_id: str) -> SessionInbox:
            """为调度器提供同一临时数据库的真实 Inbox。

            Args:
                session_id (str): 请求访问的会话编号。

            Returns:
                SessionInbox: 对应会话的持久输入访问对象。
            """
            return SessionInbox(session_id, str(db_path))

        def create_manager(session_id: str) -> StateManager:
            """为调度器提供隔离状态访问对象。

            Args:
                session_id (str): 请求访问的会话编号。

            Returns:
                StateManager: 同一 SQLite 中的会话状态访问对象。
            """
            return StateManager(session_id, str(db_path))

        manager = create_manager(SESSION_ID)
        inbox = create_inbox(SESSION_ID)
        records = TaskRecordDao(str(db_path))
        hub = InteractionEventHub(subscriptions=ChannelSubscriptionService(str(db_path)))
        scheduler = AgentRuntimeScheduler(
            hub, agent_factory=create_agent, inbox_factory=create_inbox,
            state_manager_factory=create_manager,
        )
        stream = hub.subscribe(SESSION_ID, "desktop")
        try:
            # 1. 保存旧完整轮次后启动真实运行时，再提交达到阈值的真人输入。
            await seed_history(manager, completed, 9)
            await scheduler.start()
            await anext(stream)
            current = completed + 1
            assert (await scheduler.submit(ChannelCommand(
                SESSION_ID, "desktop", "human_turn", {"message": f"human-{current}"},
            ))).accepted
            await asyncio.wait_for(models[0].requests.get(), WAIT_SECONDS)
            assert (await manager.load()).long_term_memory_turn_counter == 10

            # 2. 当前模型尚未回复时保存任务通知；工具完成后的模型前回调负责真实交付。
            await publish_terminal(records, db_path, "mid-turn")
            models[0].replies.put_nowait((StreamChunk(tool_call=ToolCall(
                id="step-1", name="activation_step", args={},
            )),))
            await asyncio.wait_for(models[0].requests.get(), WAIT_SECONDS)
            saved = await manager.load()
            assert any(isinstance(item, TaskResultMessage) and item.task_id == "mid-turn" for item in saved.messages)
            assert await inbox.claim_task_terminals() == ()
            assert saved.long_term_memory_turn_counter == 10
            assert memory.extractions == []
            assert len(hooks) == 2

            # 3. 当前轮次正常完成仍保留到期计数，记录下一输入之前的协议高水位。
            models[0].replies.put_nowait((StreamChunk(content=f"reply-{current}"),))
            await wait_event(stream, InteractionEventKind.RUN_COMPLETED)
            assert (await manager.load()).long_term_memory_turn_counter == 10
            cutoff = await memory.chat_history_store.get_latest_message_id(SESSION_ID)

            # 4. 下一真人激活才消费批次；真实日志查询应选当前十轮，并包含中途任务事实。
            assert (await scheduler.submit(ChannelCommand(
                SESSION_ID, "desktop", "human_turn", {"message": f"human-{current + 1}"},
            ))).accepted
            await asyncio.wait_for(models[1].requests.get(), WAIT_SECONDS)
            await asyncio.wait_for(memory.extracted.wait(), WAIT_SECONDS)
            selected, _history = memory.extractions[0]
            assert [item.text for item in selected if isinstance(item, HumanMessage)] == [
                f"human-{number}" for number in range(current - 9, current + 1)
            ]
            assert any(isinstance(item, TaskResultMessage) and item.task_id == "mid-turn" for item in selected)
            assert memory.cutoffs == [cutoff]
            assert len(memory.extractions) == 1
            assert (await manager.load()).long_term_memory_turn_counter == 1
            assert len(hooks) == 4
            models[1].replies.put_nowait((StreamChunk(content="下一轮完成"),))
            await wait_event(stream, InteractionEventKind.RUN_COMPLETED)
        finally:
            # 5. 先关闭运行时等待其 worker 退出，再释放订阅和测试数据库连接。
            await scheduler.close()
            await stream.aclose()
            for agent in agents:
                await agent.close()
            await manager.close()
            await inbox.close()
            await records.close()

    asyncio.run(scenario())


def test_frozen_history_range_excludes_later_completed_activations(tmp_path: Path, monkeypatch) -> None:
    """固定的旧历史范围不吸收后来已经完成的新激活轮次。

    Args:
        tmp_path (Path): Pytest 提供的隔离目录。
        monkeypatch (pytest.MonkeyPatch): 外部记忆入口的替换工具。
    """
    async def scenario() -> None:
        """用真实 Agent 保存后来两轮，再对比固定范围与最新完整历史。"""
        db_path = tmp_path / "state.sqlite3"
        memory = bind_memory(db_path, monkeypatch)
        model = QueuedModel()
        agent = await make_agent(db_path, model, [])
        manager = StateManager(SESSION_ID, str(db_path))
        try:
            # 1. 记录已有十轮的截止位置；计数置零表示旧批次已处理，避免准备额外任务。
            await seed_history(manager, 10, 0)
            cutoff = await memory.chat_history_store.get_latest_message_id(SESSION_ID)
            # 2. 通过真实新激活入口保存第十一和十二轮，确保它们已经具备普通助手回复。
            for number in (11, 12):
                model.replies.put_nowait((StreamChunk(content=f"reply-{number}"),))
                events = await drain(agent.run(f"human-{number}"))
                assert events[-1].type == EventType.DONE
            # 3. 固定范围只能选原十轮；不限定范围时应选到新轮次，证明对照材料已落盘。
            _history, frozen = await memory.chat_history_store.get_recent_completed_turns(
                SESSION_ID, current_turns=10, max_history_id=cutoff,
            )
            _history, latest = await memory.chat_history_store.get_recent_completed_turns(
                SESSION_ID, current_turns=10,
            )
            assert [item.text for item in frozen if isinstance(item, HumanMessage)] == [
                f"human-{number}" for number in range(1, 11)
            ]
            assert [item.text for item in latest if isinstance(item, HumanMessage)] == [
                f"human-{number}" for number in range(3, 13)
            ]
            assert memory.extractions == []
        finally:
            await agent.close()
            await manager.close()

    asyncio.run(scenario())


def test_runtime_only_activation_extracts_previous_batch_without_counting_notice(tmp_path: Path, monkeypatch) -> None:
    """纯任务通知的新激活仍提取到期批次，通知及其回复不混入旧范围。

    Args:
        tmp_path (Path): Pytest 提供的隔离目录。
        monkeypatch (pytest.MonkeyPatch): 外部记忆入口的替换工具。
    """
    async def scenario() -> None:
        """保存完整十轮后，通过纯内部输入入口观察提取和持久计数。"""
        db_path = tmp_path / "state.sqlite3"
        memory = bind_memory(db_path, monkeypatch)
        hooks: list[PluginHook] = []
        model = QueuedModel()
        agent = await make_agent(db_path, model, hooks)
        manager = StateManager(SESSION_ID, str(db_path))
        inbox = SessionInbox(SESSION_ID, str(db_path))
        records = TaskRecordDao(str(db_path))
        try:
            # 1. 上一批十轮已经完整；本次通知只启动新激活，不增加真人计数。
            await seed_history(manager, 10, 10)
            cutoff = await memory.chat_history_store.get_latest_message_id(SESSION_ID)
            await publish_terminal(records, db_path, "runtime-activation")
            claims = await inbox.claim_task_terminals()
            model.replies.put_nowait((StreamChunk(content="通知处理完成"),))
            events = await drain(agent.run_runtime_inputs(
                tuple(terminal_message(claim) for claim in claims), inbox_claims=claims,
            ))
            await asyncio.wait_for(memory.extracted.wait(), WAIT_SECONDS)

            # 2. 原批次真实提取一次，新通知与它的助手回复已经保存但不能进入旧批次。
            saved = await manager.load()
            assert events[-1].type == EventType.DONE
            assert saved.long_term_memory_turn_counter == 0
            assert await inbox.status(claims[0].id) == "delivered"
            assert len([item for item in saved.messages if isinstance(item, HumanMessage)]) == 10
            selected, _history = memory.extractions[0]
            assert [item.text for item in selected if isinstance(item, HumanMessage)] == [
                f"human-{number}" for number in range(1, 11)
            ]
            assert not any(isinstance(item, TaskResultMessage) for item in selected)
            assert memory.cutoffs == [cutoff]
            assert len(memory.extractions) == 1
            assert hooks == [PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, PluginHook.AFTER_ACTIVATION_INPUT_COMMIT]
        finally:
            await agent.close()
            await manager.close()
            await inbox.close()
            await records.close()

    asyncio.run(scenario())


def test_mixed_activation_and_repeated_notices_use_one_activation_hook_pair(tmp_path: Path, monkeypatch) -> None:
    """同批真人与任务通知只提交一次激活，中途多次通知不重复准备记忆。

    Args:
        tmp_path (Path): Pytest 提供的隔离目录。
        monkeypatch (pytest.MonkeyPatch): 外部记忆入口的替换工具。
    """
    async def scenario() -> None:
        """在真实管道的三次模型请求之间交付独立任务通知。"""
        db_path = tmp_path / "state.sqlite3"
        memory = bind_memory(db_path, monkeypatch)
        hooks: list[PluginHook] = []
        model = QueuedModel()
        agent = await make_agent(db_path, model, hooks)
        manager = StateManager(SESSION_ID, str(db_path))
        inbox = SessionInbox(SESSION_ID, str(db_path))
        records = TaskRecordDao(str(db_path))
        running: asyncio.Task | None = None

        async def deliver_notifications(state: AgentState) -> None:
            """由真实模型前回调领取并提交当前已到达的任务通知。

            Args:
                state (AgentState): 管道已闭合工具协议、准备下次模型请求的状态。
            """
            claims = await inbox.claim_task_terminals()
            if claims:
                await agent.commit_runtime_inputs(state, tuple(terminal_message(claim) for claim in claims), claims)

        try:
            # 1. 真人和既有任务通知同批领取，通过真人入口只执行一组激活钩子。
            await seed_history(manager, 9, 9)
            await publish_terminal(records, db_path, "initial")
            await inbox.submit_human("desktop", "human-10")
            batch = await inbox.claim_activation_batch()
            terminals = tuple(claim for claim in batch if claim.kind == "task_terminal")
            running = asyncio.create_task(drain(agent.run(
                "human-10", inbox_claims=batch,
                runtime_inputs=tuple(terminal_message(claim) for claim in terminals),
                before_next_llm=deliver_notifications,
            )))
            await asyncio.wait_for(model.requests.get(), WAIT_SECONDS)

            # 2. 每次工具调用前保存新通知；下一模型请求前实际保存通知但不进入新激活。
            for number in (1, 2):
                await publish_terminal(records, db_path, f"mid-{number}")
                model.replies.put_nowait((StreamChunk(tool_call=ToolCall(
                    id=f"step-{number}", name="activation_step", args={},
                )),))
                await asyncio.wait_for(model.requests.get(), WAIT_SECONDS)
                assert (await manager.load()).long_term_memory_turn_counter == 10
            model.replies.put_nowait((StreamChunk(content="reply-10"),))
            events = await asyncio.wait_for(running, WAIT_SECONDS)

            # 3. 所有通知都成为持久事实，计数与钩子仍只属于最初的激活。
            saved = await manager.load()
            assert events[-1].type == EventType.DONE
            assert {item.task_id for item in saved.messages if isinstance(item, TaskResultMessage)} == {
                "initial", "mid-1", "mid-2",
            }
            assert await inbox.status(terminals[0].id) == "delivered"
            assert await inbox.claim_task_terminals() == ()
            assert saved.long_term_memory_turn_counter == 10
            assert memory.cutoffs == [] and memory.extractions == []
            assert hooks == [PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, PluginHook.AFTER_ACTIVATION_INPUT_COMMIT]
        finally:
            if running is not None and not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            await agent.close()
            await manager.close()
            await inbox.close()
            await records.close()

    asyncio.run(scenario())


class PausingTool(BaseTool):
    """让真实工具协议暂停等待审批或桌面结果，不产生屏幕副作用。

    Attributes:
        kind (InterruptKind): 本次测试需要恢复的中断类别。
    """

    name = "activation_pause"
    description = "等待激活生命周期测试结果"
    parameters_schema = {"type": "object", "properties": {}}
    is_resumable = True

    def __init__(self, kind: InterruptKind) -> None:
        """保存首次执行需要返回的中断类别。

        Args:
            kind (InterruptKind): 审批或桌面续执行请求。
        """
        self.kind = kind

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """首次执行暂停，收到恢复数据后完成原工具调用。

        Args:
            args (dict): 不需要额外参数的测试调用。
            context (ToolContext): 真实管道提供的恢复数据。

        Returns:
            ToolResult: 首次中断请求或恢复后的成功结果。
        """
        if context.resume_data is None:
            if self.kind == InterruptKind.AUTHORIZATION_REQUEST:
                return ToolResult.needs_input(self.kind, "等待审批")
            return ToolResult.needs_input(InterruptKind.AUTHORIZATION_REQUEST, "等待审批")
        return ToolResult.success("原工具已恢复")


@pytest.mark.parametrize("kind", [InterruptKind.AUTHORIZATION_REQUEST])
def test_resuming_original_turn_does_not_commit_another_activation(tmp_path: Path, monkeypatch, kind: InterruptKind) -> None:
    """审批或桌面结果继续原轮次，中途任务通知不能消费该轮记忆计数。

    Args:
        tmp_path (Path): Pytest 提供的隔离目录。
        monkeypatch (pytest.MonkeyPatch): 外部记忆入口的替换工具。
        kind (InterruptKind): 本次需要继续处理的中断类别。
    """
    async def scenario() -> None:
        """通过真实 run 和 resume 入口保存中断，再继续原工具调用。"""
        db_path = tmp_path / "state.sqlite3"
        memory = bind_memory(db_path, monkeypatch)
        hooks: list[PluginHook] = []
        model = QueuedModel()
        agent = await make_agent(db_path, model, hooks)
        agent.register_tool(PausingTool(kind))
        manager = StateManager(SESSION_ID, str(db_path))
        inbox = SessionInbox(SESSION_ID, str(db_path))
        records = TaskRecordDao(str(db_path))

        async def deliver_notifications(state: AgentState) -> None:
            """原工具恢复并闭合后，在模型请求前保存已到达的通知。

            Args:
                state (AgentState): 原轮次恢复后的当前状态。
            """
            claims = await inbox.claim_task_terminals()
            if claims:
                await agent.commit_runtime_inputs(state, tuple(terminal_message(claim) for claim in claims), claims)

        try:
            # 1. 达到阈值的真人输入已持久提交，但工具中断使该轮尚无普通助手回复。
            await seed_history(manager, 9, 9)
            await inbox.submit_human("desktop", "human-10")
            human = await inbox.claim_next()
            model.replies.put_nowait((StreamChunk(tool_call=ToolCall(
                id="pause-1", name="activation_pause", args={},
            )),))
            events = await drain(agent.run("human-10", inbox_claim=human))
            assert events[-1].type == EventType.INTERRUPT
            assert (await manager.load()).long_term_memory_turn_counter == 10

            # 2. 等待期间保存任务通知，原轮次恢复后的模型前交付仍不属于新激活。
            await publish_terminal(records, db_path, "during-interrupt")
            model.replies.put_nowait((StreamChunk(content="reply-10"),))
            result = {"kind": "approve"} if kind == InterruptKind.AUTHORIZATION_REQUEST else {"executed": True}
            resumed = await drain(agent.resume(result, before_next_llm=deliver_notifications))
            saved = await manager.load()
            assert resumed[-1].type == EventType.DONE
            assert not saved.is_interrupted()
            assert any(isinstance(item, TaskResultMessage) and item.task_id == "during-interrupt" for item in saved.messages)
            assert await inbox.claim_task_terminals() == ()
            assert saved.long_term_memory_turn_counter == 10
            assert memory.cutoffs == [] and memory.extractions == []
            assert hooks == [PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, PluginHook.AFTER_ACTIVATION_INPUT_COMMIT]
        finally:
            await agent.close()
            await manager.close()
            await inbox.close()
            await records.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("activation", ["human", "runtime"])
def test_failed_activation_commit_preserves_batch_and_retry_extracts_frozen_history(tmp_path: Path, monkeypatch, activation: str) -> None:
    """真实 SQLite 提交失败不消费持久计数，新激活重试后提取固定旧批次。

    Args:
        tmp_path (Path): Pytest 提供的隔离目录。
        monkeypatch (pytest.MonkeyPatch): 外部记忆入口的替换工具。
        activation (str): 真人或纯任务通知激活路径。
    """
    async def scenario() -> None:
        """在输入事务插入 checkpoint 时注入失败，再用真实入口重试。"""
        db_path = tmp_path / "state.sqlite3"
        memory = bind_memory(db_path, monkeypatch)
        hooks: list[PluginHook] = []
        models = [QueuedModel(), QueuedModel()]
        agents = [await make_agent(db_path, model, hooks) for model in models]
        manager = StateManager(SESSION_ID, str(db_path))
        inbox = SessionInbox(SESSION_ID, str(db_path))
        records = TaskRecordDao(str(db_path))

        def activate(agent: Agent, claims: tuple[InboxEnvelope, ...]) -> AsyncIterator[AgentEvent]:
            """按测试类别从公开入口提交同一份仍有效的领取记录。

            Args:
                agent (Agent): 本次实际执行输入提交的 Agent。
                claims (tuple[InboxEnvelope, ...]): 失败后尚未交付的领取快照。

            Returns:
                AsyncIterator[AgentEvent]: 真人或纯内部通知的新激活事件流。
            """
            if activation == "human":
                return agent.run("human-11", inbox_claims=claims)
            return agent.run_runtime_inputs(tuple(terminal_message(claim) for claim in claims), inbox_claims=claims)

        try:
            # 1. 旧十轮已完整，到期批次在本次新激活之前冻结；两类输入都使用真实领取。
            await seed_history(manager, 10, 10)
            cutoff = await memory.chat_history_store.get_latest_message_id(SESSION_ID)
            if activation == "human":
                await inbox.submit_human("desktop", "human-11")
                claims = (await inbox.claim_next(),)
            else:
                await publish_terminal(records, db_path, "retry-notice")
                claims = await inbox.claim_task_terminals()

            # 2. 用真实数据库触发器拒绝输入 checkpoint，不替换状态管理器或插件分发。
            async with aiosqlite.connect(str(db_path)) as db:
                await db.execute(
                    "CREATE TRIGGER reject_activation BEFORE INSERT ON checkpoints "
                    "WHEN NEW.checkpoint_type = 'intermediate' "
                    "BEGIN SELECT RAISE(ABORT, 'activation commit rejected'); END"
                )
                await db.commit()
            with pytest.raises(aiosqlite.IntegrityError, match="activation commit rejected"):
                await drain(activate(agents[0], claims))
            assert (await manager.load()).long_term_memory_turn_counter == 10
            assert await inbox.status(claims[0].id) == "claimed"
            assert memory.extractions == []
            assert models[0].requests.empty()
            assert hooks == [PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT]

            # 3. 数据库恢复后，新 Agent 加载仍到期的旧批次，提交成功后才提取一次。
            async with aiosqlite.connect(str(db_path)) as db:
                await db.execute("DROP TRIGGER reject_activation")
                await db.commit()
            models[1].replies.put_nowait((StreamChunk(content="新激活已经完成"),))
            events = await drain(activate(agents[1], claims))
            await asyncio.wait_for(memory.extracted.wait(), WAIT_SECONDS)
            saved = await manager.load()
            assert events[-1].type == EventType.DONE
            assert saved.long_term_memory_turn_counter == (1 if activation == "human" else 0)
            assert await inbox.status(claims[0].id) == "delivered"
            selected, _history = memory.extractions[0]
            assert [item.text for item in selected if isinstance(item, HumanMessage)] == [
                f"human-{number}" for number in range(1, 11)
            ]
            assert not any(isinstance(item, TaskResultMessage) for item in selected)
            assert memory.cutoffs == [cutoff, cutoff]
            assert len(memory.extractions) == 1
            assert hooks == [
                PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT,
                PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT,
                PluginHook.AFTER_ACTIVATION_INPUT_COMMIT,
            ]
        finally:
            for agent in agents:
                await agent.close()
            await manager.close()
            await inbox.close()
            await records.close()

    asyncio.run(scenario())
