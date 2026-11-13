"""以真实 SQLite 占用验证任务结果的有限保存和重启整理。"""
from app.crud.sqlite_factory import SqliteFactory

import asyncio
from pathlib import Path
import sqlite3

import pytest

from app.agent.runtime.tasks.base import BaseTask
from app.agent.runtime.tasks.runtime import TaskRuntime
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.state.manager import SessionStateManager
from app.agent.tools.core.runtime import ToolRuntime
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.runtime_types import CommandStatus, TaskStatus, TaskExecutionPhase
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.task_types import TaskRecord


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("outcome", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED])
def test_terminal_transaction_recovers_without_repeating_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: bool, outcome: TaskStatus,
) -> None:
    """前三次 SQLite 占用失败后第四次保存原结果，业务只执行一次。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 在真实事务失败处安排放行。
        child (bool): 是否将结果交给子 Inbox。
        outcome (TaskStatus): 业务原始结果类别。
    """
    async def scenario() -> None:
        """持有真实数据库写锁，在第三次回滚后放行第四次保存。"""
        records = TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3")))
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=records)
        runtime = ToolRuntime("parent", "run", "work", "call", supervisor, supervisor.runtime_queue,
                              subagent_id="child" if child else None)
        inbox = SessionStateManager("child" if child else "parent", SqliteFactory(records.db_path),
            storage_scope=AgentStorageScope.SUBAGENT if child else AgentStorageScope.MAIN)
        started, release, rejected, retry, cleaned = (asyncio.Event() for _ in range(5))
        runs = attempts = cleanups = 0
        original = records._finish_once

        async def finish(*args, **kwargs) -> TaskRecord | None:
            """计数真实事务，第三次锁冲突返回前等待测试允许继续。"""
            nonlocal attempts
            attempts += 1
            try:
                return await original(*args, **kwargs)
            except sqlite3.OperationalError:
                if attempts == 3:
                    rejected.set()
                    await retry.wait()
                raise

        class Work(BaseTask):
            """只执行一次业务并记录实际清理次数。"""

            async def run(self, runtime: TaskRuntime) -> str:
                """等待放行，返回原结果或抛出业务错误。"""
                nonlocal runs
                runs += 1
                started.set()
                await release.wait()
                if outcome == TaskStatus.FAILED:
                    raise ValueError("业务原始错误")
                return "业务原始结果"

            async def cleanup(self) -> None:
                """确认具体资源关闭一次。"""
                nonlocal cleanups
                cleanups += 1
                cleaned.set()

        monkeypatch.setattr(records, "_finish_once", finish)
        blocker = sqlite3.connect(records.db_path)
        try:
            task_id = await supervisor.submit(Work(), runtime=runtime, name="work", title="工作", notify_agent=True)
            await asyncio.wait_for(started.wait(), 3)
            await inbox.read_task_terminals()
            blocker.execute("BEGIN IMMEDIATE")
            if outcome == TaskStatus.STOPPED:
                assert (await supervisor.stop_task(task_id)).accepted
            else:
                release.set()
            await asyncio.wait_for(rejected.wait(), 3)
            assert (await supervisor.wait(task_id, timeout_ms=0)).phase == TaskExecutionPhase.COMMITTING
            assert supervisor.active_task_count() == 1
            assert not await inbox.read_task_terminals()
            assert (await supervisor.stop_task(task_id)).status == CommandStatus.TOO_LATE
            blocker.rollback()
            retry.set()
            record = await supervisor.wait(task_id, timeout_ms=3000)
            await asyncio.wait_for(cleaned.wait(), 1)
            assert record.status == outcome
            assert record.result == ("业务原始结果" if outcome == TaskStatus.COMPLETED else None)
            assert record.error == ("业务原始错误" if outcome == TaskStatus.FAILED else None)
            assert runs == cleanups == 1 and attempts == 4
            notices = await inbox.read_task_terminals()
            assert len(notices) == 1
            assert notices[0].payload["status"] == outcome
            assert not await records.finish_once(task_id, outcome)
            assert len(await inbox.read_task_terminals()) == 1
        finally:
            blocker.rollback()
            blocker.close()
            retry.set()
            release.set()
            await supervisor.close()
            await records.close()
            await inbox.close()

    asyncio.run(scenario())


def test_persistent_terminal_failure_does_not_block_shutdown_or_restart_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """四次占用失败后查询显示保存失败，资源释放，重启按 orphaned 整理。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 统计真实事务尝试次数。
    """
    async def scenario() -> None:
        """持续持有写锁直到保存耗尽，再验证进程摘要和数据库结果的区别。"""
        records = TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3")))
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=records)
        runtime = ToolRuntime("parent", "run", "work", "call", supervisor, supervisor.runtime_queue)
        release, cleaned = asyncio.Event(), asyncio.Event()
        attempts = 0
        original = records._finish_once

        async def finish(*args, **kwargs) -> TaskRecord | None:
            """统计同一终态的真实尝试。"""
            nonlocal attempts
            attempts += 1
            return await original(*args, **kwargs)

        class Work(BaseTask):
            """业务成功后等待验证其结果保存失败。"""

            async def run(self, runtime: TaskRuntime) -> str:
                """返回一次原始结果。"""
                await release.wait()
                return "原始结果"

            async def cleanup(self) -> None:
                """报告实际资源释放。"""
                cleaned.set()

        monkeypatch.setattr(records, "_finish_once", finish)
        blocker = sqlite3.connect(records.db_path)
        inbox = SessionStateManager("parent", SqliteFactory(records.db_path))
        try:
            task_id = await supervisor.submit(Work(), runtime=runtime, name="work", title="工作", notify_agent=True)
            await inbox.read_task_terminals()
            blocker.execute("BEGIN IMMEDIATE")
            release.set()
            snapshot = await supervisor.wait(task_id, timeout_ms=3000)
            await asyncio.wait_for(cleaned.wait(), 1)
            assert snapshot.status == TaskExecutionPhase.SAVE_FAILED
            assert snapshot.phase == TaskExecutionPhase.SAVE_FAILED and snapshot.save_error
            assert snapshot.result == "原始结果"
            assert (await supervisor.list_tasks("parent"))[0] == snapshot
            assert supervisor.active_task_count() == 0 and supervisor.get_task(task_id) is None
            assert attempts == 4
            assert (await records.get(task_id)).status == TaskStatus.RUNNING
            assert not await inbox.read_task_terminals()
            await supervisor.close()
            blocker.rollback()
            restarted = TaskSupervisor(runtime_queue=asyncio.Queue(), records=records)
            await restarted.recover_orphaned()
            await restarted.recover_orphaned()
            assert (await restarted.get_task_snapshot(task_id)).status == TaskStatus.ORPHANED
            assert len(await inbox.read_task_terminals()) == 1
            await restarted.close()
        finally:
            blocker.rollback()
            blocker.close()
            release.set()
            await supervisor.close()
            await records.close()
            await inbox.close()

    asyncio.run(scenario())
