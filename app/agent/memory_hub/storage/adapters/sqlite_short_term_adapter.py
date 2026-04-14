import aiosqlite

from app.agent.memory_hub.constants import STORE_DB_PATH
from app.agent.memory_hub.storage.interfaces import ShortTermMemoryAdapter


class SqliteShortTermMemoryAdapter(ShortTermMemoryAdapter):
    """基于 sqlite short_memory 表的短期记忆适配器。"""

    async def get_latest_async(self, session_id: str) -> str:
        STORE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(STORE_DB_PATH)) as conn:
            cursor = await conn.execute(
                """
                SELECT content
                FROM short_memory
                WHERE thread_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT 1
                """,
                (session_id,),
            )
            row = await cursor.fetchone()
        return str(row[0]) if row else ""

    async def save_async(self, session_id: str, content: str) -> None:
        STORE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(STORE_DB_PATH)) as conn:
            await conn.execute(
                "INSERT INTO short_memory (thread_id, content) VALUES (?, ?)",
                (session_id, content),
            )
            await conn.commit()

