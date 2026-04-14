import asyncio
import inspect
import os
import time
from collections.abc import Callable
from typing import Any, Awaitable

import aiosqlite
from langgraph.store.sqlite import AsyncSqliteStore
from langgraph.store.sqlite.base import SqliteIndexConfig

from app.agent.memory_hub.constants import LONG_MEMORY_MERGE_SIMILARITY_THRESHOLD
from app.agent.memory_hub.constants import STORE_DB_PATH
from app.agent.memory_hub.embedding.registry import get_embedding_provider
from app.agent.memory_hub.storage.interfaces import VectorMemoryAdapter


def _require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Environment variable not found: {name}")
    return value


class SqliteVectorMemoryAdapter(VectorMemoryAdapter):
    """基于 sqlite + embedding 的向量记忆适配器。"""

    def __init__(self):
        self._store: AsyncSqliteStore | None = None
        self._init_lock = asyncio.Lock()

    async def _get_store(self) -> AsyncSqliteStore:
        if self._store is not None:
            return self._store

        async with self._init_lock:
            if self._store is not None:
                return self._store

            STORE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(STORE_DB_PATH))
            await conn.execute("PRAGMA journal_mode=WAL")

            index_config = SqliteIndexConfig(
                dims=int(_require_env("EMBEDDING_DIMENSION")),
                embed=get_embedding_provider().build(),
                fields=["text"],
            )
            store = AsyncSqliteStore(conn=conn, index=index_config)
            await store.setup()
            self._store = store
        return self._store

    async def search_async(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        namespace: str = "episodic_memory",
    ) -> list[dict[str, Any]]:
        namespaced_key = (namespace, user_id)
        store = await self._get_store()
        hits = await store.asearch(namespaced_key, query=query, limit=limit)

        results: list[dict[str, Any]] = []
        for hit in hits:
            text = ""
            if isinstance(hit.value, dict):
                text = str(hit.value.get("text", "")).strip()
            if not text:
                continue
            score = hit.score if isinstance(hit.score, (int, float)) else None
            results.append(
                {
                    "key": hit.key,
                    "text": text,
                    "score": float(score) if score is not None else None,
                }
            )
        return results

    async def upsert_async(
        self,
        user_id: str,
        memory_text: str,
        merge_text: Callable[[str, str], str | Awaitable[str]],
        namespace: str = "episodic_memory",
    ) -> None:
        store = await self._get_store()
        namespaced_key = (namespace, user_id)

        similar_items = await store.asearch(namespaced_key, query=memory_text, limit=1)
        top_item = similar_items[0] if similar_items else None
        if top_item and top_item.score is not None and top_item.score > LONG_MEMORY_MERGE_SIMILARITY_THRESHOLD:
            existing_text = ""
            if isinstance(top_item.value, dict):
                existing_text = str(top_item.value.get("text", ""))
            merged_or_awaitable = merge_text(existing_text, memory_text)
            if inspect.isawaitable(merged_or_awaitable):
                merged_text = await merged_or_awaitable
            else:
                merged_text = merged_or_awaitable
            await store.aput(namespaced_key, key=top_item.key, value={"text": merged_text})
            return

        key = f"summary:{time.time_ns()}"
        await store.aput(namespaced_key, key=key, value={"text": memory_text})

