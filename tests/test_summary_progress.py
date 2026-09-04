import asyncio
from datetime import date

from app.agent.memory.store.diary_sqlite_store import DiarySqliteStore


def test_diary_is_unique_by_session_and_effective_date(tmp_path) -> None:
    async def scenario():
        store = DiarySqliteStore(str(tmp_path / "memory.sqlite3"))
        target = date(2026, 7, 10)
        await store.add("session", target, "first")
        await store.add("session", target, "updated")
        return await store.get("session", target)

    assert asyncio.run(scenario()) == "updated"
