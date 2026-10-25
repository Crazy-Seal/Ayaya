"""统一管理后台任务的执行、清理和终态保存，向应用队列发布已保存记录。"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from app.schemas.runtime_types import TaskStatus, TaskLifetime
from app.schemas.task_types import TaskRecord, RuntimeRequest
from app.agent.runtime.tasks.base import BaseTask
from app.agent.utils.infra.owned_work import complete_owned_work
from app.agent.runtime.tasks.runtime import TaskRuntime
from app.crud.task_record_dao import TaskRecordDao

logger = logging.getLogger(__name__)
TASK_SHUTDOWN_WAIT_SECONDS = 5

class TaskSupervisor:
    """统一持有任务执行，安排停止、清理、终态事务及结果通知。

    Attributes:
        records (TaskRecordDao): 任务事实及终态通知的事务入口。
        runtime_queue (asyncio.Queue[RuntimeRequest]): 应用创建的共享队列，本对象只发布记录。
        _tasks (dict[str, BaseTask]): 执行、清理或终态保存尚未完成的任务。
        _running (set[str]): 正在执行业务、可以接收取消请求的任务编号。
        _executions (dict[str, asyncio.Task[None]]): 执行完整生命周期的协程。
        _stopping (set[str]): 已收到停止请求的任务编号，避免重复发出取消。
        _closing (bool): 应用已停止接收新任务。
        _submission_lock (asyncio.Lock): 串行登记、来源轮次结束与关闭入口。
        _finished_turns (set[tuple[str, str]]): 已结束的会话及轮次，拒绝迟到的随轮任务。
    """

    def __init__(self, *, runtime_queue: asyncio.Queue[RuntimeRequest], db_path: str | None = None) -> None:
        """准备持久化和记录队列，应用级共享，不随单轮 Agent 关闭。

        Args:
            db_path (str | None): 当前环境的 SQLite 路径。
            runtime_queue (asyncio.Queue[RuntimeRequest]): 应用创建、Scheduler 独占消费的共享请求队列。
        """
        self.records = TaskRecordDao(db_path)
        self.runtime_queue = runtime_queue
        self._tasks: dict[str, BaseTask] = {}
        self._running: set[str] = set()
        self._executions: dict[str, asyncio.Task[None]] = {}
        self._stopping: set[str] = set()
        self._closing = False
        self._submission_lock = asyncio.Lock()
        self._finished_turns: set[tuple[str, str]] = set()

    async def submit(self, task: BaseTask, *, runtime: TaskRuntime, name: str,
                     title: str, lifetime: TaskLifetime, notify_agent: bool, background: bool = True,
                     tool_name: str | None = None, origin_tool_call_id: str | None = None) -> str:
        """保存通用任务登记后启动执行，返回可查询的执行编号。

        Args:
            task (BaseTask): 提供业务执行和资源清理的具体任务。
            runtime (TaskRuntime): 框架绑定插件、会话、来源轮次及任务编号的调用对象。
            name (str): 供查询或识别本次内容用途的稳定名称。
            title (str): 供用户阅读的任务说明。
            lifetime (TaskLifetime): 任务随来源轮次结束或独立运行的配置。
            notify_agent (bool): 终态保存时是否生成给 Agent 的结果通知。
            background (bool): 是否立即显示在后台任务列表。
            tool_name (str | None): 来源工具名称，插件任务为空。
            origin_tool_call_id (str | None): 来源工具调用编号，插件任务为空。

        Returns:
            str: 已登记并启动的任务执行编号。
        """
        return await complete_owned_work(self._register(task, runtime=runtime, name=name,
            title=title, lifetime=lifetime, notify_agent=notify_agent, background=background,
            tool_name=tool_name, origin_tool_call_id=origin_tool_call_id))

    async def _register(self, task: BaseTask, *, runtime: TaskRuntime, name: str,
                        title: str, lifetime: TaskLifetime, notify_agent: bool, background: bool = True,
                        tool_name: str | None = None, origin_tool_call_id: str | None = None) -> str:
        """在同一登记操作中提交记录、发布初态并取得执行所有权。

        Args:
            task (BaseTask): 需要统一执行和清理的具体任务。
            runtime (TaskRuntime): 固定插件及来源轮次的调用对象。
            name (str): 任务稳定名称。
            title (str): 用户可读标题。
            lifetime (TaskLifetime): 随轮结束或独立运行。
            notify_agent (bool): 是否原子保存结果通知。
            background (bool): 是否立即显示在后台任务列表。
            tool_name (str | None): 来源工具名称，插件任务为空。
            origin_tool_call_id (str | None): 来源工具调用编号，插件任务为空。

        Returns:
            str: 已由 Supervisor 持有的任务编号。
        """
        async with self._submission_lock:
            if self._closing:
                raise RuntimeError("任务管理器正在关闭")
            lifetime = TaskLifetime(lifetime)
            if lifetime == TaskLifetime.TURN and (runtime.session_id, runtime.run_id) in self._finished_turns:
                raise RuntimeError("来源轮次已结束，不能提交随轮任务")
            task_id = uuid4().hex
            await self.records.insert_running(task_id, runtime.session_id,
                name=name, title=title, plugin_name=runtime.plugin_name, origin_run_id=runtime.run_id,
                lifetime=lifetime, notify_agent=notify_agent, background=background,
                tool_name=tool_name, origin_tool_call_id=origin_tool_call_id)
            # 1. 初始记录在执行开始前入队，快速任务不会把同一终态发布两次。
            await self._publish_background_record(task_id)
            self._schedule_execution(task_id, task, runtime.bind_task(task_id))
            return task_id

    def _schedule_execution(self, task_id: str, task: BaseTask, runtime: TaskRuntime) -> None:
        """持有具体任务并创建统一执行协程，所有任务共用相同收尾流程。

        Args:
            task_id (str): 本次后台执行的稳定编号。
            task (BaseTask): 提供业务执行和资源清理的具体任务。
            runtime (TaskRuntime): 框架绑定插件、会话、来源轮次及任务编号的调用对象。
        """
        self._tasks[task_id] = task
        self._executions[task_id] = asyncio.create_task(self._run_task_lifecycle(task_id, task, runtime))

    async def _run_task_lifecycle(self, task_id: str, task: BaseTask, runtime: TaskRuntime) -> None:
        """无论执行成功、失败或停止，先清理资源，再原子保存最终记录。

        Args:
            task_id (str): 本次后台执行的稳定编号。
            task (BaseTask): 提供业务执行和资源清理的具体任务。
            runtime (TaskRuntime): 框架绑定插件、会话、来源轮次及任务编号的调用对象。
        """
        result, error = None, None
        status = TaskStatus.COMPLETED
        persisted = False
        try:
            # 1. 捕获业务执行的结果或异常，停止只表示需要进入资源清理。
            try:
                if task_id in self._stopping:
                    raise asyncio.CancelledError
                self._running.add(task_id)
                result = await task.run(runtime)
                if result is not None and not isinstance(result, str):
                    result = None
                    raise TypeError("任务 run() 只能返回字符串或 None")
            except asyncio.CancelledError:
                status = TaskStatus.STOPPED
            except Exception as exc:
                status, error = TaskStatus.FAILED, str(exc)
                logger.exception("后台任务执行失败：%s", task_id)
            finally:
                self._running.discard(task_id)
            # 2. 清理具有独立任务身份，外层关闭取消时也不打断资源释放。
            try:
                await complete_owned_work(task.cleanup())
            except asyncio.CancelledError:
                # 2.1. 已确定的业务失败优先保留，其他任务按停止状态保存。
                if status != TaskStatus.FAILED:
                    status = TaskStatus.STOPPED
            except Exception as exc:
                status = TaskStatus.FAILED
                error = f"{error}；清理失败：{exc}" if error else f"清理失败：{exc}"
                logger.exception("后台任务清理失败：%s", task_id)
            # 3. 完整等待终态事务及发布；保存失败保留未完成身份，错误交给应用日志。
            try:
                await complete_owned_work(self.commit_terminal_state_once(task_id, status, result=result, error=error))
                persisted = True
            except asyncio.CancelledError:
                # 3.1. 延期取消到达此处时，事务和已保存记录的发布均已完成。
                persisted = True
            except Exception:
                logger.exception("任务终态保存失败，保留未完成记录：%s", task_id)
        finally:
            if persisted:
                self._tasks.pop(task_id, None)
            self._executions.pop(task_id, None)
            self._stopping.discard(task_id)

    async def wait(self, task_id: str, *, timeout_ms: int) -> TaskRecord | None:
        """有限等待终态保存，超时或等待者取消均不取消后台任务。

        Args:
            task_id (str): 本次后台执行的稳定编号。
            timeout_ms (int): 本次等待的毫秒预算，耗尽后任务继续运行。

        Returns:
            TaskRecord | None: 当前已保存的记录，任务不存在时为空。
        """
        if timeout_ms < 0:
            raise ValueError("等待时间不能小于零")
        execution = self._executions.get(task_id)
        if execution is not None and timeout_ms:
            try:
                await asyncio.wait_for(asyncio.shield(execution), timeout_ms / 1000)
            except asyncio.TimeoutError:
                pass
        return await self.get_task_record(task_id)

    def active_count(self) -> int:
        """查询整个应用尚未完成执行、清理或终态保存的任务数。

        Returns:
            int: 执行、清理或终态保存尚未完成的任务数量。
        """
        return len(self._tasks)

    async def stop_turn_tasks(self, session_id: str, run_id: str) -> None:
        """来源轮次结束时请求停止随轮任务，独立任务继续执行。

        Args:
            session_id (str): 任务或请求所属的会话编号。
            run_id (str): 创建任务或发出请求的来源轮次编号。
        """
        # 1. 与登记共用锁：先记下轮次结束，再停止所有已完成登记的随轮任务。
        async with self._submission_lock:
            self._finished_turns.add((session_id, run_id))
            for task_id in tuple(self._tasks):
                record = await self.get_task_record(task_id)
                if record and record.session_id == session_id and record.origin_run_id == run_id and record.lifetime == TaskLifetime.TURN:
                    await self.stop_task(task_id)

    async def recover_orphaned(self) -> None:
        """重启后将已失去执行实例的任务标记 orphaned，按配置保存唯一通知。"""
        for task_id in await self.records.running_ids():
            await self.commit_terminal_state_once(task_id, TaskStatus.ORPHANED)


    async def _publish_background_record(self, task_id: str) -> None:
        """后台状态已经提交后暂存记录，不等待会话或界面处理。

        Args:
            task_id (str): 需要重新读取已提交状态的任务编号。
        """
        record = await self.get_task_record(task_id)
        if record is not None and record.background:
            self.runtime_queue.put_nowait(record)

    async def commit_terminal_state_once(self, task_id: str, status: TaskStatus, *,
                          result: str | None = None, error: str | None = None) -> bool:
        """原子保存终态与 Inbox 通知，成功后暂存记录供 Scheduler 检查。

        Args:
            task_id (str): 任务编号。
            status (TaskStatus): 已完成执行和清理的任务终态。
            result (str | None): 任务正常返回的结果正文，无正文时为空。
            error (str | None): 执行或清理失败的错误摘要。

        Returns:
            bool: 是否首次完成提交。
        """
        # 1. 数据库保证首次终态与唯一通知一起提交，重复提交不再产生记录事件。
        if not await self.records.finish_once(task_id, status, result=result, error=error):
            return False
        # 2. 事务成功后发布已保存记录，等待者在执行协程结束后读取终态。
        record = await self.get_task_record(task_id)
        if record is not None:
            self.runtime_queue.put_nowait(record)
        return True

    def get_task(self, task_id: str) -> BaseTask | None:
        """取得执行、清理或终态保存尚未完成的具体任务对象。

        Args:
            task_id (str): 已登记的任务编号。

        Returns:
            BaseTask | None: 尚未释放的具体任务，未知或已释放时为空。
        """
        return self._tasks.get(task_id)

    async def get_task_record(self, task_id: str) -> TaskRecord | None:
        """读取已提交的持久状态，任务退出或重启后仍可查询。

        Args:
            task_id (str): 任务编号。

        Returns:
            TaskRecord | None: 已提交记录，不存在时为空。
        """
        return await self.records.get(task_id)

    async def list_tasks(self, session_id: str) -> list[TaskRecord]:
        """返回指定聊天已移交后台的任务。

        Args:
            session_id (str): Agent 聊天编号。

        Returns:
            list[TaskRecord]: 后台任务记录。
        """
        return [record for record in await self.records.list(session_id) if record.background]


    async def stop_task(self, task_id: str) -> bool:
        """请求具体任务停止执行，资源释放和终态保存仍由统一流程完成。

        Args:
            task_id (str): 需要停止的任务编号。

        Returns:
            bool: 任务仍由执行协程管理时为真，返回不表示收尾完成。
        """
        execution = self._executions.get(task_id)
        if execution is None or execution.done():
            return False
        if task_id not in self._stopping:
            self._stopping.add(task_id)
            if task_id in self._running:
                execution.cancel()
        return True

    async def begin_shutdown(self) -> None:
        """关闭新任务提交入口，并等待已经开始登记的任务加入统一管理。"""
        async with self._submission_lock:
            self._closing = True

    async def close(self) -> None:
        """封闭提交、请求全部任务停止，等待执行和清理后关闭存储。"""
        await self.begin_shutdown()
        for task_id in tuple(self._tasks):
            await self.stop_task(task_id)
        executions = tuple(self._executions.values())
        if executions:
            _, pending = await asyncio.wait(executions, timeout=TASK_SHUTDOWN_WAIT_SECONDS)
            for task in pending:
                task.cancel()
            await asyncio.gather(*executions, return_exceptions=True)
        await self.records.close()
