from langchain.tools import tool
from langgraph.prebuilt import ToolRuntime

from app.agent.memory import get_memory_manager
from app.agent.utils.log import log_tool_call


@tool
@log_tool_call()
async def search_memory(
    query: str,
    runtime: ToolRuntime
) -> str:
    """在长期记忆中搜索相关信息，并返回最相近的10条。

    Args:
        query: 搜索关键词/关键句。
    """
    try:
        state = runtime.state
        session_id = state.get("session_id") if isinstance(state, dict) else getattr(state, "session_id", None)

        if not query or not query.strip():
            return "错误: query不能为空。"
        if session_id is None:
            return "错误: 缺少会话id信息，无法定位长期记忆。"

        memory_manager = get_memory_manager(session_id)
        result = await memory_manager.search(
            query=query.strip(),
            memory_type="all",
            top_k=5,
        )

        if not result or result == "未找到相关记忆":
            return "未找到有效的长期记忆内容。"
        return "检索到的长期记忆:\n" + result

    except Exception as e:
        return f"错误: {e}"
