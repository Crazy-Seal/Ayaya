from dataclasses import dataclass

from app.agent.memory_hub.base import MemoryItem


@dataclass(frozen=True)
class MemoryContext:
    short_memory: str
    entries: list[MemoryItem]

    @property
    def merged_text(self) -> str:
        parts = [item.content for item in self.entries if item.content.strip()]
        return "\n".join(parts)


__all__ = ["MemoryContext"]
