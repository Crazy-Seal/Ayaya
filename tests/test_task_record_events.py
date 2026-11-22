"""验证任务记录的单向传递、持久事实和消费者生命周期。"""
from tests.runtime_support import runtime_hub, close_test_runtime
from tests.runtime_event_source import observe_session
from tests.runtime_support import next_runtime_event
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory

from tests.runtime_support import finish_task_record
from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator
from tests.terminal_harness import TerminalHarness
from tests.runtime_support import configured_settings

import asyncio
from contextlib import closing
from functools import partial
import sqlite3
import sys

import pytest

from app.agent.state.manager import SessionStateManager
from app.interaction.interaction_event_hub import InteractionEventHub
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor


def unexpected_agent(session_id, _manager):
    """禁止未激活会话仅凭任务记录创建 Agent。

    Args:
        _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
        session_id (str): 错误请求执行的会话。

    Raises:
        AssertionError: 本组测试没有提交真人输入。
    """
    raise AssertionError(f"任务记录不能激活会话 {session_id}")


def create_scheduler(tmp_path, supervisor):
    """将真实任务管理器和会话存储装配到同一测试目录。

    Args:
        tmp_path (Path): 隔离存储目录。
        supervisor (TaskSupervisor): Scheduler 唯一读取的任务记录来源。

    Returns:
        AgentRuntimeScheduler: 尚未启动的会话调度器。
    """
    path = str(tmp_path / "state.sqlite3")
    return build_test_scheduler(runtime_queue=supervisor.runtime_queue,
        agent_factory=unexpected_agent,
        task_supervisor=supervisor,
        session_state_manager_factory=partial(SessionStateManager, sqlite_factory=SqliteFactory(path)),
        chat_settings_dao=configured_settings(), mutation_coordinator=SettingsMutationCoordinator())


async def insert_task(supervisor, task_id="task", *, background=True):
    """登记通用任务，供测试通过正式终态事务结束。

    Args:
        supervisor (TaskSupervisor): 持有隔离任务数据库的对象。
        task_id (str): 本次登记的唯一任务编号。
        background (bool): 是否应在后台任务入口显示。
    """
    await supervisor.records.insert_running(
        task_id, "s", name="test", title="测试任务", background=background,
    )


def test_supervisor_finishes_without_consumer_and_preserves_buffer(tmp_path) -> None:
    """没有消费者时仍完成真实进程，关闭后按序读取积压记录和结束标记。

    Args:
        tmp_path (Path): 隔离数据库和日志目录。
    """
    async def scenario():
        """启动后台命令，先验证持久终态和资源收尾，再读取记录来源。"""
        path = str(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(path)))
        terminal_for_supervisor = TerminalHarness(supervisor, spool_dir=tmp_path / "tasks")

        async def wait_terminal(task_id):
            """只通过查询等待操作系统进程的终态保存。

            Args:
                task_id (str): 等待完成的命令编号。

            Returns:
                TaskRecord: 已保存的进程终态。
            """
            while True:
                record = await supervisor.get_task_snapshot(task_id)
                if record.status != "running":
                    return record
                await asyncio.sleep(0.01)

        try:
            result = await terminal_for_supervisor.launch_terminal(
                "s", "输出测试", [sys.executable, "-c", "print('done')"],
                cwd=str(tmp_path), yield_time_ms=0,
            )
            record = await asyncio.wait_for(wait_terminal(result.task_id), 3)
            assert record.status == "completed"
            assert "done" in (await terminal_for_supervisor.read_output(result.task_id, 0, 1024))["text"]
            await asyncio.wait_for(supervisor.close(), 3)
            await supervisor.close()
            await supervisor.records.close()
            buffered = []
            while not supervisor.runtime_queue.empty():
                buffered.append(supervisor.runtime_queue.get_nowait())
            assert buffered and all(item.task_id == result.task_id for item in buffered)
            assert buffered[-1].status == "completed"
            assert supervisor.runtime_queue.empty()
            inbox = SessionStateManager("s", SqliteFactory(path))
            try:
                assert len(await inbox.read_task_terminals()) == 1
            finally:
                await inbox.close()
        finally:
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_terminal_event_follows_atomic_commit_and_is_not_repeated(tmp_path) -> None:
    """事务回滚时没有记录事件，重试成功才产生唯一终态记录和 Inbox 通知。

    Args:
        tmp_path (Path): 隔离 SQLite 目录。
    """
    async def scenario():
        """用真实 SQLite 触发器拒绝通知插入，并核对事件和持久事实。"""
        path = str(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(path)))
        inbox = SessionStateManager("s", SqliteFactory(path))
        try:
            await insert_task(supervisor)
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TRIGGER reject_notice BEFORE INSERT ON session_inbox "
                           "BEGIN SELECT RAISE(ABORT, 'reject_notice'); END")
            with pytest.raises(sqlite3.IntegrityError, match="reject_notice"):
                await finish_task_record(supervisor, "task", "completed")
            assert (await supervisor.get_task_snapshot("task")).status == "running"
            assert await inbox.read_task_terminals() == ()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(supervisor.runtime_queue.get(), 0.03)
            with closing(sqlite3.connect(path)) as db:
                db.execute("DROP TRIGGER reject_notice")
            assert await finish_task_record(supervisor, "task", "completed")
            record = await asyncio.wait_for(supervisor.runtime_queue.get(), 1)
            assert record.status == "completed" and record.exit_code is None
            assert len(await inbox.read_task_terminals()) == 1
            assert not await finish_task_record(supervisor, "task", "completed")
            await supervisor.close()
            await supervisor.records.close()
            assert supervisor.runtime_queue.empty()
        finally:
            await inbox.close()
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_hub_receives_background_status_but_not_foreground_status(tmp_path) -> None:
    """真实记录经 Scheduler 到达 Hub，前台终态仅提示检查而不显示后台状态。

    Args:
        tmp_path (Path): 隔离会话和任务数据库目录。
    """
    async def scenario():
        """先订阅会话，再分别完成前台和后台记录。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        scheduler = create_scheduler(tmp_path, supervisor)
        hub = runtime_hub(scheduler)
        await scheduler.start()
        pass
        stream = observe_session("s", hub, scheduler)
        try:
            assert (await anext(stream)).kind == "session_snapshot"
            await insert_task(supervisor, "foreground", background=False)
            await finish_task_record(supervisor, "foreground", "completed")
            await insert_task(supervisor, "background")
            await finish_task_record(supervisor, "background", "failed")
            event = await asyncio.wait_for(anext(stream), 1)
            assert event.kind == "task_status" and event.task_id == "background"
            assert event.payload == {
                "task_id": "background", "status": "failed", "exit_code": None,
                "tool_name": None, "error": None, "subagent_id": None,
            }
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(anext(stream), 0.03)
            assert (await scheduler.snapshot("s")).payload["phase"] == "idle"
        finally:
            await stream.aclose()
            await hub.close()
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("release_during_shutdown", [False, True])
def test_session_lock_does_not_block_process_cleanup_or_scheduler_close(tmp_path, release_during_shutdown) -> None:
    """消费任务等待会话锁时，进程照常收尾且 Scheduler 可以独立退出。

    Args:
        tmp_path (Path): 隔离进程和会话存储目录。
        release_during_shutdown (bool): 是否先封入口再放开会话锁，让消费者自行读到来源结束。
    """
    async def scenario():
        """用会话锁制造原回调的阻塞位置，验证公开启动与关闭均可结束。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        terminal_for_supervisor = TerminalHarness(supervisor, spool_dir=tmp_path / "tasks")
        scheduler = create_scheduler(tmp_path, supervisor)
        await scheduler.start()
        reader = scheduler._task_reader
        try:
            # 1. 输入受理锁由会话私有；普通进程与输出消费者均可独立完成。
            async with scheduler._parent("s")._lock:
                result = await asyncio.wait_for(terminal_for_supervisor.launch_terminal(
                    "s", "输出测试", [sys.executable, "-c", "print('done')"],
                    cwd=str(tmp_path), yield_time_ms=0), 3)
                await asyncio.wait_for(supervisor.close(), 3)
                if release_during_shutdown:
                    await scheduler.begin_shutdown()
                await asyncio.wait_for(scheduler.close(), 1)
                assert reader.done()
            inbox = SessionStateManager("s", SqliteFactory(str(tmp_path / "state.sqlite3")))
            try:
                notices = await inbox.read_task_terminals()
                assert len(notices) == 1 and notices[0].payload["task_id"] == result.task_id
            finally:
                await inbox.close()
        finally:
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_repeated_start_has_one_reader_and_source_close_ends_it(tmp_path) -> None:
    """重复启动不增加消费者，来源关闭主动结束等待，重复关闭没有遗留任务。

    Args:
        tmp_path (Path): 隔离存储目录。
    """
    async def scenario():
        """保留消费任务身份，观察真实队列结束而非取消测试替身。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        scheduler = create_scheduler(tmp_path, supervisor)
        try:
            await scheduler.start()
            reader = scheduler._task_reader
            await scheduler.start()
            assert scheduler._task_reader is reader and reader is not None
            await supervisor.close()
            await supervisor.records.close()
            supervisor.runtime_queue.put_nowait(None)
            await asyncio.wait_for(asyncio.shield(reader), 1)
            assert not reader.cancelled()
            await close_test_runtime(scheduler)
            await close_test_runtime(scheduler)
            assert reader.done()
        finally:
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_scheduler_close_does_not_wait_for_open_source(tmp_path) -> None:
    """Supervisor 尚未关闭时，Scheduler 独立取消记录等待并拒绝后续显示。

    Args:
        tmp_path (Path): 隔离存储目录。
    """
    async def scenario():
        """先关闭消费者，再向仍开放的真实来源保存一条终态记录。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        scheduler = create_scheduler(tmp_path, supervisor)
        try:
            await scheduler.start()
            reader = scheduler._task_reader
            await asyncio.sleep(0)
            assert not reader.done()
            await asyncio.wait_for(scheduler.close(), 1)
            assert reader.done()
            await insert_task(supervisor)
            assert await finish_task_record(supervisor, "task", "completed")
            await runtime_hub(scheduler).close()
            assert await next_runtime_event(scheduler) is None
            assert (await supervisor.runtime_queue.get()).task_id == "task"
        finally:
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())
