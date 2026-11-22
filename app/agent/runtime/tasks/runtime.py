"""约定 Supervisor 向具体任务传递的来源及任务编号绑定方法。"""

from __future__ import annotations

from typing import Protocol


class TaskRuntime(Protocol):
    """插件与工具运行时共同提供固定来源，具体任务按需要使用对应能力。

    Attributes:
        session_id (str): 来源会话。
        plugin_name (str | None): 插件来源，工具任务为空。
        task_id (str | None): 本次任务编号，提交前为空。
    """

    @property
    def session_id(self) -> str:
        """读取固定来源父会话编号。"""
        ...

    @property
    def plugin_name(self) -> str | None:
        """读取插件来源，工具任务为空。"""
        ...

    @property
    def task_id(self) -> str | None:
        """读取本次任务编号。"""
        ...

    @property
    def subagent_id(self) -> str | None:
        """读取来源子上下文编号。"""
        ...

    def bind_task(self, task_id: str) -> TaskRuntime:
        """创建绑定本次编号的运行时，保留原调用来源。

        Args:
            task_id (str): Supervisor 登记的任务编号。

        Returns:
            TaskRuntime: 供具体任务执行使用的新运行时。
        """
        ...
