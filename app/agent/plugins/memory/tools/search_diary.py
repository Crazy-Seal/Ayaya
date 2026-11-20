"""由记忆插件装配查询工具，检索当前调用上下文的已保存记忆。"""

from datetime import date
from app.agent.plugins.memory.operation import MemoryOperation
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult


class SearchDiaryTool(BaseTool):
    """使用所属记忆插件绑定的管理器查询本会话记忆。"""

    name = 'search_diary'
    parent_only = True
    description = '搜索指定日期范围的日记。返回[start, end]范围内的日记内容(包括end那天的)，按日期排序，最多显示前5篇日记，超出数量的会提示未显示的篇数。'
    compressible = True
    parameters_schema = {'type': 'object', 'properties': {'start': {'type': 'string', 'description': '开始日期，格式 YYYY-MM-DD'}, 'end': {'type': 'string', 'description': '结束日期，格式 YYYY-MM-DD'}}, 'required': ['start', 'end']}

    def __init__(self, operation: MemoryOperation) -> None:
        """绑定插件提供的查询入口。

        Args:
            operation (MemoryOperation): 当前配置及操作内资源创建入口。
        """
        self._operation = operation

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """执行用户授权的记忆检索并返回可显示的结果。

        Args:
            args (dict): 模型提供的检索词或日期范围。
            context (ToolContext): 提供本次调用的真实会话编号。

        Returns:
            ToolResult: 记忆检索结果或查询错误说明。
        """
        text = await self._search(args, context)
        return ToolResult.success(text)

    async def _search(self, args: dict, context: ToolContext) -> str:
        """查询所属会话的持久记忆，将检索异常转换为工具结果说明。

        Args:
            args (dict): 已通过工具参数校验的检索条件。
            context (ToolContext): 当前工具的会话上下文。

        Returns:
            str: 格式化记忆内容或查询错误。
        """
        start, end = args.get("start", ""), args.get("end", "")
        session_id = context.session_id
        try:
            if session_id is None:
                return "错误: 缺少会话id信息，无法查找日记。"

            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)

            async with self._operation.open(session_id, diary=True) as manager:
                return await manager.search_diary(start_date, end_date)

        except ValueError as e:
            return f"错误: {e}"
        except Exception as e:
            return f"错误: {e}"
