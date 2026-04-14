import time
from datetime import datetime

from app.agent.memory_hub.config import MemoryConfig
from app.agent.memory_hub.base import BaseMemory, MemoryItem
from app.agent.memory_hub.storage.adapters import get_vector_memory_adapter


class MultimodalMemory(BaseMemory):
    memory_type = "multimodal"
    plugin_id = "multimodal_default"

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
            namespace="multimodal_mem",
        )
        return memory_item.id or f"multimodal:{time.time_ns()}"

    async def retrieve(self, query: str, limit: int = 5, **kwargs: object) -> list[MemoryItem]:
        return []

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

