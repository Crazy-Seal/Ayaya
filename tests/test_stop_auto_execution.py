from app.agent.state.manager import RequiredSaveError
"""验证用户停止后的通知保存与真人输入恢复执行。"""
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
from pathlib import Path

import pytest

from app.agent.core.events import AgentEvent, EventType
from app.agent.runtime.turn_control import TurnInterrupted
from app.agent.state.manager import SessionStateManager
from app.interaction.interaction_contract import InteractionCommand
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.schemas.runtime_types import InteractionEventKind, SessionPhase
from tests.runtime_support import configured_settings, finish_test_turn


@pytest.mark.parametrize("arrival", ["before_cleanup", "idle", "reactivate_during_cleanup"])
def test_stop_preserves_notifications_until_human_input(tmp_path: Path, arrival: str) -> None:
    """停止后保留通知，新输入与旧轮清理交错时仍恢复执行。

    Args:
        tmp_path (Path): 隔离测试数据库与任务目录。
        arrival (str): 通知到达及新真人输入相对旧轮清理的时机。
    """
    async def scenario() -> None:
        """用清理屏障控制时序，通过真实 Inbox 验证通知交付。"""
        path = str(tmp_path / "state.sqlite3")
        started = asyncio.Event()
        cleaning = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup_finished = asyncio.Event()
        runtime_started = asyncio.Event()
        resumed = asyncio.Event()
        calls: list[str] = []

        class RecordingAgent(RuntimeAgentStub):
            """保存真实输入，并允许测试暂停第一轮清理。"""

            def __init__(self) -> None:
                """记录该实例是否执行被停止的旧轮。"""
                self.old_turn = False

            async def run(self, message, *, inbox_inputs, turn_control, emit, **kwargs):
                """交付输入，旧轮等停止，新轮报告恢复。

                Args:
                    message (str): 当前真人输入正文。
                    inbox_inputs (tuple): 本轮需原子交付的 Inbox 输入。
                    turn_control (TurnControl): 当前轮次的停止信号。
                    **kwargs (dict): Scheduler 提供的其他执行参数。

                Yields:
                    AgentEvent: 执行开始与结束事件。
                """
                outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED)
                self.old_turn = message == "旧轮"
                calls.append(message)
                await emit(AgentEvent(EventType.RUN_STARTED, {"resumed": False}))
                await finish_test_turn(path, inbox_inputs, message=message)
                if self.old_turn:
                    turn_control.start_llm()
                    started.set()
                    # 1. 模拟模型等待，停止由真实 TurnControl 取消当前读取。
                    try:
                        await turn_control.next_llm_chunk(self.wait_for_stop())
                    except TurnInterrupted:
                        pass
                else:
                    resumed.set()
                await emit(AgentEvent(EventType.DONE, None))

                return outcome

            async def wait_for_stop(self):
                """保持模型读取等待，由停止信号结束。

                Yields:
                    None: 仅用于构造可取消的模型流。
                """
                await asyncio.Event().wait()
                yield None

            async def run_runtime_inputs(self, *, inbox_inputs, emit, **kwargs):
                """记录意外自动执行，并真实交付其通知。

                Args:
                    inbox_inputs (tuple): 当前待交付的任务结果。
                    **kwargs (dict): 其他执行参数。

                Yields:
                    AgentEvent: 通知启动的新轮事件。
                """
                outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED)
                runtime_started.set()
                await emit(AgentEvent(EventType.RUN_STARTED, {"resumed": False}))
                await finish_test_turn(path, inbox_inputs)
                await emit(AgentEvent(EventType.DONE, None))

                return outcome

            async def close(self) -> None:
                """暂停旧轮清理，使通知和新输入能够确定性交错。"""
                if self.old_turn:
                    cleaning.set()
                    await release_cleanup.wait()
                    cleanup_finished.set()

        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(path)))
        scheduler = build_test_scheduler(runtime_queue=supervisor.runtime_queue,
            agent_factory=lambda _session, _manager: RecordingAgent(),
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)),
            task_supervisor=supervisor,
         chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        manager = SessionStateManager("s", SqliteFactory(path))
        try:
            await scheduler.start()
            assert (await scheduler.submit(InteractionCommand(
                "s", "human_turn", {"message": "旧轮"},
            ))).accepted
            await asyncio.wait_for(started.wait(), 2)
            run_id = (await scheduler.snapshot("s")).payload["run_id"]
            worker = scheduler._parent("s")._worker
            # 2. 过期停止不能撤销资格；有效停止才暂停后续自动执行。
            assert not (await scheduler.submit(InteractionCommand(
                "s", "stop_turn", target_run_id="过期轮次",
            ))).accepted
            assert (await scheduler.snapshot("s")).payload["session_state"] == "enabled"
            assert (await scheduler.submit(InteractionCommand(
                "s", "stop_turn", target_run_id=run_id,
            ))).accepted
            await asyncio.wait_for(cleaning.wait(), 2)

            if arrival == "idle":
                release_cleanup.set()
                await asyncio.wait_for(cleanup_finished.wait(), 2)

            # 3. 终态及通知真实入库，并经 Scheduler 处理记录提醒。
            await supervisor.records.insert_running("task", "s", "后台工作", 1, str(tmp_path / "out"))
            assert await finish_task_record(supervisor, "task", "completed")
            await scheduler._handle_task_record(await supervisor.get_task_snapshot("task"))
            assert len(await manager.read_task_terminals()) == 1
            assert not runtime_started.is_set()

            if arrival == "reactivate_during_cleanup":
                assert (await scheduler.submit(InteractionCommand(
                    "s", "human_turn", {"message": "恢复"},
                ))).accepted
                release_cleanup.set()
            else:
                release_cleanup.set()
                await asyncio.wait_for(cleanup_finished.wait(), 2)
                # 4. 已存在的 worker 和后来的提醒均保持暂停，内容留待真人恢复。
                assert len(await manager.read_task_terminals()) == 1
                assert not runtime_started.is_set()
                assert (await scheduler.submit(InteractionCommand(
                    "s", "human_turn", {"message": "恢复"},
                ))).accepted

            await asyncio.wait_for(resumed.wait(), 2)
            assert calls == ["旧轮", "恢复"]
            assert await manager.read_task_terminals() == ()
            assert (await scheduler.snapshot("s")).payload["session_state"] == "enabled"
        finally:
            release_cleanup.set()
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("reported_start", [False, True])
def test_save_failure_stays_paused_during_cleanup_and_only_new_input_runs(tmp_path: Path, reported_start: bool) -> None:
    """输入交付失败后暂停并恢复已保存状态，新的明确输入开始后续工作。

    Args:
        tmp_path (Path): 隔离输入、checkpoint 和会话历史的临时目录。
        reported_start (bool): 首次输入交付失败前是否已经发出轮次开始事件。
    """
    async def scenario() -> None:
        """通过清理屏障检查错误展示、过期停止拒绝及新输入替换失败批次。"""
        path = str(tmp_path / "state.sqlite3")
        cleaning = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup_finished = asyncio.Event()
        resumed = asyncio.Event()
        calls: list[str] = []

        class FailingOnceAgent(RuntimeAgentStub):
            """第一次交付前失败并等待清理，后续实例使用真实事务保存输入。

            Attributes:
                failed (bool): 本实例是否需要等待测试放行清理。
            """

            def __init__(self) -> None:
                """初始化当前尝试的失败标记。"""
                self.failed = False

            async def run(self, message, *, inbox_inputs, emit, **kwargs):
                """按场景报告开始并模拟首次保存失败，后续正常交付并结束轮次。

                Args:
                    message (str): 当前待交付的真人正文。
                    inbox_inputs (tuple): 与正文一起原子保存的 Inbox 输入。
                    **kwargs (dict): Scheduler 提供的其他执行参数。

                Yields:
                    AgentEvent: 轮次开始和保存成功后的完成事件。

                Raises:
                    RuntimeError: 首次尝试在交付输入前模拟保存失败。
                """
                outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED)
                calls.append(message)
                if reported_start or len(calls) > 1:
                    await emit(AgentEvent(EventType.RUN_STARTED, {"resumed": False}))
                if len(calls) == 1:
                    self.failed = True
                    raise RequiredSaveError("模拟输入保存失败")
                await finish_test_turn(path, inbox_inputs, message=message)
                await emit(AgentEvent(EventType.DONE, None))
                if message == "恢复":
                    resumed.set()

                return outcome

            async def close(self) -> None:
                """暂停失败尝试的清理，让测试检查故障暂停和旧轮停止回执。"""
                if self.failed:
                    cleaning.set()
                    await release_cleanup.wait()
                    cleanup_finished.set()

        scheduler = build_test_scheduler(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(),
            agent_factory=lambda _session, _manager: FailingOnceAgent(),
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)),
        )
        manager = SessionStateManager("s", SqliteFactory(path))
        try:
            await scheduler.start()
            # 1. 首次输入已保存到 Inbox，Agent 交付失败后停在实例清理中，当前轮次可查询。
            assert (await scheduler.submit(InteractionCommand(
                "s", "human_turn", {"message": "原始输入"},
            ))).accepted
            await asyncio.wait_for(cleaning.wait(), 2)
            snapshot = await scheduler.snapshot("s")
            assert snapshot.payload["phase"] == SessionPhase.IDLE
            assert snapshot.payload["session_state"] == "paused"
            assert snapshot.payload["save_failure"]["restored"]
            failed_run = snapshot.payload["save_failure"]["run_id"]
            assert not (await scheduler.submit(InteractionCommand(
                "s", "stop_turn", target_run_id=failed_run))).accepted
            # 1. 保存故障立即显示错误和恢复位置，旧轮没有假成功完成事件。
            events = []
            while not any(event.kind == "history_restored" for event in events):
                events.append(await asyncio.wait_for(next_runtime_event(scheduler), 2))
            assert len([event for event in events if event.kind == "error"]) == 1
            assert not any(event.kind == "run_completed" for event in events)
            release_cleanup.set()
            await asyncio.wait_for(cleanup_finished.wait(), 2)
            assert calls == ["原始输入"]
            assert [item.payload["message"] for item in await manager.pending_humans()] == ["原始输入"]
            # 2. 用户新的明确输入替换失败批次，原始输入始终只尝试一次。
            assert (await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "恢复"}))).accepted
            await asyncio.wait_for(resumed.wait(), 2)
            kinds = [ (await asyncio.wait_for(next_runtime_event(scheduler), 2)).kind for _ in range(2)]
            assert kinds == [InteractionEventKind.RUN_STARTED, InteractionEventKind.RUN_COMPLETED]
            assert calls == ["原始输入", "恢复"]
            assert not await manager.pending_humans()
        finally:
            release_cleanup.set()
            await close_test_runtime(scheduler)
            await manager.close()

    asyncio.run(scenario())
