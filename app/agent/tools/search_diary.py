from datetime import date

from langchain.tools import tool
from langgraph.prebuilt import ToolRuntime

from app.agent.memory import get_memory_manager
from app.agent.utils.log import log_tool_call


@tool
@log_tool_call()
async def search_diary(
    start: str,
    end: str,
    runtime: ToolRuntime
) -> str:
    """搜索指定日期范围的日记。返回[start, end]范围内的日记内容(包括end那天的)，按日期排序。两个日期参数之间最多间隔5天。

    Args:
        start: 开始日期，格式 YYYY-MM-DD
        end: 结束日期，格式 YYYY-MM-DD
    """
    try:
        state = runtime.state
        session_id = state.get("session_id") if isinstance(state, dict) else getattr(state, "session_id", None)

        if session_id is None:
            return "错误: 缺少会话id信息，无法查找日记。"

        memory_manager = get_memory_manager(session_id)

        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)

        return await memory_manager.search_diary(start_date, end_date)

    except ValueError as e:
        return f"错误: {e}"
    except Exception as e:
        return f"错误: {e}"
