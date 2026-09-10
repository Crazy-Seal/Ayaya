"""消息时间的存储、展示、临时投影和助手输出兜底。"""

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.agent.message_names import SYSTEM_INJECTED_USER_MESSAGE_NAMES


WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
ASSISTANT_TIME_PREFIX_START = "[发送时间："
ASSISTANT_TIME_PREFIX_PATTERN = re.compile(
    r"^\[发送时间：(?P<datetime>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "
    r"[+-]\d{4}) (?P<weekday>星期[一二三四五六日])\]"
)


class AssistantTimePrefixFilter:
    """从流式助手回复开头删除系统时间投影格式。"""

    MAX_TIME_PREFIX_LENGTH = 96
    MAX_EXPRESSION_TAG_LENGTH = 256

    def __init__(self) -> None:
        self._buffer = ""
        self._resolved = False
        self._strip_separator = False
        self._strip_optional_lf = False

    def feed(self, content: str) -> str:
        if self._resolved:
            return self._after_prefix(content)

        self._buffer += content
        prefix_offset, leading_is_incomplete = self._prefix_offset()
        candidate = self._buffer[prefix_offset:]
        match = ASSISTANT_TIME_PREFIX_PATTERN.match(candidate)
        if match and self._is_valid_prefix(match):
            leading = self._buffer[:prefix_offset]
            remainder = candidate[match.end():]
            self._buffer = ""
            self._resolved = True
            self._strip_separator = True
            return leading + self._after_prefix(remainder)

        should_buffer = (
            leading_is_incomplete
            and len(candidate) <= self.MAX_EXPRESSION_TAG_LENGTH
        ) or self._is_possible_prefix(candidate)
        if should_buffer:
            leading = self._buffer[:prefix_offset]
            self._buffer = candidate
            return leading

        buffered = self._buffer
        self._buffer = ""
        self._resolved = True
        return buffered

    def finish(self) -> str:
        """流结束时释放未匹配成功的候选文本，避免吞掉正文。"""
        if self._resolved:
            return ""
        buffered = self._buffer
        self._buffer = ""
        self._resolved = True
        return buffered

    def _prefix_offset(self) -> tuple[int, bool]:
        """返回可选表情标签后的正文起点，以及标签是否仍未闭合。"""
        index = 0
        while True:
            while index < len(self._buffer) and self._buffer[index].isspace():
                index += 1
            if index == len(self._buffer):
                return index, False
            if self._buffer[index] != "<":
                return index, False
            closing = self._buffer.find(">", index + 1)
            if closing < 0:
                return index, True
            tag = self._buffer[index + 1:closing]
            if not tag or "<" in tag or "\n" in tag or "\r" in tag:
                return index, False
            index = closing + 1

    def _is_possible_prefix(self, candidate: str) -> bool:
        if len(candidate) > self.MAX_TIME_PREFIX_LENGTH:
            return False
        if ASSISTANT_TIME_PREFIX_START.startswith(candidate):
            return True
        return (
            candidate.startswith(ASSISTANT_TIME_PREFIX_START)
            and "]" not in candidate
        )

    @staticmethod
    def _is_valid_prefix(match: re.Match[str]) -> bool:
        try:
            timestamp = datetime.strptime(
                match.group("datetime"),
                "%Y-%m-%d %H:%M:%S %z",
            )
        except ValueError:
            return False
        return WEEKDAYS[timestamp.weekday()] == match.group("weekday")

    def _after_prefix(self, content: str) -> str:
        if self._strip_optional_lf:
            self._strip_optional_lf = False
            return content[1:] if content.startswith("\n") else content
        if not self._strip_separator or not content:
            return content
        self._strip_separator = False
        if content.startswith("\r"):
            if len(content) == 1:
                self._strip_optional_lf = True
                return ""
            return content[2:] if content[1] == "\n" else content[1:]
        return content[1:] if content[0].isspace() else content


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def system_timezone():
    return datetime.now().astimezone().tzinfo


def normalize_utc(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=system_timezone())
    return parsed.astimezone(timezone.utc)


def format_local_time(value: datetime | str) -> str:
    local = normalize_utc(value).astimezone()
    return f"{local.strftime('%Y-%m-%d %H:%M:%S %z')} {WEEKDAYS[local.weekday()]}"


def render_timed_text(content: str, timestamp: datetime | str) -> str:
    return f"[发送时间：{format_local_time(timestamp)}] {content}"


def _project_content(content: Any, timestamp: datetime | str) -> Any:
    if isinstance(content, str):
        return render_timed_text(content, timestamp)
    if not isinstance(content, list):
        return content
    projected = deepcopy(content)
    prefix = f"[发送时间：{format_local_time(timestamp)}]"
    for part in projected:
        if isinstance(part, dict) and part.get("type") == "text":
            part["text"] = f"{prefix} {part.get('text', '')}".rstrip()
            return projected
    projected.insert(0, {"type": "text", "text": prefix})
    return projected


def project_messages_with_time(messages: list[dict]) -> list[dict]:
    projected = deepcopy(messages)
    for message in projected:
        timestamp = message.get("_created_at")
        if not timestamp:
            continue
        role = message.get("role")
        if role == "user" and message.get("name") in SYSTEM_INJECTED_USER_MESSAGE_NAMES:
            continue
        is_plain_assistant = role == "assistant" and not message.get("tool_calls")
        if role != "user" and not is_plain_assistant:
            continue
        message["content"] = _project_content(message.get("content"), timestamp)
    return projected


@dataclass
class RuntimeContext:
    """一轮模型工具循环共享、但绝不持久化的易变上下文。"""

    captured_at: datetime
    latest_human_message_id: str = ""
    recalled_memory: str = ""

    @classmethod
    def capture(cls) -> "RuntimeContext":
        return cls(captured_at=utc_now())
