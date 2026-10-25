"""定义后台任务提供的业务执行和资源释放方法。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agent.runtime.tasks.runtime import TaskRuntime


class BaseTask(ABC):
    """由 Supervisor 启动和收尾的具体任务，不保存生命周期管理对象。"""

    @abstractmethod
    async def run(self, runtime: TaskRuntime) -> str | None:
        """执行业务工作，返回供记录和按配置通知 Agent 的结果正文。

        Args:
            runtime (TaskRuntime): 框架绑定本次来源与任务编号的调用对象。

        Returns:
            str | None: 最终结果正文，无正文时为空。
        """

    async def cleanup(self) -> None:
        """由 Supervisor 在执行结束后调用，有资源的任务覆盖此方法。"""
