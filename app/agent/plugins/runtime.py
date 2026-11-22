"""提供绑定插件来源的任务操作和单向请求方法。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable

from app.schemas.task_types import PluginRequest, PluginRequestKind, TaskSnapshot
from app.agent.runtime.requests import RuntimeRequest

if TYPE_CHECKING:
    from app.agent.runtime.tasks.base import BaseTask
    from app.agent.runtime.tasks.task_supervisor import TaskSupervisor


@dataclass(frozen=True)
class PluginRuntime:
    """保存固定调用来源，共享应用任务能力，不持有 Scheduler。

    Attributes:
        plugin_name (str): 插件稳定名称。
        session_id (str): 请求所属会话。
        task_id (str | None): 后台任务的执行编号，钩子调用时为空。
        _supervisor (TaskSupervisor): 内部任务提交、查询和有限等待能力。
        _runtime_queue (asyncio.Queue): 内部投递队列，由 Scheduler 独占消费。
    """

    plugin_name: str
    session_id: str
    _supervisor: TaskSupervisor = field(repr=False)
    _runtime_queue: asyncio.Queue[RuntimeRequest] = field(repr=False)
    task_id: str | None = None
    subagent_id: str | None = None

    def bind_task(self, task_id: str) -> PluginRuntime:
        """为具体任务绑定编号，保留所属插件和会话。

        Args:
            task_id (str): 已登记的任务编号。

        Returns:
            PluginRuntime: 供具体任务使用的新调用对象。
        """
        return replace(self, task_id=task_id)

    async def submit_task(self, task: BaseTask, *, name: str, title: str,
                          notify_agent: bool) -> str:
        """提交任务，登记成功后取得编号，执行和收尾交给 Supervisor。

        Args:
            task (BaseTask): 提供业务执行和资源清理的具体任务。
            name (str): 供查询或识别本次内容用途的稳定名称。
            title (str): 供用户阅读的任务说明。
            notify_agent (bool): 终态保存时是否生成给 Agent 的结果通知。

        Returns:
            str: 已登记并启动的任务执行编号。
        """
        return await self._supervisor.submit(task, runtime=self, name=name, title=title,
                                            notify_agent=notify_agent)

    async def get_task(self, task_id: str) -> TaskSnapshot | None:
        """查询本插件在当前会话中的任务，不存在或超出范围时返回 None。

        Args:
            task_id (str): 本次后台执行的稳定编号。

        Returns:
            TaskSnapshot | None: 当前可访问的任务摘要，包含保存故障，越范围时为空。
        """
        record = await self._supervisor.get_task_snapshot(task_id)
        return record if record and (record.session_id, record.plugin_name, record.subagent_id) == (
            self.session_id, self.plugin_name, self.subagent_id) else None

    async def wait_task(self, task_id: str, *, timeout_ms: int) -> TaskSnapshot | None:
        """有限等待可访问任务，超时返回当前记录，取消等待不停止任务。

        Args:
            task_id (str): 本次后台执行的稳定编号。
            timeout_ms (int): 本次等待的毫秒预算，耗尽后任务继续运行。

        Returns:
            TaskSnapshot | None: 结果或保存故障摘要，等待超时返回当前进展，越范围时为空。
        """
        if await self.get_task(task_id) is None:
            return None
        await self._supervisor.wait(task_id, timeout_ms=timeout_ms)
        return await self.get_task(task_id)

    async def _send(self, kind: PluginRequestKind, **kwargs: Any) -> None:
        """复制请求载荷并入队，调用完成仅表示已入队。

        Args:
            kind (PluginRequestKind): 需要 Scheduler 受理的插件动作。
            kwargs (Any): 本次请求的任务目标、通知正文或前端事件内容。
        """
        await self._runtime_queue.put(PluginRequest(kind, self.plugin_name, self.session_id,
            source_task_id=self.task_id, **kwargs))

    async def stop_task(self, task_id: str) -> None:
        """投递停止任务请求，由 Scheduler 检查所属插件和会话。

        Args:
            task_id (str): 本次后台执行的稳定编号。
        """
        await self._send(PluginRequestKind.STOP_TASK, task_id=task_id)

    async def notify(self, *, name: str, text: str, activate_session: bool = False) -> None:
        """投递持久插件通知，保存和后续交付由 Scheduler 安排。

        Args:
            name (str): 供查询或识别本次内容用途的稳定名称。
            text (str): 需要保存或显示的正文。
            activate_session (bool): 保存成功后是否恢复所属会话的执行资格。
        """
        await self._send(PluginRequestKind.NOTIFY, name=name, text=text, activate_session=activate_session)

    async def stop_turn(self) -> None:
        """请求结束受理时会话的当前执行，保留已有自动执行资格。"""
        await self._send(PluginRequestKind.STOP_TURN)

    async def steer_turn(self, text: str, *, activate_session: bool = False) -> None:
        """提交引导正文，Scheduler 先保存正文，再请求结束会话当前执行。

        Args:
            text (str): 需要保存或显示的正文。
            activate_session (bool): 引导保存成功后是否恢复所属会话的执行资格。
        """
        await self._send(PluginRequestKind.STEER_TURN, text=text, activate_session=activate_session)

    async def publish_event(self, name: str, payload: dict[str, Any]) -> None:
        """投递框架支持的前端事件，复制载荷以保留投递时内容。

        Args:
            name (str): 框架支持的前端事件名称，notice 为短暂提示，status_notice 为持续提示。
            payload (dict[str, Any]): 发送前复制的展示载荷；持续提示使用 id、active 及开启时的 text。
        """
        from copy import deepcopy
        await self._send(PluginRequestKind.PUBLISH_EVENT, name=name, payload=deepcopy(payload))


PluginRuntimeFactory = Callable[[str], PluginRuntime]
