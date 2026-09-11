"""跨上下文与记忆流程共享的轻量 token 估算。"""

import json
import math
from typing import Any


def stable_json(value: Any) -> str:
    """以稳定键序列化值，供估算和指纹计算复用。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _rough_text_tokens(value: str) -> int:
    ascii_count = sum(1 for char in value if ord(char) < 128)
    return math.ceil(ascii_count / 3) + (len(value) - ascii_count)


class RoughTokenEstimator:
    """使用保守规则估算消息、工具定义和图片的 token 数。"""

    MESSAGE_OVERHEAD = 8
    CONTENT_PART_OVERHEAD = 4
    TOOL_CALL_OVERHEAD = 12
    IMAGE_TOKENS = 2_000

    def estimate_value(self, value: Any) -> int:
        if isinstance(value, str):
            return _rough_text_tokens(value)
        if isinstance(value, list):
            total = 0
            for item in value:
                total += self.CONTENT_PART_OVERHEAD
                if isinstance(item, dict) and item.get("type") == "image_url":
                    total += self.IMAGE_TOKENS
                else:
                    total += self.estimate_value(item)
            return total
        if isinstance(value, dict):
            return _rough_text_tokens(stable_json(value))
        return _rough_text_tokens(str(value))

    def estimate_message(self, message: dict) -> int:
        total = self.MESSAGE_OVERHEAD + self.estimate_value(message.get("content", ""))
        for tool_call in message.get("tool_calls") or []:
            total += self.TOOL_CALL_OVERHEAD + self.estimate_value(tool_call)
        return total

    def estimate_raw(
        self,
        *,
        system_prompt: str,
        tools: list[dict] | None,
        messages: list[dict],
    ) -> int:
        total = self.MESSAGE_OVERHEAD + _rough_text_tokens(system_prompt or "")
        total += self.estimate_value(tools or [])
        total += sum(self.estimate_message(message) for message in messages)
        return max(total, 1)
