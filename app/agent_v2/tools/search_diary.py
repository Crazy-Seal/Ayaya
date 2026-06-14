"""search_diary 工具 - 搜索指定日期范围的日记"""

from datetime import date

from app.agent_v2.context import BaseTool, ToolContext, ToolResult
from app.agent_v2.utils.log import log_tool_call_result


class SearchDiaryTool(BaseTool):
    name = "search_diary"
    description = (
        "搜索指定日期范围的日记。返回[start, end]范围内的日记内容(包括end那天的)，"
        "按日期排序。两个日期参数之间最多间隔5天。\n"
        "Args:\n"
        "    start: 开始日期，格式 YYYY-MM-DD\n"
        "    end: 结束日期，格式 YYYY-MM-DD"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "开始日期，格式 YYYY-MM-DD"},
            "end": {"type": "string", "description": "结束日期，格式 YYYY-MM-DD"},
        },
        "required": ["start", "end"],
    }

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        start = args.get("start", "")
        end = args.get("end", "")
        result = await self._search(start, end, context.session_id)
        await log_tool_call_result(self.name, args, result)
        if isinstance(result, str) and result.startswith("错误:"):
            return ToolResult(content=result)
        return ToolResult.success(result)

    async def _search(self, start: str, end: str, session_id: str | None) -> str:
        try:
            if session_id is None:
                return "错误: 缺少会话id信息，无法查找日记。"

            from app.agent_v2.memory.manager import get_memory_manager

            memory_manager = get_memory_manager(session_id)

            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)

            return await memory_manager.search_diary(start_date, end_date)

        except ValueError as e:
            return f"错误: {e}"
        except Exception as e:
            return f"错误: {e}"
