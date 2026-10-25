"""提供绑定工具调用来源的通用任务能力。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Callable, ClassVar

from app.schemas.runtime_types import TaskLifetime
from app.schemas.task_types import RuntimeRequest, TaskRecord, TaskStopRequest

if TYPE_CHECKING:
    from app.agent.runtime.tasks.base import BaseTask
    from app.crud.task_record_dao import TaskRecordDao
    from app.agent.runtime.tasks.task_supervisor import TaskSupervisor


@dataclass(frozen=True)
class ToolRuntime:
    """保存单次工具来源，提供任务提交、会话内查询和单向停止请求。

    Attributes:
        session_id (str): 当前工具所属会话。
        run_id (str): 来源轮次，后台执行期间保持不变。
        tool_name (str): 当前调用的工具名称。
        tool_call_id (str): 模型工具调用编号。
        task_id (str | None): 具体任务执行编号，工具入口中为空。
        plugin_name (None): 工具提交的任务没有插件来源。
        _supervisor (TaskSupervisor): 应用共享的任务管理对象。
        _runtime_queue (asyncio.Queue[RuntimeRequest]): 应用共享请求队列。
    """

    session_id: str
    run_id: str
    tool_name: str
    tool_call_id: str
    _supervisor: TaskSupervisor = field(repr=False)
    _runtime_queue: asyncio.Queue[RuntimeRequest] = field(repr=False)
    task_id: str | None = None
    plugin_name: ClassVar[None] = None

    @property
    def records(self) -> TaskRecordDao:
        """提供具体任务保存专用执行信息所需的共享存储，终态仍由 Supervisor 提交。

        Returns:
            TaskRecordDao: 与 Supervisor 相同的记录事务入口。
        """
        return self._supervisor.records

    def bind_task(self, task_id: str) -> ToolRuntime:
        """绑定任务编号，保留工具、会话及轮次来源。

        Args:
            task_id (str): 已登记的执行编号。

        Returns:
            ToolRuntime: 供具体任务使用的调用对象。
        """
        return replace(self, task_id=task_id)

    async def submit_task(self, task: BaseTask, *, name: str, title: str,
                          lifetime: TaskLifetime, notify_agent: bool, background: bool = True) -> str:
        """统一提交具体任务，返回已经登记的编号。

        Args:
            task (BaseTask): 实际执行业务及释放资源的任务。
            name (str): 稳定任务名称。
            title (str): 用户可读标题。
            lifetime (TaskLifetime): 随轮结束或独立运行。
            notify_agent (bool): 是否保存结果通知。
            background (bool): 是否立即在后台列表显示。

        Returns:
            str: 已登记并启动的编号。
        """
        return await self._supervisor.submit(task, runtime=self, name=name, title=title,
            lifetime=lifetime, notify_agent=notify_agent, background=background,
            tool_name=self.tool_name, origin_tool_call_id=self.tool_call_id)

    async def get_task_record(self, task_id: str) -> TaskRecord | None:
        """查询当前会话内的持久任务记录，允许不同工具协作。

        Args:
            task_id (str): 目标任务编号。

        Returns:
            TaskRecord | None: 会话内记录，未知或其他会话编号为空。
        """
        record = await self._supervisor.get_task_record(task_id)
        return record if record and record.session_id == self.session_id else None

    async def get_task(self, task_id: str) -> BaseTask | None:
        """取得会话内尚未释放的具体任务，供工具使用其业务交互方法。

        Args:
            task_id (str): 目标任务编号。

        Returns:
            BaseTask | None: 实际运行对象，已释放、未知或越范围时为空。
        """
        if await self.get_task_record(task_id) is None:
            return None
        return self._supervisor.get_task(task_id)

    async def wait_task(self, task_id: str, *, timeout_ms: int) -> TaskRecord | None:
        """有限等待任务收尾，等待者取消不停止具体任务。

        Args:
            task_id (str): 目标任务编号。
            timeout_ms (int): 等待毫秒预算。

        Returns:
            TaskRecord | None: 当前已保存记录，未知或越范围时为空。
        """
        if await self.get_task_record(task_id) is None:
            return None
        await self._supervisor.wait(task_id, timeout_ms=timeout_ms)
        return await self.get_task_record(task_id)

    async def publish_task_record(self, task_id: str) -> None:
        """向共享队列发布已经保存的会话内记录。

        Args:
            task_id (str): 已保存的目标任务编号。
        """
        record = await self.get_task_record(task_id)
        if record is not None:
            self._runtime_queue.put_nowait(record)

    async def mark_background(self, task_id: str) -> None:
        """保存后台显示标记后发布状态，任务继续使用同一执行编号。

        Args:
            task_id (str): 当前会话已经提交的编号。

        Raises:
            KeyError: 编号未知或属于其他会话。
        """
        if await self.get_task_record(task_id) is None:
            raise KeyError("任务不存在或不属于当前聊天")
        await self.records.mark_background(task_id)
        await self.publish_task_record(task_id)

    async def stop_task(self, task_id: str) -> None:
        """发送单向停止请求，由 Scheduler 校验会话后受理。

        Args:
            task_id (str): 目标任务编号。
        """
        await self._runtime_queue.put(TaskStopRequest(self.session_id, self.run_id,
            self.tool_name, self.tool_call_id, task_id))


ToolRuntimeFactory = Callable[[str, str, str], ToolRuntime]
