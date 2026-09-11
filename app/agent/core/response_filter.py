"""流式助手输出的协议清洗。"""

import re
from datetime import datetime

from app.agent.state.time import WEEKDAYS


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
