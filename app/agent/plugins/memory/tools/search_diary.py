"""由记忆插件提供日记查询，使用当前工具调用所属的会话。"""

from datetime import date
from typing import Annotated

from app.agent.tools.core.base import ToolContext
from app.agent.tools.core.decorator import tool


@tool(
    description=(
        "搜索指定日期范围的日记。返回[start, end]范围内的日记内容(包括end那天的)，"
        "按日期排序，最多显示前5篇日记，超出数量的会提示未显示的篇数。"
    ),
    compressible=True,
)
async def search_diary(
    start: Annotated[str, "开始日期，格式 YYYY-MM-DD"],
    end: Annotated[str, "结束日期，格式 YYYY-MM-DD"],
    context: ToolContext,
) -> str:
    """搜索指定日期范围的日记。返回[start, end]范围内的日记内容(包括end那天的)，按日期排序，最多显示前5篇日记，超出数量的会提示未显示的篇数。

    Args:
        start (str): 开始日期，格式 YYYY-MM-DD
        end (str): 结束日期，格式 YYYY-MM-DD
        context (ToolContext): 执行管道提供的工具运行上下文。

    Returns:
        str: 指定日期范围的日记及未显示篇数、未找到日记的说明或查询错误。
    """
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
