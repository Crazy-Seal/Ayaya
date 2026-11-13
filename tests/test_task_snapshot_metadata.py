"""验证保存失败任务的查询、发布和输出读取使用已提交的终端资料。"""
from app.crud.sqlite_factory import SqliteFactory

import asyncio
from pathlib import Path
import sys
import sqlite3

import pytest

from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.runtime.tasks.base import BaseTask
from app.agent.runtime.tasks.runtime import TaskRuntime
from app.agent.tools.core.runtime import ToolRuntime
from app.agent.tools.terminal.service import TerminalService
from app.agent.tools.terminal.task import TerminalTask
from app.crud.task_record_dao import TaskRecordDao
from app.routes.agent import background_task_output
from app.schemas.runtime_types import TaskExecutionPhase, TaskStatus
from app.schemas.task_types import TaskRecord, TaskSnapshot


@pytest.mark.parametrize("background", [False, True])
def test_failed_save_preserves_terminal_metadata_without_reading_list_first(tmp_path: Path, background: bool) -> None:
    """真实终端保存结果失败后，单项查询、事件和输出读取立即包含已保存资料。

    Args:
        tmp_path (Path): 隔离数据库和终端输出目录。
        background (bool): 直接后台启动或前台启动后移交后台。
    """
    async def scenario() -> None:
        """仅阻断最终状态更新，保留真实进程、退出码和后台标记保存。"""
        records = TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3")))
        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=records)
        runtime = ToolRuntime("parent", "run", "exec_command", "call", supervisor, queue)
        db = await records._get_db()
        await db.execute("CREATE TRIGGER reject_result BEFORE UPDATE OF status ON task_records "
                         "BEGIN SELECT RAISE(ABORT, 'reject result'); END")
        await db.commit()
        task = TerminalTask("输出数字", [sys.executable, "-c", "print(123)"], str(tmp_path), False,
                            tmp_path / "tasks", records, TerminalService())
        try:
            task_id = await runtime.submit_task(task, name="terminal", title="输出数字",
                                               notify_agent=False, background=background)
            snapshot = await supervisor.wait(task_id, timeout_ms=5000)
            persisted = await records.get(task_id)
            assert snapshot.phase == TaskExecutionPhase.SAVE_FAILED
            assert snapshot.exit_code == persisted.exit_code == 0
            assert snapshot.pid == persisted.pid and snapshot.pid is not None
            assert snapshot.output_path == persisted.output_path and snapshot.output_path
            assert snapshot.output_ref == persisted.output_ref == task_id
            page = await background_task_output("parent", task_id, 0, 1024, supervisor)
            assert "123" in page["text"]
            # 1. 保存失败事件直接携带已落盘资料，消费端无需先查询列表。
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            failures = [event for event in events if isinstance(event, TaskSnapshot)
                        and event.phase == TaskExecutionPhase.SAVE_FAILED]
            assert len(failures) == 1 and failures[0].exit_code == 0
            assert failures[0].output_path == persisted.output_path
            # 2. 后台标记保存后立即查询和发布，列表查询始终只读。
            await runtime.mark_background(task_id)
            marked = await runtime.get_task_snapshot(task_id)
            published = queue.get_nowait()
            assert marked.background is published.background is True
            assert marked.exit_code == published.exit_code == 0
            listed = await supervisor.list_tasks("parent")
            assert listed == [marked]
            assert await runtime.get_task_snapshot(task_id) == marked
        finally:
            await supervisor.close()
            await records.close()

    asyncio.run(scenario())


def test_metadata_read_failure_still_closes_task_resources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """结果保存和资料查询同时失败时仍关闭任务，并记录查询错误。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 在任务登记后注入保存和读取故障。
        caplog (pytest.LogCaptureFixture): 核对实际错误日志保留原异常。
    """
    async def scenario() -> None:
        """登记真实任务后阻断存储，观察资源关闭、错误传播和活动数量。"""
        records = TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3")))
        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=records)
        runtime = ToolRuntime("parent", "run", "work", "call", supervisor, queue)
        release, cleaned = asyncio.Event(), asyncio.Event()
        read_error = sqlite3.OperationalError("disk I/O error while reading metadata")
        cleanups = 0

        class Work(BaseTask):
            """提供可观察的业务结束和资源关闭。"""

            async def run(self, runtime: TaskRuntime) -> str:
                """等待任务允许结束后返回结果。

                Args:
                    runtime (TaskRuntime): 本次任务的固定来源。
                Returns:
                    str: 本次任务的业务结果。
                """
                await release.wait()
                return "结果"

            async def cleanup(self) -> None:
                """记录实际资源关闭。"""
                nonlocal cleanups
                cleanups += 1
                cleaned.set()

        async def fail_save(task_id: str, status: TaskStatus, *,
                            result: str | None = None, error: str | None = None) -> TaskRecord | None:
            """模拟任务结果无法保存。

            Args:
                task_id (str): 需要保存结果的任务编号。
                status (TaskStatus): 已确定的业务结束状态。
                result (str | None): 业务结果正文。
                error (str | None): 业务失败说明。
            Raises:
                sqlite3.OperationalError: 模拟存储无法写入。
            """
            raise sqlite3.OperationalError("disk I/O error while saving result")

        async def fail_read(task_id: str) -> TaskRecord | None:
            """模拟无法查询任务资料。

            Args:
                task_id (str): 需要查询的任务编号。
            Raises:
                sqlite3.OperationalError: 模拟存储无法读取。
            """
            raise read_error

        try:
            task_id = await runtime.submit_task(Work(), name="work", title="工作", notify_agent=False)
            monkeypatch.setattr(records, "finish_once", fail_save)
            monkeypatch.setattr(records, "get", fail_read)
            release.set()
            with pytest.raises(sqlite3.OperationalError) as raised:
                await supervisor.wait(task_id, timeout_ms=1000)
            assert raised.value is read_error
            await asyncio.wait_for(cleaned.wait(), 1)
            await supervisor.close()
            assert cleanups == 1 and supervisor.active_task_count() == 0
            assert supervisor.get_task(task_id) is None
            assert any(entry.exc_info and entry.exc_info[1] is read_error for entry in caplog.records)
        finally:
            release.set()
            await supervisor.close()
            await records.close()

    asyncio.run(scenario())
