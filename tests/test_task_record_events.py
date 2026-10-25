"""验证任务记录的单向传递、持久事实和消费者生命周期。"""
from tests.terminal_harness import TerminalHarness

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
from app.services.channel_subscription_service import ChannelSubscriptionService


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
    return AgentRuntimeScheduler(runtime_queue=supervisor.runtime_queue,
        agent_factory=unexpected_agent,
        task_supervisor=supervisor,
        subscriptions=ChannelSubscriptionService(path),
        session_state_manager_factory=partial(SessionStateManager, db_path=path),
    )


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
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=path)
        terminal_for_supervisor = TerminalHarness(supervisor, spool_dir=tmp_path / "tasks")

        async def wait_terminal(task_id):
            """只通过查询等待操作系统进程的终态保存。

            Args:
                task_id (str): 等待完成的命令编号。

            Returns:
                TaskRecord: 已保存的进程终态。
            """
            while True:
                record = await supervisor.get_task_record(task_id)
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
            buffered = []
            while not supervisor.runtime_queue.empty():
                buffered.append(supervisor.runtime_queue.get_nowait())
            assert buffered and all(item.task_id == result.task_id for item in buffered)
            assert buffered[-1].status == "completed"
            assert supervisor.runtime_queue.empty()
            inbox = SessionStateManager("s", path)
            try:
                assert len(await inbox.read_task_terminals()) == 1
            finally:
                await inbox.close()
        finally:
            await supervisor.close()
    asyncio.run(scenario())


def test_terminal_event_follows_atomic_commit_and_is_not_repeated(tmp_path) -> None:
    """事务回滚时没有记录事件，重试成功才产生唯一终态记录和 Inbox 通知。

    Args:
        tmp_path (Path): 隔离 SQLite 目录。
    """
    async def scenario():
        """用真实 SQLite 触发器拒绝通知插入，并核对事件和持久事实。"""
        path = str(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=path)
        inbox = SessionStateManager("s", path)
        try:
            await insert_task(supervisor)
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TRIGGER reject_notice BEFORE INSERT ON session_inbox "
                           "BEGIN SELECT RAISE(ABORT, 'reject_notice'); END")
            with pytest.raises(sqlite3.IntegrityError, match="reject_notice"):
                await supervisor.commit_terminal_state_once("task", "completed")
            assert (await supervisor.get_task_record("task")).status == "running"
            assert await inbox.read_task_terminals() == ()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(supervisor.runtime_queue.get(), 0.03)
            with closing(sqlite3.connect(path)) as db:
                db.execute("DROP TRIGGER reject_notice")
            assert await supervisor.commit_terminal_state_once("task", "completed")
            record = await asyncio.wait_for(supervisor.runtime_queue.get(), 1)
            assert record.status == "completed" and record.exit_code is None
            assert len(await inbox.read_task_terminals()) == 1
            assert not await supervisor.commit_terminal_state_once("task", "completed")
            await supervisor.close()
            assert supervisor.runtime_queue.empty()
        finally:
            await inbox.close()
            await supervisor.close()
    asyncio.run(scenario())


def test_hub_receives_background_status_but_not_foreground_status(tmp_path) -> None:
    """真实记录经 Scheduler 到达 Hub，前台终态仅提示检查而不显示后台状态。

    Args:
        tmp_path (Path): 隔离会话和任务数据库目录。
    """
    async def scenario():
        """先订阅会话，再分别完成前台和后台记录。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "state.sqlite3"))
        scheduler = create_scheduler(tmp_path, supervisor)
        hub = InteractionEventHub(scheduler, subscriptions=scheduler.subscriptions)
        await scheduler.start()
        await hub.start()
        stream = hub.subscribe("s", "desktop")
        try:
            assert (await anext(stream)).kind == "session_snapshot"
            await insert_task(supervisor, "foreground", background=False)
            await supervisor.commit_terminal_state_once("foreground", "completed")
            await insert_task(supervisor, "background")
            await supervisor.commit_terminal_state_once("background", "failed")
            event = await asyncio.wait_for(anext(stream), 1)
            assert event.kind == "task_status" and event.task_id == "background"
            assert event.payload == {
                "task_id": "background", "status": "failed", "exit_code": None,
                "tool_name": None,
            }
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(anext(stream), 0.03)
            assert (await scheduler.snapshot("s")).payload["phase"] == "idle"
        finally:
            await stream.aclose()
            await hub.close()
            await scheduler.close()
            await supervisor.close()
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
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "state.sqlite3"))
        terminal_for_supervisor = TerminalHarness(supervisor, spool_dir=tmp_path / "tasks")
        scheduler = create_scheduler(tmp_path, supervisor)
        await scheduler.start()
        reader = scheduler._task_reader
        try:
            async with scheduler._session("s").lock:
                result = await asyncio.wait_for(terminal_for_supervisor.launch_terminal(
                    "s", "输出测试", [sys.executable, "-c", "print('done')"],
                    cwd=str(tmp_path), yield_time_ms=0,
                ), 3)
                await asyncio.wait_for(supervisor.close(), 3)
                assert reader is not None and not reader.done()
                if release_during_shutdown:
                    await scheduler.begin_shutdown()
                else:
                    await asyncio.wait_for(scheduler.close(), 1)
                    assert reader.done()
            if release_during_shutdown:
                supervisor.runtime_queue.put_nowait(None)
                # 1. 等锁期间已关闭执行入口，取得锁后也不能产生显示事件。
                await asyncio.wait_for(asyncio.shield(reader), 1)
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(scheduler.next_event(), 0.03)
                await scheduler.close()
            assert await scheduler.next_event() is None
            inbox = SessionStateManager("s", str(tmp_path / "state.sqlite3"))
            try:
                notices = await inbox.read_task_terminals()
                assert len(notices) == 1 and notices[0].payload["task_id"] == result.task_id
            finally:
                await inbox.close()
        finally:
            await scheduler.close()
            await supervisor.close()
    asyncio.run(scenario())


def test_repeated_start_has_one_reader_and_source_close_ends_it(tmp_path) -> None:
    """重复启动不增加消费者，来源关闭主动结束等待，重复关闭没有遗留任务。

    Args:
        tmp_path (Path): 隔离存储目录。
    """
    async def scenario():
        """保留消费任务身份，观察真实队列结束而非取消测试替身。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "state.sqlite3"))
        scheduler = create_scheduler(tmp_path, supervisor)
        try:
            await scheduler.start()
            reader = scheduler._task_reader
            await scheduler.start()
            assert scheduler._task_reader is reader and reader is not None
            await supervisor.close()
            supervisor.runtime_queue.put_nowait(None)
            await asyncio.wait_for(asyncio.shield(reader), 1)
            assert not reader.cancelled()
            await scheduler.close()
            await scheduler.close()
            assert reader.done()
        finally:
            await scheduler.close()
            await supervisor.close()
    asyncio.run(scenario())


def test_scheduler_close_does_not_wait_for_open_source(tmp_path) -> None:
    """Supervisor 尚未关闭时，Scheduler 独立取消记录等待并拒绝后续显示。

    Args:
        tmp_path (Path): 隔离存储目录。
    """
    async def scenario():
        """先关闭消费者，再向仍开放的真实来源保存一条终态记录。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "state.sqlite3"))
        scheduler = create_scheduler(tmp_path, supervisor)
        try:
            await scheduler.start()
            reader = scheduler._task_reader
            await asyncio.sleep(0)
            assert not reader.done()
            await asyncio.wait_for(scheduler.close(), 1)
            assert reader.done()
            await insert_task(supervisor)
            assert await supervisor.commit_terminal_state_once("task", "completed")
            assert await scheduler.next_event() is None
            assert (await supervisor.runtime_queue.get()).task_id == "task"
        finally:
            await scheduler.close()
            await supervisor.close()
    asyncio.run(scenario())
