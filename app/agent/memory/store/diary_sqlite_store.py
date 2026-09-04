"""按有效日期唯一存储日记；上下文摘要不进入此数据库。"""

from datetime import date
from pathlib import Path
import sqlite3

import aiosqlite


class DiarySqliteStore:
    TABLE_NAME = "diaries"

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._ensure_table()

    def _ensure_table(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    effective_date DATE NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(session_id, effective_date)
                )
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_diaries_session_date
                ON {self.TABLE_NAME}(session_id, effective_date)
            """)

    async def add(
        self,
        session_id: str,
        date_obj: date,
        content: str,
    ) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(f"""
                INSERT INTO {self.TABLE_NAME} (session_id, effective_date, content)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id, effective_date) DO UPDATE SET
                    content=excluded.content, updated_at=CURRENT_TIMESTAMP
            """, (session_id, date_obj.isoformat(), content))
            await conn.commit()
            return int(cursor.lastrowid or 0)

    async def get(self, session_id: str, date_obj: date) -> str | None:
        async with aiosqlite.connect(self.db_path) as conn:
            row = await (await conn.execute(
                f"SELECT content FROM {self.TABLE_NAME} WHERE session_id=? AND effective_date=?",
                (session_id, date_obj.isoformat()),
            )).fetchone()
            return row[0] if row else None

    async def exists(self, session_id: str, date_obj: date) -> bool:
        return (await self.get(session_id, date_obj)) is not None

    async def get_range(
        self, session_id: str, start: date, end: date
    ) -> list[tuple[date, str]]:
        async with aiosqlite.connect(self.db_path) as conn:
            rows = await conn.execute_fetchall(f"""
                SELECT effective_date, content FROM {self.TABLE_NAME}
                WHERE session_id=? AND effective_date>=? AND effective_date<=?
                ORDER BY effective_date ASC
            """, (session_id, start.isoformat(), end.isoformat()))
        return [(date.fromisoformat(row[0]), row[1]) for row in rows]
    async def get_recent_before_date(
        self,
        session_id: str,
        before_date: date,
        n: int = 2,
    ) -> list[tuple[date, str]]:
        async with aiosqlite.connect(self.db_path) as conn:
            rows = await conn.execute_fetchall(f"""
                SELECT effective_date, content FROM {self.TABLE_NAME}
                WHERE session_id=? AND effective_date<?
                ORDER BY effective_date DESC LIMIT ?
            """, (session_id, before_date.isoformat(), n))
        return [(date.fromisoformat(row[0]), row[1]) for row in rows]
