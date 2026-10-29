"""跨上下文与记忆流程共享的轻量 token 估算。"""

import json
import math


def stable_json(value: object) -> str:
    """以稳定键序列化值，供估算和指纹计算复用。

    Args:
        value (object): 待估算或计算指纹的数据，不可直接编码的对象转换为文字。
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _rough_text_tokens(value: str) -> int:
    """按 ASCII 每三字符及非 ASCII 每字符粗估输入 token，供本地预算使用。

    Args:
        value (str): 当前需要校验或转换的值。

    Returns:
        int: 向上取整的粗估 token 数。
    """
    ascii_count = sum(1 for char in value if ord(char) < 128)
    return math.ceil(ascii_count / 3) + (len(value) - ascii_count)


class RoughTokenEstimator:
    """使用保守规则估算消息、工具定义和图片的 token 数。"""

    MESSAGE_OVERHEAD = 8
    CONTENT_PART_OVERHEAD = 4
    TOOL_CALL_OVERHEAD = 12
    IMAGE_TOKENS = 2_000

    def estimate_value(self, value: object) -> int:
        """粗估文本或多模态值的输入大小，图片按固定开销计算而不计 Base64 文本长度。

        Args:
            value (object): 待估算的文本、多模态列表或其他值，按实际类型处理。

        Returns:
            int: 该内容的粗估 token 数。
        """
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
        """粗估一条模型消息的正文、消息包装及工具调用开销。

        Args:
            message (dict): 当前需要读取或追加的消息。

        Returns:
            int: 该消息的粗估 token 数。
        """
        if hasattr(message, "message"):
            return self.MESSAGE_OVERHEAD + self.estimate_value(message.text)
        if hasattr(message, "to_storage_format"):
            message = message.to_storage_format()
            message["content"] = message.get("content", message.get("text", message.get("result", message.get("description", ""))))
        total = self.MESSAGE_OVERHEAD + self.estimate_value(message.get("content", ""))
        for tool_call in message.get("tool_calls") or []:
            total += self.TOOL_CALL_OVERHEAD + self.estimate_value({key: value for key, value in tool_call.items() if key != "provider_data"})
        return total

    def estimate_raw(
        self,
        *,
        system_prompt: str,
        tools: list[dict] | None,
        messages: list[dict],
    ) -> int:
        """粗估完整请求的系统提示词、工具定义和历史消息，最小返回一个 token。

        Args:
            system_prompt (str): 本次模型调用的系统提示词。
            tools (list[dict] | None): 当前向模型公开的工具定义。
            messages (list[dict]): 按对话顺序排列的消息集合。

        Returns:
            int: 完整输入的本地粗估 token 数。
        """
        total = self.MESSAGE_OVERHEAD + _rough_text_tokens(system_prompt or "")
        total += self.estimate_value([tool.to_dict() if hasattr(tool, "to_dict") else tool for tool in tools or []])
        total += sum(self.estimate_message(message) for message in messages)
        return max(total, 1)
