"""原子 checkpoint 与追加式协议日志持久化。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import sqlite3
import aiosqlite
from app.agent.utils.infra.bounded_operation import close_resources
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from app.crud.sqlite_factory import SqliteFactory, SQLITE_SAVE_TIMEOUT_SECONDS
from app.crud.sqlite_save import run_sqlite_save, sqlite_transaction, SqliteRollbackError

from app.schemas.runtime_types import ApprovalDecisionKind, CheckpointType, InboxKind, InterruptKind
from app.schemas.agent_storage import AgentStorageScope
from app.crud.agent_storage_sql import agent_state_schema
from app.schemas.message_types import MessageType
from app.agent.state.messages import BaseMessage
from app.agent.state.model import AgentState, CHECKPOINT_SCHEMA_VERSION
from app.agent.state.time import utc_now
from app.agent.runtime.session_inbox import InboxEnvelope
from app.crud.session_inbox_dao import (
    ensure_inbox_schema,
    mark_delivered_in_transaction,
    release_queued_in_transaction,
)
from app.crud.session_inbox_dao import append_input_in_transaction
from app.crud.subagent_sql import require_subagent_in_transaction, interaction_exists_in_transaction, append_interaction_in_transaction
from app.schemas.subagent_types import SubagentInteractionKind


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
        sqlite_factory (SqliteFactory): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
        _db (aiosqlite.Connection | None): 参与当前原子持久化操作的 SQLite 连接。
        last_loaded_checkpoint_type (CheckpointType | None): 最近载入的 checkpoint 类型，用于恢复判断。
    """

    MAX_COMPLETED_CHECKPOINTS_PER_SESSION = 30

    async def latest_checkpoints(self) -> list[tuple[str, dict[str, Any], CheckpointType]]:
        """读取各会话最新的持久快照，供上层判断是否需要恢复收尾。

        Returns:
            list[tuple[str, dict[str, Any], CheckpointType]]: 会话编号、已解码快照及类型。
        """
        async with self._write_lock:
            db = await self._get_db()
            rows = list(await db.execute_fetchall(
                f"SELECT session_id,state_json,checkpoint_type FROM {self.storage_scope.checkpoints_table} AS current "
                f"WHERE id=(SELECT MAX(id) FROM {self.storage_scope.checkpoints_table} WHERE session_id=current.session_id)"
            ))
            return [(str(session), json.loads(payload), CheckpointType(kind)) for session, payload, kind in rows]

    def __init__(self, session_id: str, sqlite_factory: SqliteFactory, *,
                 storage_scope: AgentStorageScope = AgentStorageScope.MAIN):
        """绑定会话和数据库路径；首次读写时才创建连接及共享表。

        Args:
            session_id (str): 当前操作所属的会话编号。
            sqlite_factory (SqliteFactory): 当前环境使用的 SQLite 数据库路径。
            storage_scope (AgentStorageScope): 本上下文使用的父表或子表范围。
        """
        self.session_id = session_id
        self.storage_scope = AgentStorageScope(storage_scope)
        self._sqlite_factory = sqlite_factory
        self.db_path = str(sqlite_factory.main_path)
        self._db: aiosqlite.Connection | None = None
        self.last_loaded_checkpoint_type: CheckpointType | None = None
        self._write_lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()

    async def _get_db(self) -> aiosqlite.Connection:
        """首次使用时连接当前环境数据库并初始化共享表，之后复用本对象的连接。

        Returns:
            aiosqlite.Connection: 已完成表结构检查的 SQLite 连接。
        """
        async with self._db_lock:
            if self._db is None:
                os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
                self._db = await self._sqlite_factory.connect()
                try:
                    await self._init_tables()
                except BaseException:
                    await close_resources(((self.db_path, self._db.close),), logger=logger)
                    self._db = None
                    raise
        return self._db


    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """串行本对象的业务事务，回滚失败后清除已经停用的连接。

        Yields:
            aiosqlite.Connection: 当前安全尝试使用的连接。
        """
        async with self._write_lock:
            db = await self._get_db()
            try:
                async with sqlite_transaction(db):
                    yield db
            except SqliteRollbackError:
                self._db = None
                raise

    async def _init_tables(self) -> None:
        """初始化 v4 业务历史和 checkpoint；旧库在写入前进入离线迁移出口。"""
        db = self._db
        assert db is not None
        async with sqlite_transaction(db):
            version = list(await db.execute_fetchall("PRAGMA user_version"))[0][0]
            existing = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('checkpoints','subagent_checkpoints','conversation_messages','subagent_conversation_messages')")
            if version != CHECKPOINT_SCHEMA_VERSION and (version != 0 or existing):
                raise RuntimeError("数据库需要先执行 scripts/migrate_image_storage_v4.py 离线迁移到 v4")
            # 1. 在任何初始化写入之前识别旧格式，禁止运行时自动迁移生产记录。
            columns = {str(row[1]) for row in list(await db.execute_fetchall(f"PRAGMA table_info({self.storage_scope.messages_table})"))}
            if columns and ("role" in columns or "domain_json" in columns):
                raise RuntimeError("conversation_messages 需要先执行消息协议 v3 离线迁移")
            for scope in AgentStorageScope:
                await ensure_inbox_schema(db, storage_scope=scope)
            for statement in agent_state_schema(self.storage_scope):
                await db.execute(statement)
            await db.execute(f"PRAGMA user_version={CHECKPOINT_SCHEMA_VERSION}")



    @classmethod
    def _protocol_rows(cls, state: AgentState) -> list[tuple[Any, ...]]:
        """为原子事务准备业务历史行，系统提示词和摘要不归档。

        Args:
            state (AgentState): 需要保存、恢复或投影的会话业务状态。
        """
        return [(state.session_id, message.message_id, message.created_at.isoformat(),
                 "protocol", "exact", None, message.message_type,
                 json.dumps(message.to_storage_format(), ensure_ascii=False, separators=(",", ":")))
                for message in state.messages if message.enters_protocol_log]

    async def load(self) -> AgentState | None:
        """读取本会话编号最大的 checkpoint 并校验恢复材料，没有历史时返回空值。

        Returns:
            AgentState | None: 恢复后的状态或空值，同时记录载入的 checkpoint 类型。

        Raises:
            ValueError: checkpoint 版本或消息协议与当前代码不兼容。
        """
        async with self._write_lock:
            db = await self._get_db()
            # 1. 查询在线程内一次完成，执行取消时也会取完结果并释放 SQLite 的读锁。
            rows = list(await db.execute_fetchall(
                LATEST_CHECKPOINT_SQL.replace("checkpoints", self.storage_scope.checkpoints_table), (self.session_id,)))
            row = rows[0] if rows else None
            if row:
                checkpoint_id, state_json, checkpoint_type = row
                self.last_loaded_checkpoint_type = CheckpointType(checkpoint_type)
                state = AgentState.from_checkpoint(json.loads(state_json))
                logger.info("从 checkpoint %s 恢复状态: %s", checkpoint_id, self.session_id)
                return state
            logger.info("尚无 checkpoint: %s", self.session_id)
            self.last_loaded_checkpoint_type = None
            return None

    async def committed_history_position(self) -> str | None:
        """故障恢复时读取协议历史的实际提交位置，读取错误交给执行所有者处理。

        Returns:
            str | None: 最后一条已保存消息编号，尚无历史时为空。
        """
        async with self._write_lock:
            db = await self._get_db()
            rows = list(await db.execute_fetchall(
                f"SELECT message_id FROM {self.storage_scope.messages_table} WHERE session_id=? ORDER BY id DESC LIMIT 1",
                (self.session_id,)))
            return str(rows[0][0]) if rows else None

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
        state = state.model_copy(deep=True)
        created_at = utc_now().isoformat()
        async def save_attempt() -> int:
            """提交本次已准备的业务材料，失败后由共享规则决定安全重试。"""
            async with self._transaction() as db:
                checkpoint_id = await self._write_state_in_transaction(
                    db, state, checkpoint_type=checkpoint_type, created_at=created_at, inbox_inputs=inbox_inputs,
                    rewritten_message_id=rewritten_message_id, release_queued=release_queued)
                return checkpoint_id

        return await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    async def _write_state_in_transaction(
        self, db: aiosqlite.Connection, state: AgentState, *, checkpoint_type: CheckpointType, created_at: str,
        inbox_inputs: tuple[InboxEnvelope, ...] = (), rewritten_message_id: str | None = None,
        release_queued: bool = False,
    ) -> int:
        """在调用方事务内写入检查点、协议和交付标记，提交归具名入口负责。

        Args:
            db (aiosqlite.Connection): 当前保存尝试独占、等待统一提交或回滚的连接。
            state (AgentState): 本次准备保存的完整会话状态。
            checkpoint_type (CheckpointType): 标记输入进展或回答已经结束的检查点类别。
            created_at (str): 保存尝试前固定的检查点时间。
            inbox_inputs (tuple[InboxEnvelope, ...]): 与本次状态一起确认交付的输入快照。
            rewritten_message_id (str | None): 本次改写历史的起始消息编号。
            release_queued (bool): 是否随回答提交放行排队消息。
        """
        state_json = json.dumps(state.to_checkpoint(), ensure_ascii=False)
        protocol_rows = self._protocol_rows(state)
        cursor = await db.execute(
            f"""
            INSERT INTO {self.storage_scope.checkpoints_table} (
                session_id, state_json, checkpoint_type, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (self.session_id, state_json, checkpoint_type, created_at),
        )
        checkpoint_id = cursor.lastrowid
        if checkpoint_id is None:
            raise RuntimeError("数据库未返回 checkpoint ID")
        if checkpoint_type == CheckpointType.INTERMEDIATE:
            await db.execute(
                f"""
                DELETE FROM {self.storage_scope.checkpoints_table}
                WHERE session_id = ? AND checkpoint_type = 'intermediate' AND id <> ?
                """,
                (self.session_id, checkpoint_id),
            )
        else:
            await db.execute(
                f"DELETE FROM {self.storage_scope.checkpoints_table} WHERE session_id = ? AND checkpoint_type = 'intermediate'",
                (self.session_id,),
            )
            await self._prune_completed(db)
        if protocol_rows:
            await db.executemany(f"""INSERT INTO {self.storage_scope.messages_table} (
                session_id,message_id,created_at,record_kind,fidelity,legacy_source_id,message_type,message_json)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(session_id,message_id) DO NOTHING""", protocol_rows)
        # 3. 打断后缀重写已有 Assistant 行，保留 message_id，避免历史出现重复气泡。
        if rewritten_message_id is not None:
            rewritten = next(
                (row for row in protocol_rows if row[1] == rewritten_message_id), None,
            )
            if rewritten is None or rewritten[6] != MessageType.ASSISTANT:
                raise ValueError("仅允许重写当前状态中的 assistant 消息")
            result = await db.execute(
                f"UPDATE {self.storage_scope.messages_table} SET message_json = ? "
                "WHERE session_id = ? AND message_id = ?",
                (rewritten[7], self.session_id, rewritten_message_id),
            )
            if result.rowcount != 1:
                raise RuntimeError("中断文本对应的协议消息不存在")
        # 4. Inbox 用当前连接校验并更新交付记录，不单独提交，失败时回滚前面的状态写入。
        await mark_delivered_in_transaction(db, self.session_id, inbox_inputs, storage_scope=self.storage_scope)
        # 5. 调用方请求放行时，复用 Inbox 规则；完成状态和排队资格必须一起提交。
        if release_queued:
            await release_queued_in_transaction(db, self.session_id, storage_scope=self.storage_scope)
        return int(checkpoint_id)

    async def commit_parent_interrupt(self, state: AgentState) -> bool:
        """将子暂停、父问题及显式交互一起提交，同来源重试不重复投递。

        Args:
            state (AgentState): 已准备完整父消息等待的子状态。

        Returns:
            bool: 该问题仍是当前已保存等待；已被新指令结束时为 False。
        """
        state = state.model_copy(deep=True)
        created_at = utc_now().isoformat()
        async def save_attempt() -> bool:
            """提交本次已准备的业务材料，失败后由共享规则决定安全重试。"""
            if self.storage_scope != AgentStorageScope.SUBAGENT:
                raise ValueError("只有子 Agent 可以提交父消息等待")
            interrupt = state.interrupt_data or {}
            if interrupt.get("type") != InterruptKind.PARENT_MESSAGE:
                raise ValueError("缺少父消息中断材料")
            data = interrupt["data"]
            source_id = str(interrupt["request_id"])
            async with self._transaction() as db:
                # 1. 先检查整项投递，已结束的旧等待不能因事务重试复活。
                await require_subagent_in_transaction(db, str(data["parent_session_id"]), self.session_id)
                rows = list(await db.execute_fetchall("SELECT name FROM subagents WHERE subagent_id=?", (self.session_id,)))
                subagent_name = str(rows[0][0])
                if await interaction_exists_in_transaction(db, self.session_id, source_id, (SubagentInteractionKind.A2A,)):
                    rows = list(await db.execute_fetchall(
                        f"SELECT state_json FROM {self.storage_scope.checkpoints_table} WHERE session_id=? ORDER BY id DESC LIMIT 1",
                        (self.session_id,)))
                    return bool(rows and (json.loads(rows[0][0]).get("interrupt_data") or {}).get("request_id") == source_id)
                # 2. 状态、父 Inbox 和交互任一失败时整笔回滚。
                await self._write_state_in_transaction(db, state, checkpoint_type=CheckpointType.INTERMEDIATE, created_at=created_at)
                await append_input_in_transaction(db, str(data["parent_session_id"]), InboxKind.SUBAGENT,
                    {"text": str(data["text"]), "subagent_id": self.session_id,
                     "subagent_name": subagent_name, "request_id": source_id}, AgentStorageScope.MAIN)
                await append_interaction_in_transaction(db, self.session_id, SubagentInteractionKind.A2A,
                                                        str(data["text"]), source_id)
                return True

        return await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    async def commit_interrupt_decision(
        self, state: AgentState, *, origin_channel: str, parent_session_id: str | None = None,
    ) -> bool:
        """在写事务内确认首个有效决定，并与审批引导输入一起保存。

        Args:
            state (AgentState): 已准备决定、完整工具恢复材料的候选状态。
            origin_channel (str): 作出决定的入口渠道。
            parent_session_id (str | None): 子审批引导的父归属。

        Returns:
            bool: 本次首次决定已经保存；请求过期或已有决定时为 False。
        """
        state = state.model_copy(deep=True)
        created_at = utc_now().isoformat()
        interrupt = state.interrupt_data or {}
        decision = interrupt.get("runtime_decision") or {}

        async def save_attempt() -> bool:
            """提交本次已准备的业务材料，失败后由共享规则决定安全重试。"""
            async with self._transaction() as db:
                rows = list(await db.execute_fetchall(
                    f"SELECT state_json FROM {self.storage_scope.checkpoints_table} WHERE session_id=? ORDER BY id DESC LIMIT 1",
                    (self.session_id,)))
                current = (json.loads(rows[0][0]).get("interrupt_data") or {}) if rows else {}
                if (not interrupt.get("request_id") or current.get("request_id") != interrupt["request_id"]
                        or current.get("type") != interrupt.get("type") or current.get("runtime_decision")):
                    return False
                kind = InboxKind.AGENT_COMMAND if parent_session_id else InboxKind.HUMAN
                if decision.get("kind") == ApprovalDecisionKind.STEER:
                    payload: dict[str, object] = ({"text": decision["message"], "parent_session_id": parent_session_id} if parent_session_id
                                             else {"message": decision["message"], "images": []})
                    await append_input_in_transaction(db, self.session_id, kind, payload, self.storage_scope,
                                                      origin_channel=origin_channel)
                await self._write_state_in_transaction(db, state, checkpoint_type=CheckpointType.INTERMEDIATE, created_at=created_at)
                return True

        return await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    async def _prune_completed(self, db: aiosqlite.Connection) -> None:
        """在当前写事务中只保留本会话最近三十份完成 checkpoint，不删除对话协议日志。

        Args:
            db (aiosqlite.Connection): 当前写事务使用的 SQLite 连接，由调用方统一提交。
        """
        await db.execute(
            f"""
            DELETE FROM {self.storage_scope.checkpoints_table}
            WHERE session_id = ?
              AND checkpoint_type = 'completed'
              AND id NOT IN (
                SELECT id FROM {self.storage_scope.checkpoints_table}
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
        async def save_attempt() -> int:
            """在同一次事务中统计和删除本会话检查点，返回确认删除的数量。"""
            async with self._transaction() as db:
                async with db.execute(
                    f"SELECT COUNT(*) FROM {self.storage_scope.checkpoints_table} WHERE session_id = ?",
                    (self.session_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                await db.execute(f"DELETE FROM {self.storage_scope.checkpoints_table} WHERE session_id = ?", (self.session_id,))
                return int(row[0]) if row else 0

        deleted_count = await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)
        logger.info("清空 session %s: 删除了 %s 个 checkpoint", self.session_id, deleted_count)
        return int(deleted_count)

    async def close(self) -> None:
        """在有限预算内释放本对象连接，已开始的保存确认由写锁排序完成。"""
        async with self._write_lock:
            db, self._db = self._db, None
            if db is not None:
                await close_resources(((self.db_path, db.close),), logger=logger, retryable=(sqlite3.Error, OSError))

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
