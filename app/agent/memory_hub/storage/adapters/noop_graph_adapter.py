from __future__ import annotations

from typing import Any

from app.agent.memory_hub.storage.interfaces import GraphMemoryAdapter


class NoopGraphMemoryAdapter(GraphMemoryAdapter):
    """默认图存储实现：当前不持久化，预留 Neo4j 等后端接入点。"""

    async def upsert_semantic_item(self, user_id: str, content: str, metadata: dict[str, Any] | None = None) -> str:
        return ""

    async def search_semantic(self, user_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return []

