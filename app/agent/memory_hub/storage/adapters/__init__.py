from functools import lru_cache

from app.agent.memory_hub.storage.adapters.noop_graph_adapter import NoopGraphMemoryAdapter
from app.agent.memory_hub.storage.adapters.sqlite_short_term_adapter import SqliteShortTermMemoryAdapter
from app.agent.memory_hub.storage.adapters.sqlite_vector_store_adapter import SqliteVectorMemoryAdapter
from app.agent.memory_hub.storage.interfaces import GraphMemoryAdapter, ShortTermMemoryAdapter, VectorMemoryAdapter


@lru_cache(maxsize=1)
def get_short_term_adapter() -> ShortTermMemoryAdapter:
    return SqliteShortTermMemoryAdapter()


@lru_cache(maxsize=1)
def get_vector_memory_adapter() -> VectorMemoryAdapter:
    return SqliteVectorMemoryAdapter()


@lru_cache(maxsize=1)
def get_graph_memory_adapter() -> GraphMemoryAdapter:
    return NoopGraphMemoryAdapter()


__all__ = [
    "ShortTermMemoryAdapter",
    "VectorMemoryAdapter",
    "GraphMemoryAdapter",
    "get_short_term_adapter",
    "get_vector_memory_adapter",
    "get_graph_memory_adapter",
]

