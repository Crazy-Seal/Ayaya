"""驱动普通任务执行、结果保存和自有资源关闭。"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from app.agent.runtime.requests import RuntimeRequest
from app.agent.runtime.tasks.base import BaseTask
from app.agent.runtime.tasks.runtime import TaskRuntime
from app.agent.runtime.tasks.task_execution import TaskExecution, TaskOutcome, snapshot_record
from app.agent.utils.infra.owned_work import complete_owned_work
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.runtime_types import CommandReceipt, CommandStatus, TaskExecutionPhase, TaskStatus
from app.schemas.subagent_types import TaskRecordKind
from app.schemas.task_types import TaskSnapshot

logger = logging.getLogger(__name__)


class TaskSupervisor:
    """管理普通任务，先交付保存结果，再等待具体任务关闭资源。

    Attributes:
        records (TaskRecordDao): 应用注入的任务事务入口，由创建者关闭。
        runtime_queue (asyncio.Queue[RuntimeRequest]): 已保存记录及保存失败摘要的发布队列。
        _executions (dict[str, TaskExecution]): 每项任务的唯一运行记录；失败后保留轻量摘要。
    """

    def __init__(self, *, runtime_queue: asyncio.Queue[RuntimeRequest], records: TaskRecordDao) -> None:
        """借用应用创建的存储和队列。

        Args:
            runtime_queue (asyncio.Queue[RuntimeRequest]): 应用共享队列。
            records (TaskRecordDao): 已装配的任务记录存储。
        """
        self.records = records
        self.runtime_queue = runtime_queue
        self._executions: dict[str, TaskExecution] = {}
        self._closing = False
        self._submission_lock = asyncio.Lock()

    async def submit(self, task: BaseTask, *, runtime: TaskRuntime, name: str,
                     title: str, notify_agent: bool, background: bool = True,
                     tool_name: str | None = None, origin_tool_call_id: str | None = None,
                     origin_run_id: str | None = None) -> str:
        """保存初始记录并接管执行，来源编号用于记录和按范围停止。

        Args:
            task (BaseTask): 具体业务及资源关闭实现。
            runtime (TaskRuntime): 固定任务来源。
            name (str): 稳定业务名称。
            title (str): 用户可读说明。
            notify_agent (bool): 是否同事务保存结果通知。
            background (bool): 是否显示后台任务。
            tool_name (str | None): 来源工具。
            origin_tool_call_id (str | None): 来源工具调用编号。
            origin_run_id (str | None): 工具任务创建时的轮次，仅用于来源记录。
        Returns:
            str: 已登记任务编号。
        """
        return await complete_owned_work(self._register(task, runtime=runtime, name=name, title=title,
            notify_agent=notify_agent, background=background, tool_name=tool_name,
            origin_tool_call_id=origin_tool_call_id, origin_run_id=origin_run_id))

    async def _register(self, task: BaseTask, *, runtime: TaskRuntime, name: str, title: str,
                        notify_agent: bool, background: bool, tool_name: str | None,
                        origin_tool_call_id: str | None, origin_run_id: str | None) -> str:
        """与管理器关闭排序登记，保存后立即持有任务。

        Args:
            task (BaseTask): 具体任务。
            runtime (TaskRuntime): 固定来源。
            name (str): 任务业务名称。
            title (str): 可读说明。
            notify_agent (bool): 结果通知政策。
            background (bool): 后台展示政策。
            tool_name (str | None): 工具来源。
            origin_tool_call_id (str | None): 工具调用编号。
            origin_run_id (str | None): 工具任务的创建轮次。
        Returns:
            str: 新任务编号。
        Raises:
            RuntimeError: 应用已关闭任务受理。
        """
        async with self._submission_lock:
            if self._closing:
                raise RuntimeError("任务管理器正在关闭")
            task_id = uuid4().hex
            await self.records.insert_running(task_id, runtime.session_id, name=name, title=title,
                plugin_name=runtime.plugin_name, origin_run_id=origin_run_id, notify_agent=notify_agent,
                background=background, tool_name=tool_name, origin_tool_call_id=origin_tool_call_id,
                subagent_id=runtime.subagent_id, notification_context_id=runtime.subagent_id or runtime.session_id,
                notification_scope=AgentStorageScope.SUBAGENT if runtime.subagent_id else AgentStorageScope.MAIN)
            record = await self.records.get(task_id)
            if record is None:
                raise RuntimeError("任务登记成功后无法读取记录")
            execution = TaskExecution(record, task, runtime.bind_task(task_id))
            self._executions[task_id] = execution
            if background:
                self.runtime_queue.put_nowait(record)
            execution.runner = asyncio.create_task(self._run_task(execution))
            return task_id

    async def _run_task(self, execution: TaskExecution) -> None:
        """按业务、保存、交付、关闭顺序完成单项任务。

        Args:
            execution (TaskExecution): 已登记的任务运行记录。
        """
        task, runtime = execution.task, execution.runtime
        assert task is not None and runtime is not None
        task_id = execution.record.task_id
        try:
            # 1. 停止早于协程启动时直接结束业务，已经开始的业务只取消一次。
            try:
                if execution.phase == TaskExecutionPhase.STOPPING:
                    raise asyncio.CancelledError
                execution.business = asyncio.create_task(task.run(runtime))
                result = await execution.business
                if result is not None and not isinstance(result, str):
                    raise TypeError("任务 run() 只能返回字符串或 None")
                outcome = TaskOutcome(TaskStatus.COMPLETED, result)
            except asyncio.CancelledError:
                outcome = TaskOutcome(TaskStatus.STOPPED)
            except Exception as exc:
                outcome = TaskOutcome(TaskStatus.FAILED, error=str(exc))
                logger.exception("后台任务执行失败：%s", task_id)
            # 2. 只保存已固定结果；事务重试和取消后的提交确认由 DAO 负责。
            execution.begin_commit(outcome)
            try:
                record = await self.records.finish_once(task_id, outcome.status, result=outcome.result, error=outcome.error)
                if record is None:
                    raise RuntimeError("任务结果已经结束或任务记录不存在")
                execution.commit_succeeded()
                self.runtime_queue.put_nowait(record)
            except Exception as exc:
                execution.commit_failed(str(exc))
                logger.exception("后台任务结果保存失败：%s", task_id)
                # 2.1. 尝试通知任务保存失败；资料查询失败时记录错误，随后继续释放任务资源。
                try:
                    snapshot = await self.get_task_snapshot(task_id)
                    if snapshot is not None:
                        self.runtime_queue.put_nowait(snapshot)
                except Exception:
                    logger.exception("后台任务保存失败摘要读取失败：%s", task_id)
            # 3. 结果已可查询，具体任务在 cleanup 内完成自己的有限关闭。
            try:
                await task.cleanup()
            except Exception:
                logger.exception("后台任务资源关闭失败：%s", task_id)
        finally:
            execution.release_resources()
            if execution.phase == TaskExecutionPhase.FINISHED:
                self._executions.pop(task_id, None)

    async def wait(self, task_id: str, *, timeout_ms: int) -> TaskSnapshot | None:
        """等待结果保存或保存失败，观察者超时及取消只结束观察。

        Args:
            task_id (str): 任务编号。
            timeout_ms (int): 等待毫秒预算，零表示立即查询。
        Returns:
            TaskSnapshot | None: 当前任务摘要，未知编号为空。
        """
        if timeout_ms < 0:
            raise ValueError("等待时间不能小于零")
        execution = self._executions.get(task_id)
        if execution is not None and timeout_ms:
            try:
                await asyncio.wait_for(execution.completion.wait(), timeout_ms / 1000)
            except asyncio.TimeoutError:
                pass
        return await self.get_task_snapshot(task_id)

    def active_task_count(self) -> int:
        """统计业务运行或结果保存尚未结束的任务。

        Returns:
            int: 排除已交付结果和保存失败任务的数量。
        """
        return sum(item.phase in {TaskExecutionPhase.RUNNING, TaskExecutionPhase.STOPPING,
                                 TaskExecutionPhase.COMMITTING} for item in self._executions.values())

    async def recover_orphaned(self) -> None:
        """重启后保存旧任务的 orphaned 结果和唯一通知。"""
        for task_id in await self.records.running_ids():
            record = await self.records.finish_once(task_id, TaskStatus.ORPHANED)
            if record is not None:
                self.runtime_queue.put_nowait(record)

    def get_task(self, task_id: str) -> BaseTask | None:
        """取得仍持有的具体任务，供终端输入等业务交互使用。

        Args:
            task_id (str): 任务编号。
        Returns:
            BaseTask | None: 具体任务，资源已释放或未知时为空。
        """
        execution = self._executions.get(task_id)
        return execution.task if execution is not None else None

    async def get_task_snapshot(self, task_id: str) -> TaskSnapshot | None:
        """读取已提交任务资料，并叠加本次进程确认的阶段和保存错误。

        Args:
            task_id (str): 任务编号。
        Returns:
            TaskSnapshot | None: 包含保存故障的只读摘要。
        """
        record = await self.records.get(task_id)
        if record is None:
            return None
        execution = self._executions.get(task_id)
        if execution is not None:
            return execution.snapshot(record)
        return snapshot_record(record)

    async def list_tasks(self, session_id: str) -> list[TaskSnapshot]:
        """查询该父会话的后台普通任务，包含当前保存失败摘要。

        Args:
            session_id (str): 所属父会话编号。
        Returns:
            list[TaskSnapshot]: 按原记录顺序排列的摘要。
        """
        result: list[TaskSnapshot] = []
        for record in await self.records.list(session_id):
            if record.background and record.kind == TaskRecordKind.ORDINARY:
                execution = self._executions.get(record.task_id)
                result.append(execution.snapshot(record) if execution is not None else snapshot_record(record))
        return result

    async def stop_task(self, task_id: str) -> CommandReceipt:
        """停止正在执行的业务，保存期间返回 too_late。

        Args:
            task_id (str): 任务编号。
        Returns:
            CommandReceipt: 统一停止受理结果。
        """
        execution = self._executions.get(task_id)
        if execution is None:
            record = await self.records.get(task_id)
            return CommandReceipt(CommandStatus.NOT_FOUND if record is None else CommandStatus.TOO_LATE,
                                  (record.subagent_id or record.session_id) if record else None)
        previous = execution.phase
        receipt = execution.request_stop()
        if previous == TaskExecutionPhase.RUNNING and execution.business is not None:
            execution.business.cancel()
        return receipt

    async def stop_subagent_tasks(self, session_id: str, subagent_id: str) -> None:
        """按当前已登记归属停止子普通任务。

        Args:
            session_id (str): 所属父编号。
            subagent_id (str): 子上下文编号。
        """
        await complete_owned_work(self._stop_tasks([key for key, item in self._executions.items()
            if item.record.session_id == session_id and item.record.subagent_id == subagent_id]))

    async def stop_session_tasks(self, session_id: str) -> None:
        """按当前已登记归属停止父及子普通任务。

        Args:
            session_id (str): 被删除的父编号。
        """
        await complete_owned_work(self._stop_tasks([key for key, item in self._executions.items() if item.record.session_id == session_id]))

    async def _stop_tasks(self, selected: list[str]) -> None:
        """停止本次选中的任务并等待其保存及自有资源关闭。

        Args:
            selected (list[str]): 调用开始时确定的任务编号。
        """
        runners: list[asyncio.Task[None]] = []
        for task_id in selected:
            await self.stop_task(task_id)
            execution = self._executions.get(task_id)
            if execution is not None and execution.runner is not None:
                runners.append(execution.runner)
        if runners:
            await asyncio.gather(*runners)

    async def begin_shutdown(self) -> None:
        """关闭任务受理并等待正在登记的任务完成接管。"""
        async with self._submission_lock:
            self._closing = True

    async def close(self) -> None:
        """停止当前任务并等待保存与具体任务的有界关闭，存储由应用关闭。"""
        await self.begin_shutdown()
        await complete_owned_work(self._stop_tasks(list(self._executions)))
