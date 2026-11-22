from tests.memory_support import memory_count, set_memory_count
"""通过真实 Agent、调度器及任务验证插件组合行为，只替代外部工作。"""
from tests.runtime_support import runtime_hub, close_test_runtime
from app.routes.agent import observe_runtime
from tests.runtime_support import next_runtime_event
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory

from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator

import asyncio

import pytest

from app.agent.agent import Agent, AgentConfig
from tests.runtime_support import configured_settings
from app.agent.context import ContextStrategyConfig
from app.agent.models.llm_client import StreamChunk
from app.agent.plugins.runtime import PluginRuntime
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import ToolCall
from app.agent.runtime.tasks.base import BaseTask
from app.agent.tools.core.base import BaseTool, InterruptEvent, ToolContext, ToolResult
from app.interaction.interaction_contract import InteractionCommand
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.schemas.runtime_types import InterruptKind, TaskStatus


async def next_event_kind(scheduler: AgentRuntimeScheduler, kind: str):
    """从公共输出中读取指定类型的事件。

    Args:
        scheduler (AgentRuntimeScheduler): 本场景的真实调度器。
        kind (str): 要观察的事件类型。

    Returns:
        InteractionEvent: 最先到达的匹配事件。
    """
    while True:
        event = await asyncio.wait_for(next_runtime_event(scheduler), 3)
        if event.kind == kind:
            return event


@pytest.mark.parametrize("action", ["stop", "steer"])
def test_queued_plugin_control_targets_execution_at_acceptance(tmp_path, action):
    """A 轮取得的插件入口在 B 轮排队，到 C 轮受理时控制 C 轮。

    Args:
        tmp_path (Path): 本场景独占的数据库目录。
        action (str): 对受理时执行发出的停止或引导请求。
    """
    from app.agent.runtime.requests import RuntimeRequest
    from app.schemas.task_types import PluginRequest, PluginRequestKind
    from tests.model_capture import capture_input

    async def scenario():
        """在共享队列取出插件控制后等待，期间让真人输入替换当前轮次。"""
        blocked, accept_control, release_model = (asyncio.Event() for _ in range(3))
        started = asyncio.Queue()
        requests = []
        target_kind = PluginRequestKind.STOP_TURN if action == "stop" else PluginRequestKind.STEER_TURN

        class DelayedControlQueue(asyncio.Queue[RuntimeRequest]):
            """保留正式队列行为，在插件控制受理前提供确定的等待屏障。"""

            async def get(self):
                """取得请求后等待测试放行，让当前会话能够先处理真人输入。

                Returns:
                    RuntimeRequest: 等待结束后交给消费者的原始请求。
                """
                item = await super().get()
                if isinstance(item, PluginRequest) and item.kind == target_kind:
                    blocked.set()
                    await accept_control.wait()
                return item

        class Model:
            """保持每轮模型调用等待，供真实会话执行处理引导或停止。"""

            async def astream(self, messages, tools=None):
                """记录每轮实际收到的正文，并报告调用已经开始。

                Args:
                    messages (Sequence[InputMessage]): 当前轮次的模型输入。
                    tools (Sequence[ToolDefinition] | None): 当前可用工具。

                Yields:
                    StreamChunk: 放行后生成的最终回答。
                """
                requests.append(capture_input(messages))
                started.put_nowait(len(requests))
                await release_model.wait()
                yield StreamChunk(content="完成")

            async def close(self):
                """模型替身的等待由所属执行结束，无需释放外部连接。"""

        queue = DelayedControlQueue()
        factory = SqliteFactory(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(factory))
        scheduler = build_test_scheduler(runtime_queue=queue, task_supervisor=supervisor,
            agent_factory=lambda session, manager: Agent(AgentConfig(session_id=session, model_name="test", api_key="test",
                context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)),
                llm_client=Model(), session_state_manager=manager, sqlite_factory=factory),
            session_state_manager_factory=lambda session: SessionStateManager(session, factory),
            chat_settings_dao=configured_settings())
        try:
            await scheduler.start()
            await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "A 轮"}))
            assert await asyncio.wait_for(started.get(), 2) == 1
            runtime = PluginRuntime("game", "s", _supervisor=supervisor, _runtime_queue=queue)
            await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "B 轮"}))
            assert await asyncio.wait_for(started.get(), 2) == 2
            if action == "stop":
                await runtime.stop_turn()
            else:
                await runtime.steer_turn("优先躲避攻击")
            await asyncio.wait_for(blocked.wait(), 2)
            await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "C 轮"}))
            assert await asyncio.wait_for(started.get(), 2) == 3
            accept_control.set()
            runs = [await next_event_kind(scheduler, "run_started") for _ in range(3)]
            current = runs[-1].run_id
            if action == "steer":
                assert await asyncio.wait_for(started.get(), 2) == 4
                assert (await scheduler.snapshot("s")).payload["run_id"] != current
                notifications = [item for item in requests[3] if item.get("name") == "runtime_plugin_notification"]
                assert len(notifications) == 1 and "优先躲避攻击" in notifications[0]["content"]
            else:
                while True:
                    event = await next_event_kind(scheduler, "run_completed")
                    if event.run_id == current:
                        break
                snapshot = (await scheduler.snapshot("s")).payload
                assert snapshot["phase"] == "idle"
                assert snapshot["session_state"] == "enabled"
                assert len(requests) == 3
        finally:
            accept_control.set()
            release_model.set()
            await close_test_runtime(scheduler)
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["stop_turn", "steer_turn"])
def test_plugin_control_respects_idle_and_final_save_policy(tmp_path, kind):
    """插件控制沿用空闲与最终保存规则，已保存的引导按会话资格留存。

    Args:
        tmp_path (Path): 本场景独占的数据库目录。
        kind (str): 插件停止或引导请求的种类。
    """
    from app.agent.runtime.agent_execution import AgentExecution
    from app.agent.runtime.parent_session import ParentSession
    from app.schemas.runtime_types import CommandStatus, ExecutionPhase
    from app.schemas.task_types import PluginRequest, PluginRequestKind

    async def scenario():
        """在真实最终保存入口等待，直接核对会话受理回执与执行阶段。"""
        saving, release_save, saved = (asyncio.Event() for _ in range(3))

        class WaitingSaveManager(SessionStateManager):
            """使用真实持久化，在最终保存开始后等待测试确认控制行为。"""

            async def complete_turn(self, state, *, release_queued=False):
                """报告最终提交已开始，放行后保存完整回答。

                Args:
                    state (AgentState): 当前回答的实际会话状态。
                    release_queued (bool): 正常结束时是否放行排队输入。

                Returns:
                    int: 实际保存的 checkpoint 编号。
                """
                saving.set()
                await release_save.wait()
                result = await super().complete_turn(state, release_queued=release_queued)
                saved.set()
                return result

        class Model:
            """立即完成回答，使执行进入最终提交阶段。"""

            async def astream(self, messages, tools=None):
                """提供正常回答，持久化继续由真实 Agent 完成。

                Args:
                    messages (Sequence[InputMessage]): 当前请求消息。
                    tools (Sequence[ToolDefinition] | None): 当前可用工具。

                Yields:
                    StreamChunk: 最终回答正文。
                """
                yield StreamChunk(content="已完成回答")

            async def close(self):
                """模型替身没有外部资源。"""

        factory = SqliteFactory(tmp_path / "state.sqlite3")
        manager = WaitingSaveManager("s", factory)
        events = asyncio.Queue()
        execution = AgentExecution("s", manager=manager, events=events,
            agent_factory=lambda state_manager: Agent(AgentConfig(session_id="s", model_name="test", api_key="test",
                context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)),
                llm_client=Model(), session_state_manager=state_manager, sqlite_factory=factory))
        session = ParentSession("s", manager=manager, execution=execution, events=events)
        request_kind = PluginRequestKind(kind)
        try:
            idle = await session.accept_plugin(PluginRequest(request_kind, "game", "s", text="空闲时的引导"))
            assert idle.status == (CommandStatus.STALE if request_kind == PluginRequestKind.STOP_TURN else CommandStatus.ACCEPTED)
            assert session.snapshot()["session_state"] == "paused"
            assert execution.snapshot().phase == ExecutionPhase.IDLE
            pending = await manager.read_activation_batch()
            assert len(pending) == (0 if request_kind == PluginRequestKind.STOP_TURN else 1)
            await session.accept_human(InteractionCommand("s", "human_turn", {"message": "开始回答"}))
            await asyncio.wait_for(saving.wait(), 2)
            before = execution.snapshot()
            assert before.phase == ExecutionPhase.COMMITTING
            receipt = await session.accept_plugin(PluginRequest(request_kind, "game", "s", text="提交时的新引导"))
            assert receipt.status == (CommandStatus.TOO_LATE if request_kind == PluginRequestKind.STOP_TURN else CommandStatus.ACCEPTED)
            assert execution.snapshot() == before
            assert session.snapshot()["session_state"] == "enabled"
            pending = await manager.read_activation_batch()
            assert len(pending) == (0 if request_kind == PluginRequestKind.STOP_TURN else 1)
            if pending:
                assert pending[0].payload["text"] == "提交时的新引导"
            release_save.set()
            await asyncio.wait_for(saved.wait(), 2)
        finally:
            release_save.set()
            await session.close("测试结束")
    asyncio.run(scenario())


@pytest.mark.parametrize("decision", ["human", "steer", "approve", "stop"])
@pytest.mark.parametrize("activate_session", [False, True])
def test_background_task_survives_approval_and_turn_end_until_explicit_stop(tmp_path, decision, activate_session):
    """审批、停止和引导均保留后台任务，显式停止时才执行任务清理。

    Args:
        tmp_path (Path): 本场景独占的任务和会话数据库目录。
        decision (str): 审批批准、普通引导、审批引导或停止当前回答。
        activate_session (bool): 审批等待期间的新通知是否显式请求激活。
    """
    async def scenario():
        """通过真实审批和执行流程，确认后台任务独立运行并可显式清理。"""
        path = str(tmp_path / "state.sqlite3")
        queue = asyncio.Queue()
        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(path)))
        model_waiting, release_model = asyncio.Event(), asyncio.Event()
        task_started, release_task = asyncio.Event(), asyncio.Event()
        calls = []
        task_cleaned, model_closed = asyncio.Event(), asyncio.Event()
        task_id = None

        class Model:
            """先请求外部工具，后续模型请求保持等待。"""
            async def astream(self, messages, tools=None):
                """返回一次工具调用，恢复或替代后等待测试放行。

                Args:
                    messages (Sequence[InputMessage]): 真实 Agent 组装的请求。
                    tools (Sequence[ToolDefinition] | None): 可用工具声明。

                Yields:
                    StreamChunk: 工具调用或最终回答。
                """
                calls.append(messages)
                if len(calls) == 1:
                    yield StreamChunk(tool_call=ToolCall("connect", "game_connection", {}))
                else:
                    model_waiting.set()
                    await release_model.wait()
                    yield StreamChunk(content="继续处理完成")

            async def close(self):
                """测试模型没有外部连接。"""
                if task_id is not None and len(calls) > 1:
                    model_closed.set()

        class GameConnectionTool(BaseTool):
            """替代外部游戏接入，按现有工具协议要求一次审批。"""
            name = "game_connection"
            description = "连接游戏"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args: dict, context: ToolContext) -> ToolResult:
                """首次等待审批，批准后完成游戏连接。

                Args:
                    args (dict): 当前工具参数。
                    context (ToolContext): 真实执行管道提供的恢复决定。

                Returns:
                    ToolResult: 待审批材料或已批准的连接结果。
                """
                if context.resume_data is None:
                    return ToolResult(content="", interrupt=InterruptEvent.create(
                        InterruptKind.AUTHORIZATION_REQUEST, "允许连接游戏？"))
                return ToolResult.success("游戏已连接")

        class GameWatchTask(BaseTask):
            """保持游戏观察，供审批及回答结束后检查后台执行。"""
            async def run(self, runtime):
                """等待游戏连接事件。

                Args:
                    runtime (PluginRuntime): 当前观察任务的调用对象。
                """
                task_started.set()
                await release_task.wait()

            async def cleanup(self):
                """记录显式停止后的资源释放。"""
                task_cleaned.set()

        def create_agent(session, manager):
            """创建持有真实执行管道的 Agent，仅替代模型及游戏工具。

            Args:
                session (str): 当前会话编号。
                manager (SessionStateManager): worker 提供的状态操作入口。

            Returns:
                Agent: 当前执行或审批恢复使用的 Agent。
            """
            agent = Agent(AgentConfig(session_id=session, model_name="test", api_key="test",
                context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)),
                llm_client=Model(), session_state_manager=manager, sqlite_factory=SqliteFactory())
            agent.register_tool(GameConnectionTool())
            return agent

        scheduler = build_test_scheduler(runtime_queue=queue, task_supervisor=supervisor,
            agent_factory=create_agent,
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)), chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        manager = SessionStateManager("s", SqliteFactory(path))
        try:
            await scheduler.start()
            await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "连接游戏"}))
            pending = await next_event_kind(scheduler, "interrupt")
            runtime = PluginRuntime("game", "s", _supervisor=supervisor, _runtime_queue=queue)
            task_id = await runtime.submit_task(GameWatchTask(), name="watch", title="游戏观察", notify_agent=False)
            await asyncio.wait_for(task_started.wait(), 1)
            await runtime.notify(name="battle_started", text="审批期间发现敌人", activate_session=activate_session)
            await asyncio.wait_for(queue.join(), 1)
            assert (await runtime.get_task(task_id)).status == TaskStatus.RUNNING
            assert (await scheduler.snapshot("s")).payload["phase"] == "pending_interrupt"
            assert len(calls) == 1
            assert len(await manager.read_activation_batch()) == 1

            if decision == "human":
                command = InteractionCommand("s", "human_turn", {"message": "改做其他事情"})
            elif decision == "stop":
                command = InteractionCommand("s", "stop_turn", target_run_id=pending.run_id)
            else:
                payload = {"kind": decision}
                if decision == "steer":
                    payload["message"] = "改做其他事情"
                command = InteractionCommand("s", "resolve_authorization", payload, request_id=pending.request_id)
            assert (await scheduler.submit(command)).accepted
            if decision == "stop":
                await next_event_kind(scheduler, "run_completed")
                assert (await scheduler.snapshot("s")).payload["phase"] == "idle"
                assert len(calls) == 1
            else:
                await asyncio.wait_for(model_waiting.wait(), 2)
                current = (await scheduler.snapshot("s")).payload["run_id"]
                assert (current == pending.run_id) == (decision == "approve")
                release_model.set()
                await next_event_kind(scheduler, "run_completed")
                await asyncio.wait_for(model_closed.wait(), 3)
            assert (await runtime.get_task(task_id)).status == TaskStatus.RUNNING
            assert not task_cleaned.is_set()
            await runtime.stop_task(task_id)
            assert (await runtime.wait_task(task_id, timeout_ms=2000)).status == TaskStatus.STOPPED
            assert task_cleaned.is_set()
        finally:
            release_model.set()
            release_task.set()
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
            await manager.close()
    asyncio.run(scenario())


def test_steer_storage_failure_keeps_current_model_waiting(tmp_path):
    """引导正文保存失败不打断旧轮，之后请求仍能沿真实队列成功受理。"""
    import sqlite3
    from tests.model_capture import capture_input

    async def scenario():
        """只用 SQLite 触发器制造写入错误，观察公开轮次和最终模型输入。"""
        path = str(tmp_path / "state.sqlite3")
        queue = asyncio.Queue()
        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(path)))
        entered, replaced, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        class Model:
            """等待真实轮次控制打断，记录最终请求正文。"""
            async def astream(self, messages, tools=None):
                """记录请求后保持等待。

                Args:
                    messages (Sequence[InputMessage]): 真实请求消息。
                    tools (Sequence[ToolDefinition] | None): 工具定义。

                Yields:
                    StreamChunk: 放行后的最终回答。
                """
                calls.append(capture_input(messages))
                (entered if len(calls) == 1 else replaced).set()
                await release.wait()
                yield StreamChunk(content="完成")

            async def close(self):
                """测试模型没有外部资源。"""

        scheduler = build_test_scheduler(runtime_queue=queue, task_supervisor=supervisor,
            agent_factory=lambda session, manager: Agent(AgentConfig(session_id=session, model_name="test", api_key="test",
                context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)), llm_client=Model(), session_state_manager=manager, sqlite_factory=SqliteFactory()),
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)), chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        manager = SessionStateManager("s", SqliteFactory(path))
        try:
            await scheduler.start()
            await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "开始"}))
            await asyncio.wait_for(entered.wait(), 2)
            original = (await scheduler.snapshot("s")).payload["run_id"]
            runtime = PluginRuntime("game", "s", _supervisor=supervisor, _runtime_queue=queue)
            with sqlite3.connect(path) as db:
                db.execute("CREATE TRIGGER refuse_plugin BEFORE INSERT ON session_inbox WHEN NEW.kind = 'plugin_notification' BEGIN SELECT RAISE(ABORT, 'plugin storage unavailable'); END")
            await runtime.steer_turn("未保存的引导", activate_session=True)
            await asyncio.wait_for(queue.join(), 1)
            assert (await scheduler.snapshot("s")).payload["run_id"] == original
            assert len(calls) == 1
            assert await manager.read_activation_batch() == ()
            with sqlite3.connect(path) as db:
                db.execute("DROP TRIGGER refuse_plugin")
            await runtime.steer_turn("优先处理敌人")
            await asyncio.wait_for(replaced.wait(), 2)
            assert (await scheduler.snapshot("s")).payload["run_id"] != original
            content = "\n".join(message.get("content", "") for message in calls[1] if isinstance(message.get("content"), str))
            assert "优先处理敌人" in content and "未保存的引导" not in content
        finally:
            release.set()
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
            await manager.close()
    asyncio.run(scenario())


def test_game_watch_reports_changes_controls_current_turn_and_cleans_connection(tmp_path):
    """游戏任务跨轮引导和停止当前执行，释放连接后交付最终结果。"""
    from tests.model_capture import capture_input

    async def scenario():
        """通过连接事件驱动具体任务，观察真实调度和最终模型请求。"""
        path = str(tmp_path / "state.sqlite3")
        queue = asyncio.Queue()
        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(path)))
        waiting, replaced, release, result_seen = (asyncio.Event() for _ in range(4))
        calls = []

        class GameConnection:
            """外部游戏连接替身，测试决定何时送达状态变化。"""
            def __init__(self):
                """准备连接事件和实际关闭的观察信号。"""
                self.incoming = asyncio.Queue()
                self.processed = asyncio.Queue()
                self.closed = asyncio.Event()

            async def events(self):
                """产生游戏事件，任务读取下一个事件时确认上一个已处理。

                Yields:
                    tuple[str, str]: 事件名称和可告知 Agent 的业务正文。
                """
                while True:
                    event = await self.incoming.get()
                    yield event
                    self.processed.put_nowait(event[0])

            async def close(self):
                """标记实际连接资源已经释放。"""
                self.closed.set()

        connection = GameConnection()

        class GameWatchTask(BaseTask):
            """按游戏状态投递内容和控制请求，释放连接后保存最终结果。"""
            async def run(self, runtime):
                """持续观察事件，游戏关闭后请求结束会话当前执行并返回结果。

                Args:
                    runtime (PluginRuntime): 绑定所属会话、插件和本任务编号的调用对象。

                Returns:
                    str: 供任务结果通知交付的最终说明。
                """
                async for name, text in connection.events():
                    if name == "game_closed":
                        await runtime.stop_turn()
                        return "游戏已关闭，观察结束。"
                    if name == "battle_started":
                        await runtime.notify(name=name, text=text)
                    elif name == "danger":
                        await runtime.steer_turn(text)

            async def cleanup(self):
                """由 Supervisor 等待实际连接释放。"""
                await connection.close()

        class Model:
            """记录真实转换后的请求，保持当前模型请求以验证普通通知不打断。"""
            async def astream(self, messages, tools=None):
                """按请求中的业务内容报告最终结果是否到达。

                Args:
                    messages (Sequence[InputMessage]): 当前模型请求。
                    tools (Sequence[ToolDefinition] | None): 真实工具定义。

                Yields:
                    StreamChunk: 结束当前模型请求的正文。
                """
                request = capture_input(messages)
                calls.append(request)
                (waiting if len(calls) == 1 else replaced).set()
                if any(item.get("name") == "runtime_task_result" and "游戏已关闭" in item["content"] for item in request):
                    result_seen.set()
                await release.wait()
                yield StreamChunk(content="已处理游戏状态")

            async def close(self):
                """外部模型替身没有连接资源。"""

        scheduler = build_test_scheduler(runtime_queue=queue, task_supervisor=supervisor,
            agent_factory=lambda session, manager: Agent(AgentConfig(session_id=session, model_name="test", api_key="test",
                context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)), llm_client=Model(), session_state_manager=manager, sqlite_factory=SqliteFactory()),
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)), chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        try:
            await scheduler.start()
            await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "开始游戏"}))
            await asyncio.wait_for(waiting.wait(), 2)
            source = (await scheduler.snapshot("s")).payload["run_id"]
            runtime = PluginRuntime("game", "s", _supervisor=supervisor, _runtime_queue=queue)
            task_id = await runtime.submit_task(GameWatchTask(), name="game_watch", title="游戏观察", notify_agent=True)
            await connection.incoming.put(("battle_started", "发现两名敌人"))
            assert await asyncio.wait_for(connection.processed.get(), 1) == "battle_started"
            await asyncio.wait_for(queue.join(), 1)
            assert len(calls) == 1
            assert (await scheduler.snapshot("s")).payload["run_id"] == source
            await connection.incoming.put(("danger", "先躲避敌人的攻击"))
            await asyncio.wait_for(replaced.wait(), 2)
            current = (await scheduler.snapshot("s")).payload["run_id"]
            assert current != source
            content = str(calls[1])
            assert "发现两名敌人" in content and "先躲避敌人的攻击" in content
            await connection.incoming.put(("game_closed", ""))
            record = await runtime.wait_task(task_id, timeout_ms=1000)
            await asyncio.wait_for(queue.join(), 1)
            assert record.status == TaskStatus.COMPLETED and record.result == "游戏已关闭，观察结束。"
            assert connection.closed.is_set()
            await asyncio.wait_for(result_seen.wait(), 2)
            assert (await scheduler.snapshot("s")).payload["run_id"] != current
        finally:
            release.set()
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_after_response_commit_notifications_start_followup_turns(tmp_path):
    """提交后钩子连续投递通知会继续执行，每次通知属于独立新轮次。"""
    from app.agent.plugins.base import BasePlugin, PluginContext, PluginHook
    from app.agent.state.messages import PluginNotificationMessage
    from tests.model_capture import capture_input

    async def scenario():
        """执行两个后续通知后结束测试，验证无额外真人计数和无相互等待。"""
        path = str(tmp_path / "state.sqlite3")
        queue = asyncio.Queue()
        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(path)))
        committed = []
        requests = []

        class FollowupPlugin(BasePlugin):
            """在前三次已保存回答之间追加两条持久通知。"""
            name = "followup"

            @property
            def hooks(self):
                """订阅回答已保存的时机。

                Returns:
                    list[PluginHook]: 提交完成钩子。
                """
                return [PluginHook.AFTER_RESPONSE_COMMIT]

            async def execute(self, context):
                """确认钩子可完成单向投递，随后由调度器安排后续轮次。

                Args:
                    context (HookContext): 真实 Agent 的本次提交上下文。
                """
                committed.append(context.runtime.session_id)
                if len(committed) < 3:
                    await context.runtime.notify(name="follow_up", text=f"继续处理第{len(committed)}项")

        class Model:
            """记录请求并立即完成回答，不替换 Agent 的保存流程。"""
            async def astream(self, messages, tools=None):
                """产生一次正常完成的业务回答。

                Args:
                    messages (Sequence[InputMessage]): 真实模型输入。
                    tools (Sequence[ToolDefinition] | None): 可用工具定义。

                Yields:
                    StreamChunk: 一段回答正文。
                """
                requests.append(capture_input(messages))
                yield StreamChunk(content="本项已完成")

            async def close(self):
                """测试模型没有外部连接。"""

        class AgentWithFollowups(Agent):
            """沿用完整 Agent 执行，仅在正常装配后注册场景插件。"""
            async def initialize(self):
                """装配真实依赖和提交后钩子。"""
                await super().initialize()
                self.register_plugin(FollowupPlugin(context=PluginContext(SqliteFactory(path))))

        scheduler = build_test_scheduler(runtime_queue=queue, task_supervisor=supervisor,
            agent_factory=lambda session, manager: AgentWithFollowups(AgentConfig(session_id=session, model_name="test", api_key="test",
                context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)), llm_client=Model(), session_state_manager=manager,
                plugin_runtime_factory=lambda name: PluginRuntime(name, session, _supervisor=supervisor, _runtime_queue=queue), sqlite_factory=SqliteFactory(path)),
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)), chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        manager = SessionStateManager("s", SqliteFactory(path))
        try:
            await scheduler.start()
            await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "开始"}))
            completed = [await next_event_kind(scheduler, "run_completed") for _ in range(3)]
            assert len(requests) == 3 and len({event.run_id for event in completed}) == 3
            assert committed == ["s", "s", "s"]
            state = await manager.load()
            assert memory_count(state) == 0
            messages = [message for message in state.messages if isinstance(message, PluginNotificationMessage)]
            assert [message.text for message in messages] == ["继续处理第1项", "继续处理第2项"]
            assert await manager.read_activation_batch() == ()
        finally:
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
            await manager.close()
    asyncio.run(scenario())
