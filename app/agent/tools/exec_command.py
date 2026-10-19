"""在自动探测的 Shell 中启动受管任务。"""

import asyncio

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from app.agent.models.contracts import ToolDefinition
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.core.schema import tool_parameters_schema
from app.agent.tools.terminal.validation import failure, validate_input
from app.agent.utils.infra.safe_path import WORKDIR, safe_path
from app.agent.tools.terminal.shell import detect_shell


class ExecCommandArguments(BaseModel):
    """校验一次命令启动的模型输入，缺省字段使用真实默认值。"""

    model_config = ConfigDict(extra="forbid")
    cmd: StrictStr = Field(min_length=1, description="使用当前 Shell 语法编写的命令。")
    workdir: StrictStr | None = Field(default=None, description="工作区内的工作目录，省略时默认使用工作区根目录。")
    tty: StrictBool = Field(default=False, description="是否使用 PTY 虚拟终端")
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
            if context.task_launcher is None:
                raise ValueError("后台任务运行器未就绪")
            shell = await asyncio.to_thread(detect_shell)
        except (ValueError, RuntimeError, OSError) as exc:
            return failure(str(exc))
        try:
            result = await context.task_launcher.launch(
                context.session_id, options.cmd, shell.command(options.cmd),
                cwd=str(directory), tty=options.tty, yield_time_ms=options.yield_time_ms,
                tool_name=context.origin_tool_name or self.name,
                origin_run_id=context.origin_run_id or None,
                origin_tool_call_id=context.origin_tool_call_id or None,
            )
        except (OSError, RuntimeError) as exc:
            return failure(f"启动或登记任务失败；命令可能已有操作，请勿自动重试：{exc}")
        return ToolResult.success(result.render())
