"""上下文压缩依赖的窄端口。"""
from typing import Sequence
from app.agent.models.contracts import InputMessage, ToolDefinition

from typing import Protocol


class SummaryClient(Protocol):
    """约定压缩历史时所需的结构化模型调用，不要求调用方持有完整 Agent。"""
    async def ainvoke_structured(
        self,
        messages: Sequence[InputMessage],
        schema: dict,
        **kwargs,
    ) -> dict:
        """由压缩协调器调用模型，要求返回符合指定结构的历史摘要。

        Args:
            messages (Sequence[InputMessage]): 摘要规则和待总结的历史，使用临时业务消息。
            schema (dict): 摘要结果必须符合的 JSON Schema。
            kwargs (Any): 温度等附加模型调用选项。

        Returns:
            dict: 模型解析后的结构化摘要，调用方还会校验领域约束。
        """
        ...


class DiaryCoverage(Protocol):
    """上下文摘要清理所需的日记覆盖查询端口。"""

    async def has_diary_for_effective_date(self, effective_date: str) -> bool:
        """由压缩协调器查询指定有效日期是否已有日记，决定能否移除对应旧摘要。

        Args:
            effective_date (str): 摘要覆盖的日记有效日期，格式为 YYYY-MM-DD。

        Returns:
            bool: 该日期已有日记时为 True。
        """
        ...


__all__ = ["DiaryCoverage", "SummaryClient"]
