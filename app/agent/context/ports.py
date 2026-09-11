"""上下文压缩依赖的窄端口。"""

from typing import Protocol


class SummaryClient(Protocol):
    async def ainvoke_structured(
        self,
        messages: list[dict],
        schema: dict,
        **kwargs,
    ) -> dict: ...


class DiaryCoverage(Protocol):
    """上下文摘要清理所需的日记覆盖查询端口。"""

    async def has_diary_for_effective_date(self, effective_date: str) -> bool: ...


__all__ = ["DiaryCoverage", "SummaryClient"]
