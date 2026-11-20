from app.crud.sqlite_factory import SqliteFactory
import asyncio
from datetime import date

from app.agent.plugins.memory.store.diary_sqlite_store import DiarySqliteStore


def test_diary_is_unique_by_session_and_effective_date(tmp_path) -> None:
    """验证同会话同有效日期只保留一篇日记。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario():
        """由外层测试调用，完成“同会话同有效日期只保留一篇日记”的异步步骤。"""
        store = DiarySqliteStore(str(tmp_path / "memory.sqlite3"), sqlite_factory=SqliteFactory())
        target = date(2026, 7, 10)
        await store.add("session", target, "first")
        await store.add("session", target, "updated")
        return await store.get("session", target)

    assert asyncio.run(scenario()) == "updated"
