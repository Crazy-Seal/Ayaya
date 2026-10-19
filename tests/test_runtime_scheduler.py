from tests.runtime_support import finish_test_turn
from app.services.channel_subscription_service import ChannelSubscriptionService
from tests.model_capture import capture_input
import asyncio
from pathlib import Path

from app.agent.core.event_router import AgentEvent, EventType
from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.models.llm_client import StreamChunk
from app.agent.state import AgentState
from app.agent.state.manager import StateManager
from app.agent.state.messages import ToolCall
from app.agent.state.messages import ToolMessage
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.runtime.session_inbox import SessionInbox
from app.crud.task_record_dao import TaskRecordDao
from app.interaction.interaction_contract import ChannelCommand
from app.interaction.interaction_event_hub import InteractionEventHub
from app.orchestration.agent_runtime_scheduler import AgentRuntimeScheduler


def test_message_arriving_before_wait_signal_is_cleared_is_processed_immediately(tmp_path: Path) -> None:
    """验证空查询期间到达的新消息立即唤醒 worker，不等待读取租期复查。

    Args:
        tmp_path (Path): 测试数据库目录。
    """
    async def scenario() -> None:
        """在真实 Inbox 查询之间插入第二条消息，检查公开命令的处理结果。"""
        path = str(tmp_path / "state.sqlite3")
        processed = asyncio.Event()
        arrived = False
        calls: list[str] = []

        class InterleavingInbox(SessionInbox):
            """在第一次查不到输入后，让用户的新消息恰好到达。"""
            async def read_activation_batch(self, **kwargs):
                """完成真实查询后安排新命令，让它与 worker 进入等待交错。

                Args:
                    **kwargs (dict): worker 的原读取条件。

                Returns:
                    tuple[InboxEnvelope, ...]: 本次真实查询的结果，不包含随后刚到达的消息。
                """
                nonlocal arrived
                batch = await super().read_activation_batch(**kwargs)
                if not batch and not arrived:
                    # 1. 此时数据库查询为空，通知已在查询前清除；新命令将在会话锁释放后保存并唤醒。
                    arrived = True
                    asyncio.create_task(scheduler.submit(ChannelCommand(
                        "s", "desktop", "human_turn", {"message": "恰好到达的新消息"},
                    )))
                return batch

        class RecordingAgent:
            """使用真实数据库保存输入，只替换模型回答。"""
            async def run(self, message, *, inbox_input=None, **kwargs):
                """保存当前真人输入，记录执行顺序后正常结束。

                Args:
                    message (str): 当前真人正文。
                    inbox_input (InboxEnvelope): 输入的读取记录。
                    **kwargs (dict): 其余运行参数。

                Yields:
                    AgentEvent: 这轮正常完成的事件。
                """
                manager = StateManager("s", path)
                try:
                    state = await manager.load()
                    state.add_user_message(message)
                    await manager.save(state, checkpoint_type="completed", inbox_inputs=(inbox_input,))
                finally:
                    await manager.close()
                calls.append(message)
                if message == "恰好到达的新消息":
                    processed.set()
                yield AgentEvent(EventType.DONE, None)

            async def close(self) -> None:
                """测试 Agent 不持有外部资源。"""

        scheduler = AgentRuntimeScheduler(
           agent_factory=lambda _session: RecordingAgent(),
            inbox_factory=lambda session: InterleavingInbox(session, path),
            state_manager_factory=lambda session: StateManager(session, path),
        )
        try:
            await scheduler.start()
            # 2. 第一条消息建立会话执行资格，第二条由查询时序触发，通过同一公开入口受理。
            assert (await scheduler.submit(ChannelCommand(
                "s", "desktop", "human_turn", {"message": "第一条消息"},
            ))).accepted
            # 3. 新命令必须在测试等待期内处理，不能依赖 30 秒读取租期复查。
            await asyncio.wait_for(processed.wait(), 2)
            assert calls == ["第一条消息", "恰好到达的新消息"]
        finally:
            await scheduler.close()

    asyncio.run(scenario())


def test_committed_human_is_visible_before_first_model_chunk(tmp_path: Path) -> None:
    """验证真人输入提交后即发布，不等待模型首个分片。

    Args:
        tmp_path (Path): Pytest 创建的隔离存储目录。
    """
    async def scenario() -> None:
        """阻塞真实 Agent 的模型读取，从公共事件订阅检查真人消息。"""
        # 1. 使用真实 Agent 和 SQLite，只替换模型输出并设置确定性的首片屏障。
        model_entered = asyncio.Event()
        release_model = asyncio.Event()
        db_path = str(tmp_path / "state.sqlite3")

        class WaitingLLM:
            """在测试放行前不产生任何模型分片。"""
            async def astream(self, messages: list[dict], tools=None):
                """标记模型已开始等待，放行后提供有效回复。

                Args:
                    messages (list[dict]): Agent 生成的模型输入。
                    tools (list | None): 当前工具 schema。

                Yields:
                    StreamChunk: 放行后的预设正文分片。
                """
                messages = capture_input(messages)
                model_entered.set()
                await release_model.wait()
                yield StreamChunk(content="回复。")

            async def close(self) -> None:
                """关闭模型替身，不访问外部服务。"""
                pass

        def make_agent(session_id: str) -> Agent:
            """创建使用测试数据库和首片屏障的真实 Agent。

            Args:
                session_id (str): 当前共享会话标识。

            Returns:
                Agent: 隔离的真实运行实例。
            """
            return Agent(
                AgentConfig(
                    session_id=session_id, model_name="test", api_key="test",
                    context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
                ),
                db_path=db_path, llm_client=WaitingLLM(),
            )

        subscriptions = ChannelSubscriptionService()
        scheduler = AgentRuntimeScheduler(
            subscriptions=subscriptions, agent_factory=make_agent,
            inbox_factory=lambda session: SessionInbox(session, db_path),
            state_manager_factory=lambda session: StateManager(session, db_path),
        )
        hub = InteractionEventHub(scheduler, subscriptions=subscriptions)
        await hub.start()
        await scheduler.start()
        events = hub.subscribe("s", "desktop")
        await anext(events)
        try:
            # 2. 模型首片保持阻塞，仍必须从订阅中收到已经提交的真人消息。
            receipt = await scheduler.submit(ChannelCommand(
                "s", "desktop", "human_turn", {"message": "请先显示我的输入"},
            ))
            assert receipt.accepted
            await asyncio.wait_for(model_entered.wait(), 2)
            assert (await anext(events)).kind == "run_started"
            human = await asyncio.wait_for(anext(events), 0.2)
            assert human.kind == "human_message"
            assert human.payload["message"] == "请先显示我的输入"
            assert human.payload["message_id"]
            assert human.origin_channel == "desktop"
        finally:
            # 3. 成败都放行模型并清理订阅和 worker，不留下后台等待。
            release_model.set()
            await events.aclose()
            await hub.close()
            await scheduler.close()

    asyncio.run(scenario())


def test_subscriber_disconnect_does_not_cancel_run_and_origin_is_scoped(tmp_path) -> None:
    """验证订阅断开不取消运行，回复只向桌面及原来源投递。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：订阅断开不取消运行，回复只向桌面及原来源投递。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        release = asyncio.Event()
        completed = asyncio.Event()
        calls: list[tuple[str, str]] = []

        class FakeAgent:
            """提供隔离测试替身，用于验证：订阅断开不取消运行，回复只向桌面及原来源投递。"""
            async def run(self, message, images=None, *, origin_channel="desktop", inbox_input=None, turn_control=None, before_next_llm=None, on_input_committed=None):
                """按场景屏障产生 Agent 事件并提交测试输入。

                Args:
                    message (对象): 真人文本或类型声明对应的内部通知。
                    images (对象): 真人附件的图片 data URL 列表。
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    inbox_input (对象): 随本次 checkpoint 原子交付的单条读取快照。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。
                    on_input_committed (Callable | None): 真人输入实际提交后的观察回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                calls.append((message, origin_channel))
                manager = StateManager("s", str(tmp_path / "runtime.sqlite3"))
                try:
                    state = await manager.load()
                    human_message = state.add_user_message(message, origin_channel=origin_channel)
                    await manager.save(
                        state, checkpoint_type="intermediate", inbox_inputs=(inbox_input,),
                    )
                    # 1. 替身遵循真实 Agent：只有事务成功后才发布输入事实。
                    if on_input_committed is not None:
                        await on_input_committed(human_message)
                finally:
                    await manager.close()
                yield AgentEvent(EventType.TEXT_CHUNK, "正在处理")
                await release.wait()
                completed.set()
                yield AgentEvent(EventType.TEXT_CHUNK, "完成")
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        subscriptions = ChannelSubscriptionService()
        subscriptions.register_channel("qq")
        subscriptions.set_default_enabled("qq", True)
        subscriptions.register_channel("telegram")
        subscriptions.set_default_enabled("telegram", True)
        scheduler = AgentRuntimeScheduler(
            subscriptions=subscriptions,
           agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SessionInbox(session, str(tmp_path / "runtime.sqlite3")),
        )
        hub = InteractionEventHub(scheduler, subscriptions=subscriptions)
        await hub.start()
        await scheduler.start()
        desktop = hub.subscribe("s", "desktop")
        qq = hub.subscribe("s", "qq")
        telegram = hub.subscribe("s", "telegram")
        await anext(desktop)
        await anext(qq)
        await anext(telegram)
        receipt = await scheduler.submit(ChannelCommand("s", "qq", "human_turn", {"message": "你好"}))
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert receipt.accepted and receipt.run_id is None
        for stream in (desktop, qq):
            while (await asyncio.wait_for(anext(stream), 1)).kind != "human_message":
                pass
        await desktop.aclose()
        release.set()
        await asyncio.wait_for(completed.wait(), 1)
        seen = []
        while "run_completed" not in seen:
            seen.append((await asyncio.wait_for(anext(qq), 1)).kind)
        assert "run_completed" in seen
        assert calls == [("你好", "qq")]
        await qq.aclose()
        await telegram.aclose()
        await hub.close()
        await scheduler.close()

    asyncio.run(scenario())


def test_snapshot_excludes_reply_text_before_and_after_tool_call(tmp_path) -> None:
    """验证快照不携带回答正文，工具前后的文字仅通过增量转发。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：快照不携带回答正文，工具前后的文字仅通过增量转发。"""
        db_path = str(tmp_path / "state.sqlite3")
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        tool_started = asyncio.Event()
        release = asyncio.Event()

        class FakeAgent:
            """提供隔离测试替身，用于验证：快照不携带回答正文，工具前后的文字仅通过增量转发。"""
            async def run(self, message, images=None, *, origin_channel="desktop", inbox_input=None, turn_control=None, before_next_llm=None, on_input_committed=None):
                """按场景屏障产生 Agent 事件并提交测试输入。

                Args:
                    message (对象): 真人文本或类型声明对应的内部通知。
                    images (对象): 真人附件的图片 data URL 列表。
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    inbox_input (对象): 随本次 checkpoint 原子交付的单条读取快照。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。
                    on_input_committed (Callable | None): 真人输入实际提交后的观察回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                yield AgentEvent(EventType.TEXT_CHUNK, "已保存的说明")
                yield AgentEvent(EventType.TOOL_CALL, "example")
                tool_started.set()
                await release.wait()
                yield AgentEvent(EventType.TEXT_CHUNK, "工具后的新文本")
                await finish_test_turn(db_path, (inbox_input,), message=message, origin=origin_channel, release_queued=turn_control.reason is None)
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        scheduler = AgentRuntimeScheduler(
           agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SessionInbox(session, str(tmp_path / "state.sqlite3")),
            state_manager_factory=lambda session: StateManager(session, str(tmp_path / "state.sqlite3")),
        )
        await scheduler.start()
        try:
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "开始"}))
            await asyncio.wait_for(tool_started.wait(), 1)
            assert "partial" not in (await scheduler.snapshot("s")).payload
            release.set()
            for _ in range(100):
                if ((await scheduler.snapshot("s")).payload)["phase"] == "idle":
                    break
                await asyncio.sleep(0.01)
        finally:
            release.set()
            await scheduler.close()

    asyncio.run(scenario())


def test_stop_while_publishing_chunk_still_saves_interrupted_response(tmp_path) -> None:
    """验证发布分片期间停止仍能保存打断回复。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：发布分片期间停止仍能保存打断回复。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        publishing = asyncio.Event()
        release_publish = asyncio.Event()
        completed = asyncio.Event()
        db_path = str(tmp_path / "state.sqlite3")

        class WaitingLLM:
            """提供隔离测试替身，用于验证：发布分片期间停止仍能保存打断回复。"""
            async def astream(self, messages, tools=None):
                """按场景产生模型分片或工具调用。

                Args:
                    messages (消息序列): 需要转换或交付的领域消息集合。
                    tools (list | None): 当前模型可用的工具 schema。

                Yields:
                    LLMChunk: 当前测试场景的预设分片或事件。
                """
                messages = capture_input(messages)
                yield StreamChunk(content="已经显示")
                await asyncio.Event().wait()

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        class PausedAgent(Agent):
            """在公开 Agent 输出处暂停，独立于渠道分发构造停止安全点。"""

            async def run(self, *args, **kwargs):
                """转发真实执行，在测试指定事件之后等待停止请求。

                Args:
                    *args (tuple): 真人输入的位置参数。
                    **kwargs (dict): 当前轮次控制与输入交付参数。

                Yields:
                    AgentEvent: 原 Agent 产生的事件。
                """
                async for event in super().run(*args, **kwargs):
                    yield event
                    if event.type == EventType.TEXT_CHUNK:
                        publishing.set()
                        await release_publish.wait()
                    if event.type == EventType.DONE:
                        completed.set()

        def make_agent(session: str) -> Agent:
            """装配隔离存储和模型替身。

            Args:
                session (str): 测试会话标识。

            Returns:
                Agent: 当前用例创建并装配测试工具的 Agent。
            """
            return PausedAgent(
                AgentConfig(
                    session_id=session, model_name="test", api_key="test",
                    context_strategy=ContextStrategyConfig(
                        compression_trigger_tokens_k=100,
                    ),
                ),
                db_path=db_path, llm_client=WaitingLLM(),
            )

        scheduler = AgentRuntimeScheduler(
           agent_factory=make_agent,
            inbox_factory=lambda session: SessionInbox(session, db_path),
            state_manager_factory=lambda session: StateManager(session, db_path),
        )
        await scheduler.start()
        try:
            started = await scheduler.submit(ChannelCommand(
                "s", "desktop", "human_turn", {"message": "说句话"},
            ))
            await asyncio.wait_for(publishing.wait(), 2)
            stopped = await scheduler.submit(ChannelCommand(
                "s", "desktop", "stop_turn", target_run_id=(await scheduler.snapshot("s")).payload["run_id"],
            ))
            assert stopped.accepted
            release_publish.set()
            await asyncio.wait_for(completed.wait(), 2)
            manager = StateManager("s", db_path)
            try:
                state = await manager.load()
                assert state.messages[-1].text == "已经显示……（被用户打断）"
            finally:
                await manager.close()
        finally:
            release_publish.set()
            await scheduler.close()

    asyncio.run(scenario())


def test_stop_after_tool_notice_prevents_unstarted_tool_side_effect(tmp_path) -> None:
    """验证工具提示后受理停止可阻止尚未开始的副作用。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：工具提示后受理停止可阻止尚未开始的副作用。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        tool_notice = asyncio.Event()
        release_notice = asyncio.Event()
        completed = asyncio.Event()
        db_path = str(tmp_path / "state.sqlite3")
        executed: list[str] = []

        class RecordingTool(BaseTool):
            """提供隔离测试替身，用于验证：工具提示后受理停止可阻止尚未开始的副作用。"""
            name = "recording_tool"
            description = "记录是否执行"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args: dict, context: ToolContext) -> ToolResult:
                """记录测试副作用并返回工具结果。

                Args:
                    args (dict): 测试工具参数或转发的位置参数。
                    context (ToolContext): 后端注入的工具上下文。

                Returns:
                    ToolResult: 供本用例检查的工具执行结果。
                """
                executed.append("executed")
                return ToolResult.success("完成")

        class ToolLLM:
            """提供隔离测试替身，用于验证：工具提示后受理停止可阻止尚未开始的副作用。"""
            async def astream(self, messages, tools=None):
                """按场景产生模型分片或工具调用。

                Args:
                    messages (消息序列): 需要转换或交付的领域消息集合。
                    tools (list | None): 当前模型可用的工具 schema。

                Yields:
                    LLMChunk: 当前测试场景的预设分片或事件。
                """
                messages = capture_input(messages)
                yield StreamChunk(tool_call=ToolCall(id="call-1", name="recording_tool", args={}))
                yield StreamChunk(finish_reason="tool_calls")

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        class PausedAgent(Agent):
            """在公开 Agent 输出处暂停，独立于渠道分发构造停止安全点。"""

            async def run(self, *args, **kwargs):
                """转发真实执行，在测试指定事件之后等待停止请求。

                Args:
                    *args (tuple): 真人输入的位置参数。
                    **kwargs (dict): 当前轮次控制与输入交付参数。

                Yields:
                    AgentEvent: 原 Agent 产生的事件。
                """
                async for event in super().run(*args, **kwargs):
                    yield event
                    if event.type == EventType.TOOL_CALL:
                        tool_notice.set()
                        await release_notice.wait()
                    if event.type == EventType.DONE:
                        completed.set()

        def make_agent(session: str) -> Agent:
            """装配隔离存储和模型替身。

            Args:
                session (str): 测试会话标识。

            Returns:
                Agent: 当前用例创建并装配测试工具的 Agent。
            """
            agent = PausedAgent(
                AgentConfig(
                    session_id=session, model_name="test", api_key="test",
                    context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
                ),
                db_path=db_path, llm_client=ToolLLM(),
            )
            agent.tool_manager.register(RecordingTool())
            return agent

        scheduler = AgentRuntimeScheduler(
           agent_factory=make_agent,
            inbox_factory=lambda session: SessionInbox(session, db_path),
            state_manager_factory=lambda session: StateManager(session, db_path),
        )
        await scheduler.start()
        try:
            started = await scheduler.submit(ChannelCommand(
                "s", "desktop", "human_turn", {"message": "执行工具"},
            ))
            await asyncio.wait_for(tool_notice.wait(), 2)
            assert (await scheduler.submit(ChannelCommand(
                "s", "desktop", "stop_turn", target_run_id=(await scheduler.snapshot("s")).payload["run_id"],
            ))).accepted
            release_notice.set()
            await asyncio.wait_for(completed.wait(), 2)
            assert executed == []
            manager = StateManager("s", db_path)
            try:
                state = await manager.load()
                assert any(isinstance(item, ToolMessage) for item in state.messages)
            finally:
                await manager.close()
        finally:
            release_notice.set()
            await scheduler.close()

    asyncio.run(scenario())


def test_new_message_from_other_channel_steers_active_run(tmp_path) -> None:
    """验证其他渠道的新输入能引导共享会话的活动轮次。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：其他渠道的新输入能引导共享会话的活动轮次。"""
        db_path = str(tmp_path / "state.sqlite3")
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        first_running = asyncio.Event()
        finished = asyncio.Event()
        calls: list[tuple[str, str]] = []

        class FakeAgent:
            """提供隔离测试替身，用于验证：其他渠道的新输入能引导共享会话的活动轮次。"""
            async def run(self, message, images=None, *, origin_channel="desktop", inbox_input=None, turn_control=None, before_next_llm=None, on_input_committed=None):
                """按场景屏障产生 Agent 事件并提交测试输入。

                Args:
                    message (对象): 真人文本或类型声明对应的内部通知。
                    images (对象): 真人附件的图片 data URL 列表。
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    inbox_input (对象): 随本次 checkpoint 原子交付的单条读取快照。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。
                    on_input_committed (Callable | None): 真人输入实际提交后的观察回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                calls.append((message, origin_channel))
                if message == "first":
                    first_running.set()
                    while turn_control.reason is None:
                        await asyncio.sleep(0.01)
                else:
                    finished.set()
                await finish_test_turn(db_path, (inbox_input,), message=message, origin=origin_channel, release_queued=turn_control.reason is None)
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        subscriptions = ChannelSubscriptionService()
        subscriptions.register_channel("qq")
        subscriptions.set_default_enabled("qq", True)
        scheduler = AgentRuntimeScheduler(
            subscriptions=subscriptions, agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SessionInbox(session, str(tmp_path / "state.sqlite3")),
        )
        hub = InteractionEventHub(scheduler, subscriptions=subscriptions)
        await hub.start()
        await scheduler.start()
        try:
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "first"}))
            await asyncio.wait_for(first_running.wait(), 1)
            receipt = await scheduler.submit(ChannelCommand("s", "qq", "human_turn", {"message": "second"}))
            assert receipt.accepted
            await asyncio.wait_for(finished.wait(), 1)
            assert calls == [("first", "desktop"), ("second", "qq")]
        finally:
            await hub.close()
            await scheduler.close()

    asyncio.run(scenario())


def test_queued_turn_receives_new_run_id(tmp_path) -> None:
    """验证排队输入放行后取得独立运行标识。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：排队输入放行后取得独立运行标识。"""
        db_path = str(tmp_path / "state.sqlite3")
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        release = asyncio.Event()
        second_done = asyncio.Event()
        ids: list[str] = []

        class FakeAgent:
            """提供隔离测试替身，用于验证：排队输入放行后取得独立运行标识。"""
            async def run(self, message, images=None, *, origin_channel="desktop", inbox_input=None, turn_control=None, before_next_llm=None, on_input_committed=None):
                """按场景屏障产生 Agent 事件并提交测试输入。

                Args:
                    message (对象): 真人文本或类型声明对应的内部通知。
                    images (对象): 真人附件的图片 data URL 列表。
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    inbox_input (对象): 随本次 checkpoint 原子交付的单条读取快照。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。
                    on_input_committed (Callable | None): 真人输入实际提交后的观察回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                if message == "first":
                    await release.wait()
                else:
                    second_done.set()
                await finish_test_turn(db_path, (inbox_input,), message=message, origin=origin_channel, release_queued=turn_control.reason is None)
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        subscriptions = ChannelSubscriptionService()
        scheduler = AgentRuntimeScheduler(
            subscriptions=subscriptions, agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SessionInbox(session, str(tmp_path / "state.sqlite3")),
        )
        hub = InteractionEventHub(scheduler, subscriptions=subscriptions)
        await hub.start()
        await scheduler.start()
        desktop = hub.subscribe("s", "desktop")
        await anext(desktop)
        try:
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "first"}))
            while True:
                event = await asyncio.wait_for(anext(desktop), 1)
                if event.kind == "run_started":
                    ids.append(event.run_id)
                    break
            receipt = await scheduler.submit(ChannelCommand("s", "desktop", "queue_turn", {"message": "second"}))
            assert receipt.accepted
            release.set()
            await asyncio.wait_for(second_done.wait(), 1)
            while len(ids) < 2:
                event = await asyncio.wait_for(anext(desktop), 1)
                if event.kind == "run_started":
                    ids.append(event.run_id)
            assert ids[0] != ids[1]
        finally:
            await desktop.aclose()
            await hub.close()
            await scheduler.close()

    asyncio.run(scenario())


def test_interrupted_turn_applies_steer_before_older_queued_message(tmp_path) -> None:
    """验证打断后先处理引导，再放行较早的排队输入。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：打断后先处理引导，再放行较早的排队输入。"""
        db_path = str(tmp_path / "state.sqlite3")
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        started = asyncio.Event()
        all_done = asyncio.Event()
        calls: list[str] = []

        class FakeAgent:
            """提供隔离测试替身，用于验证：打断后先处理引导，再放行较早的排队输入。"""
            async def run(self, message, images=None, *, origin_channel="desktop", inbox_input=None, turn_control=None, before_next_llm=None, on_input_committed=None):
                """按场景屏障产生 Agent 事件并提交测试输入。

                Args:
                    message (对象): 真人文本或类型声明对应的内部通知。
                    images (对象): 真人附件的图片 data URL 列表。
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    inbox_input (对象): 随本次 checkpoint 原子交付的单条读取快照。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。
                    on_input_committed (Callable | None): 真人输入实际提交后的观察回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                calls.append(message)
                if message == "first":
                    started.set()
                    while turn_control.reason is None:
                        await asyncio.sleep(0.01)
                if len(calls) == 3:
                    all_done.set()
                await finish_test_turn(db_path, (inbox_input,), message=message, origin=origin_channel, release_queued=turn_control.reason is None)
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        scheduler = AgentRuntimeScheduler(
           agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SessionInbox(session, str(tmp_path / "state.sqlite3")),
        )
        await scheduler.start()
        try:
            first = await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "first"}))
            await asyncio.wait_for(started.wait(), 1)
            assert (await scheduler.submit(ChannelCommand(
                "s", "desktop", "queue_turn", {"message": "queued"},
            ))).accepted
            assert (await scheduler.submit(ChannelCommand(
                "s", "desktop", "human_steer", {"message": "steer"},
                target_run_id=(await scheduler.snapshot("s")).payload["run_id"],
            ))).accepted
            await asyncio.wait_for(all_done.wait(), 2)
            assert calls == ["first", "steer", "queued"]
        finally:
            await scheduler.close()

    asyncio.run(scenario())


def test_task_only_wakeup_does_not_release_queue_left_by_stopped_turn(tmp_path) -> None:
    """验证后台通知唤醒不放行停止轮次遗留的排队消息。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：后台通知唤醒不放行停止轮次遗留的排队消息。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        first_started = asyncio.Event()
        task_done = asyncio.Event()
        queue_done = asyncio.Event()
        calls: list[str] = []

        class FakeAgent:
            """提供隔离测试替身，用于验证：后台通知唤醒不放行停止轮次遗留的排队消息。"""
            async def run(self, message, images=None, *, origin_channel="desktop", inbox_input=None, turn_control=None, before_next_llm=None, on_input_committed=None):
                """按场景屏障产生 Agent 事件并提交测试输入。

                Args:
                    message (对象): 真人文本或类型声明对应的内部通知。
                    images (对象): 真人附件的图片 data URL 列表。
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    inbox_input (对象): 随本次 checkpoint 原子交付的单条读取快照。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。
                    on_input_committed (Callable | None): 真人输入实际提交后的观察回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                calls.append(message)
                if message == "旧轮次":
                    first_started.set()
                    while turn_control.reason is None:
                        await asyncio.sleep(0.01)
                if message == "排队消息":
                    queue_done.set()
                await finish_test_turn(db_path, (inbox_input,), message=message, origin=origin_channel, release_queued=turn_control.reason is None)
                yield AgentEvent(EventType.DONE, None)

            async def run_runtime_inputs(self, messages, *, inbox_inputs, turn_control=None, before_next_llm=None):
                """验证后台通知批次并产生测试回复。

                Args:
                    messages (消息序列): 需要转换或交付的领域消息集合。
                    inbox_inputs (对象): 随本次 checkpoint 原子交付的读取快照集合。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                task_done.set()
                await finish_test_turn(db_path, inbox_inputs, runtime_inputs=messages)
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        scheduler = AgentRuntimeScheduler(
           agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SessionInbox(session, db_path),
            state_manager_factory=lambda session: StateManager(session, db_path),
        )
        await scheduler.start()
        records = TaskRecordDao(db_path)
        try:
            first = await scheduler.submit(ChannelCommand(
                "s", "desktop", "human_turn", {"message": "旧轮次"},
            ))
            await asyncio.wait_for(first_started.wait(), 1)
            assert (await scheduler.submit(ChannelCommand(
                "s", "desktop", "queue_turn", {"message": "排队消息"},
            ))).accepted
            assert (await scheduler.submit(ChannelCommand(
                "s", "desktop", "stop_turn", target_run_id=(await scheduler.snapshot("s")).payload["run_id"],
            ))).accepted
            for _ in range(100):
                if ((await scheduler.snapshot("s")).payload)["phase"] == "idle":
                    break
                await asyncio.sleep(0.01)
            await records.insert_running("task-1", "s", "后台任务", 1, str(tmp_path / "out"))
            assert await records.finish_once("task-1", "completed", 0)
            await scheduler.notify_task_terminal("s")
            await asyncio.wait_for(task_done.wait(), 1)
            assert not queue_done.is_set()
            snapshot = (await scheduler.snapshot("s")).payload
            assert snapshot["queued_inputs"][0]["message"] == "排队消息"
            assert (await scheduler.submit(ChannelCommand(
                "s", "desktop", "human_turn", {"message": "新轮次"},
            ))).accepted
            await asyncio.wait_for(queue_done.wait(), 1)
            assert calls == ["旧轮次", "新轮次", "排队消息"]
        finally:
            await records.close()
            await scheduler.close()

    asyncio.run(scenario())


def test_after_restart_abandoned_queue_is_discarded(tmp_path) -> None:
    """验证异常重启后排队输入作废，不能由新轮次重新放行。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：异常重启后排队输入作废，不能由新轮次重新放行。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        before_restart = SessionInbox("s", db_path)
        try:
            await before_restart.submit_human("desktop", "崩溃前排队", queued=True)
        finally:
            await before_restart.close()

        calls: list[str] = []
        queue_done = asyncio.Event()

        class FakeAgent:
            """提供隔离测试替身，用于验证：异常重启后排队输入作废，不能由新轮次重新放行。"""
            async def run(self, message, images=None, *, origin_channel="desktop", inbox_input=None, turn_control=None, before_next_llm=None, on_input_committed=None):
                """按场景屏障产生 Agent 事件并提交测试输入。

                Args:
                    message (对象): 真人文本或类型声明对应的内部通知。
                    images (对象): 真人附件的图片 data URL 列表。
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    inbox_input (对象): 随本次 checkpoint 原子交付的单条读取快照。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。
                    on_input_committed (Callable | None): 真人输入实际提交后的观察回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                calls.append(message)
                if message == "崩溃前排队":
                    queue_done.set()
                await finish_test_turn(db_path, (inbox_input,), message=message, origin=origin_channel, release_queued=turn_control.reason is None)
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        scheduler = AgentRuntimeScheduler(
           agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SessionInbox(session, db_path),
            state_manager_factory=lambda session: StateManager(session, db_path),
        )
        await scheduler.start()
        try:
            snapshot = (await scheduler.snapshot("s")).payload
            assert snapshot["queued_inputs"] == []
            assert calls == []
            assert (await scheduler.submit(ChannelCommand(
                "s", "desktop", "human_turn", {"message": "新的正常轮次"},
            ))).accepted
            for _ in range(100):
                if calls:
                    break
                await asyncio.sleep(0.01)
            assert calls == ["新的正常轮次"]
            assert not queue_done.is_set()
        finally:
            await scheduler.close()

    asyncio.run(scenario())


def test_stale_steer_cannot_cancel_current_run(tmp_path) -> None:
    """验证过期引导不能停止当前不同轮次。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：过期引导不能停止当前不同轮次。"""
        db_path = str(tmp_path / "state.sqlite3")
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        started = asyncio.Event()
        release = asyncio.Event()
        controls = []
        messages = []
        second_done = asyncio.Event()

        class FakeAgent:
            """提供隔离测试替身，用于验证：过期引导不能停止当前不同轮次。"""
            async def run(self, message, images=None, *, origin_channel="desktop", inbox_input=None, turn_control=None, before_next_llm=None, on_input_committed=None):
                """按场景屏障产生 Agent 事件并提交测试输入。

                Args:
                    message (对象): 真人文本或类型声明对应的内部通知。
                    images (对象): 真人附件的图片 data URL 列表。
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    inbox_input (对象): 随本次 checkpoint 原子交付的单条读取快照。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。
                    on_input_committed (Callable | None): 真人输入实际提交后的观察回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                controls.append(turn_control)
                messages.append(message)
                started.set()
                if message == "当前任务":
                    await release.wait()
                else:
                    second_done.set()
                await finish_test_turn(db_path, (inbox_input,), message=message, origin=origin_channel, release_queued=turn_control.reason is None)
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        scheduler = AgentRuntimeScheduler(
           agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SessionInbox(session, str(tmp_path / "state.sqlite3")),
        )
        await scheduler.start()
        try:
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "当前任务"}))
            await asyncio.wait_for(started.wait(), 1)
            accepted = await scheduler.submit(ChannelCommand(
                "s", "desktop", "human_steer", {"message": "迟到引导"},
                target_run_id="旧轮次",
            ))
            assert accepted.accepted
            assert controls[0].reason is None
            assert ((await scheduler.snapshot("s")).payload)["queued_inputs"][0]["message"] == "迟到引导"
            release.set()
            await asyncio.wait_for(second_done.wait(), 1)
            assert messages == ["当前任务", "迟到引导"]
        finally:
            release.set()
            await scheduler.close()

    asyncio.run(scenario())


def test_run_completion_waits_for_steer_inbox_commit(tmp_path) -> None:
    """验证轮次结束与引导持久受理的竞争不会丢失输入。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：轮次结束与引导持久受理的竞争不会丢失输入。"""
        db_path = str(tmp_path / "state.sqlite3")
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        started = asyncio.Event()
        finish_first = asyncio.Event()
        writing_steer = asyncio.Event()
        finish_write = asyncio.Event()
        second_done = asyncio.Event()
        controls = []

        class SlowInbox(SessionInbox):
            """提供隔离测试替身，用于验证：轮次结束与引导持久受理的竞争不会丢失输入。"""
            async def submit_human(self, origin_channel, message, **kwargs):
                """延迟持久受理以验证并发时序。

                Args:
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    message (对象): 真人文本或类型声明对应的内部通知。
                    kwargs (dict): 转发给被替换调用的关键字参数。
                """
                if message == "新指令":
                    writing_steer.set()
                    await finish_write.wait()
                return await super().submit_human(origin_channel, message, **kwargs)

        class FakeAgent:
            """提供隔离测试替身，用于验证：轮次结束与引导持久受理的竞争不会丢失输入。"""
            async def run(self, message, images=None, *, origin_channel="desktop", inbox_input=None, turn_control=None, before_next_llm=None, on_input_committed=None):
                """按场景屏障产生 Agent 事件并提交测试输入。

                Args:
                    message (对象): 真人文本或类型声明对应的内部通知。
                    images (对象): 真人附件的图片 data URL 列表。
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    inbox_input (对象): 随本次 checkpoint 原子交付的单条读取快照。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。
                    on_input_committed (Callable | None): 真人输入实际提交后的观察回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                controls.append(turn_control)
                if message == "旧指令":
                    started.set()
                    await finish_first.wait()
                else:
                    second_done.set()
                await finish_test_turn(db_path, (inbox_input,), message=message, origin=origin_channel, release_queued=turn_control.reason is None)
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        scheduler = AgentRuntimeScheduler(
           agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SlowInbox(session, str(tmp_path / "state.sqlite3")),
        )
        await scheduler.start()
        try:
            first = await scheduler.submit(ChannelCommand(
                "s", "desktop", "human_turn", {"message": "旧指令"},
            ))
            await asyncio.wait_for(started.wait(), 1)
            steer = asyncio.create_task(scheduler.submit(ChannelCommand(
                "s", "desktop", "human_steer", {"message": "新指令"},
                target_run_id=(await scheduler.snapshot("s")).payload["run_id"],
            )))
            await asyncio.wait_for(writing_steer.wait(), 1)
            finish_first.set()
            await asyncio.sleep(0)
            finish_write.set()
            assert (await asyncio.wait_for(steer, 1)).accepted
            assert controls[0].reason == "steer"
            await asyncio.wait_for(second_done.wait(), 1)
        finally:
            finish_write.set()
            finish_first.set()
            await scheduler.close()

    asyncio.run(scenario())


def test_failed_input_commit_never_publishes_human_message(tmp_path) -> None:
    """验证输入 checkpoint 失败时不会发布真人消息。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：输入 checkpoint 失败时不会发布真人消息。"""
        db_path = str(tmp_path / "state.sqlite3")
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        class FakeAgent:
            """提供隔离测试替身，用于验证：输入 checkpoint 失败时不会发布真人消息。"""
            async def run(self, message, images=None, *, origin_channel="desktop", inbox_input=None, turn_control=None, before_next_llm=None, on_input_committed=None):
                """按场景屏障产生 Agent 事件并提交测试输入。

                Args:
                    message (对象): 真人文本或类型声明对应的内部通知。
                    images (对象): 真人附件的图片 data URL 列表。
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    inbox_input (对象): 随本次 checkpoint 原子交付的单条读取快照。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。
                    on_input_committed (Callable | None): 真人输入实际提交后的观察回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                raise RuntimeError("checkpoint 写入失败")
                await finish_test_turn(db_path, (inbox_input,), message=message, origin=origin_channel, release_queued=turn_control.reason is None)
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        subscriptions = ChannelSubscriptionService()
        scheduler = AgentRuntimeScheduler(
            subscriptions=subscriptions, agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SessionInbox(session, str(tmp_path / "state.sqlite3")),
        )
        hub = InteractionEventHub(scheduler, subscriptions=subscriptions)
        await hub.start()
        await scheduler.start()
        stream = hub.subscribe("s", "desktop")
        await anext(stream)
        try:
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "不可幽灵显示"}))
            events = [(await asyncio.wait_for(anext(stream), 1)).kind for _ in range(2)]
            assert events == ["run_started", "error"]
        finally:
            await stream.aclose()
            await hub.close()
            await scheduler.close()

    asyncio.run(scenario())


def test_queued_message_waits_during_approval_and_can_be_cancelled(tmp_path, monkeypatch) -> None:
    """验证审批期间排队消息保持等待且可撤销。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
        monkeypatch (pytest.MonkeyPatch): Pytest 依赖替换 fixture。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：审批期间排队消息保持等待且可撤销。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        state = AgentState.create_new("s")
        call = ToolCall(id="tool-1", name="some_tool", args={})
        state.record_assistant_step("", (call,))
        state.clear_pending_tool_calls()
        state.set_interrupt({
            "type": "authorization_request", "request_id": "approval-1",
            "message": "确认", "origin_channel": "qq",
            "tool_call_id": call.id, "tool_name": call.name, "tool_args": {},
        })
        manager = StateManager("s", db_path)
        await manager.save(state, checkpoint_type="intermediate")
        await manager.close()
        class ApprovalAgent:
            """保存真实审批，供公开排队命令使用。"""

            async def run(self, message, *, inbox_input, **kwargs):
                """交付触发审批的输入，保存后产生审批通知。

                Args:
                    message (str): 启动消息。
                    inbox_input (InboxEnvelope): 原输入读取。
                    **kwargs (dict): 其余本轮参数。

                Yields:
                    AgentEvent: 已保存的审批。
                """
                manager = StateManager("s", db_path)
                try:
                    await manager.save(state, checkpoint_type="intermediate", inbox_inputs=(inbox_input,))
                finally:
                    await manager.close()
                yield AgentEvent(EventType.INTERRUPT, state.interrupt_data)

            async def close(self):
                """测试对象无外部资源。"""

        subscriptions = ChannelSubscriptionService()
        subscriptions.register_channel("qq")
        subscriptions.set_default_enabled("qq", True)
        scheduler = AgentRuntimeScheduler(
            subscriptions=subscriptions, agent_factory=lambda _session: ApprovalAgent(),
            inbox_factory=lambda session: SessionInbox(session, db_path),
            state_manager_factory=lambda session: StateManager(session, db_path),
        )
        hub = InteractionEventHub(scheduler, subscriptions=subscriptions)
        await hub.start()
        await scheduler.start()
        stream = hub.subscribe("s", "qq")
        await anext(stream)
        await scheduler.submit(ChannelCommand("s", "qq", "human_turn", {"message": "启动审批"}))
        while (await asyncio.wait_for(anext(stream), 1)).kind != "interrupt":
            pass
        try:
            receipt = await scheduler.submit(ChannelCommand(
                "s", "qq", "queue_turn", {"message": "等这轮结束"},
            ))
            assert receipt.accepted
            snapshot = (await scheduler.snapshot("s")).payload
            assert snapshot["phase"] == "pending_interrupt"
            assert snapshot["queued_inputs"][0]["message"] == "等这轮结束"
            sequence = snapshot["queued_inputs"][0]["input_sequence"]
            manager = StateManager("s", db_path)
            try:
                assert (await manager.load()).interrupt_data["request_id"] == "approval-1"
            finally:
                await manager.close()
            cancelled = await scheduler.submit(ChannelCommand(
                "s", "qq", "cancel_queued", {"input_sequence": sequence},
            ))
            assert cancelled.accepted
            assert ((await scheduler.snapshot("s")).payload)["queued_inputs"] == []
        finally:
            await stream.aclose()
            await hub.close()
            await scheduler.close()

    asyncio.run(scenario())


def test_authorization_is_first_wins_across_channels(tmp_path) -> None:
    """验证多端审批只接受第一个有效决定。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：多端审批只接受第一个有效决定。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "runtime.sqlite3")
        decisions: list[dict] = []

        class FakeAgent:
            """提供隔离测试替身，用于验证：多端审批只接受第一个有效决定。"""
            async def run(self, message, images=None, *, origin_channel="desktop", inbox_input=None, turn_control=None, before_next_llm=None, on_input_committed=None):
                """按场景屏障产生 Agent 事件并提交测试输入。

                Args:
                    message (对象): 真人文本或类型声明对应的内部通知。
                    images (对象): 真人附件的图片 data URL 列表。
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    inbox_input (对象): 随本次 checkpoint 原子交付的单条读取快照。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。
                    on_input_committed (Callable | None): 真人输入实际提交后的观察回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                manager = StateManager("s", db_path)
                try:
                    state = AgentState.create_new("s")
                    human_message = state.add_user_message(message, origin_channel=origin_channel)
                    call = ToolCall(id="tool-1", name="some_tool", args={})
                    state.record_assistant_step("", (call,), origin_channel=origin_channel)
                    state.clear_pending_tool_calls()
                    state.set_interrupt({
                        "type": "authorization_request", "request_id": "approval-1",
                        "message": "批准?", "tool_call_id": call.id,
                        "tool_name": call.name, "tool_args": {},
                        "origin_channel": origin_channel,
                    })
                    await manager.save(state, checkpoint_type="intermediate", inbox_inputs=(inbox_input,))
                    # 1. 替身遵循真实 Agent：只有事务成功后才发布输入事实。
                    if on_input_committed is not None:
                        await on_input_committed(human_message)
                finally:
                    await manager.close()
                yield AgentEvent(EventType.INTERRUPT, {
                    "type": "authorization_request", "request_id": "approval-1", "message": "批准?",
                })

            async def resume(self, decision, *, initial_state=None, turn_control=None, before_next_llm=None):
                """记录恢复决定并产生测试事件。

                Args:
                    decision (dict): 审批决定或恢复执行结果。
                    initial_state (AgentState | None): Scheduler 重读的持久审批状态。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                decisions.append(decision)
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        subscriptions = ChannelSubscriptionService()
        subscriptions.register_channel("qq")
        subscriptions.set_default_enabled("qq", True)
        scheduler = AgentRuntimeScheduler(
            subscriptions=subscriptions, agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SessionInbox(session, db_path),
            state_manager_factory=lambda session: StateManager(session, db_path),
        )
        hub = InteractionEventHub(scheduler, subscriptions=subscriptions)
        await hub.start()
        await scheduler.start()
        desktop = hub.subscribe("s", "desktop")
        qq = hub.subscribe("s", "qq")
        await anext(desktop)
        await anext(qq)
        await scheduler.submit(ChannelCommand("s", "qq", "human_turn", {"message": "请操作"}))
        while (await asyncio.wait_for(anext(desktop), 1)).kind != "interrupt":
            pass
        first = await scheduler.submit(ChannelCommand(
            "s", "desktop", "resolve_authorization", {"kind": "approve"},
            request_id="approval-1",
        ))
        second = await scheduler.submit(ChannelCommand(
            "s", "qq", "resolve_authorization", {"kind": "reject"},
            request_id="approval-1",
        ))
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert first.accepted and not second.accepted
        while (await asyncio.wait_for(anext(qq), 1)).kind != "authorization_resolved":
            pass
        after_approval = []
        while True:
            event = await asyncio.wait_for(anext(qq), 1)
            after_approval.append(event.kind)
            if event.kind == "run_completed":
                break
        assert "run_started" in after_approval
        assert decisions == [{"kind": "approve"}]
        await desktop.aclose()
        await qq.aclose()
        await hub.close()
        await scheduler.close()

    asyncio.run(scenario())
