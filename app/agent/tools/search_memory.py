from langchain.tools import tool
from langgraph.prebuilt import ToolRuntime

from app.agent.memory_hub.storage.adapters import get_vector_memory_adapter
from app.agent.utils.log import log_tool_call


@tool
@log_tool_call()
async def search_memory(
    query: str,
    runtime: ToolRuntime
) -> str:
    """在长期记忆中搜索相关信息，并返回最相近的5条。

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

        items = await get_vector_memory_adapter().search_async(
            user_id=str(session_id),
            query=query.strip(),
            limit=5,
            namespace="episodic_memory",
        )

        lines: list[str] = []
        for idx, item in enumerate(items, start=1):
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            score_value = item.get("score")
            score = f"{score_value:.4f}" if isinstance(score_value, (int, float)) else "N/A"
            lines.append(f"{idx}. {text} (score={score})")

        if not lines:
            return "未找到有效的长期记忆内容。"
        return "检索到的长期记忆:\n" + "\n".join(lines)
    except Exception as e:
        return f"错误: {e}"
