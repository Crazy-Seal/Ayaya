"""提供主 Agent 主动清空应用 Python 状态的原生工具。"""

from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.repl.repl_service import ReplService
from app.schemas.tool_types import PYTHON_RESET_TOOL_NAME


class PythonResetTool(BaseTool):
    """空闲时清空持久命名空间及导入模块，下一调用重新装配能力。"""

    name = PYTHON_RESET_TOOL_NAME
    description = "重置 Python REPL，清空变量、函数和导入等资源。"
    parameters_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, service: ReplService) -> None:
        """保存应用提供的唯一 REPL。

        Args:
            service (ReplService): 应用持有的 Python 执行组件。
        """
        self._service = service

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """请求空闲重置，忙或关闭状态作为工具错误交回。

        Args:
            args (dict): 该工具的参数为空。
            context (ToolContext): 当前会话的工具上下文。

        Returns:
            ToolResult: 已重置或当前无法重置的说明。
        """
        if args:
            return ToolResult.success("python_reset 不接受参数")
        try:
            await self._service.reset()
        except RuntimeError as exc:
            return ToolResult.success(str(exc))
        return ToolResult.success("Python REPL 已重置，下次执行重新装配能力")
