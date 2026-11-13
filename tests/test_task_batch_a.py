"""验证任务接管、真实事务取消、迟到通知和有界资源关闭。"""
from app.crud.sqlite_factory import SqliteFactory

import asyncio
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from app.agent.runtime.tasks.base import BaseTask
from app.agent.runtime.tasks.runtime import TaskRuntime
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.tools.core.runtime import ToolRuntime
from app.agent.tools.terminal.task import TerminalTask
from app.agent.utils.infra.bounded_operation import OperationTimeoutError
from app.agent.state.manager import SessionStateManager
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.runtime_types import CommandStatus, TaskStatus
from app.schemas.agent_storage import AgentStorageScope


def test_stop_before_runner_start_saves_once_without_running_business(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """登记返回前停止两次，业务尚未调度，最终仅有一条停止通知。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 在登记发布后的协程创建处安排立即停止。
    """
    async def scenario() -> None:
        """登记期间固定任务调度顺序，再通过公开停止和等待核对结果。"""
        records = TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3")))
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=records)
        runtime = ToolRuntime("parent", "run", "work", "call", supervisor, supervisor.runtime_queue)
        runs = cleanups = 0

        class Work(BaseTask):
            """记录具体业务和资源关闭次数。"""

            async def run(self, runtime: TaskRuntime) -> str:
                """记录模型工具尚未开始前就应被停止的工作。"""
                nonlocal runs
                runs += 1
                return "不应执行"

            async def cleanup(self) -> None:
                """释放已登记任务的准备资源。"""
                nonlocal cleanups
                cleanups += 1

        original = supervisor._run_task

        async def start(execution) -> None:
            """runner 第一条业务前通过公开入口安排重复停止。"""
            assert (await supervisor.stop_task(execution.record.task_id)).accepted
            assert (await supervisor.stop_task(execution.record.task_id)).accepted
            await original(execution)

        monkeypatch.setattr(supervisor, "_run_task", start)
        inbox = SessionStateManager("parent", SqliteFactory(records.db_path))
        try:
            task_id = await supervisor.submit(Work(), runtime=runtime, name="work", title="工作", notify_agent=True)
            record = await supervisor.wait(task_id, timeout_ms=2000)
            await supervisor.close()
            assert record.status == TaskStatus.STOPPED
            assert runs == 0 and cleanups == 1
            assert len(await inbox.read_task_terminals()) == 1
            assert (await supervisor.stop_task(task_id)).status == CommandStatus.TOO_LATE
            assert (await supervisor.stop_task("missing")).status == CommandStatus.NOT_FOUND
        finally:
            await supervisor.close()
            await records.close()
            await inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
@pytest.mark.parametrize("register", [False, True])
def test_real_sqlite_statement_settles_rollback_before_reporting_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, register: bool,
) -> None:
    """SQLite 线程中的写语句尚未退出时，保存所有者等待实际回滚。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 缩短本次保存预算并标识回滚开始。
        mode (str): 总预算耗尽或调用者取消。
        register (bool): 初始登记或最终结果事务。
    """
    async def scenario() -> None:
        """由真实触发器阻塞 SQLite 工作线程，取消后检查实际数据库。"""
        records = TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3")))
        entered, release = threading.Event(), threading.Event()
        rolling_back = asyncio.Event()
        db = await records._get_db()
        original_rollback = db.rollback

        def pause() -> int:
            """在 SQLite 线程中等待测试放行当前语句。"""
            entered.set()
            if not release.wait(3):
                raise RuntimeError("测试未放行 SQLite")
            return 0

        async def rollback() -> None:
            """报告协程已开始回滚，实际回滚排在阻塞语句之后。"""
            rolling_back.set()
            await original_rollback()

        monkeypatch.setattr(db, "rollback", rollback)
        monkeypatch.setattr("app.crud.task_record_dao.TASK_SAVE_TIMEOUT_SECONDS", 0.05 if mode == "timeout" else 5)
        await db.create_function("pause_task_save", 0, pause)
        if not register:
            await records.insert_running("task", "parent", title="工作")
        operation = "INSERT" if register else "UPDATE"
        await db.execute(f"CREATE TRIGGER pause_save BEFORE {operation} ON task_records BEGIN SELECT pause_task_save(); END")
        await db.commit()
        saving = asyncio.create_task(records.insert_running("task", "parent", title="工作") if register else
                                     records.finish_once("task", TaskStatus.COMPLETED, result="结果"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            if mode == "cancel":
                saving.cancel()
            await asyncio.wait_for(rolling_back.wait(), 2)
            assert not saving.done()
            release.set()
            with pytest.raises(OperationTimeoutError if mode == "timeout" else asyncio.CancelledError):
                await saving
            # 1. 保存返回失败前实际回滚已经完成，随后查询始终保留原状态。
            record = await records.get("task")
            assert record is None if register else record.status == TaskStatus.RUNNING
            with sqlite3.connect(records.db_path) as inspection:
                assert inspection.execute("SELECT count(*) FROM session_inbox").fetchone()[0] == 0
        finally:
            release.set()
            await asyncio.gather(saving, return_exceptions=True)
            await records.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
@pytest.mark.parametrize("register", [False, True])
def test_commit_in_progress_returns_actual_success_after_timeout_or_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, register: bool,
) -> None:
    """提交已开始时等待真实提交结果，避免先报失败后旧写入生效。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 在真实提交前设置可控屏障。
        mode (str): 总预算耗尽或调用者取消。
        register (bool): 初始登记或最终结果事务。
    """
    async def scenario() -> None:
        """保持真实 commit，只延迟其返回，随后核对持久结果与唯一通知。"""
        records = TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3")))
        db = await records._get_db()
        if not register:
            await records.insert_running("task", "parent", title="工作")
        entered, release = asyncio.Event(), asyncio.Event()
        original_commit = db.commit

        async def commit() -> None:
            """在同一个提交任务中等待测试后执行实际 SQLite 提交。"""
            entered.set()
            await release.wait()
            await original_commit()

        monkeypatch.setattr(db, "commit", commit)
        monkeypatch.setattr("app.crud.task_record_dao.TASK_SAVE_TIMEOUT_SECONDS", 0.2 if mode == "timeout" else 5)
        saving = asyncio.create_task(records.insert_running("task", "parent", title="工作") if register else
                                     records.finish_once("task", TaskStatus.COMPLETED, result="结果"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if mode == "cancel":
                saving.cancel()
            # 1. 超过保存预算仍等待提交确认，当前任务的结果保持未交付。
            done, _ = await asyncio.wait({saving}, timeout=0.35)
            assert not done
            release.set()
            result = await asyncio.wait_for(saving, 1)
            assert result is None if register else result.status == TaskStatus.COMPLETED
            assert (await records.get("task")).status == (TaskStatus.RUNNING if register else TaskStatus.COMPLETED)
            if not register:
                with sqlite3.connect(records.db_path) as inspection:
                    assert inspection.execute("SELECT count(*) FROM session_inbox").fetchone()[0] == 1
                assert not await records.finish_once("task", TaskStatus.COMPLETED)
        finally:
            release.set()
            await asyncio.gather(saving, return_exceptions=True)
            await records.close()

    asyncio.run(scenario())


def test_active_summary_uses_real_supervisor_and_excludes_cleanup(tmp_path: Path) -> None:
    """真实活动查询在运行时返回一，在结果已交付但清理阻塞时返回零。

    Args:
        tmp_path (Path): 隔离存储目录。
    """
    from main import active_task_summary
    from fastapi import FastAPI, Request

    async def scenario() -> None:
        """使用真实端点函数、请求和管理器查询运行及清理两个时点。"""
        records = TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3")))
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=records)
        runtime = ToolRuntime("parent", "run", "work", "call", supervisor, supervisor.runtime_queue)
        release_run, cleaning, release_cleanup = (asyncio.Event() for _ in range(3))

        class Work(BaseTask):
            """分别控制业务与资源关闭的完成时间。"""

            async def run(self, runtime: TaskRuntime) -> str:
                """等待测试放行后完成业务。"""
                await release_run.wait()
                return "结果"

            async def cleanup(self) -> None:
                """结果可读后保持资源关闭等待。"""
                cleaning.set()
                await release_cleanup.wait()

        app = FastAPI()
        app.state.task_supervisor = supervisor
        request = Request({"type": "http", "app": app})
        try:
            task_id = await supervisor.submit(Work(), runtime=runtime, name="work", title="工作", notify_agent=False)
            assert await active_task_summary(request) == {"active_count": 1}
            release_run.set()
            await asyncio.wait_for(cleaning.wait(), 2)
            assert (await supervisor.wait(task_id, timeout_ms=100)).status == TaskStatus.COMPLETED
            assert await active_task_summary(request) == {"active_count": 0}
            closing = asyncio.create_task(supervisor.close())
            await asyncio.sleep(0)
            assert not closing.done()
            release_cleanup.set()
            await closing
        finally:
            release_run.set()
            release_cleanup.set()
            await supervisor.close()
            await records.close()

    asyncio.run(scenario())


def test_terminal_cleanup_limits_retries_without_changing_saved_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """具体终端关闭四次失败后结束等待，持久成功结果保持成功。

    Args:
        tmp_path (Path): 隔离存储目录。
        monkeypatch (pytest.MonkeyPatch): 注入具体资源关闭故障。
    """
    async def scenario() -> None:
        """单独调用真实终端 cleanup，核对实际资源关闭次数和持久结果。"""
        records = TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3")))
        attempts = 0

        async def dispose(process) -> None:
            """模拟同一终端资源持续无法关闭。"""
            nonlocal attempts
            attempts += 1
            raise OSError("终端关闭失败")

        monkeypatch.setattr("app.agent.tools.terminal.task.dispose_terminal", dispose)
        try:
            await records.insert_running("task", "parent", title="工作")
            await records.finish_once("task", TaskStatus.COMPLETED, result="结果")
            task = TerminalTask("测试", [], str(tmp_path), False, tmp_path, records, SimpleNamespace())
            task.process = SimpleNamespace()
            await task.cleanup()
            assert attempts == 4
            assert (await records.get("task")).status == TaskStatus.COMPLETED
        finally:
            await records.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("parent_delete", [False, True])
def test_result_and_notice_survive_closed_recipient_without_restarting_agent(tmp_path: Path, parent_delete: bool) -> None:
    """父删除或子废弃后才保存任务结果，通知仍存在，Agent 保持关闭。

    Args:
        tmp_path (Path): 隔离数据库目录。
        parent_delete (bool): 删除父配置或废弃子身份。
    """
    from app.agent.models.contracts import StreamChunk
    from app.schemas.subagent_types import SubagentAction
    from app.services.chat_settings_service import ChatSettingsService
    from tests.subagent_support import SubagentHarness, wait_for_subagent_worker

    async def scenario() -> None:
        """让任务在收到停止后等待放行，确保真实结果事务发生在持久关闭之后。"""
        started, cancelled, release = (asyncio.Event() for _ in range(3))
        model_calls = 0

        async def script(messages, tools):
            """记录子模型调用次数，关闭后通知到达时应保持原次数。"""
            nonlocal model_calls
            model_calls += 1
            yield StreamChunk(content="委派完成")

        class Work(BaseTask):
            """保持任务结果保存晚于身份关闭。"""

            async def run(self, runtime: TaskRuntime) -> None:
                """收到范围停止后等待测试放行，再交回停止结果。"""
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                    raise

        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            runtime = ToolRuntime("parent", "run", "work", "call", harness.supervisor, harness.queue,
                                 subagent_id=None if parent_delete else receipt.context_id)
            context = "parent" if parent_delete else receipt.context_id
            scope = AgentStorageScope.MAIN if parent_delete else AgentStorageScope.SUBAGENT
            inbox = SessionStateManager(context, SqliteFactory(harness.path), storage_scope=scope)
            try:
                task_id = await runtime.submit_task(Work(), name="work", title="等待关闭", notify_agent=True)
                await asyncio.wait_for(started.wait(), 2)
                if parent_delete:
                    service = ChatSettingsService(harness.supervisor_settings, mutation_coordinator=harness.supervisor_mutations)
                    await service.delete_chat_settings("parent")
                    harness.scheduler.schedule_parent_cleanup("parent")
                else:
                    result = await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.DESTROY)
                    assert result.accepted
                await asyncio.wait_for(cancelled.wait(), 3)
                release.set()
                assert (await harness.supervisor.wait(task_id, timeout_ms=3000)).status == TaskStatus.STOPPED
                await asyncio.wait_for(harness.queue.join(), 3)
                notices = await inbox.read_task_terminals()
                assert len([item for item in notices if item.payload["task_id"] == task_id]) == 1
                assert model_calls == 1
                assert not await harness.scheduler.snapshot_subagents("parent")
                assert not await harness.supervisor.records.finish_once(task_id, TaskStatus.COMPLETED)
            finally:
                release.set()
                await inbox.close()

    asyncio.run(scenario())
