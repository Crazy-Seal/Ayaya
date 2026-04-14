import time
from datetime import datetime

from app.agent.memory_hub.config import MemoryConfig
from app.agent.memory_hub.base import BaseMemory, MemoryItem
from app.agent.memory_hub.storage.adapters import get_vector_memory_adapter


class EpisodicMemory(BaseMemory):
    memory_type = "episodic"
    plugin_id = "episodic_default"

    def __init__(self, user_id: str, memory_config: MemoryConfig):
        super().__init__(user_id=user_id, config=memory_config)

    async def add(self, memory_item: MemoryItem) -> str:
        content = memory_item.content.strip()
        if not content:
            return ""
        await get_vector_memory_adapter().upsert_async(
            user_id=self.user_id,
            memory_text=content,
            merge_text=lambda existing, new: existing if new in existing else f"{existing}；{new}",
            namespace="episodic_memory",
        )
        return memory_item.id or f"episodic:{time.time_ns()}"

    async def retrieve(self, query: str, limit: int = 5, **kwargs: object) -> list[MemoryItem]:
        query = query.strip()
        if not query:
            return []

        results: list[MemoryItem] = []
        hits = await get_vector_memory_adapter().search_async(
            self.user_id,
            query=query,
            limit=limit,
            namespace="episodic_memory",
        )
        for index, hit in enumerate(hits):
            results.append(
                MemoryItem(
                    id=str(hit.get("key") or f"episodic:{index}"),
                    user_id=self.user_id,
                    memory_type=self.memory_type,
                    content=str(hit.get("text", "")),
                    importance=float(hit.get("score")) if isinstance(hit.get("score"), (int, float)) else 0.5,
                    metadata={"key": hit.get("key")},
                )
            )
        return results

    async def forget(
        self,
        *,
        max_importance: float | None = None,
        older_than: datetime | None = None,
        **kwargs: object,
    ) -> None:
        return

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        importance: float | None = None,
        metadata: dict[str, object] | None = None,
    ) -> bool:
        return False

    async def remove(self, memory_id: str) -> bool:
        return False

    async def clear(self) -> None:
        return

    async def get_stats(self) -> dict[str, object]:
        return {
            "memory_type": self.memory_type,
            "user_id": self.user_id,
        }

