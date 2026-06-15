"""上下文窗口管理（OpenAI dict 消息版）。

从旧 LangGraph reducer（app/agent/state.py + work_memory.py）忠实移植：
- compress_screenshot_messages: 截图 TTL 压缩 + 数量限制
- slice_recent_messages_by_human: 按人类消息数量保留尾部窗口

消息为 OpenAI 风格 dict（role/content/name/tool_calls/tool_call_id）。
"""

# 跨模块共用的窗口尺寸与截图消息名集中在 infra/constants.py
from app.agent_v2.utils.infra.constants import (
    SCREENSHOT_MESSAGE_NAME,
    SCREENSHOT_COMPRESSED_NAME,
    SCREENSHOT_TTL_HUMAN_MESSAGES,
    MAX_SCREENSHOTS_IN_CONTEXT,
)

_COMPRESSED_PLACEHOLDER = "[系统消息]已被压缩的旧截图"

def is_user(msg: dict) -> bool:
    return isinstance(msg, dict) and msg.get("role") == "user"


def is_screenshot_message(msg: dict) -> bool:
    """是否为截图消息（含压缩后的占位）。"""
    return is_user(msg) and msg.get("name") in (
        SCREENSHOT_MESSAGE_NAME,
        SCREENSHOT_COMPRESSED_NAME,
    )


def is_real_human(msg: dict) -> bool:
    """真正的用户消息（排除截图）。"""
    return is_user(msg) and msg.get("name") not in (
        SCREENSHOT_MESSAGE_NAME,
        SCREENSHOT_COMPRESSED_NAME,
    )


def _compressed_screenshot() -> dict:
    return {"role": "user", "content": _COMPRESSED_PLACEHOLDER, "name": SCREENSHOT_COMPRESSED_NAME}


def compress_screenshot_messages(messages: list[dict]) -> list[dict]:
    """压缩截图消息：TTL 过期压缩 + 数量限制（互补两策略）。"""
    result = list(messages)

    # Step 1: TTL 压缩
    for i, msg in enumerate(messages):
        if not (is_user(msg) and msg.get("name") == SCREENSHOT_MESSAGE_NAME):
            continue
        human_count_after = sum(
            1 for j in range(i + 1, len(messages)) if is_real_human(messages[j])
        )
        if human_count_after >= SCREENSHOT_TTL_HUMAN_MESSAGES:
            result[i] = _compressed_screenshot()

    # Step 2: 数量限制（仅保留最近 N 个未压缩截图）
    screenshot_indices = [
        i for i, msg in enumerate(result)
        if is_user(msg) and msg.get("name") == SCREENSHOT_MESSAGE_NAME
    ]
    if len(screenshot_indices) > MAX_SCREENSHOTS_IN_CONTEXT:
        excess = len(screenshot_indices) - MAX_SCREENSHOTS_IN_CONTEXT
        for idx in screenshot_indices[:excess]:
            result[idx] = _compressed_screenshot()

    return result


def slice_recent_messages_by_human(
    messages: list[dict],
    max_human_messages: int = 10,
) -> list[dict]:
    """从后往前数到第 max_human_messages 条真实人类消息，保留该条到结尾。

    截图消息不计入人类消息计数。
    """
    human_count = 0
    start_index = 0
    for index in range(len(messages) - 1, -1, -1):
        if is_real_human(messages[index]):
            human_count += 1
            if human_count == max_human_messages:
                start_index = index
                break
    return messages[start_index:]
