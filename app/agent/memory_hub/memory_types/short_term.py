import uuid
from datetime import datetime

from app.agent.memory_hub.config import MemoryConfig
from app.agent.memory_hub.base import BaseMemory, MemoryItem
from app.agent.memory_hub.storage.adapters import get_short_term_adapter


class ShortTermMemory(BaseMemory):
    memory_type = "short_term"
    plugin_id = "short_term_default"

    def __init__(self, user_id: str, memory_config: MemoryConfig):
        super().__init__(user_id=user_id, config=memory_config)


    async def add(self, memory_item: MemoryItem) -> str:
        content = memory_item.content.strip()
        if not content:
            return ""
        await get_short_term_adapter().save_async(self.user_id, content)
        return memory_item.id or str(uuid.uuid4())

    async def retrieve(self, query: str, limit: int = 5, **kwargs: object) -> list[MemoryItem]:
        content = await get_short_term_adapter().get_latest_async(self.user_id)
        if not content.strip():
            return []
        return [
            MemoryItem(
                id="short-term-latest",
                user_id=self.user_id,
                memory_type=self.memory_type,
                content=content,
            )
        ]

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
        if content is None or not content.strip():
            return False
        await get_short_term_adapter().save_async(self.user_id, content)
        return True

    async def remove(self, memory_id: str) -> bool:
        return False

    async def clear(self) -> None:
        return

    async def get_stats(self) -> dict[str, object]:
        content = await get_short_term_adapter().get_latest_async(self.user_id)
        return {
            "memory_type": self.memory_type,
            "user_id": self.user_id,
            "has_short_memory": bool(content.strip()),
        }

