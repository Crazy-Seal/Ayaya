"""Mem0 LLM 响应兼容层。

只修正 Ayaya 自定义事实提取阶段的结构契约，不改动 Mem0 的更新、删除或
其他 LLM 调用语义。
"""

import json
import logging
from typing import Any

from mem0.memory.utils import remove_code_blocks
from mem0.llms.base import LLMBase


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

    def __init__(self, delegate: LLMBase) -> None:
        """保存原模型客户端，仅在识别到 Mem0 事实提取请求时规范化其响应结构。

        Args:
            delegate (LLMBase): 原有模型客户端，非事实提取请求原样交给它。
        """
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        """将未覆盖的属性访问交回原模型客户端，保留它的其他能力。

        Args:
            name (str): 当前对象的名称。

        Returns:
            Any: 原客户端的对应属性。
        """
        return getattr(self._delegate, name)

    def generate_response(
        self,
        messages: list[dict[str, Any]],
        response_format: Any = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> Any:
        """委托模型调用，仅在事实提取阶段校验并规范化 JSON。

        Args:
            messages (list[dict[str, Any]]): 按对话顺序排列的消息集合。
            response_format (Any): 原 Mem0 客户端请求的模型输出格式。
            tools (list[dict[str, Any]] | None): 当前向模型公开的工具定义。
            tool_choice (str): 原模型请求规定的工具选择方式。
            kwargs (Any): 传递给被调用函数的其他关键字参数。
        """
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
        """通过 system 消息中的已知提取标记识别 Mem0 事实提取，避免误改普通响应。

        Args:
            messages (list[dict[str, Any]]): 按对话顺序排列的消息集合。

        Returns:
            bool: 消息包含事实提取标记时为 True。
        """
        return any(
            message.get("role") == "system"
            and MEM0_FACT_EXTRACTION_MARKER in str(message.get("content", ""))
            for message in messages
        )

    @staticmethod
    def _normalize_fact_response(response: object) -> str:
        """校验事实提取 JSON，并兼容把 memories 列表改名为 facts；无法识别的结构明确报错。

        Args:
            response (object): 模型返回的事实提取结果，先检查文本类型再解析 JSON。

        Returns:
            str: 仅包含经校验 facts 列表的 JSON 字符串。

        Raises:
            Mem0FactSchemaError: 响应不是对象 JSON，缺少事实列表或列表项格式无效。
        """
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
