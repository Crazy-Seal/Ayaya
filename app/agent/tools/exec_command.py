"""在自动探测的 Shell 中启动受管任务。"""

import asyncio

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from app.agent.models.contracts import ToolDefinition
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.core.schema import tool_parameters_schema
from app.agent.tools.terminal.validation import failure, validate_input
from app.agent.utils.infra.safe_path import WORKDIR, safe_path
from app.agent.tools.terminal.shell import detect_shell
from app.agent.tools.terminal.service import TerminalService
from app.agent.tools.terminal.task import TerminalTask
from app.agent.utils.infra.owned_work import complete_owned_work
from app.environment import get_data_dir
from app.schemas.task_types import TERMINAL_TASK_NAME


class ExecCommandArguments(BaseModel):
    """校验一次命令启动的模型输入，缺省字段使用真实默认值。"""

    model_config = ConfigDict(extra="forbid")
    cmd: StrictStr = Field(min_length=1, description="使用当前 Shell 语法编写的命令。")
    workdir: StrictStr | None = Field(default=None, description="工作区内的工作目录，省略时默认使用工作区根目录。")
    tty: StrictBool = Field(default=False, description="是否使用 PTY 虚拟终端，运行交互式命令时应启用。")
    yield_time_ms: int = Field(default=10000, ge=0, le=30000, strict=True, description="等待命令执行的毫秒数；默认10000表示10秒，超时不终止命令，转为后台运行。该值为0时表示不等待，直接使用后台运行")


class ExecCommandTool(BaseTool):
    """运行Shell命令"""

    name = "exec_command"
    description = (
        "启动一个当前默认的 Shell 终端任务，执行对应的 Shell 命令。"
        "任务后台运行时，你可以根据本工具返回的任务编号 task_id ，调用 write_stdin 读取命令的输出增量或继续向其发送输入。"
        "任务运行结束时，会将完成状态和完整输出日志位置主动通知你。禁止运行可能损害计算机系统的命令。"
    )
    parameters_schema = tool_parameters_schema(ExecCommandArguments)

    def __init__(self) -> None:
        """准备系统终端创建能力，每次 execute 单独创建具体任务。"""
        self._terminal = TerminalService()

    def definition(self) -> ToolDefinition:
        """在模型请求定义中追加实际 Shell 信息，不改变权限目录静态描述。

        Returns:
            ToolDefinition: 当前环境对应的工具定义。
        """
        try:
            environment = detect_shell().describe()
        except RuntimeError as exc:
            environment = str(exc)
        return ToolDefinition(self.name, self.description + "\n" + environment, self.parameters_schema)

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """校验参数和工作目录后，将生命周期交给共享任务管理器。

        Args:
            args (dict): 模型提供的命令启动参数。
            context (ToolContext): Pipeline 注入的聊天与来源信息。

        Returns:
            ToolResult: 当前观察或尚未执行的错误提示。
        """
        try:
            options = ExecCommandArguments.model_validate(args)
            validate_input(options.cmd)
            directory = safe_path(options.workdir) if options.workdir is not None else WORKDIR
            if not directory.is_dir():
                raise ValueError("工作目录不存在或不是目录")
            if context.runtime is None:
                raise ValueError("后台任务运行器未就绪")
            shell = await asyncio.to_thread(detect_shell)
        except (ValueError, RuntimeError, OSError) as exc:
            return failure(str(exc))
        runtime = context.runtime
        task = TerminalTask(options.cmd, shell.command(options.cmd), str(directory),
            options.tty, get_data_dir() / "tasks", runtime.records, self._terminal)
        task_id = None

        async def submit() -> str:
            """完整完成提交，并让取消后的调用方保留实际任务编号。

            Returns:
                str: 已由 Supervisor 持有的编号。
            """
            nonlocal task_id
            task_id = await runtime.submit_task(task, name=TERMINAL_TASK_NAME, title=options.cmd,
                notify_agent=True, background=options.yield_time_ms == 0)
            return task_id

        try:
            # 1. 任务登记完成后再观察输出，调用者取消仍保留同一后台任务。
            await complete_owned_work(submit())
            result = await task.interact(runtime, yield_time_ms=options.yield_time_ms, initial=True)
            if result.background:
                await runtime.mark_background(task_id)
        except asyncio.CancelledError:
            if task_id is not None:
                await complete_owned_work(runtime.mark_background(task_id))
            raise
        except (OSError, RuntimeError) as exc:
            return failure(f"启动或登记任务失败；命令可能已有操作，请勿自动重试：{exc}")
        return ToolResult.success(result.render())
