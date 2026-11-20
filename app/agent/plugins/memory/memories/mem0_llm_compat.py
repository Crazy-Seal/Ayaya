"""整理 Mem0 事实提取正文，并兼容事实列表字段。"""

import json
import logging
from typing import Any

from mem0.llms.base import LLMBase
from mem0.memory.utils import remove_code_blocks


logger = logging.getLogger(__name__)
_MISSING = object()


class Mem0FactSchemaError(ValueError):
    """事实提取响应的 JSON 结构无法转换为 Mem0 所需的事实列表。"""


class Mem0FactExtractionLLMAdapter:
    """交付格式化的事实提取正文，并校验及规范化事实列表字段。

    Attributes:
        _delegate (LLMBase): Mem0 原有模型客户端。
        _fact_input (str): 按角色称呼和通知来源整理的本次对话正文。
    """

    def __init__(self, delegate: LLMBase, fact_input: str) -> None:
        """绑定本次提取正文和实际请求模型的客户端。

        Args:
            delegate (LLMBase): Mem0 原有模型客户端。
            fact_input (str): 本次事实提取使用的完整 Input 正文。
        """
        self._delegate = delegate
        self._fact_input = fact_input

    def __getattr__(self, name: str) -> Any:
        """将模型配置等属性访问交给原客户端。

        Args:
            name (str): 需要读取的客户端属性名称。

        Returns:
            Any: 原模型客户端的对应属性。
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
        """替换事实提取正文并规范化响应字段，格式错误时直接抛出异常。

        Mem0 的事实提取请求包含 system 和 user 消息；更新记忆请求仅包含
        user 消息。根据这一请求结构，事实提取时使用本次整理的对话正文。

        Args:
            messages (list[dict[str, Any]]): Mem0 构造的模型请求。
            response_format (Any): 原请求的模型响应格式。
            tools (list[dict[str, Any]] | None): 原请求公开的工具定义。
            tool_choice (str): 原请求的工具选择方式。
            kwargs (Any): 原模型客户端接收的其他调用参数。

        Returns:
            Any: 事实提取返回包含 facts 的 JSON 文本，其他阶段返回原模型响应。

        Raises:
            Mem0FactSchemaError: 事实提取响应类型、JSON 对象或事实列表字段无效。
        """
        is_fact_extraction = any(message.get("role") == "system" for message in messages)
        if is_fact_extraction:
            messages = [
                {**message, "content": self._fact_input}
                if message.get("role") == "user" else message
                for message in messages
            ]
        response = self._delegate.generate_response(
            messages=messages,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )

        return self._normalize_fact_response(response) if is_fact_extraction else response

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
