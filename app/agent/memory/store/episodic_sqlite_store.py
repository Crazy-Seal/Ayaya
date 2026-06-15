"""情景记忆 SQLite 存储 - 权威数据备份"""

import logging
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


class EpisodicSqliteStore:
    """情景记忆 SQLite 存储类

    职责：
    - 作为情景记忆的权威数据源
    - 支持按时间范围、重要度等结构化查询
    - 记录 embedding 模型信息，支持模型迁移

    与 Chroma 的关系：
    - Chroma: 向量检索索引层
    - SQLite: 权威数据存储层（可恢复数据）
    """

    TABLE_NAME = "episodic_memories"

    def __init__(self, db_path: str, embedding_model: str):
        self.db_path = db_path
        self.embedding_model = embedding_model
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
                    event_date DATE NOT NULL,
                    importance REAL NOT NULL,
                    embedding_model TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_episodic_session
                ON {self.TABLE_NAME}(session_id)
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_episodic_date
                ON {self.TABLE_NAME}(event_date)
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_episodic_importance
                ON {self.TABLE_NAME}(importance)
            """)
            conn.commit()
        logger.info("[EpisodicSqliteStore] 表初始化完成: %s", self.db_path)

    # ==================== 写入方法 ====================

    async def add(
        self,
        record_id: str,
        session_id: str,
        content: str,
        event_date: str,
        importance: float,
    ) -> None:
        """添加情景记忆"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute(
                f"""
                INSERT OR REPLACE INTO {self.TABLE_NAME}
                (id, session_id, content, event_date, importance, embedding_model)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (record_id, session_id, content, event_date, importance, self.embedding_model),
            )
            await conn.commit()
        logger.debug("[EpisodicSqliteStore] 添加记忆: record_id=%s", record_id)

    async def update(
        self,
        record_id: str,
        content: str | None = None,
        importance: float | None = None,
    ) -> bool:
        """更新情景记忆"""
        updates = []
        params = []

        if content is not None:
            updates.append("content = ?")
            params.append(content)

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
        """删除情景记忆"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                f"DELETE FROM {self.TABLE_NAME} WHERE id = ?",
                (record_id,),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def delete_by_metadata(
        self,
        session_id: str | None = None,
        event_date_before: str | None = None,
        importance_below: float | None = None,
    ) -> int:
        """按条件批量删除"""
        conditions = []
        params = []

        if session_id is not None:
            conditions.append("session_id = ?")
            params.append(session_id)

        if event_date_before is not None:
            conditions.append("event_date < ?")
            params.append(event_date_before)

        if importance_below is not None:
            conditions.append("importance < ?")
            params.append(importance_below)

        if not conditions:
            return 0

        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                f"DELETE FROM {self.TABLE_NAME} WHERE {' AND '.join(conditions)}",
                params,
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
                SELECT id, session_id, content, event_date, importance, embedding_model, created_at
                FROM {self.TABLE_NAME}
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
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """获取所有记忆（用于重建向量索引）"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = aiosqlite.Row

            if session_id:
                cursor = await conn.execute(
                    f"""
                    SELECT id, session_id, content, event_date, importance, embedding_model, created_at
                    FROM {self.TABLE_NAME}
                    WHERE session_id = ?
                    ORDER BY event_date DESC
                    LIMIT ?
                    """,
                    (session_id, limit),
                )
            else:
                cursor = await conn.execute(
                    f"""
                    SELECT id, session_id, content, event_date, importance, embedding_model, created_at
                    FROM {self.TABLE_NAME}
                    ORDER BY event_date DESC
                    LIMIT ?
                    """,
                    (limit,),
                )

            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_by_date_range(
        self,
        session_id: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        """获取时间范围内的记忆"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                f"""
                SELECT id, session_id, content, event_date, importance, embedding_model, created_at
                FROM {self.TABLE_NAME}
                WHERE session_id = ? AND event_date >= ? AND event_date <= ?
                ORDER BY event_date ASC
                """,
                (session_id, start_date, end_date),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_by_importance(
        self,
        session_id: str,
        importance_below: float,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """获取重要性低于指定值的记忆"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                f"""
                SELECT id, session_id, content, event_date, importance, embedding_model, created_at
                FROM {self.TABLE_NAME}
                WHERE session_id = ? AND importance < ?
                ORDER BY importance ASC
                LIMIT ?
                """,
                (session_id, importance_below, limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== 统计方法 ====================

    async def count(self, session_id: str | None = None) -> int:
        """统计记忆数量"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            if session_id:
                cursor = await conn.execute(
                    f"SELECT COUNT(*) FROM {self.TABLE_NAME} WHERE session_id = ?",
                    (session_id,),
                )
            else:
                cursor = await conn.execute(
                    f"SELECT COUNT(*) FROM {self.TABLE_NAME}"
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

            # 按日期分布（最近7天）
            if session_id:
                cursor = await conn.execute(
                    f"""
                    SELECT event_date, COUNT(*) as count
                    FROM {self.TABLE_NAME}
                    WHERE session_id = ?
                    GROUP BY event_date
                    ORDER BY event_date DESC
                    LIMIT 7
                    """,
                    (session_id,),
                )
            else:
                cursor = await conn.execute(
                    f"""
                    SELECT event_date, COUNT(*) as count
                    FROM {self.TABLE_NAME}
                    GROUP BY event_date
                    ORDER BY event_date DESC
                    LIMIT 7
                    """
                )
            date_rows = await cursor.fetchall()
            date_distribution = {row["event_date"]: row["count"] for row in date_rows}

            # 按重要性分布
            if session_id:
                cursor = await conn.execute(
                    f"""
                    SELECT
                        CASE
                            WHEN importance < 0.2 THEN '0.0-0.2'
                            WHEN importance < 0.4 THEN '0.2-0.4'
                            WHEN importance < 0.6 THEN '0.4-0.6'
                            WHEN importance < 0.8 THEN '0.6-0.8'
                            ELSE '0.8-1.0'
                        END as range,
                        COUNT(*) as count
                    FROM {self.TABLE_NAME}
                    WHERE session_id = ?
                    GROUP BY range
                    """,
                    (session_id,),
                )
            else:
                cursor = await conn.execute(
                    f"""
                    SELECT
                        CASE
                            WHEN importance < 0.2 THEN '0.0-0.2'
                            WHEN importance < 0.4 THEN '0.2-0.4'
                            WHEN importance < 0.6 THEN '0.4-0.6'
                            WHEN importance < 0.8 THEN '0.6-0.8'
                            ELSE '0.8-1.0'
                        END as range,
                        COUNT(*) as count
                    FROM {self.TABLE_NAME}
                    GROUP BY range
                    """
                )
            imp_rows = await cursor.fetchall()
            importance_distribution = {row["range"]: row["count"] for row in imp_rows}

            return {
                "total": total,
                "avg_importance": avg_importance,
                "date_distribution": date_distribution,
                "importance_distribution": importance_distribution,
            }

    async def get_embedding_models(self) -> list[str]:
        """获取所有使用的 embedding 模型"""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                f"SELECT DISTINCT embedding_model FROM {self.TABLE_NAME}"
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]
