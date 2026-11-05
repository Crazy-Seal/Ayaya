"""验证普通任务的终态事务故障恢复，主任务与子任务共用 Supervisor。"""

import asyncio
from contextlib import closing
from tests.runtime_support import configured_settings
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from app.agent.runtime.tasks.base import BaseTask
from app.agent.runtime.tasks.runtime import TaskRuntime
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.state.manager import SessionStateManager
from app.agent.tools.core.runtime import ToolRuntime
from app.crud.subagent_dao import SubagentDao
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.runtime_types import TaskStatus
from app.schemas.subagent_types import SubagentDefinition, SubagentAction


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("outcome", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED])
def test_terminal_transaction_recovers_without_repeating_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: bool, outcome: TaskStatus,
) -> None:
    """数据库恢复后自动保存原终态，业务和清理各执行一次，通知唯一。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 在真实 DAO 故障返回处报告测试屏障。
        child (bool): 普通任务是否由子 Agent 提交。
        outcome (TaskStatus): 需要在保存重试期间保留的实际业务终态。
    """
    async def scenario() -> None:
        """真实 SQLite 触发器阻止终态事务，解除后仅等待框架自动恢复。"""
        path = str(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=path)
        definition_dao = SubagentDao(path)
        context = "child" if child else "parent"
        scope = AgentStorageScope.SUBAGENT if child else AgentStorageScope.MAIN
        inbox = SessionStateManager(context, path, storage_scope=scope)
        runtime = ToolRuntime("parent", "run", "work", "call", supervisor, supervisor.runtime_queue,
                              subagent_id="child" if child else None)
        started, release, rejected = asyncio.Event(), asyncio.Event(), asyncio.Event()
        runs = cleanups = 0
        original = supervisor.records.finish_once

        async def finish(task_id: str, status: TaskStatus, *, result: str | None = None,
                         error: str | None = None, notify: bool = True) -> bool:
            """保留真实事务，在触发器已经导致回滚后通知测试解除故障。"""
            try:
                return await original(task_id, status, result=result, error=error, notify=notify)
            except sqlite3.IntegrityError:
                rejected.set()
                raise

        class Work(BaseTask):
            """提供三种业务结果，统计实际执行与清理次数。"""
            async def run(self, runtime: TaskRuntime) -> str:
                """等待测试放行，按场景返回、失败或等待停止。"""
                nonlocal runs
                runs += 1
                started.set()
                await release.wait()
                if outcome == TaskStatus.FAILED:
                    raise ValueError("业务原始错误")
                return "业务原始结果"

            async def cleanup(self) -> None:
                """记录一次实际资源释放。"""
                nonlocal cleanups
                cleanups += 1

        monkeypatch.setattr(supervisor.records, "finish_once", finish)
        try:
            if child:
                await definition_dao.create_with_command(SubagentDefinition("child", "parent", "助手", "测试",
                    frozenset(), True, datetime.now(timezone.utc)), "初始任务", "create")
            task_id = await supervisor.submit(Work(), runtime=runtime, name="work", title="测试工作",
                notify_agent=True)
            await asyncio.wait_for(started.wait(), 3)
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TRIGGER refuse_terminal BEFORE UPDATE ON task_records "
                           "BEGIN SELECT RAISE(ABORT, 'terminal storage unavailable'); END")
                db.commit()
            if outcome == TaskStatus.STOPPED:
                assert await supervisor.stop_task(task_id)
            else:
                release.set()
            await asyncio.wait_for(rejected.wait(), 3)
            assert (await supervisor.wait(task_id, timeout_ms=0)).status == TaskStatus.RUNNING
            assert supervisor.active_task_count() == 1
            assert not await inbox.read_task_terminals()
            # 1. 保存阶段收到停止不改变已经完成或失败的业务结果，也不取消重提。
            await supervisor.stop_task(task_id)
            with closing(sqlite3.connect(path)) as db:
                db.execute("DROP TRIGGER refuse_terminal")
                db.commit()
            # 2. 测试不再次提交终态，数据库恢复后由同一任务协程完成原事务。
            record = await supervisor.wait(task_id, timeout_ms=3000)
            assert record.status == outcome
            assert record.result == ("业务原始结果" if outcome == TaskStatus.COMPLETED else None)
            assert record.error == ("业务原始错误" if outcome == TaskStatus.FAILED else None)
            assert runs == cleanups == 1
            assert supervisor.active_task_count() == 0
            notices = await inbox.read_task_terminals()
            assert len(notices) == 1
            assert notices[0].payload["status"] == outcome
            assert notices[0].payload["result"] == (record.result or "")
            assert notices[0].payload.get("error") == record.error
            events = []
            while not supervisor.runtime_queue.empty():
                events.append(supervisor.runtime_queue.get_nowait())
            assert len([event for event in events if event.status != TaskStatus.RUNNING]) == 1
        finally:
            release.set()
            await supervisor.close()
            await inbox.close()
            await definition_dao.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("commit_waiting", [False, True])
def test_persistent_terminal_failure_does_not_block_shutdown_or_restart_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commit_waiting: bool,
) -> None:
    """永久保存失败时按原关闭预算结束等待，重启仍可将未保存记录整理为 orphaned。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 缩短关闭等待并注入持续保存失败。
        commit_waiting (bool): 关闭取消发生在事务等待期间，或失败后的延时期间。
    """
    async def scenario() -> None:
        """用保存屏障覆盖延期取消后仍失败的路径，检查关闭及重启后的公开记录。"""
        path = str(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=path)
        runtime = ToolRuntime("parent", "run", "work", "call", supervisor, supervisor.runtime_queue)
        entered, release = asyncio.Event(), asyncio.Event()
        inspector = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=path)
        inbox = SessionStateManager("parent", path)

        class Work(BaseTask):
            """完成业务后触发无法保存终态的场景。"""
            async def run(self, runtime: TaskRuntime) -> str:
                """正常返回原始结果。"""
                return "原始结果"

        async def fail(task_id: str, status: TaskStatus, *, result: str | None = None,
                       error: str | None = None, notify: bool = True) -> bool:
            """模拟保存无法完成，按场景在外层取消到达前后报告真实错误。"""
            entered.set()
            if commit_waiting:
                await release.wait()
            raise sqlite3.OperationalError("测试持续保存失败")

        monkeypatch.setattr(supervisor.records, "finish_once", fail)
        monkeypatch.setattr("app.agent.runtime.tasks.task_supervisor.TASK_SHUTDOWN_WAIT_SECONDS", 0)
        try:
            task_id = await supervisor.submit(Work(), runtime=runtime, name="work", title="工作",
                notify_agent=True)
            await asyncio.wait_for(entered.wait(), 3)
            closing = asyncio.create_task(supervisor.close())
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(closing, 1)
            assert supervisor.active_task_count() == 1
            assert (await inspector.get_task_record(task_id)).status == TaskStatus.RUNNING
            await inspector.recover_orphaned()
            record = await inspector.get_task_record(task_id)
            assert record.status == TaskStatus.ORPHANED
            assert len(await inbox.read_task_terminals()) == 1
        finally:
            release.set()
            await supervisor.close()
            await inspector.close()
            await inbox.close()
    asyncio.run(scenario())


def test_one_destroy_completes_after_child_task_terminal_storage_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子普通任务仅首次保存失败后，一次 destroy 即等待自动恢复并完成历史留档。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 在普通任务首次保存时注入故障。
    """
    from app.agent.models.contracts import StreamChunk
    from tests.subagent_support import SubagentHarness, wait_for_subagent_worker, subagent_worker_running

    async def scenario() -> None:
        """先完成保留型委派，再提交普通任务，故障后只发出一次 destroy。"""
        failed, recovered = asyncio.Event(), asyncio.Event()
        runs = cleanups = 0

        async def script(messages, tools):
            """正常结束保留型子委派。"""
            yield StreamChunk(content="委派完成")

        class Work(BaseTask):
            """提供可以核对的独立后台任务结果。"""
            async def run(self, runtime: TaskRuntime) -> str:
                """只执行一次业务工作。"""
                nonlocal runs
                runs += 1
                return "子后台原始结果"

            async def cleanup(self) -> None:
                """只执行一次资源释放。"""
                nonlocal cleanups
                cleanups += 1

        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.subagent_id), 3)
            original = harness.supervisor.records.finish_once

            async def finish(task_id: str, status: TaskStatus, *, result: str | None = None,
                             error: str | None = None, notify: bool = True) -> bool:
                """故障只发生在测试明确恢复保存入口之前，原事务始终由 DAO 完成。"""
                if not recovered.is_set():
                    failed.set()
                    raise sqlite3.OperationalError("测试子普通任务保存失败")
                return await original(task_id, status, result=result, error=error, notify=notify)

            monkeypatch.setattr(harness.supervisor.records, "finish_once", finish)
            runtime = ToolRuntime("parent", "work-run", "work", "call", harness.supervisor,
                                  harness.queue, subagent_id=receipt.subagent_id)
            task_id = await harness.supervisor.submit(Work(), runtime=runtime, name="work", title="独立工作",
                notify_agent=True)
            await asyncio.wait_for(failed.wait(), 3)
            destroyed = await harness.scheduler.interact_subagent("parent", receipt.subagent_id, SubagentAction.DESTROY)
            assert destroyed.accepted
            try:
                # 1. 公开销毁已经完成，实际资源清理继续等待原任务保存恢复。
                assert not await harness.scheduler.snapshot_subagents("parent")
                assert subagent_worker_running(harness.scheduler, destroyed.subagent_id)
            finally:
                recovered.set()
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, destroyed.subagent_id), 3)
            record = await harness.supervisor.get_task_record(task_id)
            assert record.status == TaskStatus.COMPLETED
            assert record.result == "子后台原始结果"
            assert runs == cleanups == 1
            assert not await harness.scheduler.snapshot_subagents("parent")
            assert not (await harness.scheduler.get_subagent_details("parent", receipt.subagent_id))["context_available"]
    asyncio.run(scenario())
