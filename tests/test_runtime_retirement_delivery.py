"""验证子身份废弃失败和普通任务状态展示各自遵守业务事实。"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import closing
from pathlib import Path

import pytest

from app.agent.models.contracts import StreamChunk, ToolDefinition
from app.agent.runtime.tasks.base import BaseTask
from app.agent.runtime.tasks.runtime import TaskRuntime
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import BaseMessage
from app.agent.tools.core.runtime import ToolRuntime
from app.crud.sqlite_factory import SqliteFactory
from app.interaction.interaction_contract import InteractionEvent
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.runtime_types import InteractionEventKind, SessionState, TaskExecutionPhase, TaskStatus
from app.schemas.subagent_types import SubagentAction
from tests.runtime_support import next_runtime_event
from tests.subagent_support import SubagentHarness, wait_for_subagent_worker


class WaitingTask(BaseTask):
    """让场景控制普通任务完成，记录任务启动及资源释放。

    Attributes:
        started (asyncio.Event): 任务已开始执行业务。
        release (asyncio.Event): 允许业务正常返回，也可由子关闭请求取消业务。
        cleaned (asyncio.Event): Supervisor 已安排释放任务资源。
    """

    def __init__(self) -> None:
        """为每项隔离任务创建独立观察事件。"""
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaned = asyncio.Event()

    async def run(self, runtime: TaskRuntime) -> str:
        """等待正常完成或所属子结束时发出的停止。

        Args:
            runtime (TaskRuntime): Supervisor 绑定的任务来源。

        Returns:
            str: 正常放行后的业务结果。
        """
        self.started.set()
        await self.release.wait()
        return "后台工作已完成"

    async def cleanup(self) -> None:
        """通知场景本任务的业务及结果保存已经结束。"""
        self.cleaned.set()


async def task_events_through(
    harness: SubagentHarness, task_id: str, status: TaskStatus | TaskExecutionPhase,
) -> list[InteractionEvent]:
    """从实际 Hub 收集事件，直到目标任务的指定状态已经发布。

    Args:
        harness (SubagentHarness): 使用真实 Scheduler 和 Hub 的隔离运行实例。
        task_id (str): 要观察的普通任务编号。
        status (TaskStatus | TaskExecutionPhase): 预期可展示的任务状态。

    Returns:
        list[InteractionEvent]: 截至目标状态的所有可观察事件。
    """
    events: list[InteractionEvent] = []
    while True:
        event = await next_runtime_event(harness.scheduler)
        events.append(event)
        if (event.kind == InteractionEventKind.TASK_STATUS and event.task_id == task_id
                and event.payload["status"] == status):
            return events


@pytest.mark.parametrize("ending", ["completed", "failed", "stop"])
def test_one_shot_retirement_failure_keeps_paused_identity_and_stops_tasks(
    tmp_path: Path, ending: str,
) -> None:
    """自动废弃事务回滚后保留原会话及错误，停止一次性任务，允许再次销毁。

    Args:
        tmp_path (Path): 当前场景独占的数据库目录。
        ending (str): 子工作正常完成、业务失败或用户停止。
    """
    async def scenario() -> None:
        """用真实 SQLite 触发器拒绝废弃，观察身份、事件、后台任务和后续销毁。"""
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def script(
            messages: Sequence[BaseMessage], tools: Sequence[ToolDefinition] | None,
        ) -> AsyncIterator[StreamChunk]:
            """保持模型活动，直到场景指定正常结束、失败或停止。

            Args:
                messages (Sequence[BaseMessage]): 真实管道准备的模型输入。
                tools (Sequence[ToolDefinition] | None): 本次可见的工具定义。

            Yields:
                StreamChunk: 正常完成时的子回答。

            Raises:
                RuntimeError: 场景要求模型业务失败。
            """
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            if ending == "failed":
                raise RuntimeError("模型业务失败")
            yield StreamChunk(content="子工作结束")

        async with SubagentHarness(tmp_path / "retirement.sqlite3", script, retain=False) as harness:
            child = await harness.create()
            await asyncio.wait_for(entered.wait(), 3)
            session = harness.scheduler._sessions[child.context_id]
            work = WaitingTask()
            runtime = ToolRuntime("parent", "task-origin", "work", "call", harness.supervisor,
                harness.queue, subagent_id=child.context_id)
            task_id = await runtime.submit_task(work, name="work", title="一次性子后台任务", notify_agent=True)
            await asyncio.wait_for(work.started.wait(), 3)
            with closing(sqlite3.connect(harness.path)) as db:
                db.execute("CREATE TRIGGER refuse_retirement BEFORE UPDATE OF retired_at ON subagents "
                    "BEGIN SELECT RAISE(ABORT,'retirement rejected'); END")
            try:
                if ending == "stop":
                    assert (await harness.scheduler.interact_subagent(
                        "parent", child.context_id, SubagentAction.STOP)).accepted
                else:
                    release.set()
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, child.context_id), 3)
                await harness.queue.join()

                # 1. 原身份和原会话保持暂停，面板保留废弃失败说明及已经交付的业务结果。
                assert harness.scheduler._sessions[child.context_id] is session
                assert session.snapshot()["session_state"] == SessionState.PAUSED
                summaries = await harness.scheduler.snapshot_subagents("parent")
                assert len(summaries) == 1 and summaries[0].error is not None
                assert "retirement rejected" in summaries[0].error
                details = await harness.scheduler.get_subagent_details("parent", child.context_id)
                assert details["context_available"] and len(details["results"]) == 1
                assert details["results"][0]["status"] == {
                    "completed": TaskStatus.COMPLETED, "failed": TaskStatus.FAILED, "stop": TaskStatus.STOPPED,
                }[ending]
                assert work.cleaned.is_set()
                assert (await harness.supervisor.get_task_snapshot(task_id)).status == TaskStatus.STOPPED
                events = await asyncio.wait_for(task_events_through(harness, task_id, TaskStatus.STOPPED), 3)
                assert any(event.kind == InteractionEventKind.ERROR
                    and "retirement rejected" in str(event.payload.get("detail")) for event in events)
                assert not any(event.kind == InteractionEventKind.SUBAGENT_STATUS
                    and event.payload.get("subagent_id") == child.context_id
                    and event.payload.get("removed") for event in events)

                # 2. 故障仍存在时再次销毁明确失败，原身份继续可见。
                rejected = await harness.scheduler.interact_subagent(
                    "parent", child.context_id, SubagentAction.DESTROY)
                assert not rejected.accepted and rejected.detail is not None
                assert "retirement rejected" in rejected.detail
                assert len(await harness.scheduler.snapshot_subagents("parent")) == 1
            finally:
                release.set()
                with closing(sqlite3.connect(harness.path)) as db:
                    db.execute("DROP TRIGGER refuse_retirement")

            # 3. 修复存储后由明确销毁结束原会话，模型和已交付报告均保持一次。
            assert (await harness.scheduler.interact_subagent(
                "parent", child.context_id, SubagentAction.DESTROY)).accepted
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, child.context_id), 3)
            await harness.queue.join()
            assert not await harness.scheduler.snapshot_subagents("parent")
            assert child.context_id not in harness.scheduler._sessions
            assert calls == 1
            details = await harness.scheduler.get_subagent_details("parent", child.context_id)
            assert not details["context_available"] and len(details["results"]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["one_shot", "destroy", "late_task"])
@pytest.mark.parametrize("notify", [False, True])
@pytest.mark.parametrize("save_failed", [False, True])
def test_retired_child_task_status_reaches_hub_without_reactivating_child(
    tmp_path: Path, ending: str, notify: bool, save_failed: bool,
) -> None:
    """来源身份关闭后继续发布任务结束或保存失败，Inbox 政策和子关闭状态保持有效。

    Args:
        tmp_path (Path): 当前场景独占的数据库目录。
        ending (str): 一次性结束、保留型销毁或身份废弃后才登记任务。
        notify (bool): 普通任务是否保存 Inbox 通知。
        save_failed (bool): 是否拒绝普通任务的终态事务。
    """
    async def scenario() -> None:
        """沿真实任务保存和队列发布观察界面状态，核对关闭身份始终没有再次运行。"""
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def script(
            messages: Sequence[BaseMessage], tools: Sequence[ToolDefinition] | None,
        ) -> AsyncIterator[StreamChunk]:
            """在普通任务登记前保留活动子执行。

            Args:
                messages (Sequence[BaseMessage]): 当前真实模型输入。
                tools (Sequence[ToolDefinition] | None): 当前可见工具。

            Yields:
                StreamChunk: 一次性工作正常完成的回答。
            """
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            yield StreamChunk(content="子工作完成")

        async with SubagentHarness(tmp_path / "task-events.sqlite3", script,
                retain=ending != "one_shot") as harness:
            child = await harness.create()
            await asyncio.wait_for(entered.wait(), 3)
            if ending == "late_task":
                assert (await harness.scheduler.interact_subagent(
                    "parent", child.context_id, SubagentAction.DESTROY)).accepted
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, child.context_id), 3)
                await harness.queue.join()

            work = WaitingTask()
            runtime = ToolRuntime("parent", "task-origin", "work", "call", harness.supervisor,
                harness.queue, subagent_id=child.context_id)
            task_id = await runtime.submit_task(work, name="work", title="子来源普通任务", notify_agent=notify)
            await asyncio.wait_for(work.started.wait(), 3)
            await asyncio.wait_for(task_events_through(harness, task_id, TaskStatus.RUNNING), 3)
            if save_failed:
                with closing(sqlite3.connect(harness.path)) as db:
                    db.execute("CREATE TRIGGER refuse_task_result BEFORE UPDATE OF status ON task_records "
                        "WHEN OLD.kind='ordinary' BEGIN SELECT RAISE(ABORT,'task result rejected'); END")
            try:
                if ending == "one_shot":
                    release.set()
                elif ending == "destroy":
                    assert (await harness.scheduler.interact_subagent(
                        "parent", child.context_id, SubagentAction.DESTROY)).accepted
                else:
                    work.release.set()
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, child.context_id), 3)
                await asyncio.wait_for(work.cleaned.wait(), 3)
                await harness.queue.join()

                # 1. 界面在观察到 running 后能收到真实终态，也能收到结果保存失败。
                expected = (TaskExecutionPhase.SAVE_FAILED if save_failed else
                    TaskStatus.COMPLETED if ending == "late_task" else TaskStatus.STOPPED)
                events = await asyncio.wait_for(task_events_through(harness, task_id, expected), 3)
                terminal = events[-1]
                assert terminal.payload["subagent_id"] == child.context_id
                assert (await harness.supervisor.get_task_snapshot(task_id)).status == expected
                assert not await harness.scheduler.snapshot_subagents("parent")
                assert child.context_id not in harness.scheduler._sessions and calls == 1

                # 2. 通知只按任务配置和事务结果写入，接收身份关闭保持不变。
                manager = SessionStateManager(child.context_id, SqliteFactory(harness.path),
                    storage_scope=AgentStorageScope.SUBAGENT, parent_session_id="parent")
                try:
                    notices = await manager.read_task_terminals()
                    assert [item.payload["task_id"] for item in notices] == (
                        [task_id] if notify and not save_failed else [])
                finally:
                    await manager.close()
            finally:
                release.set()
                work.release.set()
                if save_failed:
                    with closing(sqlite3.connect(harness.path)) as db:
                        db.execute("DROP TRIGGER refuse_task_result")

    asyncio.run(scenario())
