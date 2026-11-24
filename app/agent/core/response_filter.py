"""流式助手输出的协议清洗。"""

import re


ASSISTANT_TIME_PREFIX_START = "[发送时间："
ASSISTANT_TIME_PREFIX_PATTERN = re.compile(r"^\[发送时间：[^]]*\]")


class AssistantTimePrefixFilter:
    """在流式回复开始时识别并去掉模型复述的发送时间标签，保留表情标签和正常正文。"""

    MAX_EXPRESSION_TAG_LENGTH = 256

    def __init__(self) -> None:
        """创建当前回复独享的前缀缓冲和分隔符处理状态，不能在两轮回复之间复用。"""
        self._buffer = ""
        self._resolved = False
        self._strip_separator = False
        self._strip_optional_lf = False

    def feed(self, content: str) -> str:
        """执行管道收到文字片段时检查开头时间标签，暂存可能尚未完整到达的前缀。

        Args:
            content (str): 当前处理的正文。

        Returns:
            str: 本次可以发送给界面的文字；仍需等待后续片段确认时可能为空。
        """
        if self._resolved:
            return self._after_prefix(content)

        self._buffer += content
        prefix_offset, leading_is_incomplete = self._prefix_offset()
        candidate = self._buffer[prefix_offset:]
        match = ASSISTANT_TIME_PREFIX_PATTERN.match(candidate)
        if match:
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
        """判断发送时间标签是否仍在等待流式输出补齐。

        Args:
            candidate (str): 尚未匹配完整时间标签的开头文本。

        Returns:
            bool: 尚需等待后续文字才能确认标签时为 True。
        """
        if ASSISTANT_TIME_PREFIX_START.startswith(candidate):
            return True
        return (
            candidate.startswith(ASSISTANT_TIME_PREFIX_START)
            and "]" not in candidate
        )

    def _after_prefix(self, content: str) -> str:
        """时间标签删除后最多移除一个分隔符，跨片段到达的 CRLF 作为一个换行处理。

        Args:
            content (str): 当前处理的正文。

        Returns:
            str: 去除标签后分隔符的正文，其余格式保留。
        """
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
