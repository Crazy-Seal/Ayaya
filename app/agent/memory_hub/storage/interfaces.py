from collections.abc import Callable
from typing import Awaitable
from typing import Any, Protocol

class ShortTermMemoryAdapter(Protocol):
    async def get_latest_async(self, session_id: str) -> str:
        ...

    async def save_async(self, session_id: str, content: str) -> None:
        ...


class VectorMemoryAdapter(Protocol):
    async def search_async(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        namespace: str = "episodic_memory",
    ) -> list[dict[str, Any]]:
        ...

    async def upsert_async(
        self,
        user_id: str,
        memory_text: str,
        merge_text: Callable[[str, str], str | Awaitable[str]],
        namespace: str = "episodic_memory",
    ) -> None:
        ...


class GraphMemoryAdapter(Protocol):
    """图存储适配器接口，供语义记忆未来扩展图检索使用。"""

    async def upsert_semantic_item(self, user_id: str, content: str, metadata: dict[str, Any] | None = None) -> str:
        ...

    async def search_semantic(self, user_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        ...

