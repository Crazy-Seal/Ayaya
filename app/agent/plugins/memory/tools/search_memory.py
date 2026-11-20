"""由记忆插件装配查询工具，检索当前调用上下文的已保存记忆。"""

from app.agent.plugins.memory.operation import MemoryOperation
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult


class SearchMemoryTool(BaseTool):
    """使用所属记忆插件绑定的管理器查询本会话记忆。"""

    name = 'search_memory'
    parent_only = True
    description = '在长期记忆中搜索相关信息，并返回最相近的10条。当你有想要知道的关于用户或关于你自己的记忆时，调用此工具搜索。'
    compressible = True
    parameters_schema = {'type': 'object', 'properties': {'query': {'type': 'string', 'description': '搜索关键词/关键句。不要使用问句。'}}, 'required': ['query']}

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
        query = args.get("query", "")
        session_id = context.session_id
        try:
            if not query or not query.strip():
                return "错误: query不能为空。"
            if session_id is None:
                return "错误: 缺少会话id信息，无法定位长期记忆。"

            async with self._operation.open(session_id, episodic=True, semantic=True) as manager:
                result = await manager.search(query=query.strip(), memory_type="all", top_k=5)

            if not result or result == "未找到相关记忆":
                return "未找到有效的长期记忆内容。"
            return "检索到的长期记忆:\n" + result

        except Exception as e:
            return f"错误: {e}"
