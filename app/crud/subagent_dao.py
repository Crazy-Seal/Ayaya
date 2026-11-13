"""持久保存子 Agent 定义、父子显式交互及对应 Inbox 输入。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

import logging
import sqlite3
import aiosqlite
from app.agent.utils.infra.bounded_operation import close_resources
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from app.crud.sqlite_factory import SqliteFactory, SQLITE_SAVE_TIMEOUT_SECONDS
from app.crud.sqlite_save import run_sqlite_save, sqlite_transaction, SqliteRollbackError

from app.crud.session_inbox_dao import ensure_inbox_schema, discard_pending_in_transaction, append_input_in_transaction
from app.crud.subagent_sql import append_interaction_in_transaction, ensure_subagent_schema, require_subagent_in_transaction, read_interaction_kind_in_transaction
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.runtime_types import InboxKind, InboxStatus
from app.schemas.subagent_types import SubagentDefinition, SubagentRecord, SubagentInteraction, SubagentInteractionKind
from app.schemas.tool_identity import ToolIdentity


logger = logging.getLogger(__name__)

class SubagentDao:
    """拥有子定义、命令受理及显式交互的 SQL 事务。

    Attributes:
        db_path (str): 与检查点和任务结果共用的数据库。
        _lock (asyncio.Lock): 串行该连接上的身份、命令和废弃事务。
    """

    def __init__(self, sqlite_factory: SqliteFactory) -> None:
        """准备当前环境存储，连接在首次读取时建立。

        Args:
            sqlite_factory (SqliteFactory): 应用装配提供的 SQLite 连接配置。
        """
        self._sqlite_factory = sqlite_factory
        self.db_path = str(sqlite_factory.main_path)
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def _get_db(self) -> aiosqlite.Connection:
        """初始化子元数据及父子收件表，已有表结构由离线迁移校验。"""
        if self._db is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            db = await self._sqlite_factory.connect()
            try:
                async with sqlite_transaction(db):
                    await ensure_subagent_schema(db)
                    for scope in AgentStorageScope:
                        await ensure_inbox_schema(db, storage_scope=scope)
            except BaseException:
                await close_resources(((self.db_path, db.close),), logger=logger)
                raise
            self._db = db
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

    async def create_with_command(self, definition: SubagentDefinition, text: str, source_id: str) -> SubagentDefinition:
        """同事务登记子身份、初始命令及交互，成功后才允许启动执行。

        Args:
            definition (SubagentDefinition): 稳定子身份及创建配置。
            text (str): 要交付的初始任务正文。
            source_id (str): 发起创建的父工具调用身份。

        Raises:
            Exception: 任一写入失败，定义、Inbox 和交互一起回滚。
        """
        async def save_attempt() -> SubagentDefinition:
            """提交本次已准备的业务材料，失败后由共享规则决定安全重试。"""
            async with self._transaction() as db:
                # 1. 在同一写事务内保存子定义、初始指令及交互。
                existing = list(await db.execute_fetchall(
                    "SELECT s.subagent_id,s.session_id,s.name,s.system_prompt,s.tool_whitelist_json,"
                    "s.if_retain_context,s.created_at,s.retired_at FROM subagents s "
                    "JOIN subagent_interactions i ON i.subagent_id=s.subagent_id "
                    "WHERE s.session_id=? AND i.kind=? AND i.source_id=? ORDER BY i.interaction_id LIMIT 1",
                    (definition.session_id, SubagentInteractionKind.TASK.value, source_id)))
                if existing:
                    return self._record(existing[0]).definition
                await db.execute("INSERT INTO subagents(subagent_id,session_id,name,system_prompt,tool_whitelist_json,"
                                 "if_retain_context,created_at) VALUES (?,?,?,?,?,?,?)",
                                 (definition.subagent_id, definition.session_id, definition.name, definition.system_prompt,
                                  json.dumps([asdict(tool) for tool in sorted(definition.tool_whitelist, key=lambda tool: tool.stable_key)]),
                                  definition.if_retain_context, definition.created_at.isoformat()))
                await append_input_in_transaction(db, definition.subagent_id, InboxKind.AGENT_COMMAND,
                                         {"text": text, "parent_session_id": definition.session_id}, AgentStorageScope.SUBAGENT)
                await append_interaction_in_transaction(db, definition.subagent_id, SubagentInteractionKind.TASK, text, source_id)
                # 2. 只有提交完成后 Scheduler 才能启动 worker 或返回成功受理回执。
                return definition

        return await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    async def append_message(self, definition: SubagentDefinition, kind: SubagentInteractionKind,
                             text: str, source_id: str, *, request_id: str | None = None,
                             discarded_ids: tuple[int, ...] = ()) -> tuple[SubagentInteractionKind, bool, int | None]:
        """保存父子显式交互，命令、引导和子问题同时写入接收方 Inbox。

        父回复与普通命令一样写入子 Inbox，随后通过引导交付。
        同来源在任务、引导及回复类别之间统一去重。

        Args:
            definition (SubagentDefinition): 已确认归属的子定义。
            kind (SubagentInteractionKind): 新委派、引导、父回复或子问题。
            text (str): 交付正文。
            source_id (str): 实际发起工具或界面命令的编号。
            request_id (str | None): 子问题写入父 Inbox 时携带的原工具等待编号。

            discarded_ids (tuple[int, ...]): 前次交付失败且由本会话明确放弃的输入编号。

        Returns:
            tuple[SubagentInteractionKind, bool, int | None]: 原受理类别及是否首次提交，供运行层选择首次受理或观察回执。

        Raises:
            Exception: 输入或交互保存失败，整笔事务回滚。
        """
        async def save_attempt() -> tuple[SubagentInteractionKind, bool, int | None]:
            """提交本次已准备的业务材料，失败后由共享规则决定安全重试。"""
            async with self._transaction() as db:
                # 1. 子问题进父 Inbox；任务和引导进子 Inbox，分别与交互记录同事务提交。
                await require_subagent_in_transaction(db, definition.session_id, definition.subagent_id)
                if discarded_ids:
                    placeholders = ",".join("?" for _ in discarded_ids)
                    await db.execute(f"DELETE FROM {AgentStorageScope.SUBAGENT.inbox_table} WHERE session_id=? "
                        f"AND status=? AND kind=? AND id IN ({placeholders})",
                        (definition.subagent_id, InboxStatus.PENDING.value, InboxKind.AGENT_COMMAND.value, *discarded_ids))
                kinds = ((SubagentInteractionKind.A2A,) if kind == SubagentInteractionKind.A2A else
                         (SubagentInteractionKind.TASK, SubagentInteractionKind.STEER, SubagentInteractionKind.REPLY))
                previous = await read_interaction_kind_in_transaction(db, definition.subagent_id, source_id, kinds)
                if previous is not None:
                    return previous, False, None
                scope = AgentStorageScope.MAIN if kind == SubagentInteractionKind.A2A else AgentStorageScope.SUBAGENT
                context = definition.session_id if scope == AgentStorageScope.MAIN else definition.subagent_id
                inbox_kind = InboxKind.SUBAGENT if scope == AgentStorageScope.MAIN else InboxKind.AGENT_COMMAND
                payload: dict[str, object] = {"text": text, "parent_session_id": definition.session_id,
                                             "subagent_id": definition.subagent_id, "subagent_name": definition.name}
                if request_id is not None:
                    payload["request_id"] = request_id
                input_id = await append_input_in_transaction(db, context, inbox_kind, payload, scope)
                await append_interaction_in_transaction(db, definition.subagent_id, kind, text, source_id)
                return kind, True, input_id

        return await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    @staticmethod
    def _record(row: sqlite3.Row | tuple) -> SubagentRecord:
        """从固定列顺序还原子定义及工具身份。

        Args:
            row (sqlite3.Row | tuple): 按查询列顺序返回的持久子身份记录。
        """
        return SubagentRecord(SubagentDefinition(str(row[0]), str(row[1]), str(row[2]), str(row[3]),
                              frozenset(ToolIdentity(**tool) for tool in json.loads(row[4])), bool(row[5]),
                              datetime.fromisoformat(row[6])), datetime.fromisoformat(row[7]) if row[7] else None)

    async def get(self, session_id: str, subagent_id: str) -> SubagentRecord | None:
        """按父子归属读取定义，已废弃身份仍可提供详情和报告。

        Args:
            session_id (str): 本次查询或操作所属的父会话编号。
            subagent_id (str): 实际子上下文编号。
        """
        async with self._write_lock:
            db = await self._get_db()
            rows = list(await db.execute_fetchall("SELECT subagent_id,session_id,name,system_prompt,tool_whitelist_json,"
                                            "if_retain_context,created_at,retired_at FROM subagents "
                                            "WHERE session_id=? AND subagent_id=?", (session_id, subagent_id)))
        return self._record(rows[0]) if rows else None

    async def list_records(self, session_id: str | None = None) -> tuple[SubagentRecord, ...]:
        """读取包括废弃身份的定义，供启动整理保留历史。

        Args:
            session_id (str | None): 本次查询或操作所属的父会话编号。
        """
        async with self._write_lock:
            db = await self._get_db()
            rows = list(await db.execute_fetchall("SELECT subagent_id,session_id,name,system_prompt,tool_whitelist_json,"
                                            "if_retain_context,created_at,retired_at FROM subagents "
                                            "WHERE (? IS NULL OR session_id=?) ORDER BY created_at",
                                            (session_id, session_id)))
        return tuple(self._record(row) for row in rows)

    async def list_available(self, session_id: str | None = None) -> tuple[SubagentRecord, ...]:
        """读取当前未废弃的子身份，父配置资格由调用方检查。"""
        return tuple(record for record in await self.list_records(session_id) if record.retired_at is None)

    async def retire(self, subagent_id: str) -> None:
        """受理销毁时废弃子身份，保留历史并删除所有未交付输入。

        Args:
            subagent_id (str): 需要立即停止受派的子身份。

        Raises:
            Exception: 废弃标记或输入删除失败，整笔事务回滚。
        """
        prepared_at = datetime.now(timezone.utc).isoformat()
        async def save_attempt() -> None:
            """提交本次已准备的业务材料，失败后由共享规则决定安全重试。"""
            async with self._transaction() as db:
                # 1. 首次废弃时记录时间，重复收尾继续使用原废弃记录。
                await db.execute("UPDATE subagents SET retired_at=COALESCE(retired_at,?) WHERE subagent_id=?",
                                 (prepared_at, subagent_id))
                # 2. 和废弃标记一起清除已有未交付输入，迟到任务仍可保存通知。
                await discard_pending_in_transaction(db, subagent_id, storage_scope=AgentStorageScope.SUBAGENT)

        return await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    async def recent_interactions(self, subagent_id: str, limit: int = 10) -> tuple[SubagentInteraction, ...]:
        """按发生顺序返回父子之间最近的显式交互。

        Args:
            subagent_id (str): 实际子上下文编号。
            limit (int): 本次最多返回的显式交互条数。
        """
        async with self._write_lock:
            db = await self._get_db()
            rows = list(await db.execute_fetchall("SELECT interaction_id,subagent_id,kind,text,created_at,source_id "
                                            "FROM subagent_interactions WHERE subagent_id=? ORDER BY interaction_id DESC LIMIT ?",
                                            (subagent_id, limit)))
        return tuple(SubagentInteraction(int(row[0]), str(row[1]), SubagentInteractionKind(row[2]), str(row[3]),
                                        datetime.fromisoformat(row[4]), str(row[5])) for row in reversed(rows))

    async def close(self) -> None:
        """在有限预算内释放本对象连接，已开始的保存确认由写锁排序完成。"""
        async with self._write_lock:
            db, self._db = self._db, None
            if db is not None:
                await close_resources(((self.db_path, db.close),), logger=logger, retryable=(sqlite3.Error, OSError))
