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
        has_more (bool): 日志中是否还有本次未读取的内容。
        input_status (str): not_sent、sent、unknown 或 none。
        error (str | None): 输入失败或运行器故障说明。
    """

    task_id: str
    background: bool
    output: str
    task_status: TaskStatus = TaskStatus.COMPLETED
    exit_code: int | None = None
    has_more: bool = False
    input_status: str = "none"
    error: str | None = None

    def render(self) -> str:
        """生成固定元信息和原始正文，再交统一长结果处理器。

        Returns:
            str: 元信息放在正文前，长结果裁剪时仍保留任务身份。
        """
        status = "error" if self.error else "success"
        summary = self.error or ("任务仍在运行" if self.background else "任务已结束")
        action = (
            "使用 write_stdin(task_id=...) 继续读取或交互；不要重复发送已经发送的输入。"
            if self.background else "请查看完成通知；需要完整输出时读取通知中的日志引用。"
        )
        return (
            f"status: {status}\nsummary: {summary}\ntask_id: {self.task_id}\n"
            f"task_status: {self.task_status.value}\nexit_code: {self.exit_code}\n"
            f"input_status: {self.input_status}\nhas_more: {str(self.has_more).lower()}\n"
            f"next_actions: {action}\noutput:\n{self.output}"
        )
