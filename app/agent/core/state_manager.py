"""Agent checkpoint 与 append-only OpenAI 协议日志的原子持久化。"""

import json
import logging
import os
from typing import Literal
from uuid import uuid4

import aiosqlite

from app.agent.state import AgentState
from app.agent.message_time import normalize_utc, utc_now
from app.runtime import get_checkpoint_db

logger = logging.getLogger(__name__)

CheckpointType = Literal["intermediate", "completed"]


class StateManager:
    """状态管理器 - checkpoint 和状态持久化。"""

    # 每个 session 最多保留的完成态数量；中间态另外最多保留一条。
    MAX_COMPLETED_CHECKPOINTS_PER_SESSION = 30

    def __init__(
        self,
        session_id: str,
        db_path: str | None = None,
    ):
        self.session_id = session_id
        self.db_path = str(db_path or get_checkpoint_db())
        self._db: aiosqlite.Connection | None = None
        self.last_loaded_checkpoint_type: CheckpointType | None = None

    async def _get_db(self) -> aiosqlite.Connection:
        """获取数据库连接。"""
        if self._db is None:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._db = await aiosqlite.connect(self.db_path)
            await self._init_tables()
        return self._db

    async def _init_tables(self) -> None:
        """初始化 checkpoint 与协议日志。"""
        db = await self._get_db()
        await db.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                state_json TEXT NOT NULL,
                checkpoint_type TEXT NOT NULL DEFAULT 'completed'
                    CHECK(checkpoint_type IN ('intermediate', 'completed')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_checkpoints_session_type_id
            ON checkpoints(session_id, checkpoint_type, id DESC)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
                message_json TEXT NOT NULL,
                record_kind TEXT NOT NULL CHECK (
                    record_kind IN ('protocol', 'tool_image', 'legacy_tool_notice')
                ),
                fidelity TEXT NOT NULL CHECK (fidelity IN ('exact', 'projected')),
                associated_message_id TEXT,
                attachments_json TEXT,
                legacy_source_id TEXT,
                UNIQUE (session_id, message_id)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversation_messages_session_id
            ON conversation_messages(session_id, id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversation_messages_session_time
            ON conversation_messages(session_id, created_at, id)
        """)
        await db.commit()

    @staticmethod
    def _ensure_message_metadata(state: AgentState) -> None:
        for message in state.messages:
            message.setdefault("_message_id", uuid4().hex)
            created_at = message.get("_created_at") or state.updated_at
            message["_created_at"] = normalize_utc(created_at).isoformat()

    @staticmethod
    def _protocol_rows(state: AgentState) -> list[tuple]:
        rows: list[tuple] = []
        for message in state.messages:
            role = message.get("role")
            name = message.get("name")
            if role == "system" or name == "context_summary":
                continue
            if role not in {"user", "assistant", "tool"}:
                continue
            record_kind = "tool_image" if role == "user" and name in {
                "mcp_tool_image", "system_screenshot"
            } else "protocol"
            payload = {
                key: value for key, value in message.items()
                if key not in {"_message_id", "_created_at"}
            }
            attachments = message.get("image_filenames")
            rows.append((
                state.session_id,
                message["_message_id"],
                message["_created_at"],
                role,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
                record_kind,
                "exact",
                message.get("_associated_message_id"),
                json.dumps(attachments, ensure_ascii=False) if attachments else None,
                None,
            ))
        return rows

    async def load(self) -> AgentState:
        """从最新 checkpoint 恢复状态；没有记录时创建新状态。"""
        db = await self._get_db()

        async with db.execute(
            """
            SELECT id, state_json, checkpoint_type FROM checkpoints
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (self.session_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            checkpoint_id, state_json, checkpoint_type = row
            self.last_loaded_checkpoint_type = checkpoint_type
            state = AgentState.from_checkpoint(json.loads(state_json))
            logger.info("从 checkpoint %s 恢复状态: %s", checkpoint_id, self.session_id)
            return state

        logger.info("创建新状态: %s", self.session_id)
        self.last_loaded_checkpoint_type = None
        return AgentState.create_new(self.session_id)

    async def save(
        self,
        state: AgentState,
        *,
        checkpoint_type: CheckpointType,
    ) -> int:
        """原子保存指定类型的 checkpoint，并执行对应的分层裁剪。"""
        db = await self._get_db()
        self._ensure_message_metadata(state)
        state_json = json.dumps(state.to_checkpoint(), ensure_ascii=False, default=str)
        protocol_rows = self._protocol_rows(state)

        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                INSERT INTO checkpoints (
                    session_id, state_json, checkpoint_type, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    self.session_id,
                    state_json,
                    checkpoint_type,
                    utc_now().isoformat(),
                ),
            )
            checkpoint_id = cursor.lastrowid
            if checkpoint_id is None:
                raise RuntimeError("数据库未返回 checkpoint ID")

            if checkpoint_type == "intermediate":
                await db.execute(
                    """
                    DELETE FROM checkpoints
                    WHERE session_id = ?
                      AND checkpoint_type = 'intermediate'
                      AND id <> ?
                    """,
                    (self.session_id, checkpoint_id),
                )
            else:
                # 完成态已包含本轮完整状态，对应中间态不再有恢复价值。
                await db.execute(
                    """
                    DELETE FROM checkpoints
                    WHERE session_id = ? AND checkpoint_type = 'intermediate'
                    """,
                    (self.session_id,),
                )
                await self._prune_completed(db)

            if protocol_rows:
                await db.executemany(
                    """
                    INSERT INTO conversation_messages (
                        session_id, message_id, created_at, role, message_json,
                        record_kind, fidelity, associated_message_id,
                        attachments_json, legacy_source_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, message_id) DO UPDATE SET
                        attachments_json = COALESCE(
                            excluded.attachments_json,
                            conversation_messages.attachments_json
                        )
                    """,
                    protocol_rows,
                )

            await db.commit()
        except BaseException:
            await db.rollback()
            raise

        logger.info(
            "保存%s checkpoint %s: %s",
            "中间态" if checkpoint_type == "intermediate" else "完成态",
            checkpoint_id,
            self.session_id,
        )
        return checkpoint_id

    async def _prune_completed(self, db: aiosqlite.Connection) -> None:
        """仅裁剪完成态，保留当前 session 最新的 30 条。"""
        await db.execute(
            """
            DELETE FROM checkpoints
            WHERE session_id = ?
              AND checkpoint_type = 'completed'
              AND id NOT IN (
                SELECT id FROM checkpoints
                WHERE session_id = ? AND checkpoint_type = 'completed'
                ORDER BY id DESC
                LIMIT ?
            )
            """,
            (
                self.session_id,
                self.session_id,
                self.MAX_COMPLETED_CHECKPOINTS_PER_SESSION,
            ),
        )

    async def clear_session(self) -> int:
        """清空指定 session 的所有 checkpoint，并返回删除数量。"""
        db = await self._get_db()

        async with db.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?",
            (self.session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            deleted_count = row[0] if row else 0

        await db.execute(
            "DELETE FROM checkpoints WHERE session_id = ?",
            (self.session_id,),
        )
        await db.commit()

        logger.info("清空 session %s: 删除了 %s 个 checkpoint", self.session_id, deleted_count)
        return deleted_count

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._db:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> "StateManager":
        await self._get_db()
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()
