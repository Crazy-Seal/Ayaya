"""保存一项普通任务的执行进展、固定结果和结果等待通知。"""

import asyncio
from dataclasses import asdict, dataclass

from app.agent.runtime.tasks.base import BaseTask
from app.agent.runtime.tasks.runtime import TaskRuntime
from app.schemas.runtime_types import CommandReceipt, CommandStatus, TaskExecutionPhase, TaskStatus
from app.schemas.task_types import TaskRecord, TaskSnapshot


TASK_EDGES: dict[TaskExecutionPhase, set[TaskExecutionPhase]] = {
    TaskExecutionPhase.RUNNING: {TaskExecutionPhase.STOPPING, TaskExecutionPhase.COMMITTING},
    TaskExecutionPhase.STOPPING: {TaskExecutionPhase.COMMITTING},
    TaskExecutionPhase.COMMITTING: {TaskExecutionPhase.FINISHED, TaskExecutionPhase.SAVE_FAILED},
    TaskExecutionPhase.FINISHED: set(),
    TaskExecutionPhase.SAVE_FAILED: set(),
}


@dataclass(frozen=True)
class TaskOutcome:
    """固定业务已返回的结果，供同一结果的保存重试使用。

    Attributes:
        status (TaskStatus): 业务成功、失败或停止。
        result (str | None): 已取得的结果正文。
        error (str | None): 业务失败原因。
    """

    status: TaskStatus
    result: str | None = None
    error: str | None = None


def snapshot_record(record: TaskRecord) -> TaskSnapshot:
    """将持久任务转换为只读查询摘要。

    Args:
        record (TaskRecord): 已保存的任务来源和结果。
    Returns:
        TaskSnapshot: 从持久结果推导的任务进展。
    """
    phase = TaskExecutionPhase.RUNNING if record.status == TaskStatus.RUNNING else TaskExecutionPhase.FINISHED
    return TaskSnapshot(**asdict(record), phase=phase)


class TaskExecution:
    """由 Supervisor 驱动一项任务，独立维护业务结束和资源释放。

    Attributes:
        record (TaskRecord): 初始登记的固定来源，查询所需的最新资料由 DAO 提供。
        task (BaseTask | None): 持有具体资源的任务，清理后释放。
        runtime (TaskRuntime | None): 本次任务固定来源，清理后释放。
        runner (asyncio.Task[None] | None): 执行、保存及清理的完整协程。
        business (asyncio.Task[str | None] | None): 可由停止请求取消的业务协程。
        completion (asyncio.Event): 结果已保存或保存明确失败时唤醒观察者。
    """

    def __init__(self, record: TaskRecord, task: BaseTask, runtime: TaskRuntime) -> None:
        """建立刚登记的任务及独立结果等待通知。

        Args:
            record (TaskRecord): 已保存的初始记录。
            task (BaseTask): 具体业务及自有资源。
            runtime (TaskRuntime): 绑定 task_id 的调用来源。
        """
        self.record = record
        self.task: BaseTask | None = task
        self.runtime: TaskRuntime | None = runtime
        self.runner: asyncio.Task[None] | None = None
        self.business: asyncio.Task[str | None] | None = None
        self.completion = asyncio.Event()
        self._phase = TaskExecutionPhase.RUNNING
        self._outcome: TaskOutcome | None = None
        self._save_error: str | None = None

    @property
    def phase(self) -> TaskExecutionPhase:
        """读取本任务唯一阶段。

        Returns:
            TaskExecutionPhase: 当前任务进展。
        """
        return self._phase

    @property
    def outcome(self) -> TaskOutcome:
        """读取已经确定的业务结果。

        Returns:
            TaskOutcome: 保存期间保持不变的结果。
        Raises:
            RuntimeError: 业务尚未返回结果。
        """
        if self._outcome is None:
            raise RuntimeError("任务业务尚未结束")
        return self._outcome

    def _change(self, target: TaskExecutionPhase) -> None:
        """按已审阅的转换表推进任务阶段。

        Args:
            target (TaskExecutionPhase): 本操作要求的下一阶段。
        Raises:
            RuntimeError: 操作与当前任务阶段冲突。
        """
        if target not in TASK_EDGES[self._phase]:
            raise RuntimeError(f"任务阶段错误：{self._phase} -> {target}")
        self._phase = target

    def request_stop(self) -> CommandReceipt:
        """受理业务停止；保存开始后返回太迟。

        Returns:
            CommandReceipt: 固定来源关联的停止受理结果。
        """
        status = CommandStatus.TOO_LATE
        if self._phase in {TaskExecutionPhase.RUNNING, TaskExecutionPhase.STOPPING}:
            if self._phase == TaskExecutionPhase.RUNNING:
                self._change(TaskExecutionPhase.STOPPING)
            status = CommandStatus.ACCEPTED
        return CommandReceipt(status, self.record.subagent_id or self.record.session_id, self.record.origin_run_id)

    def begin_commit(self, outcome: TaskOutcome) -> None:
        """固定业务结果并开始保存。

        Args:
            outcome (TaskOutcome): 业务已结束的原始结果。
        """
        self._change(TaskExecutionPhase.COMMITTING)
        self._outcome = outcome

    def commit_succeeded(self) -> None:
        """确认结果已经提交并结束结果等待。"""
        self._change(TaskExecutionPhase.FINISHED)
        self.completion.set()

    def commit_failed(self, error: str) -> None:
        """记录保存耗尽并结束结果等待。

        Args:
            error (str): 事务确认失败的原因。
        """
        self._change(TaskExecutionPhase.SAVE_FAILED)
        self._save_error = error
        self.completion.set()

    def snapshot(self, record: TaskRecord) -> TaskSnapshot:
        """提供任务当前进展、已保存的终端信息和结果保存错误。

        Args:
            record (TaskRecord): 查询时读取的持久记录，包含已保存的终端信息和后台标记。
        Returns:
            TaskSnapshot: 保存失败时覆盖数据库旧 running，并保留原业务结果。
        """
        values = asdict(record)
        if self._phase == TaskExecutionPhase.SAVE_FAILED:
            values.update(status=TaskExecutionPhase.SAVE_FAILED, result=self.outcome.result, error=self._save_error)
        return TaskSnapshot(**values, phase=self._phase, save_error=self._save_error)

    def release_resources(self) -> None:
        """清理结束后释放具体任务、来源能力及协程引用。"""
        self.task = None
        self.runtime = None
        self.runner = None
        self.business = None
