"""search_diary 工具 - 搜索指定日期范围的日记"""

from datetime import date
from typing import Annotated

from app.agent.context import ToolContext
from app.agent.tools.decorator import tool


@tool
async def search_diary(
    start: Annotated[str, "开始日期，格式 YYYY-MM-DD"],
    end: Annotated[str, "结束日期，格式 YYYY-MM-DD"],
    context: ToolContext,
) -> str:
    """搜索指定日期范围的日记。返回[start, end]范围内的日记内容(包括end那天的)，按日期排序。两个日期参数之间最多间隔5天。"""
    session_id = context.session_id
    try:
        if session_id is None:
            return "错误: 缺少会话id信息，无法查找日记。"

        from app.agent.memory.manager import get_memory_manager

        memory_manager = get_memory_manager(session_id)

        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)

        return await memory_manager.search_diary(start_date, end_date)

    except ValueError as e:
        return f"错误: {e}"
    except Exception as e:
        return f"错误: {e}"
