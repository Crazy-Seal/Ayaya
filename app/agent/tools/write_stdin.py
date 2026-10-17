"""向受管任务写入 stdin 或读取运行中的增量。"""

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.terminal.validation import failure, validate_input


class WriteStdinArguments(BaseModel):
    """校验输入与增量读取参数；空字符串表示只读取。"""

    model_config = ConfigDict(extra="forbid")
    task_id: StrictStr = Field(min_length=1, description="exec_command 返回的任务编号。")
    chars: StrictStr = Field(default="", description="原样发送的输入，不自动补换行；空字符串只读取。")
    yield_time_ms: int = Field(default=10000, ge=0, le=30000, strict=True, description="本次等待毫秒数，范围 0～30000，超时不终止进程。")


class WriteStdinTool(BaseTool):
    """复用 task_id 与同一个进程交互，不启动或重放命令。"""

    name = "write_stdin"
    description = (
        "向 exec_command 返回的 task_id 原样发送 chars，不自动补换行；chars 为空时只读取增量。"
        "默认等待 10 秒；0 表示立即读取，超时不结束任务。管道和 PTY 均可交互。"
        "任务结束后只返回状态，不返回剩余正文；完整日志引用由完成通知提供。"
        "不要因等待超时自动重发输入。PTY 中可发送 \\u0003 中断前台命令。"
        "Windows PTY 中按回车发送 \\r，普通管道和 Unix 通常使用 \\n；工具不会转换。"
    )
    parameters_schema = WriteStdinArguments.model_json_schema()

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """校验聊天归属后读取或写入，由运行器表达输入是否已经发送。

        Args:
            args (dict): 模型提供的任务编号、输入和等待预算。
            context (ToolContext): 当前聊天的受控任务端口。

        Returns:
            ToolResult: 当前增量、已结束提示或输入未发送的错误。
        """
        try:
            options = WriteStdinArguments.model_validate(args)
            validate_input(options.chars)
            if context.task_launcher is None:
                raise ValueError("后台任务运行器未就绪")
            result = await context.task_launcher.interact(
                context.session_id, options.task_id, chars=options.chars,
                yield_time_ms=options.yield_time_ms,
            )
        except (ValueError, KeyError, PermissionError) as exc:
            return failure(str(exc))
        return ToolResult.success(result.render())
