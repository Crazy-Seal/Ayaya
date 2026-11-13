"""向受管任务写入 stdin 或读取运行中的增量。"""

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.core.schema import tool_parameters_schema
from app.agent.tools.terminal.validation import failure, validate_input
from app.agent.tools.terminal.task import TerminalTask
from app.agent.tools.terminal.models import TaskLaunchResult
from app.schemas.runtime_types import TaskStatus


class WriteStdinArguments(BaseModel):
    """校验输入与增量读取参数；空字符串表示只读取。"""

    model_config = ConfigDict(extra="forbid")
    task_id: StrictStr = Field(min_length=1, description="需要交互的任务编号。")
    chars: StrictStr = Field(default="", description="给该任务终端发送的输入，不自动补换行符，如需换行，你得自己换。若为空字符串，则仅读取任务的输出增量。")
    yield_time_ms: int = Field(default=10000, ge=0, le=30000, strict=True, description="等待命令执行的毫秒数；默认10000表示10秒，超时不终止命令，转为后台运行。该值为0时表示不等待，直接使用后台运行")


class WriteStdinTool(BaseTool):
    """复用 task_id 与同一个进程交互，不启动或重放命令。"""

    name = "write_stdin"
    description = (
        "与任务进程交互。向编号为 task_id 的任务继续发送输入并返回输出增量，chars 为空时只返回输出增量。"
        "无论是否启用 PTY ，均可与任务交互。"
        "任务后台运行时，你可以重复调用本工具读取命令的输出增量或继续向其发送输入。"
        "任务运行结束时，会将完成状态和完整输出日志位置主动通知你。"
        "不要因等待太久而随便重发输入。PTY 中可发送 \\u0003 中断命令执行。"
        "Windows PTY 中使用 \\r 换行，普通管道和 Unix 通常使用 \\n；本工具不会帮你转换。"
    )
    parameters_schema = tool_parameters_schema(WriteStdinArguments)

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
            if context.runtime is None:
                raise ValueError("后台任务运行器未就绪")
            runtime = context.runtime
            record = await runtime.get_task_snapshot(options.task_id)
            if record is None:
                raise KeyError("任务不存在或不属于当前聊天")
            task = await runtime.get_task(options.task_id)
            if isinstance(task, TerminalTask):
                result = await task.interact(runtime, chars=options.chars, yield_time_ms=options.yield_time_ms)
            else:
                # 1. 查询对象期间可能刚完成清理，重新读取已提交记录再回答终态。
                record = await runtime.get_task_snapshot(options.task_id)
                if record.status != TaskStatus.RUNNING and record.output_path:
                    result = TaskLaunchResult(options.task_id, False, "", record.status, record.exit_code,
                        error="任务已结束，输入未发送" if options.chars else None)
                else:
                    raise ValueError("此任务没有可交互的终端")
        except (ValueError, KeyError, PermissionError) as exc:
            return failure(str(exc))
        return ToolResult.success(result.render(include_task_id=False))
