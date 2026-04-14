import re

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage


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


def get_last_human_text(messages: list[AnyMessage]) -> str:
    """反向查找最近一条用户消息文本。"""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return extract_text(msg.content)
    return ""


def _message_role_name(message: AnyMessage) -> str:
    if isinstance(message, HumanMessage):
        return "主人"
    if isinstance(message, AIMessage):
        return "AI"
    return "工具"


def build_summary_source(messages: list[AnyMessage]) -> str:
    """把消息整理为“角色: 内容”文本，供总结模型使用。"""
    lines: list[str] = []
    for message in messages:
        text = extract_text(message.content)
        if text:
            lines.append(f"{_message_role_name(message)}: {text}")
    return "\n".join(lines)


def split_context(
    messages: list[AnyMessage],
    later_human_count: int,
    previous_human_count: int,
) -> tuple[list[AnyMessage], list[AnyMessage]]:
    """把上下文切分成“前情提要段”和“待总结段”。"""
    human_indices = [idx for idx, msg in enumerate(messages) if isinstance(msg, HumanMessage)]
    if not human_indices:
        return [], []

    later_start_human_pos = max(len(human_indices) - later_human_count, 0)
    later_start_idx = human_indices[later_start_human_pos]
    later_messages = messages[later_start_idx:]

    before_messages = messages[:later_start_idx]
    before_human_indices = [idx for idx, msg in enumerate(before_messages) if isinstance(msg, HumanMessage)]
    if not before_human_indices:
        return [], later_messages

    previous_start_human_pos = max(len(before_human_indices) - previous_human_count, 0)
    previous_start_idx = before_human_indices[previous_start_human_pos]
    previous_tail_messages = before_messages[previous_start_idx:]
    return previous_tail_messages, later_messages


def split_summary_items(summary_text: str) -> list[str]:
    """把多行摘要拆成独立记忆条目。"""
    items: list[str] = []
    for raw_line in summary_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*•]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        if line:
            items.append(line)
    if items:
        return items
    line = summary_text.strip()
    return [line] if line else []


def extract_multimodal_signals(messages: list[AnyMessage]) -> list[str]:
    """从消息中提取可持久化的多模态信号文本。"""
    signals: list[str] = []
    for msg in messages:
        if not isinstance(msg, HumanMessage):
            continue
        content = msg.content
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
