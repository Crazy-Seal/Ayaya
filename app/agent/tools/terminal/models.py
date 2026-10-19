"""终端工具与应用级任务协调共用的观察结果契约。"""

from dataclasses import dataclass

from app.schemas.runtime_types import TaskStatus


@dataclass(frozen=True)
class TaskLaunchResult:
    """向工具返回一次观察，不附加完整日志引用。

    Attributes:
        task_id (str): 稳定任务编号。
        background (bool): 当前任务是否仍在运行。
        output (str): 本次读取的有限输出。
        task_status (TaskStatus): 数据库已提交的任务状态。
        exit_code (int | None): 已提交的退出码。
        error (str | None): 输入失败或运行器故障说明。
    """

    task_id: str
    background: bool
    output: str
    task_status: TaskStatus = TaskStatus.COMPLETED
    exit_code: int | None = None
    error: str | None = None

    def render(self, *, include_task_id: bool = True) -> str:
        """以简短状态说明和本次输出回答工具调用，不序列化内部字段。

        Args:
            include_task_id (bool): 启动命令时提供新任务编号；交互时不重复调用参数中的编号。

        Returns:
            str: 状态说明在前，已有输出原样接在后面，再交统一长结果处理器。
        """
        # 1. 运行中只说明仍在运行；结束后说明真实终态及退出码，不输出空字段和固定操作提示。
        if self.background:
            details = ["任务仍在运行"]
        else:
            labels = {
                TaskStatus.COMPLETED: "任务已完成",
                TaskStatus.FAILED: "任务执行失败",
                TaskStatus.STOPPED: "任务已停止",
                TaskStatus.ORPHANED: "任务已失去进程关联",
            }
            details = [labels[self.task_status],
                       f"退出码 {self.exit_code}" if self.exit_code is not None else "退出码未知"]
        if include_task_id:
            details.append(f"task_id={self.task_id}")
        text = "，".join(details) + "。"
        # 2. 输入或读取发生异常时直接解释；正常结果不附加输入状态、待读标记或输出字段标签。
        if self.error:
            text += f"\n错误：{self.error}"
        if self.output:
            text += "\n\n" + self.output
        return text
