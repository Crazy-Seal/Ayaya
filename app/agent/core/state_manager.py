"""
状态管理器

负责状态持久化、checkpoint 管理、回滚。
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any

import aiosqlite

from app.agent.state import AgentState
from app.runtime import get_checkpoint_db

logger = logging.getLogger(__name__)


class StateManager:
    """状态管理器 - checkpoint 和状态持久化"""

    # 每个 session 最多保留的 checkpoint 行数（防止全量快照无限膨胀）
    MAX_CHECKPOINTS_PER_SESSION = 30

    def __init__(
        self,
        session_id: str,
        db_path: str | None = None,
    ):
        self.session_id = session_id
        self.db_path = str(db_path or get_checkpoint_db())
        self._db: aiosqlite.Connection | None = None
        self._watermark: int | None = None  # 用于回滚的水位线

    async def _get_db(self) -> aiosqlite.Connection:
        """获取数据库连接"""
        if self._db is None:
            # 确保目录存在
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._db = await aiosqlite.connect(self.db_path)
            await self._init_tables()
        return self._db

    async def _init_tables(self) -> None:
        """初始化数据库表"""
        db = await self._get_db()
        await db.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_checkpoints_session
            ON checkpoints(session_id, created_at DESC)
        """)
        await db.commit()

    async def load(self) -> AgentState:
        """加载状态（从最新的 checkpoint 恢复或创建新状态）"""
        db = await self._get_db()

        async with db.execute(
            """
            SELECT id, state_json FROM checkpoints
            WHERE session_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (self.session_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            checkpoint_id, state_json = row
            self._watermark = checkpoint_id
            state_data = json.loads(state_json)
            state = AgentState.from_checkpoint(state_data)
            logger.info(f"从 checkpoint {checkpoint_id} 恢复状态: {self.session_id}")
            return state
        else:
            # 创建新状态
            self._watermark = None
            state = AgentState.create_new(self.session_id)
            logger.info(f"创建新状态: {self.session_id}")
            return state

    async def save(self, state: AgentState) -> int:
        """保存状态到 checkpoint

        Returns:
            int: checkpoint ID
        """
        db = await self._get_db()

        state_json = json.dumps(state.to_checkpoint(), ensure_ascii=False, default=str)

        cursor = await db.execute(
            """
            INSERT INTO checkpoints (session_id, state_json, created_at)
            VALUES (?, ?, ?)
            """,
            (self.session_id, state_json, datetime.now().isoformat())
        )
        await db.commit()

        checkpoint_id = cursor.lastrowid
        logger.info(f"保存 checkpoint {checkpoint_id}: {self.session_id}")

        await self._prune(db)
        return checkpoint_id

    async def _prune(self, db: aiosqlite.Connection) -> None:
        """裁剪：每个 session 仅保留最近 N 个 checkpoint，防止无限膨胀"""
        await db.execute(
            """
            DELETE FROM checkpoints
            WHERE session_id = ? AND id NOT IN (
                SELECT id FROM checkpoints
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
            """,
            (self.session_id, self.session_id, self.MAX_CHECKPOINTS_PER_SESSION),
        )
        await db.commit()

    async def rollback(self) -> bool:
        """回滚到上一个 checkpoint

        Returns:
            bool: 是否成功回滚
        """
        if self._watermark is None:
            logger.warning("没有可回滚的 checkpoint")
            return False

        db = await self._get_db()

        # watermark 是本轮开始前 load() 记录的良好状态；删除本轮新增版本。
        async with db.execute(
            """
            SELECT COUNT(*) FROM checkpoints
            WHERE session_id = ? AND id > ?
            """,
            (self.session_id, self._watermark),
        ) as cursor:
            row = await cursor.fetchone()

        if not row or row[0] == 0:
            logger.warning("水位线之后没有可回滚的检查点: %s", self.session_id)
            return False

        await db.execute(
            """
            DELETE FROM checkpoints
            WHERE session_id = ? AND id > ?
            """,
            (self.session_id, self._watermark),
        )
        await db.commit()

        logger.info("回滚检查点: %s（水位线=%s）", self.session_id, self._watermark)

        return True

    async def rollback_to_watermark(self, watermark: int) -> int:
        """回滚到指定的 watermark

        Args:
            watermark: 目标 watermark（不删除）

        Returns:
            int: 删除的 checkpoint 数量
        """
        db = await self._get_db()

        # 统计要删除的数量
        async with db.execute(
            """
            SELECT COUNT(*) FROM checkpoints
            WHERE session_id = ? AND id > ?
            """,
            (self.session_id, watermark)
        ) as cursor:
            row = await cursor.fetchone()
            deleted_count = row[0] if row else 0

        # 删除 watermark 之后的所有 checkpoint
        await db.execute(
            """
            DELETE FROM checkpoints
            WHERE session_id = ? AND id > ?
            """,
            (self.session_id, watermark)
        )
        await db.commit()

        self._watermark = watermark
        logger.info(f"回滚到 watermark {watermark}: 删除了 {deleted_count} 个 checkpoint")
        return deleted_count

    def get_watermark(self) -> int | None:
        """获取当前 watermark"""
        return self._watermark

    async def clear_session(self) -> int:
        """清空指定 session 的所有 checkpoint

        Returns:
            int: 删除的 checkpoint 数量
        """
        db = await self._get_db()

        async with db.execute(
            """
            SELECT COUNT(*) FROM checkpoints WHERE session_id = ?
            """,
            (self.session_id,)
        ) as cursor:
            row = await cursor.fetchone()
            deleted_count = row[0] if row else 0

        await db.execute(
            """
            DELETE FROM checkpoints WHERE session_id = ?
            """,
            (self.session_id,)
        )
        await db.commit()

        self._watermark = None
        logger.info(f"清空 session {self.session_id}: 删除了 {deleted_count} 个 checkpoint")
        return deleted_count

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._db:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> "StateManager":
        await self._get_db()
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()
