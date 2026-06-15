"""语义记忆 SQLite 存储 - 权威数据源"""

import logging
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


class SemanticSqliteStore:
    """语义记忆 SQLite 存储类

    职责：
    - 作为语义记忆的权威数据源
    - 存储完整记忆记录 + 语义三元组
    - 支持冲突检测查询
    """

    TABLE_NAME = "semantic_memories"

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_table()

    def _ensure_table(self):
        """确保表存在（同步方法，仅在初始化时调用）"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")

            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    importance REAL NOT NULL,
                    event_date DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                    -- 语义知识（可为空）
                    subject TEXT,
                    relation TEXT,
                    obj TEXT,
                    time_note TEXT,
                    is_single_value BOOLEAN DEFAULT 1,

                    -- 冲突处理
                    is_current BOOLEAN DEFAULT 1,
                    superseded_by TEXT
                )
            """)

            # 创建索引
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_semantic_session
                ON {self.TABLE_NAME}(session_id)
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_semantic_subject
                ON {self.TABLE_NAME}(session_id, subject)
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_semantic_conflict
                ON {self.TABLE_NAME}(session_id, subject, relation)
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_semantic_current
                ON {self.TABLE_NAME}(session_id, is_current)
            """)

            conn.commit()
        logger.info("[SemanticSqliteStore] 表初始化完成: %s", self.db_path)

    # ==================== 写入方法 ====================

    async def add(
        self,
        record_id: str,
        session_id: str,
        content: str,
        importance: float,
        event_date: str,
        subject: str | None = None,
        relation: str | None = None,
        obj: str | None = None,
        time_note: str | None = None,
        is_single_value: bool = True,
    ) -> None:
        """添加语义记忆"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute(
                f"""
                INSERT INTO {self.TABLE_NAME}
                (id, session_id, content, importance, event_date,
                 subject, relation, obj, time_note, is_single_value)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (record_id, session_id, content, importance, event_date,
                 subject, relation, obj, time_note, is_single_value),
            )
            await conn.commit()
        logger.debug("[SemanticSqliteStore] 添加记忆: record_id=%s", record_id)

    async def update(
        self,
        record_id: str,
        is_current: bool | None = None,
        superseded_by: str | None = None,
        importance: float | None = None,
    ) -> bool:
        """更新语义记忆"""
        updates = []
        params = []

        if is_current is not None:
            updates.append("is_current = ?")
            params.append(is_current)

        if superseded_by is not None:
            updates.append("superseded_by = ?")
            params.append(superseded_by)

        if importance is not None:
            updates.append("importance = ?")
            params.append(importance)

        if not updates:
            return False

        params.append(record_id)

        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                f"""
                UPDATE {self.TABLE_NAME}
                SET {', '.join(updates)}
                WHERE id = ?
                """,
                params,
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def delete(self, record_id: str) -> bool:
        """删除语义记忆"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                f"DELETE FROM {self.TABLE_NAME} WHERE id = ?",
                (record_id,),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def delete_by_session(self, session_id: str) -> int:
        """删除指定会话的所有记忆"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                f"DELETE FROM {self.TABLE_NAME} WHERE session_id = ?",
                (session_id,),
            )
            await conn.commit()
            return cursor.rowcount

    # ==================== 查询方法 ====================

    async def get(self, record_id: str) -> dict[str, Any] | None:
        """获取单条记忆"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                f"""
                SELECT * FROM {self.TABLE_NAME}
                WHERE id = ?
                """,
                (record_id,),
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None

    async def get_all(
        self,
        session_id: str | None = None,
        is_current: bool | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """获取所有记忆"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = aiosqlite.Row

            conditions = []
            params = []

            if session_id is not None:
                conditions.append("session_id = ?")
                params.append(session_id)

            if is_current is not None:
                conditions.append("is_current = ?")
                params.append(is_current)

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            params.append(limit)
            cursor = await conn.execute(
                f"""
                SELECT * FROM {self.TABLE_NAME}
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            )

            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def find_conflicting(
        self,
        session_id: str,
        subject: str,
        relation: str,
        exclude_obj: str,
    ) -> list[str]:
        """查找冲突的记忆 ID

        冲突定义：同 session + 同 subject + 同 relation + 不同 obj + 当前有效
        """
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                f"""
                SELECT id FROM {self.TABLE_NAME}
                WHERE session_id = ?
                  AND subject = ?
                  AND relation = ?
                  AND obj != ?
                  AND is_current = 1
                  AND is_single_value = 1
                """,
                (session_id, subject, relation, exclude_obj),
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def find_by_triple(
        self,
        session_id: str,
        subject: str,
        relation: str,
        obj: str,
    ) -> dict[str, Any] | None:
        """按三元组查询记忆"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                f"""
                SELECT * FROM {self.TABLE_NAME}
                WHERE session_id = ?
                  AND subject = ?
                  AND relation = ?
                  AND obj = ?
                  AND is_current = 1
                LIMIT 1
                """,
                (session_id, subject, relation, obj),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def find_by_subject(
        self,
        session_id: str,
        subject: str,
        is_current: bool = True,
    ) -> list[dict[str, Any]]:
        """按主体查询记忆"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                f"""
                SELECT * FROM {self.TABLE_NAME}
                WHERE session_id = ?
                  AND subject = ?
                  AND is_current = ?
                ORDER BY created_at DESC
                """,
                (session_id, subject, is_current),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def find_by_relation(
        self,
        session_id: str,
        subject: str,
        relation: str,
    ) -> list[dict[str, Any]]:
        """按主体和关系查询记忆"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                f"""
                SELECT * FROM {self.TABLE_NAME}
                WHERE session_id = ?
                  AND subject = ?
                  AND relation = ?
                  AND is_current = 1
                ORDER BY created_at DESC
                """,
                (session_id, subject, relation),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== 统计方法 ====================

    async def count(
        self,
        session_id: str | None = None,
        is_current: bool | None = None,
    ) -> int:
        """统计记忆数量"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")

            conditions = []
            params = []

            if session_id is not None:
                conditions.append("session_id = ?")
                params.append(session_id)

            if is_current is not None:
                conditions.append("is_current = ?")
                params.append(is_current)

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            cursor = await conn.execute(
                f"SELECT COUNT(*) FROM {self.TABLE_NAME} {where_clause}",
                params,
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def get_stats(self, session_id: str | None = None) -> dict[str, Any]:
        """获取统计信息"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = aiosqlite.Row

            # 总数
            if session_id:
                cursor = await conn.execute(
                    f"SELECT COUNT(*) as count FROM {self.TABLE_NAME} WHERE session_id = ?",
                    (session_id,),
                )
            else:
                cursor = await conn.execute(
                    f"SELECT COUNT(*) as count FROM {self.TABLE_NAME}"
                )
            total_row = await cursor.fetchone()
            total = total_row["count"] if total_row else 0

            # 当前有效数量
            if session_id:
                cursor = await conn.execute(
                    f"SELECT COUNT(*) as count FROM {self.TABLE_NAME} WHERE session_id = ? AND is_current = 1",
                    (session_id,),
                )
            else:
                cursor = await conn.execute(
                    f"SELECT COUNT(*) as count FROM {self.TABLE_NAME} WHERE is_current = 1"
                )
            current_row = await cursor.fetchone()
            current = current_row["count"] if current_row else 0

            # 平均重要性
            if session_id:
                cursor = await conn.execute(
                    f"SELECT AVG(importance) as avg_importance FROM {self.TABLE_NAME} WHERE session_id = ?",
                    (session_id,),
                )
            else:
                cursor = await conn.execute(
                    f"SELECT AVG(importance) as avg_importance FROM {self.TABLE_NAME}"
                )
            avg_row = await cursor.fetchone()
            avg_importance = avg_row["avg_importance"] if avg_row and avg_row["avg_importance"] else 0.0

            return {
                "total": total,
                "current": current,
                "obsolete": total - current,
                "avg_importance": avg_importance,
            }
