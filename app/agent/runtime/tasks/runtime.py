"""约定 Supervisor 向具体任务传递的来源及任务编号绑定方法。"""

from __future__ import annotations

from typing import Protocol, Self


class TaskRuntime(Protocol):
    """插件与工具运行时共同提供固定来源，具体任务按需要使用对应能力。

    Attributes:
        session_id (str): 来源会话。
        run_id (str): 固定来源轮次。
        plugin_name (str | None): 插件来源，工具任务为空。
        task_id (str | None): 本次任务编号，提交前为空。
    """

    session_id: str
    run_id: str
    plugin_name: str | None
    task_id: str | None

    def bind_task(self, task_id: str) -> Self:
        """创建绑定本次编号的运行时，保留原调用来源。

        Args:
            task_id (str): Supervisor 登记的任务编号。

        Returns:
            Self: 供具体任务执行使用的新运行时。
        """
        ...
