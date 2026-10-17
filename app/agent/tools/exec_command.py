"""在自动探测的 Shell 中启动受管任务。"""

import asyncio

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from app.agent.models.contracts import ToolDefinition
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.terminal.validation import failure, validate_input
from app.agent.utils.infra.safe_path import WORKDIR, safe_path
from app.agent.tools.terminal.shell import detect_shell


class ExecCommandArguments(BaseModel):
    """校验一次命令启动的模型输入，缺省字段使用真实默认值。"""

    model_config = ConfigDict(extra="forbid")
    cmd: StrictStr = Field(min_length=1, description="使用当前 Shell 语法编写的命令。")
    workdir: StrictStr | None = Field(default=None, description="工作区内的工作目录，省略时使用工作区根目录。")
    tty: StrictBool = Field(default=False, description="是否使用 PTY；普通管道也能写入 stdin。")
    yield_time_ms: int = Field(default=10000, ge=0, le=30000, strict=True, description="本次等待毫秒数；0 表示立即返回，超时不终止进程。")


class ExecCommandTool(BaseTool):
    """启动命令并返回当前输出，超时后保留同一受管进程。"""

    name = "exec_command"
    description = (
        "执行当前默认 Shell 命令。默认等待 10 秒，0 表示启动后立即返回；等待结束不会杀死进程。"
        "使用返回的 task_id 调用 write_stdin 读取增量或发送输入。短结果直接返回，长结果由系统统一保存和裁剪。"
        "任务结束时会主动通知状态和完整日志引用。禁止运行损害系统的命令。"
    )
    parameters_schema = ExecCommandArguments.model_json_schema()

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
            return failure(f"启动或登记任务失败；命令可能已有操作，请勿自动重试：{exc}", "unknown")
        return ToolResult.success(result.render())
