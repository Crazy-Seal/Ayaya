"""文本处理工具函数

适配 agent_v2：消息为 OpenAI 风格的 dict（含 "role"/"content"/"name" 等字段），
不再依赖 LangChain 消息类型。
"""

# 截图消息的 name 标识（与 core/pipeline.py 中 SCREENSHOT_MESSAGE_NAME 保持一致）
SCREENSHOT_MESSAGE_NAME = "system_screenshot"


def is_screenshot_message(message: dict) -> bool:
    """判断一条消息是否为截图消息。"""
    if not isinstance(message, dict):
        return False
    return message.get("name") == SCREENSHOT_MESSAGE_NAME


def _is_human(message: dict) -> bool:
    """判断一条消息是否为用户消息。"""
    if not isinstance(message, dict):
        return False
    return message.get("role") == "user"


def extract_text(content: object) -> str:
    """从模型消息 content 中提取纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
        return "".join(text_parts)
    return ""


def get_last_human_text(messages: list[dict]) -> str:
    """反向查找最近一条用户消息文本。

    注意：截图消息不计入用户消息。
    """
    for msg in reversed(messages):
        if _is_human(msg) and not is_screenshot_message(msg):
            return extract_text(msg.get("content"))
    return ""


def split_context(
    messages: list[dict],
    later_human_count: int,
    previous_human_count: int,
) -> tuple[list[dict], list[dict]]:
    """把上下文切分成"前情提要段"和"待总结段"。

    Args:
        messages: 消息列表（OpenAI dict 格式）
        later_human_count: 待提取的人类消息数量
        previous_human_count: 前情提要的人类消息数量

    Returns:
        (前情提要消息, 待提取消息)

    注意：截图消息不计入人类消息计数。
    """
    # 过滤截图消息后的人类消息索引
    human_indices = [
        idx for idx, msg in enumerate(messages)
        if _is_human(msg) and not is_screenshot_message(msg)
    ]
    if not human_indices:
        return [], []

    later_start_human_pos = max(len(human_indices) - later_human_count, 0)
    later_start_idx = human_indices[later_start_human_pos]
    later_messages = messages[later_start_idx:]

    before_messages = messages[:later_start_idx]
    before_human_indices = [
        idx for idx, msg in enumerate(before_messages)
        if _is_human(msg) and not is_screenshot_message(msg)
    ]
    if not before_human_indices:
        return [], later_messages

    previous_start_human_pos = max(len(before_human_indices) - previous_human_count, 0)
    previous_start_idx = before_human_indices[previous_start_human_pos]
    previous_tail_messages = before_messages[previous_start_idx:]
    return previous_tail_messages, later_messages


def extract_multimodal_signals(messages: list[dict]) -> list[str]:
    """从消息中提取可持久化的多模态信号文本。"""
    signals: list[str] = []
    for msg in messages:
        if not _is_human(msg):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "")).strip() or "unknown"
            if item_type == "text":
                continue
            payload = {k: v for k, v in item.items() if k != "text"}
            signals.append(f"多模态输入[{item_type}]：{payload}")
    return signals
