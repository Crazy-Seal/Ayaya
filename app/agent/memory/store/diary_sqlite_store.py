"""
SQLite 存储 - 摘要记忆和日记存储
"""

import sqlite3
from datetime import date
from pathlib import Path

import aiosqlite


class DiarySqliteStore:
    """日记、摘要记忆 SQLite 存储类"""

    TABLE_NAME = "summary_diary"

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_table()

    def _ensure_table(self):
        """确保表存在"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")

            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    date DATE NOT NULL,
                    content TEXT NOT NULL,
                    is_diary BOOLEAN NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(session_id, date, is_diary)
                )
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_session_date
                ON {self.TABLE_NAME}(session_id, date)
            """)
            conn.commit()

    # ==================== 写入方法 ====================

    async def add(
        self,
        session_id: str,
        date_obj: date,
        content: str,
        is_diary: bool = False,
    ) -> int:
        """添加或更新摘要/日记（覆盖更新）"""
        date_str = date_obj.isoformat()
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                f"""
                INSERT INTO {self.TABLE_NAME} (session_id, date, content, is_diary)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, date, is_diary)
                DO UPDATE SET content = excluded.content, created_at = CURRENT_TIMESTAMP
                """,
                (session_id, date_str, content, 1 if is_diary else 0),
            )
            await conn.commit()
            return cursor.lastrowid

    # ==================== 查询方法 ====================

    async def get(
        self,
        session_id: str,
        date_obj: date,
        is_diary: bool = False,
    ) -> str | None:
        """获取指定日期的内容"""
        date_str = date_obj.isoformat()
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                f"SELECT content FROM {self.TABLE_NAME} WHERE session_id = ? AND date = ? AND is_diary = ?",
                (session_id, date_str, 1 if is_diary else 0),
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def exists(
        self,
        session_id: str,
        date_obj: date,
        is_diary: bool = False,
    ) -> bool:
        """检查指定日期是否存在记录"""
        date_str = date_obj.isoformat()
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                f"SELECT 1 FROM {self.TABLE_NAME} WHERE session_id = ? AND date = ? AND is_diary = ?",
                (session_id, date_str, 1 if is_diary else 0),
            )
            row = await cursor.fetchone()
            return row is not None

    async def get_range(
        self,
        session_id: str,
        start: date,
        end: date,
        is_diary: bool = True,
    ) -> list[tuple[date, str]]:
        """获取时间范围内的内容"""
        start_str = start.isoformat()
        end_str = end.isoformat()
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                f"""
                SELECT date, content FROM {self.TABLE_NAME}
                WHERE session_id = ? AND date >= ? AND date <= ? AND is_diary = ?
                ORDER BY date ASC
                """,
                (session_id, start_str, end_str, 1 if is_diary else 0),
            )
            rows = await cursor.fetchall()
            return [(date.fromisoformat(row[0]), row[1]) for row in rows]

    async def get_recent_before_date(
        self,
        session_id: str,
        before_date: date,
        n: int = 2,
        is_diary: bool = True,
    ) -> list[tuple[date, str]]:
        """获取指定日期之前最近的 n 条日记或摘要"""
        date_str = before_date.isoformat()
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                f"""
                SELECT date, content FROM {self.TABLE_NAME}
                WHERE session_id = ? AND date < ? AND is_diary = ?
                ORDER BY date DESC
                LIMIT ?
                """,
                (session_id, date_str, 1 if is_diary else 0, n),
            )
            rows = await cursor.fetchall()
            return [(date.fromisoformat(row[0]), row[1]) for row in rows]
