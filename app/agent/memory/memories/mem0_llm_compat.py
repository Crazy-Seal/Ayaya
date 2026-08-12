"""Mem0 LLM 响应兼容层。

只修正 Ayaya 自定义事实提取阶段的结构契约，不改动 Mem0 的更新、删除或
其他 LLM 调用语义。
"""

import json
import logging
from typing import Any

from mem0.memory.utils import remove_code_blocks


logger = logging.getLogger(__name__)

MEM0_FACT_EXTRACTION_MARKER = "[AYAYA_MEM0_FACT_EXTRACTION_V1]"
_REPAIR_PROMPT = (
    "上一次事实提取响应不符合约定。请重新处理同一输入，且只返回合法 JSON："
    '{"facts": ["记忆1", "记忆2"]}。'
    '没有可提取内容时返回：{"facts": []}。不得使用其他顶层字段。'
)
_MISSING = object()


class Mem0FactSchemaError(ValueError):
    """Mem0 第一阶段事实提取结果不符合 Ayaya 约定。"""


class Mem0FactExtractionLLMAdapter:
    """在 Mem0 自带 LLM 外围规范化事实提取响应。"""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def generate_response(
        self,
        messages: list[dict[str, Any]],
        response_format: Any = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> Any:
        """委托模型调用，仅在事实提取阶段校验并规范化 JSON。"""
        response = self._delegate.generate_response(
            messages=messages,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )
        if not self._is_fact_extraction(messages):
            return response

        try:
            return self._normalize_fact_response(response)
        except Mem0FactSchemaError as first_error:
            logger.warning(
                "[Mem0Compat] 事实提取响应结构异常，重试一次: %s",
                first_error,
            )

        repair_messages = [
            *messages,
            {"role": "user", "content": _REPAIR_PROMPT},
        ]
        repaired_response = self._delegate.generate_response(
            messages=repair_messages,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )
        try:
            return self._normalize_fact_response(repaired_response)
        except Mem0FactSchemaError as second_error:
            raise Mem0FactSchemaError(
                f"事实提取两次响应均不符合约定: {second_error}"
            ) from second_error

    @staticmethod
    def _is_fact_extraction(messages: list[dict[str, Any]]) -> bool:
        return any(
            message.get("role") == "system"
            and MEM0_FACT_EXTRACTION_MARKER in str(message.get("content", ""))
            for message in messages
        )

    @staticmethod
    def _normalize_fact_response(response: Any) -> str:
        if not isinstance(response, str):
            raise Mem0FactSchemaError(
                f"响应类型不是字符串: {type(response).__name__}"
            )

        try:
            payload = json.loads(remove_code_blocks(response), strict=False)
        except (json.JSONDecodeError, TypeError) as exc:
            raise Mem0FactSchemaError("响应不是合法 JSON 对象") from exc

        if not isinstance(payload, dict):
            raise Mem0FactSchemaError(
                f"JSON 顶层不是对象: {type(payload).__name__}"
            )

        facts = payload.get("facts", _MISSING)
        if facts is _MISSING:
            memories = payload.get("memories", _MISSING)
            if memories is _MISSING:
                raise Mem0FactSchemaError(
                    f"缺少 facts，实际字段: {sorted(payload)}"
                )
            facts = memories
            logger.warning(
                "[Mem0Compat] 将事实提取字段 memories 规范化为 facts"
            )

        if not isinstance(facts, list):
            raise Mem0FactSchemaError(
                f"facts 不是数组: {type(facts).__name__}"
            )

        return json.dumps({"facts": facts}, ensure_ascii=False)
