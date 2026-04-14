from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MemoryItem:
    """统一记忆项模型，便于后续做跨类型治理。"""

    id: str
    user_id: str
    memory_type: str
    content: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    importance: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseMemory(ABC):
    """四层架构中的记忆类型基类。"""

    memory_type: str
    plugin_id: str

    def __init__(self, user_id: str, config: Any):
        self.user_id = user_id
        self.config = config


    @abstractmethod
    async def add(self, memory_item: MemoryItem) -> str:
        ...

    @abstractmethod
    async def retrieve(self, query: str, limit: int = 5, **kwargs: Any) -> list[MemoryItem]:
        ...

    @abstractmethod
    async def forget(
        self,
        *,
        max_importance: float | None = None,
        older_than: datetime | None = None,
        **kwargs: Any,
    ) -> None:
        """策略性遗忘接口：按重要性或时间范围清理记忆。"""
        ...

    @abstractmethod
    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        importance: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        ...

    @abstractmethod
    async def remove(self, memory_id: str) -> bool:
        """CRUD 删除接口：按 memory_id 精确删除单条记忆。"""
        ...

    @abstractmethod
    async def clear(self) -> None:
        ...

    @abstractmethod
    async def get_stats(self) -> dict[str, Any]:
        ...

