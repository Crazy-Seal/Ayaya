"""验证任务记录的单向传递、持久事实和消费者生命周期。"""

import asyncio
from contextlib import closing
from functools import partial
import sqlite3
import sys

import pytest

from app.agent.state.manager import SessionStateManager
from app.interaction.interaction_event_hub import InteractionEventHub
from app.orchestration.agent_runtime_scheduler import AgentRuntimeScheduler
from app.orchestration.task_supervisor import TaskSupervisor
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
    return AgentRuntimeScheduler(
        agent_factory=unexpected_agent,
        task_supervisor=supervisor,
        subscriptions=ChannelSubscriptionService(path),
        session_state_manager_factory=partial(SessionStateManager, db_path=path),
    )


async def insert_task(supervisor, task_id="task", *, background=True):
    """登记没有真实进程的任务，供测试通过正式终态事务结束。

    Args:
        supervisor (TaskSupervisor): 持有隔离任务数据库的对象。
        task_id (str): 本次登记的唯一任务编号。
        background (bool): 是否应在后台任务入口显示。
    """
    await supervisor.records.insert_running(
        task_id, "s", "测试任务", 1, str(supervisor.spool_dir / task_id),
        background=background, tool_name="exec_command",
    )


def test_supervisor_finishes_without_consumer_and_preserves_buffer(tmp_path) -> None:
    """没有消费者时仍完成真实进程，关闭后按序读取积压记录和结束标记。

    Args:
        tmp_path (Path): 隔离数据库和日志目录。
    """
    async def scenario():
        """启动后台命令，先验证持久终态和资源收尾，再读取记录来源。"""
        path = str(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(db_path=path, spool_dir=tmp_path / "tasks")

        async def wait_terminal(task_id):
            """只通过查询等待操作系统进程的终态保存。

            Args:
                task_id (str): 等待完成的命令编号。

            Returns:
                TaskRecord: 已保存的进程终态。
            """
            while True:
                record = await supervisor.get(task_id)
                if record.status != "running":
                    return record
                await asyncio.sleep(0.01)

        try:
            result = await supervisor.launch(
                "s", "输出测试", [sys.executable, "-c", "print('done')"],
                cwd=str(tmp_path), yield_time_ms=0,
            )
            record = await asyncio.wait_for(wait_terminal(result.task_id), 3)
            assert record.status == "completed"
            assert "done" in (await supervisor.read_output(result.task_id, 0, 1024))["text"]
            await asyncio.wait_for(supervisor.close(), 3)
            await supervisor.close()
            buffered = []
            while (item := await asyncio.wait_for(supervisor.next_event(), 1)) is not None:
                buffered.append(item)
            assert buffered and all(item.task_id == result.task_id for item in buffered)
            assert buffered[-1].status == "completed"
            assert await asyncio.wait_for(supervisor.next_event(), 1) is None
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
        supervisor = TaskSupervisor(db_path=path, spool_dir=tmp_path / "tasks")
        inbox = SessionStateManager("s", path)
        try:
            await insert_task(supervisor)
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TRIGGER reject_notice BEFORE INSERT ON session_inbox "
                           "BEGIN SELECT RAISE(ABORT, 'reject_notice'); END")
            with pytest.raises(sqlite3.IntegrityError, match="reject_notice"):
                await supervisor.finish_once("task", "completed", 0)
            assert (await supervisor.get("task")).status == "running"
            assert await inbox.read_task_terminals() == ()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(supervisor.next_event(), 0.03)
            with closing(sqlite3.connect(path)) as db:
                db.execute("DROP TRIGGER reject_notice")
            assert await supervisor.finish_once("task", "completed", 0)
            record = await asyncio.wait_for(supervisor.next_event(), 1)
            assert record.status == "completed" and record.exit_code == 0
            assert len(await inbox.read_task_terminals()) == 1
            assert not await supervisor.finish_once("task", "completed", 0)
            await supervisor.close()
            assert await supervisor.next_event() is None
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
        supervisor = TaskSupervisor(db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks")
        scheduler = create_scheduler(tmp_path, supervisor)
        hub = InteractionEventHub(scheduler, subscriptions=scheduler.subscriptions)
        await scheduler.start()
        await hub.start()
        stream = hub.subscribe("s", "desktop")
        try:
            assert (await anext(stream)).kind == "session_snapshot"
            await insert_task(supervisor, "foreground", background=False)
            await supervisor.finish_once("foreground", "completed", 0)
            await insert_task(supervisor, "background")
            await supervisor.finish_once("background", "failed", 2)
            event = await asyncio.wait_for(anext(stream), 1)
            assert event.kind == "task_status" and event.task_id == "background"
            assert event.payload == {
                "task_id": "background", "status": "failed", "exit_code": 2,
                "tool_name": "exec_command",
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
        supervisor = TaskSupervisor(db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks")
        scheduler = create_scheduler(tmp_path, supervisor)
        await scheduler.start()
        reader = scheduler._task_reader
        try:
            async with scheduler._session("s").lock:
                result = await asyncio.wait_for(supervisor.launch(
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
        supervisor = TaskSupervisor(db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks")
        scheduler = create_scheduler(tmp_path, supervisor)
        try:
            await scheduler.start()
            reader = scheduler._task_reader
            await scheduler.start()
            assert scheduler._task_reader is reader and reader is not None
            await supervisor.close()
            await asyncio.wait_for(asyncio.shield(reader), 1)
            assert not reader.cancelled()
            await scheduler.close()
            await scheduler.close()
            assert reader.done()
        finally:
            await scheduler.close()
            await supervisor.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("buffered", [False, True])
def test_storage_close_failure_still_ends_record_source(tmp_path, monkeypatch, buffered) -> None:
    """数据库关闭报告失败时保留积压记录，并结束正在等待的消费者。

    Args:
        tmp_path (Path): 隔离存储目录。
        monkeypatch (pytest.MonkeyPatch): 只替换数据库关闭的报告结果。
        buffered (bool): 关闭前是否有终态积压，分别覆盖等待中的读取和积压读取。
    """
    async def scenario():
        """先产生终态记录，再注入关闭失败并按公开接口读取到结束。"""
        supervisor = TaskSupervisor(db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks")
        original_close = supervisor.records.close

        async def failing_close():
            """实际释放测试数据库后报告错误，避免故障测试遗留句柄。

            Raises:
                RuntimeError: 模拟存储关闭报告失败。
            """
            await original_close()
            raise RuntimeError("存储关闭失败")

        try:
            if buffered:
                await insert_task(supervisor)
                await supervisor.finish_once("task", "completed", 0)
            waiting = asyncio.create_task(supervisor.next_event())
            await asyncio.sleep(0)
            monkeypatch.setattr(supervisor.records, "close", failing_close)
            with pytest.raises(RuntimeError, match="存储关闭失败"):
                await supervisor.close()
            record = await asyncio.wait_for(waiting, 1)
            if buffered:
                assert record.status == "completed"
            else:
                assert record is None
            assert await asyncio.wait_for(supervisor.next_event(), 1) is None
            assert await asyncio.wait_for(supervisor.next_event(), 1) is None
        finally:
            monkeypatch.setattr(supervisor.records, "close", original_close)
            await supervisor.close()
    asyncio.run(scenario())


def test_scheduler_close_does_not_wait_for_open_source(tmp_path) -> None:
    """Supervisor 尚未关闭时，Scheduler 独立取消记录等待并拒绝后续显示。

    Args:
        tmp_path (Path): 隔离存储目录。
    """
    async def scenario():
        """先关闭消费者，再向仍开放的真实来源保存一条终态记录。"""
        supervisor = TaskSupervisor(db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks")
        scheduler = create_scheduler(tmp_path, supervisor)
        try:
            await scheduler.start()
            reader = scheduler._task_reader
            await asyncio.sleep(0)
            assert not reader.done()
            await asyncio.wait_for(scheduler.close(), 1)
            assert reader.done()
            await insert_task(supervisor)
            assert await supervisor.finish_once("task", "completed", 0)
            assert await scheduler.next_event() is None
            assert (await supervisor.next_event()).task_id == "task"
        finally:
            await scheduler.close()
            await supervisor.close()
    asyncio.run(scenario())


def test_reader_failure_is_reported_and_cleanup_does_not_restart_it(tmp_path, monkeypatch, caplog) -> None:
    """记录读取意外失败时明确报告，最终关闭检查失败且不自动重启。

    Args:
        tmp_path (Path): 隔离存储目录。
        monkeypatch (pytest.MonkeyPatch): 在记录读取边界注入异常。
        caplog (pytest.LogCaptureFixture): 检查运行和关闭阶段的错误报告。
    """
    async def scenario():
        """让唯一消费者失败，再通过正式关闭接口回收它。"""
        supervisor = TaskSupervisor(db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks")
        scheduler = create_scheduler(tmp_path, supervisor)
        entered = asyncio.Event()
        calls = 0

        async def failing_read():
            """标记读取发生后抛出异常。

            Raises:
                RuntimeError: 模拟记录来源异常。
            """
            nonlocal calls
            calls += 1
            entered.set()
            raise RuntimeError("记录读取失败")

        monkeypatch.setattr(supervisor, "next_event", failing_read)
        try:
            await scheduler.start()
            reader = scheduler._task_reader
            await asyncio.wait_for(entered.wait(), 1)
            await scheduler.close()
            assert reader.done() and calls == 1
            assert "读取任务记录失败" in caplog.text
            assert "任务记录消费者异常退出" in caplog.text
        finally:
            await scheduler.close()
            await supervisor.close()
    asyncio.run(scenario())
