from typing import Annotated

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel

from app.agent.utils.work_memory import slice_recent_messages_by_human


MAX_HUMAN_MESSAGES_IN_CHECKPOINT = 20  # checkpoint 人类消息数量上限
SUMMARY_EVERY_HUMAN_MESSAGES = 10  # 每隔多少条人类消息总结一次
RECENT_CONTEXT_HUMAN_MESSAGES = 10  # 最近上下文的人类消息数量
SCREENSHOT_TTL_HUMAN_MESSAGES = 2  # 截图存活轮数，超过则压缩

# 截图消息的 name 标记
SCREENSHOT_MESSAGE_NAME = "system_screenshot"
SCREENSHOT_COMPRESSED_NAME = "system_screenshot_compressed"


def is_screenshot_message(msg: AnyMessage) -> bool:
    """判断是否为截图消息（包括压缩后的）"""
    return isinstance(msg, HumanMessage) and getattr(msg, "name", None) in (
        SCREENSHOT_MESSAGE_NAME,
        SCREENSHOT_COMPRESSED_NAME,
    )


MAX_SCREENSHOTS_IN_CONTEXT = 2  # 上下文中最多保留的截图数量


def compress_screenshot_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
    """压缩截图消息：TTL 过期压缩 + 数量限制。

    1. TTL 压缩：存活超过 SCREENSHOT_TTL_HUMAN_MESSAGES 轮的截图压缩为占位符
    2. 数量限制：超过 MAX_SCREENSHOTS_IN_CONTEXT 个截图时，压缩最旧的

    这两个策略互补：
    - TTL 防止单个截图长期占用上下文
    - 数量限制防止一回合内多次截图导致上下文爆炸
    """
    result = list(messages)

    # Step 1: TTL 压缩
    for i, msg in enumerate(messages):
        # 只处理未压缩的截图消息
        if not (isinstance(msg, HumanMessage) and getattr(msg, "name", None) == SCREENSHOT_MESSAGE_NAME):
            continue

        # 计算该截图之后有多少条真实用户消息
        human_count_after = 0
        for j in range(i + 1, len(messages)):
            m = messages[j]
            if isinstance(m, HumanMessage) and not is_screenshot_message(m):
                human_count_after += 1

        # 如果超过阈值，压缩该截图
        if human_count_after >= SCREENSHOT_TTL_HUMAN_MESSAGES:
            result[i] = HumanMessage(
                content="[系统消息]已被压缩的旧截图",
                name=SCREENSHOT_COMPRESSED_NAME,
                id=msg.id if hasattr(msg, "id") else None
            )

    # Step 2: 数量限制（只保留最近的 N 个截图）
    # 找出所有未压缩的截图索引
    screenshot_indices = [
        i for i, msg in enumerate(result)
        if isinstance(msg, HumanMessage) and getattr(msg, "name", None) == SCREENSHOT_MESSAGE_NAME
    ]

    # 如果超过限制，压缩最旧的截图（索引小的）
    if len(screenshot_indices) > MAX_SCREENSHOTS_IN_CONTEXT:
        # 计算需要压缩的数量
        excess = len(screenshot_indices) - MAX_SCREENSHOTS_IN_CONTEXT
        # 压缩最旧的 excess 个（索引最小的）
        for idx in screenshot_indices[:excess]:
            msg = result[idx]
            result[idx] = HumanMessage(
                content="[系统消息]已被压缩的旧截图",
                name=SCREENSHOT_COMPRESSED_NAME,
                id=msg.id if hasattr(msg, "id") else None
            )

    return result


def reduce_messages_keep_recent_humans(
    left: list[AnyMessage],
    right: list[AnyMessage] | AnyMessage,
) -> list[AnyMessage]:
    """合并 LangGraph 消息，并仅保留最近固定窗口的人类消息."""
    merged = add_messages(left, right)
    # 压缩旧的截图消息，只保留最后一个携带图片的
    merged = compress_screenshot_messages(merged)
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
