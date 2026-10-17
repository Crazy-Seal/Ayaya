"""仅供历史未执行调用恢复的 PowerShell 工具兼容入口，不向新模型请求公开。"""

from dataclasses import replace

from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.exec_command import ExecCommandTool
from app.agent.tools.terminal.validation import failure
from app.agent.tools.terminal.shell import detect_shell


def _build_command(command: str) -> list[str]:
    """兼容旧调用入口，按当前实际 Shell 构造启动参数。

    Args:
        command (str): 历史命令正文。

    Returns:
        list[str]: 当前实际 Shell 的执行参数。
    """
    return detect_shell().command(command)


async def _run_command(command: str, context: ToolContext | None, *, run_in_background: bool = False) -> str:
    """将尚未执行的历史调用转换为新工具，不复制进程管理逻辑。

    Args:
        command (str): 旧调用保存的命令。
        context (ToolContext | None): Pipeline 恢复旧调用时提供的上下文。
        run_in_background (bool): 旧显式后台参数，转换为零等待。

    Returns:
        str: 新工具的观察结果正文。
    """
    if context is None:
        return failure("缺少工具上下文").text
    result = await ExecCommandTool().execute(
        {"cmd": command, "yield_time_ms": 0 if run_in_background else 10000},
        replace(context, origin_tool_name=context.origin_tool_name or "run_ps"),
    )
    return result.text


class RunPsTool(BaseTool):
    """保留旧名称和参数契约以恢复历史审批，不加入工具注册目录。"""

    name = "run_ps"
    description = "历史终端工具的隐藏兼容入口。"
    parameters_schema = {
        "type": "object", "properties": {
            "command": {"type": "string"},
            "run_in_background": {"type": "boolean", "default": False},
        }, "required": ["command"], "additionalProperties": False,
    }

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """在统一授权之后转换旧参数，来源工具名仍然保留 run_ps。

        Args:
            args (dict): 历史调用原始参数。
            context (ToolContext): 当前恢复执行的上下文。

        Returns:
            ToolResult: 新终端实现的执行结果。
        """
        if not isinstance(args.get("command"), str) or not isinstance(args.get("run_in_background", False), bool):
            return failure("历史调用参数无效，命令未启动")
        return ToolResult.success(await _run_command(
            args["command"], context, run_in_background=args.get("run_in_background", False),
        ))
