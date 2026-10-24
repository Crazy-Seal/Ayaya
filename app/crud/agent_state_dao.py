"""原子 checkpoint 与追加式协议日志持久化。"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import aiosqlite

from app.schemas.runtime_types import CheckpointType
from app.schemas.message_types import ImageContextState, MessageType
from app.agent.state.messages import (
    BaseMessage,
    PROTOCOL_MESSAGE_TYPES,
)
from app.agent.state.model import AgentState
from app.agent.state.time import utc_now
from app.agent.runtime.session_inbox import InboxEnvelope
from app.crud.session_inbox_dao import (
    ensure_inbox_schema,
    mark_delivered_in_transaction,
    release_queued_in_transaction,
)
from app.runtime import get_checkpoint_db


logger = logging.getLogger(__name__)


LATEST_CHECKPOINT_SQL = """
SELECT id, state_json, checkpoint_type FROM checkpoints
WHERE id = (
    SELECT MAX(id) FROM checkpoints WHERE session_id = ?
)
"""

class AgentStateDao:
    """拥有会话 checkpoint 和协议历史，协调状态保存与输入交付的原子提交。

    由 SessionStateManager 调用，负责存储编码、跨表事务和历史保留。
    不准备业务消息、不保存图片文件，也不调用 Agent 或 Scheduler。
    Inbox 事务函数使用本类当前连接，任一步失败或取消时回滚整笔数据库写入。

    Attributes:
        session_id (str): 共享 Ayaya 会话标识。
        db_path (str | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
        _db (aiosqlite.Connection | None): 参与当前原子持久化操作的 SQLite 连接。
        last_loaded_checkpoint_type (CheckpointType | None): 最近载入的 checkpoint 类型，用于恢复判断。
    """

    MAX_COMPLETED_CHECKPOINTS_PER_SESSION = 30

    async def latest_checkpoints(self) -> list[tuple[str, dict[str, Any], CheckpointType]]:
        """读取各会话最新的持久快照，供上层判断是否需要恢复收尾。

        Returns:
            list[tuple[str, dict[str, Any], CheckpointType]]: 会话编号、已解码快照及类型。
        """
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT session_id,state_json,checkpoint_type FROM checkpoints AS current "
            "WHERE id=(SELECT MAX(id) FROM checkpoints WHERE session_id=current.session_id)"
        )
        return [(str(session), json.loads(payload), CheckpointType(kind)) for session, payload, kind in rows]

    def __init__(self, session_id: str, db_path: str | None = None):
        """绑定会话和数据库路径；首次读写时才创建连接及共享表。

        Args:
            session_id (str): 当前操作所属的会话编号。
            db_path (str | None): 当前环境使用的 SQLite 数据库路径。
        """
        self.session_id = session_id
        self.db_path = str(db_path or get_checkpoint_db())
        self._db: aiosqlite.Connection | None = None
        self.last_loaded_checkpoint_type: CheckpointType | None = None

    async def _get_db(self) -> aiosqlite.Connection:
        """首次使用时连接当前环境数据库并初始化共享表，之后复用本对象的连接。

        Returns:
            aiosqlite.Connection: 已完成表结构检查的 SQLite 连接。
        """
        if self._db is None:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._db = await aiosqlite.connect(self.db_path)
            await self._init_tables()
        return self._db


    async def _init_tables(self) -> None:
        """初始化 v3 业务历史和 checkpoint；旧表必须先离线迁移。"""
        db = await self._get_db()
        # 1. 在任何初始化写入之前识别旧格式，禁止运行时自动迁移生产记录。
        columns = {str(row[1]) for row in await db.execute_fetchall("PRAGMA table_info(conversation_messages)")}
        if columns and ("role" in columns or "domain_json" in columns):
            raise RuntimeError("conversation_messages 需要先执行消息协议 v3 离线迁移")
        await ensure_inbox_schema(db)
        await db.execute("""CREATE TABLE IF NOT EXISTS checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            state_json TEXT NOT NULL, checkpoint_type TEXT NOT NULL DEFAULT 'completed'
            CHECK(checkpoint_type IN ('intermediate','completed')), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_session_type_id ON checkpoints(session_id, checkpoint_type, id DESC)")
        allowed = ",".join(f"'{value}'" for value in PROTOCOL_MESSAGE_TYPES)
        await db.execute(f"""CREATE TABLE IF NOT EXISTS conversation_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, message_id TEXT NOT NULL,
            created_at TEXT NOT NULL, record_kind TEXT NOT NULL CHECK(record_kind IN ('protocol','legacy_tool_notice')),
            fidelity TEXT NOT NULL CHECK(fidelity IN ('exact','projected')), legacy_source_id TEXT,
            message_type TEXT CHECK(message_type IN ({allowed})), message_json TEXT NOT NULL,
            UNIQUE(session_id,message_id))""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_conversation_messages_session_id ON conversation_messages(session_id,id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_conversation_messages_session_time ON conversation_messages(session_id,created_at,id)")
        await db.execute("PRAGMA user_version=3")
        await db.commit()


    @staticmethod
    def _protocol_payload(message: BaseMessage) -> dict[str, Any]:
        """编码完整业务历史；已落盘图片在历史中使用本地引用。

        Args:
            message (BaseMessage): 待读取或转换的业务消息。
        """
        payload = message.to_storage_format()
        for part in payload.get("content", []) if isinstance(payload.get("content"), list) else []:
            if part.get("type") == "image_url" and part.get("storage_ref"):
                part["image_url"]["url"] = part["storage_ref"]
        return payload

    @classmethod
    def _protocol_rows(cls, state: AgentState) -> list[tuple[Any, ...]]:
        """为原子事务准备业务历史行，系统提示词和摘要不归档。

        Args:
            state (AgentState): 需要保存、恢复或投影的会话业务状态。
        """
        return [(state.session_id, message.message_id, message.created_at.isoformat(),
                 "protocol", "exact", None, message.message_type,
                 json.dumps(cls._protocol_payload(message), ensure_ascii=False, separators=(",", ":")))
                for message in state.messages if message.enters_protocol_log]

    @staticmethod
    async def _upgrade_image_refs(db: aiosqlite.Connection, rows: list[tuple[Any, ...]]) -> None:
        """仅补历史原始图片的文件引用，不覆盖后续压缩文字或图片状态。

        Args:
            db (aiosqlite.Connection): 调用方已经开启事务的 SQLite 连接。
            rows (list[tuple[Any, ...]]): 待归档的完整业务历史行。
        """
        for row in rows:
            incoming = json.loads(row[7])
            image_parts = [part for part in incoming.get("content", []) if isinstance(part, dict)
                           and part.get("type") == "image_url" and part.get("storage_ref")
                           and part.get("context_state") == ImageContextState.AVAILABLE] if isinstance(incoming.get("content"), list) else []
            if not image_parts:
                continue
            found = await db.execute_fetchall("SELECT message_json FROM conversation_messages WHERE session_id=? AND message_id=?", (row[0], row[1]))
            if not found:
                continue
            original = json.loads(found[0][0])
            original_images = [part for part in original.get("content", []) if part.get("type") == "image_url"]
            if len(original_images) != len(image_parts):
                continue
            changed = False
            # 1. 只修改未外置的图片地址与引用，原文字、来源和压缩状态保持初次归档值。
            for old, new in zip(original_images, image_parts, strict=True):
                if not old.get("storage_ref") and old.get("context_state") == ImageContextState.AVAILABLE:
                    old["storage_ref"] = new["storage_ref"]
                    old["image_url"]["url"] = new["storage_ref"]
                    changed = True
            if changed:
                await db.execute("UPDATE conversation_messages SET message_json=? WHERE session_id=? AND message_id=?",
                    (json.dumps(original, ensure_ascii=False, separators=(",", ":")), row[0], row[1]))

    async def load(self) -> AgentState | None:
        """读取本会话编号最大的 checkpoint 并校验恢复材料，没有历史时返回空值。

        Returns:
            AgentState | None: 恢复后的状态或空值，同时记录载入的 checkpoint 类型。

        Raises:
            ValueError: checkpoint 版本或消息协议与当前代码不兼容。
        """
        db = await self._get_db()
        async with db.execute(LATEST_CHECKPOINT_SQL, (self.session_id,)) as cursor:
            row = await cursor.fetchone()
        if row:
            checkpoint_id, state_json, checkpoint_type = row
            self.last_loaded_checkpoint_type = CheckpointType(checkpoint_type)
            state = AgentState.from_checkpoint(json.loads(state_json))
            logger.info("从 checkpoint %s 恢复状态: %s", checkpoint_id, self.session_id)
            return state
        logger.info("尚无 checkpoint: %s", self.session_id)
        self.last_loaded_checkpoint_type = None
        return None

    async def save(
        self,
        state: AgentState,
        *,
        checkpoint_type: CheckpointType,
        inbox_inputs: tuple[InboxEnvelope, ...] = (),
        rewritten_message_id: str | None = None,
        release_queued: bool = False,
    ) -> int:
        """在同一事务中提交 checkpoint、协议日志、Inbox 交付和排队放行。

        调用方决定本次是否放行排队输入。本方法先保存状态和协议历史，再调用
        Inbox 的事务函数；这些函数不提交，任一步失败或取消都由本方法统一回滚。

        Args:
            state (AgentState): 当前 Agent 状态。
            checkpoint_type (CheckpointType): 中间态或完成态检查点类型。
            inbox_inputs (tuple[InboxEnvelope, ...]): 随本次 checkpoint 原子交付的输入快照集合。
            rewritten_message_id (str | None): 需要同步更新协议行的既有 Assistant 消息标识。
            release_queued (bool): 是否随正常完成的 checkpoint 原子放行排队输入。

        Returns:
            int: 本次新写入的 checkpoint 数据库编号，其他写入已经同事务提交。

        Raises:
            RuntimeError: 数据库未返回 checkpoint ID，或 Inbox 输入已失效。
            ValueError: 重写目标不是当前 assistant 消息，或输入属于其他会话。
        """
        db = await self._get_db()
        # 1. 媒体由上层准备；此处编码 checkpoint 和协议历史，再进入原子事务。
        state_json = json.dumps(state.to_checkpoint(), ensure_ascii=False)
        protocol_rows = self._protocol_rows(state)
        try:
            # 2. checkpoint 与协议历史在同一写事务提交，保留原有中间态清理及 completed 保留规则。
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                INSERT INTO checkpoints (
                    session_id, state_json, checkpoint_type, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (self.session_id, state_json, checkpoint_type, utc_now().isoformat()),
            )
            checkpoint_id = cursor.lastrowid
            if checkpoint_id is None:
                raise RuntimeError("数据库未返回 checkpoint ID")
            if checkpoint_type == CheckpointType.INTERMEDIATE:
                await db.execute(
                    """
                    DELETE FROM checkpoints
                    WHERE session_id = ? AND checkpoint_type = 'intermediate' AND id <> ?
                    """,
                    (self.session_id, checkpoint_id),
                )
            else:
                await db.execute(
                    "DELETE FROM checkpoints WHERE session_id = ? AND checkpoint_type = 'intermediate'",
                    (self.session_id,),
                )
                await self._prune_completed(db)
            if protocol_rows:
                await db.executemany("""INSERT INTO conversation_messages (
                    session_id,message_id,created_at,record_kind,fidelity,legacy_source_id,message_type,message_json)
                    VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(session_id,message_id) DO NOTHING""", protocol_rows)
                await self._upgrade_image_refs(db, protocol_rows)
            # 3. 打断后缀重写已有 Assistant 行，保留 message_id，避免历史出现重复气泡。
            if rewritten_message_id is not None:
                rewritten = next(
                    (row for row in protocol_rows if row[1] == rewritten_message_id), None,
                )
                if rewritten is None or rewritten[6] != MessageType.ASSISTANT:
                    raise ValueError("仅允许重写当前状态中的 assistant 消息")
                result = await db.execute(
                    "UPDATE conversation_messages SET message_json = ? "
                    "WHERE session_id = ? AND message_id = ?",
                    (rewritten[7], self.session_id, rewritten_message_id),
                )
                if result.rowcount != 1:
                    raise RuntimeError("中断文本对应的协议消息不存在")
            # 4. Inbox 用当前连接校验并更新交付记录，不单独提交，失败时回滚前面的状态写入。
            await mark_delivered_in_transaction(db, self.session_id, inbox_inputs)
            # 5. 调用方请求放行时，复用 Inbox 规则；完成状态和排队资格必须一起提交。
            if release_queued:
                await release_queued_in_transaction(db, self.session_id)
            await db.commit()
        # 6. 输入失效、数据库失败或取消时回滚 checkpoint、协议行和交付标记。
        except BaseException:
            await db.rollback()
            raise
        logger.info(
            "保存%s checkpoint %s: %s",
            "中间态" if checkpoint_type == CheckpointType.INTERMEDIATE else "完成态",
            checkpoint_id,
            self.session_id,
        )
        return int(checkpoint_id)

    async def _prune_completed(self, db: aiosqlite.Connection) -> None:
        """在当前写事务中只保留本会话最近三十份完成 checkpoint，不删除对话协议日志。

        Args:
            db (aiosqlite.Connection): 当前写事务使用的 SQLite 连接，由调用方统一提交。
        """
        await db.execute(
            """
            DELETE FROM checkpoints
            WHERE session_id = ?
              AND checkpoint_type = 'completed'
              AND id NOT IN (
                SELECT id FROM checkpoints
                WHERE session_id = ? AND checkpoint_type = 'completed'
                ORDER BY id DESC LIMIT ?
              )
            """,
            (
                self.session_id,
                self.session_id,
                self.MAX_COMPLETED_CHECKPOINTS_PER_SESSION,
            ),
        )

    async def clear_session(self) -> int:
        """删除本会话的 checkpoint 并提交，对话协议日志和其他会话记录保留。

        Returns:
            int: 本次删除的 checkpoint 数量。
        """
        db = await self._get_db()
        async with db.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?",
            (self.session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            deleted_count = row[0] if row else 0
        await db.execute("DELETE FROM checkpoints WHERE session_id = ?", (self.session_id,))
        await db.commit()
        logger.info("清空 session %s: 删除了 %s 个 checkpoint", self.session_id, deleted_count)
        return int(deleted_count)

    async def close(self) -> None:
        """释放本对象持有的数据库连接；重复调用不会再次关闭已释放连接。"""
        if self._db:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> "AgentStateDao":
        """异步上下文开始时连接数据库并检查共享表。

        Returns:
            AgentStateDao: 当前 AgentStateDao。
        """
        await self._get_db()
        return self

    async def __aexit__(self, *args) -> None:
        """异步上下文结束时关闭数据库连接，不吞掉调用方异常。

        Args:
            args (Any): 当前工具调用或被包装函数的参数。
        """
        await self.close()
