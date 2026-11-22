from tests.memory_support import operation_for
from tests.memory_support import memory_count, set_memory_count
"""通过正式任务入口验证插件任务的执行、清理和结果查询。"""
from tests.runtime_support import runtime_hub, close_test_runtime
from app.routes.agent import observe_runtime
from tests.runtime_support import next_runtime_event
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory

from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator

import asyncio
import threading
import sqlite3
import sys
import pytest

from app.agent.runtime.tasks.base import BaseTask
from tests.runtime_support import configured_settings
from app.agent.plugins.runtime import PluginRuntime
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.schemas.runtime_types import TaskStatus, TaskExecutionPhase
from app.agent.state.manager import SessionStateManager


def test_plugin_notifications_are_persisted_in_order(tmp_path):
    """连续保存的插件通知保留独立输入，供后续执行按顺序交付。

    Args:
        tmp_path (Path): 本次测试的隔离数据库目录。
    """
    async def scenario() -> None:
        """保存两条独立通知，并通过待交付输入查询核对保存顺序和正文。"""
        manager = SessionStateManager("s", SqliteFactory(tmp_path / "state.sqlite3"))
        try:
            await manager.submit_plugin_notification("game", "danger", "先躲避攻击")
            await manager.submit_plugin_notification("game", "danger", "再寻找掩体")
            pending = await manager.read_activation_batch()
            assert [item.payload["text"] for item in pending] == ["先躲避攻击", "再寻找掩体"]
            assert pending[0].id < pending[1].id
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_shutdown_repeated_cancellation_keeps_memory_work_active(tmp_path, monkeypatch):
    """关闭期间多次取消等待时，线程中的记忆工作仍计为活动任务。"""
    from types import SimpleNamespace
    from app.agent.plugins.memory.tasks.memory_tasks import LongTermMemoryTask
    from app.agent.plugins.memory.manager import MemoryManager
    from app.agent.plugins.memory.config import MemoryPluginConfig
    from app.agent.plugins.memory.memories.semantic_mem0 import Mem0SemanticMemory
    from app.agent.state.messages import HumanMessage, text_content


    async def scenario():
        """通过真实线程屏障观察停止请求、关闭超时和最终保存。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        runtime = PluginRuntime("memory", "s", _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue)
        started, release = threading.Event(), threading.Event()

        class History:
            """提供固定历史材料，不访问生产存储。"""

            async def get_recent_completed_turns(self, session_id, **kwargs):
                """返回本批次真实领域消息。

                Args:
                    session_id (str): 来源会话。
                    kwargs (dict): 固定历史范围。

                Returns:
                    tuple: 历史和当前完整轮次。
                """
                return [], [HumanMessage(content=text_content("记住这件事"))]

        class SyncMemory:
            """只替代外部 Mem0 同步调用，保留实际线程和应用记忆代码。"""

            def add(self, messages, *, agent_id):
                """标记业务开始并等待同步操作实际完成。

                Args:
                    messages (list): 交给 Mem0 的材料。
                    agent_id (str): 当前会话。

                Returns:
                    dict: 外部提取结果。
                """
                started.set()
                release.wait(5)
                return {"results": []}

        semantic = object.__new__(Mem0SemanticMemory)
        semantic.session_id = "s"
        semantic.config = SimpleNamespace(extraction_max_context_tokens=0)
        semantic.chat_settings = SimpleNamespace(name="Ayaya", address="主人", feature="本地", character="助手")
        semantic._memory = SyncMemory()
        semantic._memory.llm = SimpleNamespace()
        semantic._memory.config = SimpleNamespace()
        manager = object.__new__(MemoryManager)
        manager.options = MemoryPluginConfig()
        manager.chat_history_store = History()
        manager.episodic_memory = None
        manager.semantic_memory = semantic

        closing = None
        try:
            task_id = await runtime.submit_task(LongTermMemoryTask(operation_for(manager), "s", 1, 1, False, True), name="diary", title="日记", notify_agent=False)
            assert await asyncio.to_thread(started.wait, 2)
            closing = asyncio.create_task(supervisor.close())
            await asyncio.sleep(0.05)
            assert supervisor.active_task_counts()[0] == 1
            assert not closing.done()
            assert (await runtime.get_task(task_id)).status == TaskStatus.RUNNING
        finally:
            release.set()
            if closing is not None:
                await closing
                await supervisor.records.close()
            else:
                await supervisor.close()
                await supervisor.records.close()
    asyncio.run(scenario())


def test_cancel_during_committed_submission_keeps_task_owned(tmp_path, monkeypatch):
    """调用者在任务登记提交后被取消，Supervisor 仍须执行和收尾任务。"""
    from app.crud.task_record_dao import TaskRecordDao

    async def scenario():
        """仅在真实提交返回处注入外层取消，使用正式查询观察结果。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        runtime = PluginRuntime("game", "s", _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue)
        cleaned = asyncio.Event()
        caller = asyncio.current_task()
        insert_running = TaskRecordDao.insert_running

        async def commit_then_cancel(self, *args, **kwargs):
            """保留真实 SQLite 提交，在返回后模拟用户停止取消钩子。"""
            await insert_running(self, *args, **kwargs)
            caller.cancel()

        class GreetingTask(BaseTask):
            """被登记的工作始终有执行者负责。"""
            async def run(self, runtime):
                """返回最终结果。"""
                return "Hello, World!"

            async def cleanup(self):
                """标记资源释放完成。"""
                cleaned.set()

        monkeypatch.setattr(TaskRecordDao, "insert_running", commit_then_cancel)
        try:
            with pytest.raises(asyncio.CancelledError):
                await runtime.submit_task(GreetingTask(), name="greeting", title="问候", notify_agent=True)
            await asyncio.wait_for(cleaned.wait(), 1)
            records = await supervisor.list_tasks("s")
            assert len(records) == 1
            record = await runtime.wait_task(records[0].task_id, timeout_ms=1000)
            assert record.status == TaskStatus.COMPLETED
            assert record.result == "Hello, World!"
            assert supervisor.active_task_counts()[0] == 0
        finally:
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_cancel_during_terminal_registration_keeps_process_owned(tmp_path, monkeypatch):
    """终端登记成功时取消调用者，进程仍移交后台并由 Supervisor 收尾。"""
    from app.crud.task_record_dao import TaskRecordDao
    from app.agent.tools.exec_command import ExecCommandTool
    from app.agent.tools.core.base import ToolContext
    from app.agent.tools.core.runtime import ToolRuntime
    from app.agent.state.model import AgentState
    monkeypatch.setattr("app.agent.tools.exec_command.WORKDIR", tmp_path)

    async def scenario():
        """在真实登记返回处取消前台调用，查询后台记录的最终状态。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        runtime = ToolRuntime("s", "run", "exec_command", "call", _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue)
        context = ToolContext("s", AgentState(session_id="s"), runtime=runtime)
        caller = asyncio.current_task()
        insert_running = TaskRecordDao.insert_running

        async def commit_then_cancel(self, *args, **kwargs):
            """保留真实提交，在进程取得管理身份前取消调用者。"""
            await insert_running(self, *args, **kwargs)
            caller.cancel()

        monkeypatch.setattr(TaskRecordDao, "insert_running", commit_then_cancel)
        try:
            with pytest.raises(asyncio.CancelledError):
                await ExecCommandTool().execute({"cmd": "echo done"}, context)
            records = await supervisor.list_tasks("s")
            assert len(records) == 1
            assert (await supervisor.wait(records[0].task_id, timeout_ms=2000)).status == TaskStatus.COMPLETED
            assert supervisor.active_task_counts()[0] == 0
        finally:
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())




def test_failed_terminal_persistence_reports_save_failure_to_waiter(tmp_path, monkeypatch):
    """终态事务永久失败时返回保存失败摘要并释放资源。"""


    async def scenario():
        """通过真实 SQLite 触发器制造持久化错误，不替换任务 DAO。"""
        path = str(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(path)))
        runtime = PluginRuntime("game", "s", _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue)
        release = asyncio.Event()

        class GreetingTask(BaseTask):
            """完成业务后由框架保存最终状态。"""
            async def run(self, runtime):
                """等待触发器安装，再返回业务结果。"""
                await release.wait()
                return "Hello, World!"

        try:
            task_id = await runtime.submit_task(GreetingTask(), name="greeting", title="问候", notify_agent=True)
            with sqlite3.connect(path) as db:
                db.execute("CREATE TRIGGER refuse_terminal BEFORE UPDATE ON task_records BEGIN SELECT RAISE(ABORT, 'terminal storage unavailable'); END")
            release.set()
            assert (await runtime.wait_task(task_id, timeout_ms=1000)).status == TaskExecutionPhase.SAVE_FAILED
            await supervisor.close()
            await supervisor.records.close()
            assert supervisor.active_task_counts()[0] == 0
        finally:
            release.set()
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("fail_run, expected_status", [
    (True, TaskStatus.FAILED),
    (False, TaskStatus.COMPLETED),
])
def test_shutdown_cleanup_cancellation_preserves_task_outcome(tmp_path, monkeypatch, fail_run, expected_status):
    """关闭期间等待资源清理时，保留已经保存的成功或失败结果。

    Args:
        tmp_path (Path): Pytest 提供的隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的测试补丁工具。
        fail_run (bool): 本次业务执行是否抛出异常。
        expected_status (TaskStatus): 清理完成后应保存和通知的任务状态。
    """


    async def scenario() -> None:
        """等待任务进入清理后关闭 Supervisor，再重新读取任务记录和 Inbox。"""
        database = str(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(database)))
        runtime = PluginRuntime("game", "s", _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue)
        cleaning, cleaned = asyncio.Event(), asyncio.Event()
        inspector = None
        inbox = SessionStateManager("s", SqliteFactory(database))

        class OutcomeTask(BaseTask):
            """按用例完成或报告业务失败，并在关闭期间完成资源清理。"""

            async def run(self, runtime: PluginRuntime) -> str:
                """返回业务结果，或报告本次执行失败。

                Args:
                    runtime (PluginRuntime): 框架绑定任务编号和来源的运行时。

                Returns:
                    str: 正常执行取得的业务结果。

                Raises:
                    ValueError: 用例要求模拟业务执行失败。
                """
                if fail_run:
                    raise ValueError("业务执行失败")
                return "业务结果"

            async def cleanup(self) -> None:
                """报告开始清理，经过短暂异步等待后确认资源已经释放。"""
                cleaning.set()
                await asyncio.sleep(0.05)
                cleaned.set()

        try:
            # 1. 业务先完成或失败；收到清理开始信号后，再触发应用关闭。
            task_id = await runtime.submit_task(OutcomeTask(), name="game.outcome", title="检查执行结果",
                notify_agent=True)
            await asyncio.wait_for(cleaning.wait(), 1)
            await asyncio.wait_for(supervisor.close(), 1)
            assert cleaned.is_set()
            assert supervisor.active_task_counts()[0] == 0

            # 2. 重新打开持久记录，检查关闭期间的取消处理后仍保留实际业务结果。
            inspector_settings = configured_settings()
            inspector_mutations = SettingsMutationCoordinator()
            inspector = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(database)))
            record = await inspector.get_task_snapshot(task_id)
            assert record.status == expected_status
            assert record.error == ("业务执行失败" if fail_run else None)
            assert record.result == (None if fail_run else "业务结果")

            # 3. 给 Agent 的唯一通知与记录使用相同终态，错误和结果正文也完整保存。
            notifications = await inbox.read_task_terminals()
            assert len(notifications) == 1
            assert notifications[0].payload["status"] == expected_status
            assert notifications[0].payload.get("error") == record.error
            assert notifications[0].payload["result"] == (record.result or "")
        finally:
            await supervisor.close()
            await supervisor.records.close()
            if inspector is not None:
                await inspector.close()
                await inspector.records.close()
            await inbox.close()

    asyncio.run(scenario())


def test_silent_task_saves_result_without_notice_and_filters_plugin_access(tmp_path):
    """静默任务保存结果且通知为空，查询与等待按插件来源过滤。"""
    async def scenario():
        """使用真实队列与临时数据库提交一次任务。"""
        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        runtime = PluginRuntime("game", "s", _supervisor=supervisor, _runtime_queue=queue)
        other = PluginRuntime("other", "s", _supervisor=supervisor, _runtime_queue=queue)
        steps = []

        class GreetingTask(BaseTask):
            """返回可交付文本，同时记录资源清理。"""
            async def run(self, runtime):
                """记录已绑定的编号并返回最终结果。"""
                assert runtime.task_id
                steps.append("run")
                return "Hello, World!"

            async def cleanup(self):
                """释放本次任务资源。"""
                steps.append("cleanup")

        try:
            task_id = await runtime.submit_task(GreetingTask(), name="greeting", title="问候", notify_agent=False)
            record = await runtime.wait_task(task_id, timeout_ms=1000)
            assert record.status == TaskStatus.COMPLETED
            assert record.result == "Hello, World!"
            assert await other.get_task(task_id) is None
            assert (await supervisor.records.get(task_id)).origin_run_id is None
            assert await other.wait_task(task_id, timeout_ms=0) is None
            assert await runtime.wait_task("missing", timeout_ms=0) is None
            manager = SessionStateManager("s", SqliteFactory(str(tmp_path / "state.sqlite3")))
            try:
                assert await manager.read_task_terminals() == ()
            finally:
                await manager.close()
            await supervisor.close()
            assert steps == ["run", "cleanup"]
        finally:
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("user_stop", [False, True])
def test_plugin_stop_keeps_activation_and_controls_current_turn(tmp_path, user_stop):
    """插件跨轮停止保留自动执行，用户停止暂停后由真人输入恢复资格。"""
    from app.agent.agent import Agent, AgentConfig
    from app.agent.context import ContextStrategyConfig
    from app.agent.models.llm_client import StreamChunk
    from app.interaction.interaction_contract import InteractionCommand
    from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler

    async def scenario():
        """阻塞真实 Agent 的模型等待，通过公开事件和输入查询观察控制效果。"""
        path = str(tmp_path / "state.sqlite3")
        queue = asyncio.Queue()
        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(path)))
        first_call, second_call, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        class Model:
            """模型等待可由真实 TurnControl 中断。"""
            async def astream(self, messages, tools=None):
                """标记每轮执行，在测试结束前保持模型等待。"""
                calls.append(messages)
                (first_call if len(calls) == 1 else second_call).set()
                await release.wait()
                yield StreamChunk(content="完成")

            async def close(self):
                """模型替身不持有外部资源。"""

        scheduler = build_test_scheduler(runtime_queue=queue, task_supervisor=supervisor,
            agent_factory=lambda session, manager: Agent(AgentConfig(session_id=session, model_name="test", api_key="test",
                context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)), llm_client=Model(), session_state_manager=manager, sqlite_factory=SqliteFactory()),
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)), chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)

        async def next_kind(kind):
            """从正式事件输出读取指定事件，跳过本场景不关心的其他事件。"""
            while True:
                event = await asyncio.wait_for(next_runtime_event(scheduler), 2)
                if event.kind == kind:
                    return event

        try:
            await scheduler.start()
            await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "开始"}))
            await asyncio.wait_for(first_call.wait(), 2)
            started = await next_kind("run_started")
            runtime = PluginRuntime("game", "s", _supervisor=supervisor, _runtime_queue=queue)
            if user_stop:
                assert (await scheduler.submit(InteractionCommand("s", "stop_turn", target_run_id=started.run_id))).accepted
            else:
                assert await runtime.stop_turn() is None
                await asyncio.wait_for(queue.join(), 1)
            await next_kind("run_completed")
            await runtime.notify(name="battle_started", text="发现敌人")
            await asyncio.wait_for(queue.join(), 1)
            if user_stop:
                manager = SessionStateManager("s", SqliteFactory(path))
                try:
                    assert (await manager.read_activation_batch())[0].kind == "plugin_notification"
                    assert len(calls) == 1
                finally:
                    await manager.close()
                await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "继续"}))
            await asyncio.wait_for(second_call.wait(), 2)
            current = (await scheduler.snapshot("s")).payload["run_id"]
            assert current != started.run_id
            await runtime.stop_turn()
            await asyncio.wait_for(queue.join(), 1)
            stopped = await next_kind("run_completed")
            assert stopped.run_id == current
            snapshot = await scheduler.snapshot("s")
            assert snapshot.payload["phase"] == "idle"
            assert snapshot.payload["session_state"] == "enabled"
        finally:
            await close_test_runtime(scheduler)
            release.set()
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("fail_cleanup", [False, True])
def test_wait_timeout_and_cleanup_preserve_execution_and_result(tmp_path, fail_cleanup):
    """等待超时与取消保留任务，保存的结果在资源关闭期间即可读取。"""
    async def scenario():
        """用执行和清理屏障检查公开查询、等待及活动任务数量。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        runtime = PluginRuntime("game", "s", _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue)
        run_started, release_run = asyncio.Event(), asyncio.Event()
        cleaning, release_cleanup = asyncio.Event(), asyncio.Event()

        class WaitingTask(BaseTask):
            """在测试放行前保持执行或清理状态。"""
            async def run(self, runtime):
                """返回最终结果，不把执行日志当作结果。"""
                run_started.set()
                await release_run.wait()
                return "Hello, World!"

            async def cleanup(self):
                """等待资源释放，按场景报告清理失败。"""
                cleaning.set()
                await release_cleanup.wait()
                if fail_cleanup:
                    raise RuntimeError("关闭连接失败")

        try:
            task_id = await runtime.submit_task(WaitingTask(), name="wait", title="等待", notify_agent=True)
            await run_started.wait()
            assert (await runtime.wait_task(task_id, timeout_ms=1)).status == TaskStatus.RUNNING
            waiter = asyncio.create_task(runtime.wait_task(task_id, timeout_ms=1000))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            release_run.set()
            await cleaning.wait()
            assert supervisor.active_task_counts()[0] == 0
            assert (await runtime.wait_task(task_id, timeout_ms=1000)).status == TaskStatus.COMPLETED
            release_cleanup.set()
            record = await runtime.wait_task(task_id, timeout_ms=1000)
            assert record.result == "Hello, World!"
            assert record.status == TaskStatus.COMPLETED
            assert supervisor.active_task_counts()[0] == 0
            manager = SessionStateManager("s", SqliteFactory(str(tmp_path / "state.sqlite3")))
            try:
                notices = await manager.read_task_terminals()
                assert len(notices) == 1
                assert notices[0].payload["result"] == "Hello, World!"
            finally:
                await manager.close()
        finally:
            release_run.set()
            release_cleanup.set()
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_plugin_notification_is_persistent_and_reaches_real_model_encoding(tmp_path):
    """通知入队后经 Scheduler、Inbox、消息和真实模型编码，保留插件来源。"""
    from app.agent.agent import Agent, AgentConfig
    from app.agent.context import ContextStrategyConfig
    from app.agent.models.llm_client import StreamChunk
    from app.agent.state.messages import PluginNotificationMessage
    from app.interaction.interaction_contract import InteractionCommand
    from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
    from tests.model_capture import capture_input

    async def scenario():
        """未激活时只保存通知，真人输入到达后原子交付同批内容。"""
        path = str(tmp_path / "state.sqlite3")
        queue = asyncio.Queue()
        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(path)))
        runtime = PluginRuntime("game", "s", _supervisor=supervisor, _runtime_queue=queue)
        captured, called = [], asyncio.Event()

        class GreetingTask(BaseTask):
            """提供一条最终结果，验证它与主动通知沿不同路径进入同一批输入。"""
            async def run(self, runtime):
                """返回业务结果正文。"""
                await runtime.publish_event("notice", {"text": "任务已连接"})
                return "Hello, World!"

        class Model:
            """只替代外部模型，实际业务消息经过真实编码。"""
            async def astream(self, messages, tools=None):
                """保存最终模型请求并提供可完成的回答。"""
                captured.extend(capture_input(messages))
                called.set()
                yield StreamChunk(content="收到战斗消息")

            async def close(self):
                """外部模型替身无资源需要释放。"""

        scheduler = build_test_scheduler(runtime_queue=queue,
            agent_factory=lambda session, manager: Agent(AgentConfig(session_id=session, model_name="test", api_key="test", context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)),
                llm_client=Model(), session_state_manager=manager, sqlite_factory=SqliteFactory()),
            task_supervisor=supervisor, session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)), chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        manager = SessionStateManager("s", SqliteFactory(path))
        try:
            await scheduler.start()
            task_id = await runtime.submit_task(GreetingTask(), name="greeting", title="问候", notify_agent=True)
            await runtime.wait_task(task_id, timeout_ms=1000)
            assert await runtime.notify(name="battle_started", text="发现两名敌人。") is None
            await asyncio.wait_for(queue.join(), 1)
            while True:
                event = await asyncio.wait_for(next_runtime_event(scheduler), 1)
                if event.kind == "notice":
                    assert event.task_id == task_id
                    break
            assert not called.is_set()
            assert (await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "继续"}))).accepted
            await asyncio.wait_for(called.wait(), 2)
            state = await manager.load()
            notice = next(message for message in state.messages if isinstance(message, PluginNotificationMessage))
            assert notice.plugin_name == "game" and notice.name == "battle_started"
            model_notice = next(item for item in captured if item.get("name") == "runtime_plugin_notification")
            assert "发现两名敌人。" in model_notice["content"]
            assert "game/battle_started" in model_notice["content"]
            model_result = next(item for item in captured if item.get("name") == "runtime_task_result")
            assert "Hello, World!" in model_result["content"]
            assert "output_ref" not in model_result["content"] and "task_id" not in model_result["content"]
            assert memory_count(state) == 0
        finally:
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
            await manager.close()
    asyncio.run(scenario())
