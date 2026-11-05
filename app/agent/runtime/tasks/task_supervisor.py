"""统一管理后台任务的执行、清理和终态保存，向应用队列发布已保存记录。"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from app.schemas.runtime_types import TaskStatus
from app.schemas.task_types import TaskRecord
from app.agent.runtime.requests import RuntimeRequest
from app.agent.runtime.tasks.base import BaseTask
from app.agent.utils.infra.owned_work import complete_owned_work
from app.agent.runtime.tasks.runtime import TaskRuntime
from app.crud.chat_settings_dao import ChatSettingsDao
from app.services.settings_mutation import SettingsMutationCoordinator
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.subagent_types import TaskRecordKind
from app.crud.subagent_dao import SubagentDao
from app.services.agent_identity_service import AgentIdentityService

logger = logging.getLogger(__name__)
TASK_SHUTDOWN_WAIT_SECONDS = 5
TASK_TERMINAL_RETRY_SECONDS = 1

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
        _submission_lock (asyncio.Lock): 串行任务登记与关闭入口。
    """

    def __init__(self, *, runtime_queue: asyncio.Queue[RuntimeRequest], db_path: str | None = None,
                 chat_settings_dao: ChatSettingsDao | None = None,
                 mutation_coordinator: SettingsMutationCoordinator | None = None,
                 identity_service: AgentIdentityService | None = None) -> None:
        """准备持久化和记录队列，应用级共享，不随单轮 Agent 关闭。

        Args:
            db_path (str | None): 当前环境的 SQLite 路径。
            runtime_queue (asyncio.Queue[RuntimeRequest]): 应用创建、Scheduler 独占消费的共享请求队列。
            chat_settings_dao (ChatSettingsDao | None): 提供父配置存在性，不保存额外可用状态。
            mutation_coordinator (SettingsMutationCoordinator | None): 与配置删除、Scheduler 共用的受理短锁。
        """
        self.settings_dao = chat_settings_dao or ChatSettingsDao()
        self.mutation_coordinator = mutation_coordinator or SettingsMutationCoordinator()
        self.records = TaskRecordDao(db_path)
        self.identity = identity_service or AgentIdentityService(self.settings_dao, SubagentDao(self.records.db_path))
        self._owns_identity = identity_service is None
        self.runtime_queue = runtime_queue
        self._tasks: dict[str, BaseTask] = {}
        self._running: set[str] = set()
        self._executions: dict[str, asyncio.Task[None]] = {}
        self._stopping: set[str] = set()
        self._closing = False
        self._submission_lock = asyncio.Lock()
        self._cleanup_pending: dict[str, BaseTask] = {}

    async def submit(self, task: BaseTask, *, runtime: TaskRuntime, name: str,
                     title: str, notify_agent: bool, background: bool = True,
                     tool_name: str | None = None, origin_tool_call_id: str | None = None) -> str:
        """保存通用任务登记后启动执行，返回可查询的执行编号。

        Args:
            task (BaseTask): 提供业务执行和资源清理的具体任务。
            runtime (TaskRuntime): 框架绑定插件、会话、来源轮次及任务编号的调用对象。
            name (str): 供查询或识别本次内容用途的稳定名称。
            title (str): 供用户阅读的任务说明。
            notify_agent (bool): 终态保存时是否生成给 Agent 的结果通知。
            background (bool): 是否立即显示在后台任务列表。
            tool_name (str | None): 来源工具名称，插件任务为空。
            origin_tool_call_id (str | None): 来源工具调用编号，插件任务为空。

        Returns:
            str: 已登记并启动的任务执行编号。
        """
        return await complete_owned_work(self._register(task, runtime=runtime, name=name,
            title=title, notify_agent=notify_agent, background=background,
            tool_name=tool_name, origin_tool_call_id=origin_tool_call_id))

    async def _register(self, task: BaseTask, *, runtime: TaskRuntime, name: str,
                        title: str, notify_agent: bool, background: bool = True,
                        tool_name: str | None = None, origin_tool_call_id: str | None = None) -> str:
        """在同一登记操作中提交记录、发布初态并取得执行所有权。

        Args:
            task (BaseTask): 需要统一执行和清理的具体任务。
            runtime (TaskRuntime): 固定插件及来源轮次的调用对象。
            name (str): 任务稳定名称。
            title (str): 用户可读标题。
            notify_agent (bool): 是否原子保存结果通知。
            background (bool): 是否立即显示在后台任务列表。
            tool_name (str | None): 来源工具名称，插件任务为空。
            origin_tool_call_id (str | None): 来源工具调用编号，插件任务为空。

        Returns:
            str: 已由 Supervisor 持有的任务编号。
        """
        async with self._submission_lock, self.mutation_coordinator.admission_lock:
            if self._closing:
                raise RuntimeError("任务管理器正在关闭")
            await self.identity.require_available(runtime.session_id, runtime.subagent_id)
            task_id = uuid4().hex
            await self.records.insert_running(task_id, runtime.session_id,
                name=name, title=title, plugin_name=runtime.plugin_name, origin_run_id=runtime.run_id,
                notify_agent=notify_agent, background=background,
                tool_name=tool_name, origin_tool_call_id=origin_tool_call_id,
                subagent_id=runtime.subagent_id,
                notification_context_id=runtime.subagent_id or runtime.session_id,
                notification_scope=AgentStorageScope.SUBAGENT if runtime.subagent_id else AgentStorageScope.MAIN)
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
        """业务结束后清理资源，持有原结果直到终态保存完成或应用结束等待。

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
                self._cleanup_pending[task_id] = task
                logger.exception("后台任务清理失败：%s", task_id)
            # 3. 原执行协程继续持有状态、结果和错误，保存失败后只重提终态事务。
            while not persisted:
                try:
                    await complete_owned_work(self.commit_terminal_state_once(task_id, status, result=result, error=error))
                    persisted = True
                except asyncio.CancelledError:
                    # 3.1. 延期取消到达此处时，事务和已保存记录的发布均已完成。
                    persisted = True
                except Exception:
                    logger.exception("任务终态保存失败，保留原结果等待重试：%s", task_id)
                    # 3.2. 应用关闭后结束失败保存，数据库仍为 running，重启按原规则整理。
                    if self._closing:
                        break
                    # 3.3. 延时等待允许数据库恢复，也让关闭等待超时后能够取消该协程。
                    await asyncio.sleep(TASK_TERMINAL_RETRY_SECONDS)
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

    def active_task_count(self) -> int:
        """查询整个应用尚未完成执行、清理或终态保存的任务数。

        Returns:
            int: 执行、清理或终态保存尚未完成的任务数量。
        """
        return len(self._tasks)

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
        async with self.mutation_coordinator.admission_lock:
            previous = await self.records.get(task_id)
            notify = previous is not None and await self.identity.read_availability(previous.session_id, previous.subagent_id)
            if not await self.records.finish_once(task_id, status, result=result, error=error, notify=notify):
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
        return [record for record in await self.records.list(session_id)
                if record.background and record.kind == TaskRecordKind.ORDINARY]


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

    async def ensure_available(self, session_id: str, subagent_id: str | None = None) -> None:
        """模型工具执行前检查配置和子废弃记录，迟到的工具调用明确失败。

        Args:
            session_id (str): 工具所属父配置编号。
            subagent_id (str | None): 子工具的身份，父工具为空。
        """
        async with self.mutation_coordinator.admission_lock:
            await self.identity.require_available(session_id, subagent_id)

    async def stop_subagent_tasks(self, session_id: str, subagent_id: str) -> None:
        """停止并等待本子普通任务，实际清理失败由 Scheduler 自动重试。"""
        await self._stop_tasks([record.task_id for record in await self.records.list(session_id)
            if record.subagent_id == subagent_id and record.kind == TaskRecordKind.ORDINARY])

    async def stop_session_tasks(self, session_id: str) -> None:
        """父配置删除后停止全部所属普通任务，包括子任务。"""
        await self._stop_tasks([record.task_id for record in await self.records.list(session_id)
            if record.kind == TaskRecordKind.ORDINARY])

    async def _stop_tasks(self, selected: list[str]) -> None:
        """复用任务执行协程和清理句柄，等待终态保存后只重试未完成资源清理。"""
        executions = []
        for task_id in selected:
            await self.stop_task(task_id)
            execution = self._executions.get(task_id)
            if execution is not None:
                executions.append(execution)
        if executions:
            await asyncio.gather(*executions)
        for task_id in selected:
            task = self._cleanup_pending.get(task_id)
            if task is not None:
                await complete_owned_work(task.cleanup())
                self._cleanup_pending.pop(task_id)
            if task_id in self._tasks:
                raise RuntimeError("后台任务的终态保存尚未完成")

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
        if self._owns_identity:
            await self.identity.subagents.close()
