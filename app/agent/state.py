from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel

from app.agent.utils.work_memory import slice_recent_messages_by_human


MAX_HUMAN_MESSAGES_IN_CHECKPOINT = 20  # checkpoint 人类消息数量上限
SUMMARY_EVERY_HUMAN_MESSAGES = 10  # 每隔多少条人类消息总结一次
RECENT_CONTEXT_HUMAN_MESSAGES = 10  # 最近上下文的人类消息数量


def reduce_messages_keep_recent_humans(
    left: list[AnyMessage],
    right: list[AnyMessage] | AnyMessage,
) -> list[AnyMessage]:
    """合并 LangGraph 消息，并仅保留最近固定窗口的人类消息."""
    merged = add_messages(left, right)
    # checkpoint 里只保留最近窗口，避免历史消息无限增长。
    return slice_recent_messages_by_human(
        merged,
        max_human_messages=MAX_HUMAN_MESSAGES_IN_CHECKPOINT,
    )


class AgentState(BaseModel):
    # StateGraph 消息历史，经过 reducer 自动裁剪。
    messages: Annotated[list[AnyMessage], reduce_messages_keep_recent_humans]
    # 会话隔离标识，贯穿记忆检索、工具调用和 checkpoint。
    session_id: str
    # 人类消息计数器，达到阈值后触发一次 memory finalize。
    summary_counter: int = 0
    # 单回合缓存，避免工具回环重复检索。
    memory_text: str | None = None
