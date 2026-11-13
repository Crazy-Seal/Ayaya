"""验证任务资料保存共享事务确认，取消与超预算后仍返回实际结果。"""
from app.crud.sqlite_factory import SqliteFactory

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from app.crud.task_record_dao import TaskRecordDao
from app.crud.sqlite_save import SqliteRollbackError
from app.schemas.runtime_types import TaskStatus
from app.schemas.subagent_types import SubagentDefinition
from app.agent.runtime.tasks.base import BaseTask
from app.agent.runtime.tasks.runtime import TaskRuntime
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.tools.core.runtime import ToolRuntime


@pytest.mark.parametrize("operation", ["background", "terminal", "exit_code", "subagent"])
def test_metadata_commit_survives_repeated_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    """终端资料和后台标记开始提交后，多次取消仍等待真实成功。

    Args:
        tmp_path (Path): 隔离任务数据库目录。
        monkeypatch (pytest.MonkeyPatch): 为真实提交安排可释放的屏障。
        operation (str): 本次需要提交的任务资料。
    """
    async def scenario() -> None:
        """阻塞提交并多次取消保存，放行后核对实际已提交字段。"""
        records = TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3")))
        await records.insert_running("task", "parent", title="工作", background=False)
        db = await records._get_db()
        entered, release = asyncio.Event(), asyncio.Event()
        original_commit = db.commit

        async def commit() -> None:
            """等待放行后提交同一笔任务资料。"""
            entered.set()
            await release.wait()
            await original_commit()

        async def save() -> None:
            """调用一次具名资料保存方法。"""
            if operation == "background":
                await records.mark_background("task")
            elif operation == "terminal":
                await records.attach_terminal("task", command="输出", pid=123, output_path="output.txt")
            elif operation == "exit_code":
                await records.save_terminal_exit_code("task", 0)
            else:
                definition = SubagentDefinition("child", "parent", "子任务", "任务", frozenset(),
                                                False, datetime.now(timezone.utc))
                await records.start_subagent_run(definition, "run", None)

        monkeypatch.setattr(db, "commit", commit)
        saving = asyncio.create_task(save())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            for _ in range(3):
                saving.cancel()
                await asyncio.sleep(0)
            assert not saving.done()
            release.set()
            await saving
            record = await records.get("task")
            assert record is not None
            if operation == "background":
                assert record.background
            elif operation == "terminal":
                assert (record.pid, record.output_path, record.command) == (123, "output.txt", "输出")
            elif operation == "exit_code":
                assert record.exit_code == 0
            else:
                assert (await records.get("run")).subagent_id == "child"
        finally:
            release.set()
            await asyncio.gather(saving, return_exceptions=True)
            await records.close()

    asyncio.run(scenario())


def test_supervisor_shutdown_waits_for_commit_then_closes_task_and_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """退出等待关键提交的实际成功，再完成一次结果通知和资源关闭。

    Args:
        tmp_path (Path): 隔离任务数据库目录。
        monkeypatch (pytest.MonkeyPatch): 阻塞提交并缩短后续尝试预算。
        caplog (pytest.LogCaptureFixture): 检查真实成功没有被报告为保存失败。
    """
    async def scenario() -> None:
        """实际任务完成后阻塞提交，启动退出并观察保存与关闭顺序。"""
        records = TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3")))
        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=records)
        runtime = ToolRuntime("parent", "run", "work", "call", supervisor, queue)
        release_work, entered, release_commit = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order: list[str] = []
        runs = 0

        class Work(BaseTask):
            """记录一次业务执行与具体任务资源关闭。"""

            async def run(self, runtime: TaskRuntime) -> str:
                """等待放行后交付原始任务结果。

                Args:
                    runtime (TaskRuntime): 当前任务的固定来源。
                Returns:
                    str: 本次已准备的业务结果。
                """
                nonlocal runs
                runs += 1
                await release_work.wait()
                return "结果"

            async def cleanup(self) -> None:
                """在结果提交后关闭任务资源。"""
                order.append("task_closed")

        task_id = await runtime.submit_task(Work(), name="work", title="工作", notify_agent=True)
        db = await records._get_db()
        original_commit = db.commit

        async def commit() -> None:
            """等待放行后确认当前结果和通知提交成功。"""
            entered.set()
            await release_commit.wait()
            await original_commit()
            order.append("committed")

        async def shutdown() -> None:
            """按应用顺序等待任务结束，随后关闭存储。"""
            await supervisor.close()
            await records.close()
            order.append("storage_closed")

        monkeypatch.setattr(db, "commit", commit)
        monkeypatch.setattr("app.crud.task_record_dao.TASK_SAVE_TIMEOUT_SECONDS", 0.05)
        closing: asyncio.Task[None] | None = None
        try:
            release_work.set()
            await asyncio.wait_for(entered.wait(), 1)
            closing = asyncio.create_task(shutdown())
            done, _ = await asyncio.wait({closing}, timeout=0.1)
            assert not done and order == []
            release_commit.set()
            await asyncio.wait_for(closing, 1)
            assert order == ["committed", "task_closed", "storage_closed"] and runs == 1
            with sqlite3.connect(records.db_path) as inspection:
                assert inspection.execute("SELECT status FROM task_records WHERE task_id=?", (task_id,)).fetchone()[0] == TaskStatus.COMPLETED
                assert inspection.execute("SELECT count(*) FROM session_inbox").fetchone()[0] == 1
            assert not any("后台任务结果保存失败" in entry.getMessage() for entry in caplog.records)
        finally:
            release_work.set()
            release_commit.set()
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            await supervisor.close()
            await records.close()

    asyncio.run(scenario())


def test_failed_rollback_discards_connection_before_queries_and_later_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回滚失败后读到原已提交资料，后续保存可重新建连且仅通知一次。

    Args:
        tmp_path (Path): 隔离任务数据库目录。
        monkeypatch (pytest.MonkeyPatch): 模拟提交和回滚都发生存储故障。
    """
    async def scenario() -> None:
        """让事务已有未提交写入，故障后观察查询和后续保存。"""
        records = TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3")))
        await records.insert_running("task", "parent", title="工作", background=False)
        db = await records._get_db()
        commits = rollbacks = 0
        failure = sqlite3.OperationalError("database is locked during rollback")
        failure.sqlite_errorcode = sqlite3.SQLITE_BUSY

        async def commit() -> None:
            """模拟当前提交明确失败。

            Raises:
                sqlite3.OperationalError: 提交发生存储故障。
            """
            nonlocal commits
            commits += 1
            raise sqlite3.OperationalError("disk I/O error during commit")

        async def rollback() -> None:
            """模拟回滚也失败，连接需要退出使用。

            Raises:
                sqlite3.OperationalError: 本次回滚未完成。
            """
            nonlocal rollbacks
            rollbacks += 1
            raise failure

        monkeypatch.setattr(db, "commit", commit)
        monkeypatch.setattr(db, "rollback", rollback)
        try:
            with pytest.raises(SqliteRollbackError) as raised:
                await records.mark_background("task")
            assert raised.value.__cause__ is failure
            assert commits == rollbacks == 1
            assert not (await records.get("task")).background
            assert not (await records.list("parent"))[0].background
            record = await records.finish_once("task", TaskStatus.COMPLETED, result="结果")
            assert record.status == TaskStatus.COMPLETED
            with sqlite3.connect(records.db_path) as inspection:
                assert inspection.execute("SELECT count(*) FROM session_inbox").fetchone()[0] == 1
            assert await records.finish_once("task", TaskStatus.COMPLETED) is None
        finally:
            await records.close()

    asyncio.run(scenario())


def test_commit_failure_after_cancellation_preserves_original_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认期间取消后若提交实际失败，调用者得到原始存储错误。

    Args:
        tmp_path (Path): 隔离任务数据库目录。
        monkeypatch (pytest.MonkeyPatch): 在提交确认处注入不可重试故障。
    """
    async def scenario() -> None:
        """取消保存后放行失败提交，核对错误和真实回滚结果。"""
        records = TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3")))
        db = await records._get_db()
        entered, release = asyncio.Event(), asyncio.Event()
        failure = sqlite3.OperationalError("disk I/O error during commit")

        async def commit() -> None:
            """等待确认后返回提交的实际失败原因。

            Raises:
                sqlite3.OperationalError: 本次提交发生存储故障。
            """
            entered.set()
            await release.wait()
            raise failure

        monkeypatch.setattr(db, "commit", commit)
        saving = asyncio.create_task(records.insert_running("task", "parent", title="工作"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            saving.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(sqlite3.OperationalError) as raised:
                await saving
            assert raised.value is failure
            assert await records.get("task") is None
        finally:
            release.set()
            await asyncio.gather(saving, return_exceptions=True)
            await records.close()

    asyncio.run(scenario())
