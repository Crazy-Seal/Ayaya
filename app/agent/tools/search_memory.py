"""search_memory 工具 - 在长期记忆中检索相关信息"""

from typing import Annotated

from app.agent.context import ToolContext
from app.agent.tools.decorator import tool


@tool
async def search_memory(
    query: Annotated[str, "搜索关键词/关键句。"],
    context: ToolContext,
) -> str:
    """在长期记忆中搜索相关信息，并返回最相近的10条。"""
    session_id = context.session_id
    try:
        if not query or not query.strip():
            return "错误: query不能为空。"
        if session_id is None:
            return "错误: 缺少会话id信息，无法定位长期记忆。"

        from app.agent.memory.manager import get_memory_manager

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
