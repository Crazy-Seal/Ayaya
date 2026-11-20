"""按有效日期唯一存储日记；上下文摘要不进入此数据库。"""

from datetime import date
from pathlib import Path

from contextlib import closing
from app.crud.sqlite_factory import SqliteFactory


class DiarySqliteStore:
    """在独立日记表中按会话和有效日期保存唯一日记，重复保存更新同一天内容。"""
    TABLE_NAME = "diaries"

    def __init__(self, db_path: str, *, sqlite_factory: SqliteFactory) -> None:
        """保存日记数据库路径并同步创建目录、表和日期索引。

        Args:
            db_path (str): 当前环境使用的 SQLite 数据库路径。
            sqlite_factory (SqliteFactory): 应用装配提供的 SQLite 连接配置。
        """
        self._sqlite_factory = sqlite_factory
        self.db_path = db_path
        self._ensure_table()

    def _ensure_table(self) -> None:
        """初始化日记表及会话日期索引，唯一约束防止同一天产生多份记录。"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._sqlite_factory.connect_sync(self.db_path)) as conn, conn:
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
        """保存并提交指定日期的日记；同会话同日期已经存在时更新正文。

        Args:
            session_id (str): 当前操作所属的会话编号。
            date_obj (date): 要保存或查询的日记有效日期。
            content (str): 当前处理的正文。

        Returns:
            int: SQLite 游标提供的最近插入编号，未提供时为零；更新分支不保证是被更新行的编号。
        """
        async with self._sqlite_factory.connection(self.db_path) as conn:
            cursor = await conn.execute(f"""
                INSERT INTO {self.TABLE_NAME} (session_id, effective_date, content)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id, effective_date) DO UPDATE SET
                    content=excluded.content, updated_at=CURRENT_TIMESTAMP
            """, (session_id, date_obj.isoformat(), content))
            await conn.commit()
            return int(cursor.lastrowid or 0)

    async def get(self, session_id: str, date_obj: date) -> str | None:
        """按会话和有效日期读取一篇已保存日记。

        Args:
            session_id (str): 当前操作所属的会话编号。
            date_obj (date): 要保存或查询的日记有效日期。

        Returns:
            str | None: 日记正文，不存在时为空。
        """
        async with self._sqlite_factory.connection(self.db_path) as conn:
            row = await (await conn.execute(
                f"SELECT content FROM {self.TABLE_NAME} WHERE session_id=? AND effective_date=?",
                (session_id, date_obj.isoformat()),
            )).fetchone()
            return row[0] if row else None

    async def exists(self, session_id: str, date_obj: date) -> bool:
        """检查指定会话和有效日期是否已有日记记录。

        Args:
            session_id (str): 当前操作所属的会话编号。
            date_obj (date): 要保存或查询的日记有效日期。

        Returns:
            bool: 查到日记正文时为 True。
        """
        return (await self.get(session_id, date_obj)) is not None

    async def get_range(
        self, session_id: str, start: date, end: date
    ) -> list[tuple[date, str]]:
        """读取包含起止日期的日记范围，并按日期从早到晚排列。

        Args:
            session_id (str): 当前操作所属的会话编号。
            start (date): 查询范围的起始日期或时间。
            end (date): 查询范围的结束日期或时间。

        Returns:
            list[tuple[date, str]]: 有效日期与正文组成的记录列表。
        """
        async with self._sqlite_factory.connection(self.db_path) as conn:
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
        """查询指定日期之前最近的若干篇日记，供新日记理解前情。

        Args:
            session_id (str): 当前操作所属的会话编号。
            before_date (date): 只读取该有效日期之前的记录。
            n (int): 最多返回的日记条数。

        Returns:
            list[tuple[date, str]]: 按日期从晚到早排列的记录列表。
        """
        async with self._sqlite_factory.connection(self.db_path) as conn:
            rows = await conn.execute_fetchall(f"""
                SELECT effective_date, content FROM {self.TABLE_NAME}
                WHERE session_id=? AND effective_date<?
                ORDER BY effective_date DESC LIMIT ?
            """, (session_id, before_date.isoformat(), n))
        return [(date.fromisoformat(row[0]), row[1]) for row in rows]
