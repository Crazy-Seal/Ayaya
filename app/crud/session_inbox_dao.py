"""按会话排序并持久保存待交付给 Agent 的输入。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import asyncio
from typing import Any

import logging
import sqlite3
import aiosqlite
from app.agent.utils.infra.bounded_operation import close_resources
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from app.crud.sqlite_factory import SqliteFactory, SQLITE_SAVE_TIMEOUT_SECONDS
from app.crud.sqlite_save import run_sqlite_save, sqlite_transaction, SqliteRollbackError

from app.agent.runtime.session_inbox import InboxEnvelope, InputNoLongerPending
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.runtime_types import InboxDeliveryMode, InboxKind, InboxStatus


INBOX_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('human', 'task_terminal', 'plugin_notification', 'agent_command', 'subagent', 'parent_reply')),
    payload_json TEXT NOT NULL,
    delivery_mode TEXT NOT NULL DEFAULT 'immediate'
        CHECK (delivery_mode IN ('immediate', 'queued')),
    queue_ready INTEGER NOT NULL DEFAULT 0 CHECK (queue_ready IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered')),
    created_at TEXT NOT NULL,
    delivered_at TEXT
)
"""


async def append_input_in_transaction(
    db: aiosqlite.Connection, context_id: str, kind: InboxKind,
    payload: dict[str, object], storage_scope: AgentStorageScope,
) -> int:
    """用调用方事务保存一条输入，返回编号，由调用方统一提交。

    Args:
        db (aiosqlite.Connection): 已开始事务的连接。
        context_id (str): 接收方上下文。
        kind (InboxKind): 真人、父命令或内部通知类别。
        payload (dict[str, object]): 正文及来源。
        storage_scope (AgentStorageScope): 接收方存储范围。

    Returns:
        int: 已写入、尚待提交的输入编号。
    """
    cursor = await db.execute(
        f"INSERT INTO {storage_scope.inbox_table}(session_id,kind,payload_json,created_at) VALUES (?,?,?,?)",
        (context_id, kind.value, json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("数据库未返回新输入编号")
    return int(cursor.lastrowid)


async def ensure_inbox_schema(db: aiosqlite.Connection, *,
                              storage_scope: AgentStorageScope = AgentStorageScope.MAIN) -> None:
    """创建两阶段 Inbox；已有 claimed 表必须先执行离线迁移。

    Args:
        db (aiosqlite.Connection): 当前存储操作的数据库连接。

        storage_scope (AgentStorageScope): 本上下文使用的父表或子表范围。

    Raises:
        RuntimeError: 现有 Inbox 仍包含已删除的 claimed 结构。
    """
    rows = list(await db.execute_fetchall(f"SELECT sql FROM sqlite_master WHERE type = 'table' AND name = '{storage_scope.inbox_table}'"))
    if rows and "claimed" in rows[0][0]:
        raise RuntimeError("session_inbox 需要先执行 scripts/migrate_inbox_two_states.py 离线迁移")
    await db.execute(INBOX_SCHEMA_SQL.replace("session_inbox", storage_scope.inbox_table))




async def discard_pending_in_transaction(
    db: aiosqlite.Connection, session_id: str, *, storage_scope: AgentStorageScope,
) -> None:
    """随子身份废弃事务删除未交付输入，保留已经加入对话的记录。

    Args:
        db (aiosqlite.Connection): 调用方已开启事务的连接，本函数不自行提交。
        session_id (str): 要停止接收输入的上下文编号。
        storage_scope (AgentStorageScope): 与上下文对应的固定 Inbox 表范围。
    """
    await db.execute(f"DELETE FROM {storage_scope.inbox_table} WHERE session_id=? AND status=?",
                     (session_id, InboxStatus.PENDING.value))


async def mark_delivered_in_transaction(
    db: aiosqlite.Connection, session_id: str, inputs: tuple[InboxEnvelope, ...],
    *, storage_scope: AgentStorageScope = AgentStorageScope.MAIN,
) -> None:
    """随消息和 checkpoint 一起验证输入并标记交付，不自行提交。

    Args:
        db (aiosqlite.Connection): AgentStateDao 已开启事务的连接。
        session_id (str): 当前保存的会话。
        inputs (tuple[InboxEnvelope, ...]): 本次准备交付的输入。

    Raises:
        ValueError: 输入属于其他会话。
        InputNoLongerPending: 输入已删除或交付，整批保存必须回滚。
    """
    for item in inputs:
        if item.session_id != session_id or item.storage_scope != storage_scope:
            raise ValueError("Inbox 输入不属于当前会话")
        # 1. 验证与更新共用一条 SQL；取消先提交时，这里不会匹配到记录。
        result = await db.execute(
            f"UPDATE {storage_scope.inbox_table} SET status = ?, delivered_at = ? "
            "WHERE id = ? AND session_id = ? AND status = ?",
            (InboxStatus.DELIVERED.value, datetime.now(timezone.utc).isoformat(),
             item.id, session_id, InboxStatus.PENDING.value),
        )
        # 2. 任一输入失效，由外层撤销整批消息、checkpoint 和交付更新。
        if result.rowcount != 1:
            raise InputNoLongerPending("Inbox 输入已失效")


async def release_queued_in_transaction(db: aiosqlite.Connection, session_id: str, *,
                                       storage_scope: AgentStorageScope = AgentStorageScope.MAIN) -> None:
    """在调用方事务中放行当前会话尚未交付的排队真人输入。

    AgentStateDao.save 在保存正常完成状态时调用；SessionInboxDao.release_queued
    为需要独立放行的调用方复用同一规则。本函数不改变交付状态，不自行提交或
    回滚；是否允许放行由调用方决定，持久提交及失败处理也由调用方负责。

    Args:
        db (aiosqlite.Connection): 调用方提供的已初始化连接，沿用其事务。
        session_id (str): 本次请求放行的会话。
    """
    # 1. 仅修改 pending 排队真人输入的执行资格，重复放行不重复交付输入。
    await db.execute(
        f"UPDATE {storage_scope.inbox_table} SET queue_ready = 1 "
        "WHERE session_id = ? AND kind = ? AND delivery_mode = ? "
        "AND status = ?",
        (session_id, InboxKind.HUMAN.value, InboxDeliveryMode.QUEUED.value, InboxStatus.PENDING.value),
    )


async def append_task_terminal_in_transaction(
    db: aiosqlite.Connection, *, session_id: str, task_id: str, task_name: str,
    status: str, result: str, tool_name: str | None = None,
    exit_code: int | None = None, output_ref: str | None = None, error: str | None = None,
    storage_scope: AgentStorageScope = AgentStorageScope.MAIN,
) -> None:
    """在任务结束事务中，将任务事实编码为一条待交付的 Inbox 通知。

    TaskRecordDao.finish_once 仅在首次终态更新成功后调用；任务终态校验和
    去重由 DAO 完成，通知字段、来源和编码由 Inbox 模块负责。本函数只写入，
    不提交或回滚；由 DAO 将通知与任务终态一起提交，失败或取消时一起回滚。

    Args:
        db (aiosqlite.Connection): 调用方已开启任务结束事务的连接。
        session_id (str): 接收任务通知的会话。
        task_id (str): 已首次进入终态的任务标识。
        task_name (str): 用于通知内容的原始任务命令。
        status (str): 调用方已验证的任务终态协议值。
        result (str): 兼容旧通知的摘要字段，新任务为空。
        tool_name (str | None): 创建任务的实际工具名。
        exit_code (int | None): 真实退出码或未知。
        output_ref (str | None): 完整任务日志引用。
        error (str | None): 执行或清理失败的错误摘要。
    """
    # 1. 沿用现有通知载荷和默认 pending、immediate、未排队状态，等待 worker 读取。
    await db.execute(
        f"INSERT INTO {storage_scope.inbox_table} "
        "(session_id, kind, payload_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, InboxKind.TASK_TERMINAL.value, json.dumps({
            "task_id": task_id, "task_name": task_name, "status": status, "result": result,
            **({"error": error} if error else {}),
            **({"tool_name": tool_name, "exit_code": exit_code, "output_ref": output_ref}
               if output_ref is not None else {}),
        }, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
    )


logger = logging.getLogger(__name__)

class SessionInboxDao:
    """访问一个会话的待处理输入，包含真人消息和后台任务结束通知。

    各会话的数据保存在同一张 SQLite 表中，通过 session_id 区分。一个会话可以
    有多个本对象，由各自拥有它的 SessionStateManager 使用，读写同一份持久输入。
    输入保存为 pending；worker 只查询，Agent 保存消息和 checkpoint 后才成为 delivered。
    本模块集中定义输入的读取、交付和排队规则；跨表保存通过模块级事务函数复用
    这些规则，最终提交或回滚由 AgentStateDao 和 TaskRecordDao 的事务入口负责。

    Attributes:
        session_id (str): 共享 Ayaya 会话标识。
        sqlite_factory (SqliteFactory): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
        _db (aiosqlite.Connection | None): 参与当前原子持久化操作的 SQLite 连接。
    """

    def __init__(self, session_id: str, sqlite_factory: SqliteFactory, *,
                 storage_scope: AgentStorageScope = AgentStorageScope.MAIN):
        """绑定会话和测试可替换的数据库路径，延迟创建 SQLite 连接。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            sqlite_factory (SqliteFactory): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
            storage_scope (AgentStorageScope): 本上下文使用的父表或子表范围。
        """
        self.session_id = session_id
        self.storage_scope = AgentStorageScope(storage_scope)
        self._sqlite_factory = sqlite_factory
        self.db_path = str(sqlite_factory.main_path)
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()

    async def reset_after_restart(self) -> None:
        """启动时作废旧真人意图，普通通知继续保留。"""
        async def save_attempt() -> None:
            """在同一事务中删除上次进程尚未交付的真人输入。"""
            async with self._transaction() as db:
                await db.execute(f"DELETE FROM {self.storage_scope.inbox_table} WHERE kind=? AND status=?",
                                 (InboxKind.HUMAN.value, InboxStatus.PENDING.value))
        await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    async def _get_db(self) -> aiosqlite.Connection:
        """复用会话连接并幂等初始化 Inbox 表及排序索引。

        Returns:
            aiosqlite.Connection: 已经完成幂等 Inbox 初始化的实例连接。
        """
        async with self._db_lock:
            if self._db is None:
                db = await self._sqlite_factory.connect()
                try:
                    async with sqlite_transaction(db):
                        await ensure_inbox_schema(db, storage_scope=self.storage_scope)
                        await db.execute(
                            f"CREATE INDEX IF NOT EXISTS idx_{self.storage_scope.inbox_table}_order "
                            f"ON {self.storage_scope.inbox_table}(session_id, status, id)")
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

    def _from_row(self, row: sqlite3.Row | tuple[Any, ...]) -> InboxEnvelope:
        """将固定列顺序的数据库行还原为不可变输入快照。

        Args:
            row (tuple[Any, ...]): 固定查询列顺序的 SQLite 行。

        Returns:
            InboxEnvelope: 从查询行恢复的输入状态与排队方式。
        """
        return InboxEnvelope(
            id=int(row[0]),
            session_id=str(row[1]),
            kind=InboxKind(row[2]),
            payload=json.loads(row[3]),
            status=InboxStatus(row[4]),
            delivery_mode=InboxDeliveryMode(row[5]),
            storage_scope=self.storage_scope,
        )

    async def submit_plugin_notification(self, plugin_name: str, name: str, text: str) -> None:
        """持久保存插件内容；受理完成后仍需由 Agent 原子交付。

        Args:
            plugin_name (str): 发出内容或创建任务的插件稳定名称。
            name (str): 供查询或识别本次内容用途的稳定名称。
            text (str): 需要保存或显示的正文。
        """
        from app.agent.state.messages import PluginNotificationMessage
        PluginNotificationMessage(plugin_name=plugin_name, name=name, text=text)
        prepared_at = datetime.now(timezone.utc).isoformat()
        async def save_attempt() -> None:
            """提交本次已准备的业务材料，失败后由共享规则决定安全重试。"""
            async with self._transaction() as db:
                await db.execute(
                    f"INSERT INTO {self.storage_scope.inbox_table} (session_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
                    (self.session_id, InboxKind.PLUGIN_NOTIFICATION.value,
                     json.dumps({"plugin_name": plugin_name, "name": name, "text": text}, ensure_ascii=False),
                     prepared_at))

        await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    async def read_runtime_inputs(self) -> tuple[InboxEnvelope, ...]:
        """按输入编号读取任务和插件通知，留待统一的消息交付事务保存。"""
        return tuple(item for item in await self._pending(None, False) if item.kind != InboxKind.HUMAN)

    async def submit_human(
        self, message: str, *, images: list[str] | None = None,
        queued: bool = False, discarded_ids: tuple[int, ...] = (),
    ) -> InboxEnvelope:
        """校验真人内容并持久写入 pending 输入，返回值不代表已交付模型。

        Args:
            message (str): 真人文本或类型声明对应的内部通知。
            images (list[str] | None): 真人附件的图片 data URL 列表。
            queued (bool): 是否等前一真人轮次正常完成后才交付。

            discarded_ids (tuple[int, ...]): 前次交付失败且由本会话明确放弃的输入编号。

        Returns:
            InboxEnvelope: 已持久受理但尚未交付模型的输入快照。

        Raises:
            ValueError: 消息内容不能为空。
            RuntimeError: 数据库未返回 Inbox ID。
        """
        if not message.strip() and not images:
            raise ValueError("消息内容不能为空")
        payload = {"message": message, "images": list(images or [])}
        prepared_at = datetime.now(timezone.utc).isoformat()
        async def save_attempt() -> InboxEnvelope:
            """提交本次已准备的业务材料，失败后由共享规则决定安全重试。"""
            async with self._transaction() as db:
                if discarded_ids:
                    placeholders = ",".join("?" for _ in discarded_ids)
                    await db.execute(f"DELETE FROM {self.storage_scope.inbox_table} WHERE session_id=? "
                        f"AND status=? AND kind=? AND id IN ({placeholders})",
                        (self.session_id, InboxStatus.PENDING.value, InboxKind.HUMAN.value, *discarded_ids))
                # 2. 先写 pending 记录并提交，再返回 input_sequence；此时尚未交付模型。
                cursor = await db.execute(
                    f"INSERT INTO {self.storage_scope.inbox_table} "
                    "(session_id, kind, payload_json, delivery_mode, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (self.session_id, InboxKind.HUMAN.value, json.dumps(payload, ensure_ascii=False),
                     (InboxDeliveryMode.QUEUED if queued else InboxDeliveryMode.IMMEDIATE).value,
                     prepared_at),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("数据库未返回 Inbox ID")
                return InboxEnvelope(
                    int(cursor.lastrowid), self.session_id, InboxKind.HUMAN,
                    payload, InboxStatus.PENDING, InboxDeliveryMode.QUEUED if queued else InboxDeliveryMode.IMMEDIATE,
                    self.storage_scope,
                )

        return await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    async def _pending(self, kind: InboxKind | str | None, include_queued: bool) -> tuple[InboxEnvelope, ...]:
        """读取可交付输入，不改变数据库状态。

        Args:
            kind (InboxKind | str | None): 需要的类别，为空时查询全部。
            include_queued (bool): 是否包括已经放行的排队输入。

        Returns:
            tuple[InboxEnvelope, ...]: 按输入编号排序的 pending 记录。
        """
        async with self._write_lock:
            db = await self._get_db()
            kind_value = kind.value if isinstance(kind, InboxKind) else kind
            rows = list(await db.execute_fetchall(
                "SELECT id, session_id, kind, payload_json, status, delivery_mode "
                f"FROM {self.storage_scope.inbox_table} WHERE session_id = ? AND status = ? "
                "AND (? IS NULL OR kind = ?) "
                "AND (delivery_mode = ? OR (? AND queue_ready = 1)) ORDER BY id",
                (self.session_id, InboxStatus.PENDING.value, kind_value, kind_value,
                 InboxDeliveryMode.IMMEDIATE.value, include_queued),
            ))
            return tuple(self._from_row(row) for row in rows)

    async def read_next(self, *, kind: InboxKind | str | None = None, include_queued: bool = True) -> InboxEnvelope | None:
        """查询最早的可交付输入，读取后仍可撤销排队输入。

        Args:
            kind (InboxKind | str | None): 输入类别过滤。
            include_queued (bool): 是否包括已放行的排队输入。

        Returns:
            InboxEnvelope | None: 最早一条输入，没有时为空。
        """
        rows = await self._pending(kind, include_queued)
        return rows[0] if rows else None

    async def read_activation_batch(
        self, *, kind: InboxKind | str | None = None, include_queued: bool = True,
    ) -> tuple[InboxEnvelope, ...]:
        """查询当前任务通知及最早一条真人输入，由唯一 worker 顺序交付。

        Args:
            kind (InboxKind | str | None): 审批期间可限定只读取真人输入。
            include_queued (bool): 是否包括已放行的排队输入。

        Returns:
            tuple[InboxEnvelope, ...]: 当前准备交付的一批 pending 输入。
        """
        rows = await self._pending(kind, include_queued)
        human = next((item for item in rows if item.kind == InboxKind.HUMAN), None)
        return tuple(item for item in rows if item.kind != InboxKind.HUMAN or item is human)

    async def release_queued(self) -> None:
        """幂等放行尚未交付的排队输入，停止或异常结束不得调用此入口。

        本方法供独立放行调用方复用事务内放行规则，
        再提交自身连接上的更新。完成 checkpoint 的原子放行由 AgentStateDao
        直接调用模块级事务函数完成，不经此独立提交入口。
        """
        async def save_attempt() -> None:
            """提交本次已准备的业务材料，失败后由共享规则决定安全重试。"""
            async with self._transaction() as db:
                # 1. 复用唯一的放行筛选规则，完成后才提交本次独立更新。
                await release_queued_in_transaction(db, self.session_id, storage_scope=self.storage_scope)

        return await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    async def read_task_terminals(self) -> tuple[InboxEnvelope, ...]:
        """模型调用前查询待交付任务通知，不改变输入状态。

        Returns:
            tuple[InboxEnvelope, ...]: 尚未进入 Agent 状态的任务事实。
        """
        return await self._pending(InboxKind.TASK_TERMINAL, False)

    async def discard_human_input(self, envelope_id: int) -> None:
        """重试耗尽时移除未交付真人输入，由上层调度流程发送失败展示。

        Args:
            envelope_id (int): 当前失败真人输入的编号。
        """
        async def save_attempt() -> None:
            """提交本次已准备的业务材料，失败后由共享规则决定安全重试。"""
            async with self._transaction() as db:
                await db.execute(f"DELETE FROM {self.storage_scope.inbox_table} WHERE session_id = ? AND id = ? AND kind = ? AND status = ?",
                             (self.session_id, envelope_id, InboxKind.HUMAN.value, InboxStatus.PENDING.value))

        return await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    async def status(self, envelope_id: int) -> InboxStatus | None:
        """查询指定输入的交付状态，用于确保真人事件在 checkpoint 提交后发布。

        Args:
            envelope_id (int): Inbox 自增输入编号。

        Returns:
            InboxStatus | None: 该输入的交付状态，记录不存在时为空。
        """
        async with self._write_lock:
            db = await self._get_db()
            async with db.execute(
                f"SELECT status FROM {self.storage_scope.inbox_table} WHERE session_id = ? AND id = ?",
                (self.session_id, envelope_id),
            ) as cursor:
                row = await cursor.fetchone()
            return InboxStatus(row[0]) if row else None

    async def pending_humans(self) -> list[InboxEnvelope]:
        """按会话和输入顺序查询尚未交付的真人输入。

        Returns:
            list[InboxEnvelope]: 按输入序号排序的未交付真人输入。
        """
        async with self._write_lock:
            db = await self._get_db()
            rows = list(await db.execute_fetchall(
                "SELECT id, session_id, kind, payload_json, status, "
                f"delivery_mode FROM {self.storage_scope.inbox_table} "
                "WHERE session_id = ? AND kind = ? AND status != ? "
                "ORDER BY id",
                (self.session_id, InboxKind.HUMAN.value, InboxStatus.DELIVERED.value),
            ))
            return [self._from_row(row) for row in rows]

    async def cancel_queued(self, envelope_id: int) -> bool:
        """删除本会话仍处于待交付状态的指定排队消息。

        Args:
            envelope_id (int): Inbox 自增输入编号。

        Returns:
            bool: 本会话待交付的排队记录实际被删除时为真。
        """
        async def save_attempt() -> bool:
            """提交本次已准备的业务材料，失败后由共享规则决定安全重试。"""
            async with self._transaction() as db:
                # 1. 条件删除同时约束会话、输入编号、排队模式和 pending，已经交付的输入不可撤销。
                cursor = await db.execute(
                    f"DELETE FROM {self.storage_scope.inbox_table} WHERE session_id = ? AND id = ? "
                    "AND kind = ? "
                    "AND delivery_mode = ? AND status = ?",
                    (self.session_id, envelope_id, InboxKind.HUMAN.value,
                     InboxDeliveryMode.QUEUED.value, InboxStatus.PENDING.value),
                )
                return cursor.rowcount == 1

        return await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    async def pending_sessions(self) -> tuple[str, ...]:
        """列出尚有待交付输入的上下文，供启动整理已删除的父身份。"""
        async with self._write_lock:
            db = await self._get_db()
            rows = list(await db.execute_fetchall(
                f"SELECT DISTINCT session_id FROM {self.storage_scope.inbox_table} WHERE status=?",
                (InboxStatus.PENDING.value,)))
            return tuple(str(row[0]) for row in rows)

    async def discard_pending(self) -> None:
        """父配置已删除时清除全部待交付输入，保留已经交付的历史。"""
        async def save_attempt() -> None:
            """提交本次已准备的业务材料，失败后由共享规则决定安全重试。"""
            async with self._transaction() as db:
                try:
                    await discard_pending_in_transaction(db, self.session_id, storage_scope=self.storage_scope)
                except BaseException:
                    await db.rollback()
                    raise

        return await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    async def discard_pending_commands(self) -> None:
        """重启或最终停止时作废未消费的指令和 A2A 回复，保留后台通知。"""
        async def save_attempt() -> None:
            """提交本次已准备的业务材料，失败后由共享规则决定安全重试。"""
            async with self._transaction() as db:
                await db.execute(f"DELETE FROM {self.storage_scope.inbox_table} WHERE session_id=? AND status=? "
                             "AND kind IN (?,?,?)", (self.session_id, InboxStatus.PENDING.value, InboxKind.HUMAN.value,
                                                     InboxKind.AGENT_COMMAND.value, InboxKind.PARENT_REPLY.value))

        return await run_sqlite_save(save_attempt, timeout_seconds=SQLITE_SAVE_TIMEOUT_SECONDS)

    async def close(self) -> None:
        """在有限预算内释放本对象连接，已开始的保存确认由写锁排序完成。"""
        async with self._write_lock:
            db, self._db = self._db, None
            if db is not None:
                await close_resources(((self.db_path, db.close),), logger=logger, retryable=(sqlite3.Error, OSError))
