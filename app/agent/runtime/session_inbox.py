"""按会话排序并持久保存待交付给 Agent 的输入。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from typing import Any
from uuid import uuid4

import aiosqlite

from app.runtime import get_checkpoint_db
from app.schemas.runtime_types import InboxDeliveryMode, InboxKind, InboxStatus, SYSTEM_INPUT_ORIGIN


# 领取租期与 Scheduler 的空闲复查周期职责不同，即使同为 30 秒也分别定义。
INBOX_CLAIM_LEASE_SECONDS = 30


INBOX_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('human', 'task_terminal')),
    origin_channel TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    delivery_mode TEXT NOT NULL DEFAULT 'immediate'
        CHECK (delivery_mode IN ('immediate', 'queued')),
    queue_ready INTEGER NOT NULL DEFAULT 0 CHECK (queue_ready IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'delivered')),
    claim_token TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    delivered_at TEXT
)
"""


async def ensure_inbox_schema(db: aiosqlite.Connection) -> None:
    """幂等初始化 Inbox 表，并为已有运行时表补充排队字段。

    Args:
        db (aiosqlite.Connection): 参与当前原子持久化操作的 SQLite 连接。
    """
    # 1. 先建新表，再查看既有列，升级只增加字段而不重写历史输入。
    await db.execute(INBOX_SCHEMA_SQL)
    columns = {
        str(row[1]) for row in await db.execute_fetchall("PRAGMA table_info(session_inbox)")
    }
    # 2. 为早期 Inbox 表补默认立即交付模式和排队放行标记。
    if "delivery_mode" not in columns:
        await db.execute(
            "ALTER TABLE session_inbox ADD COLUMN delivery_mode TEXT NOT NULL "
            "DEFAULT 'immediate' CHECK (delivery_mode IN ('immediate', 'queued'))"
        )
    if "queue_ready" not in columns:
        await db.execute(
            "ALTER TABLE session_inbox ADD COLUMN queue_ready INTEGER NOT NULL "
            "DEFAULT 0 CHECK (queue_ready IN (0, 1))"
        )


@dataclass(frozen=True)
class InboxEnvelope:
    """表示一次持久输入的领取快照，领取令牌用于 checkpoint 事务验证交付。

    Attributes:
        id (int): SQLite 自增输入序号，在事件中作为 input_sequence。
        session_id (str): 共享 Ayaya 会话标识。
        kind (InboxKind): 真人输入或后台终态通知。
        origin_channel (str): 输入或本轮回复所属的渠道。
        payload (dict[str, Any]): 该类别携带的专用载荷。
        status (InboxStatus): 尚未领取、已领取或原子交付完成。
        claim_token (str | None): 本次领取的随机令牌，交付时核对。
        delivery_mode (InboxDeliveryMode): 立即交付或等待正常轮次完成后放行。
    """
    id: int
    session_id: str
    kind: InboxKind
    origin_channel: str
    payload: dict[str, Any]
    status: InboxStatus
    claim_token: str | None
    delivery_mode: InboxDeliveryMode = InboxDeliveryMode.IMMEDIATE


async def mark_delivered_in_transaction(
    db: aiosqlite.Connection, session_id: str, claims: tuple[InboxEnvelope, ...],
) -> None:
    """在状态保存事务中核对领取令牌，并将输入标记为已交付。

    StateManager.save 在写入 checkpoint 和协议历史后调用；连接和 Inbox 表
    已由调用方准备。本函数不提交或回滚，返回仅表示 SQL 已执行；调用方必须
    统一提交，或在异常、取消时回滚包括前面交付更新在内的全部数据库写入。

    Args:
        db (aiosqlite.Connection): 调用方已开启状态保存事务的连接。
        session_id (str): 本次状态保存所属的会话。
        claims (tuple[InboxEnvelope, ...]): 与 checkpoint 一起交付的输入领取快照。

    Raises:
        ValueError: 领取不属于当前会话或缺少令牌。
        RuntimeError: 记录已不处于 claimed 状态、令牌失效或记录已不存在。
    """
    # 1. 按领取顺序逐项更新；后续领取无效时，由调用方撤销前面已执行的交付。
    for claim in claims:
        if claim.session_id != session_id or claim.claim_token is None:
            raise ValueError("Inbox claim 不属于当前会话或缺少令牌")
        # 1.1. 只允许当前有效领取成为 delivered，不能覆盖重新领取后的新令牌。
        delivered = await db.execute(
            "UPDATE session_inbox SET status = ?, delivered_at = ?, "
            "claim_token = NULL WHERE id = ? AND session_id = ? "
            "AND status = ? AND claim_token = ?",
            (InboxStatus.DELIVERED.value, datetime.now(timezone.utc).isoformat(), claim.id, session_id,
             InboxStatus.CLAIMED.value, claim.claim_token),
        )
        # 1.2. 不匹配时终止保存，避免 checkpoint 接受已经失效的输入领取。
        if delivered.rowcount != 1:
            raise RuntimeError("Inbox claim 已失效")


async def release_queued_in_transaction(db: aiosqlite.Connection, session_id: str) -> None:
    """在调用方事务中放行当前会话尚未领取的排队真人输入。

    StateManager.save 在保存正常完成状态时调用；SessionInbox.release_queued
    在 Scheduler 请求独立放行时复用。本函数不改变交付状态，不自行提交或
    回滚；是否允许放行由调用方决定，持久提交及失败处理也由调用方负责。

    Args:
        db (aiosqlite.Connection): 调用方提供的已初始化连接，沿用其事务。
        session_id (str): 本次请求放行的会话。
    """
    # 1. 仅修改 pending 排队真人输入的执行资格，重复放行不重复交付输入。
    await db.execute(
        "UPDATE session_inbox SET queue_ready = 1 "
        "WHERE session_id = ? AND kind = ? AND delivery_mode = ? "
        "AND status = ?",
        (session_id, InboxKind.HUMAN.value, InboxDeliveryMode.QUEUED.value, InboxStatus.PENDING.value),
    )


async def append_task_terminal_in_transaction(
    db: aiosqlite.Connection, *, session_id: str, task_id: str, task_name: str,
    status: str, result: str,
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
        result (str): 任务结束时的输出摘要。
    """
    # 1. 沿用现有通知载荷和默认 pending、immediate、未排队状态，等待 worker 领取。
    await db.execute(
        "INSERT INTO session_inbox "
        "(session_id, kind, origin_channel, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, InboxKind.TASK_TERMINAL.value, SYSTEM_INPUT_ORIGIN, json.dumps({
            "task_id": task_id, "task_name": task_name, "status": status, "result": result,
        }, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
    )


class SessionInbox:
    """访问一个会话的待处理输入，包含真人消息和后台任务结束通知。

    各会话的数据保存在同一张 SQLite 表中，通过 session_id 区分。一个会话可以
    有多个本对象，例如提交命令和 worker 分别创建对象，但读写的是同一份数据库，也是同一个队列。
    输入先保存为 pending，worker 领取后为 claimed，Agent 保存消息后才标记 delivered。
    本模块集中定义输入的领取、交付和排队规则；跨表保存通过模块级事务函数复用
    这些规则，最终提交或回滚由 StateManager 和 TaskRecordDao 的事务入口负责。

    Attributes:
        session_id (str): 共享 Ayaya 会话标识。
        db_path (str | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
        _db (aiosqlite.Connection | None): 参与当前原子持久化操作的 SQLite 连接。
    """

    def __init__(self, session_id: str, db_path: str | None = None):
        """绑定会话和测试可替换的数据库路径，延迟创建 SQLite 连接。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            db_path (str | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
        """
        self.session_id = session_id
        self.db_path = str(db_path or get_checkpoint_db())
        self._db: aiosqlite.Connection | None = None

    @staticmethod
    async def reset_after_restart(db_path: str | None = None) -> None:
        """原子作废未交付真人意图，并释放任务通知的遗留领取。

        Args:
            db_path (str | None): 当前环境的共享 SQLite 路径。

        Raises:
            Exception: 清理失败时回滚全部变更，由生命周期入口终止启动。
        """
        database = str(db_path or get_checkpoint_db())
        # 1. 新环境第一次启动时尚未创建 SQLite 目录，清理不能依赖先运行另一条 DAO 路径。
        directory = os.path.dirname(database)
        if directory:
            os.makedirs(directory, exist_ok=True)
        async with aiosqlite.connect(database) as db:
            await ensure_inbox_schema(db)
            await db.commit()
            try:
                # 1. 不区分排队放行状态，旧真人输入不再代表当前执行意图。
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "DELETE FROM session_inbox WHERE kind = ? AND status IN (?, ?)",
                    (InboxKind.HUMAN.value, InboxStatus.PENDING.value, InboxStatus.CLAIMED.value),
                )
                # 2. 通知属于执行事实，释放领取令牌但不删除内容或重复已交付记录。
                await db.execute(
                    "UPDATE session_inbox SET status = ?, claim_token = NULL, claimed_at = NULL "
                    "WHERE kind = ? AND status = ?",
                    (InboxStatus.PENDING.value, InboxKind.TASK_TERMINAL.value, InboxStatus.CLAIMED.value),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    @staticmethod
    async def pending_sessions(db_path: str | None = None) -> list[str]:
        """只查询存在未交付输入的会话供诊断，不赋予跨重启执行资格。

        Args:
            db_path (str | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。

        Returns:
            list[str]: 存在 pending 或 claimed 输入的会话标识列表。
        """
        async with aiosqlite.connect(str(db_path or get_checkpoint_db())) as db:
            await ensure_inbox_schema(db)
            rows = await db.execute_fetchall(
                "SELECT DISTINCT session_id FROM session_inbox "
                "WHERE status IN (?, ?)",
                (InboxStatus.PENDING.value, InboxStatus.CLAIMED.value),
            )
        return [str(row[0]) for row in rows]

    async def _get_db(self) -> aiosqlite.Connection:
        """复用会话连接并幂等初始化 Inbox 表及排序索引。

        Returns:
            aiosqlite.Connection: 已经完成幂等 Inbox 初始化的实例连接。
        """
        # 1. 按环境路径延迟打开连接，建表和索引只在该连接首次使用时初始化。
        if self._db is None:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._db = await aiosqlite.connect(self.db_path)
            await ensure_inbox_schema(self._db)
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_inbox_order "
                "ON session_inbox(session_id, status, id)"
            )
            await self._db.commit()
        return self._db

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> InboxEnvelope:
        """将固定列顺序的数据库行还原为不可变输入领取快照。

        Args:
            row (tuple[Any, ...]): 固定查询列顺序的 SQLite 行。

        Returns:
            InboxEnvelope: 从查询行恢复的输入状态及领取令牌。
        """
        return InboxEnvelope(
            id=int(row[0]),
            session_id=str(row[1]),
            kind=InboxKind(row[2]),
            origin_channel=str(row[3]),
            payload=json.loads(row[4]),
            status=InboxStatus(row[5]),
            claim_token=row[6],
            delivery_mode=InboxDeliveryMode(row[7]),
        )

    async def submit_human(
        self, origin_channel: str, message: str, *, images: list[str] | None = None,
        queued: bool = False,
    ) -> InboxEnvelope:
        """校验真人内容并持久写入 pending 输入，返回值不代表已交付模型。

        Args:
            origin_channel (str): 输入或本轮回复所属的渠道。
            message (str): 真人文本或类型声明对应的内部通知。
            images (list[str] | None): 真人附件的图片 data URL 列表。
            queued (bool): 是否等前一真人轮次正常完成后才交付。

        Returns:
            InboxEnvelope: 已持久受理但尚未交付模型的输入快照。

        Raises:
            ValueError: 消息来源和内容不可为空。
            RuntimeError: 数据库未返回 Inbox ID。
        """
        # 1. 同时验证来源及文本或附件，纯图片输入也可受理。
        if not origin_channel or (not message.strip() and not images):
            raise ValueError("消息来源和内容不可为空")
        db = await self._get_db()
        payload = {"message": message, "images": images or []}
        # 2. 先写 pending 记录并提交，再返回 input_sequence；此时尚未交付模型。
        cursor = await db.execute(
            "INSERT INTO session_inbox "
            "(session_id, kind, origin_channel, payload_json, delivery_mode, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.session_id, InboxKind.HUMAN.value, origin_channel, json.dumps(payload, ensure_ascii=False),
             (InboxDeliveryMode.QUEUED if queued else InboxDeliveryMode.IMMEDIATE).value,
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("数据库未返回 Inbox ID")
        return InboxEnvelope(
            int(cursor.lastrowid), self.session_id, InboxKind.HUMAN, origin_channel,
            payload, InboxStatus.PENDING, None, InboxDeliveryMode.QUEUED if queued else InboxDeliveryMode.IMMEDIATE,
        )

    async def claim_next(
        self, *, kind: InboxKind | str | None = None, include_queued: bool = True,
    ) -> InboxEnvelope | None:
        """在写事务内领取最早可交付输入，过期领取可使用新令牌重新认领。

        Args:
            kind (InboxKind | str | None): 已知输入类别或原始过滤值，省略时不限定类别。
            include_queued (bool): 是否允许领取已放行的排队输入。

        Returns:
            InboxEnvelope | None: 本次领取的最早输入，没有符合条件的输入时为空。
        """
        db = await self._get_db()
        try:
            # 1. 写事务串行化选择和领取，只有 ready 排队输入与过期 claim 可参与。
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT id, session_id, kind, origin_channel, payload_json, status, "
                "claim_token, delivery_mode "
                "FROM session_inbox WHERE session_id = ? "
                "AND (? IS NULL OR kind = ?) "
                "AND (delivery_mode = ? OR (? AND queue_ready = 1)) "
                "AND (status = ? OR (status = ? AND claimed_at < ?)) "
                "ORDER BY id LIMIT 1",
                (self.session_id, kind.value if isinstance(kind, InboxKind) else kind,
                 kind.value if isinstance(kind, InboxKind) else kind, InboxDeliveryMode.IMMEDIATE.value,
                 include_queued, InboxStatus.PENDING.value, InboxStatus.CLAIMED.value,
                 (datetime.now(timezone.utc) - timedelta(seconds=INBOX_CLAIM_LEASE_SECONDS)).isoformat()),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.commit()
                return None
            # 2. 为本次领取生成新令牌，StateManager.save 必须匹配它才能标记 delivered。
            token = str(uuid4())
            await db.execute(
                "UPDATE session_inbox SET status = ?, claim_token = ?, claimed_at = ? "
                "WHERE id = ?", (InboxStatus.CLAIMED.value, token, datetime.now(timezone.utc).isoformat(), row[0]),
            )
            await db.commit()
            return self._from_row((*row[:5], InboxStatus.CLAIMED.value, token, row[7]))
        # 3. 领取失败或取消时回滚，输入仍可由后续激活重新领取。
        except BaseException:
            await db.rollback()
            raise

    async def claim_activation_batch(
        self, *, kind: InboxKind | str | None = None, include_queued: bool = True,
    ) -> tuple[InboxEnvelope, ...]:
        """冻结当前激活批次，合并已到达任务终态并至多领取一条真人输入。

        Args:
            kind (InboxKind | str | None): 已知输入类别或原始过滤值，省略时不限定类别。
            include_queued (bool): 是否允许领取已放行的排队输入。

        Returns:
            tuple[InboxEnvelope, ...]: 当前已到达任务终态和至多一个真人输入的领取集合。
        """
        db = await self._get_db()
        try:
            # 1. 在事务内冻结当前可交付集合，避免批次选择与领取被其他写入交错。
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.execute_fetchall(
                "SELECT id, session_id, kind, origin_channel, payload_json, status, "
                "claim_token, delivery_mode FROM session_inbox WHERE session_id = ? "
                "AND (? IS NULL OR kind = ?) "
                "AND (delivery_mode = ? OR (? AND queue_ready = 1)) "
                "AND (status = ? OR (status = ? AND claimed_at < ?)) "
                "ORDER BY id",
                (self.session_id, kind.value if isinstance(kind, InboxKind) else kind,
                 kind.value if isinstance(kind, InboxKind) else kind, InboxDeliveryMode.IMMEDIATE.value,
                 include_queued, InboxStatus.PENDING.value, InboxStatus.CLAIMED.value,
                 (datetime.now(timezone.utc) - timedelta(seconds=INBOX_CLAIM_LEASE_SECONDS)).isoformat()),
            )
            # 2. 取最早一条真人输入并合并已到达终态，后续真人输入保留各自回复轮次。
            human = next((row for row in rows if row[2] == InboxKind.HUMAN.value), None)
            chosen = [row for row in rows if row[2] == InboxKind.TASK_TERMINAL.value]
            if human is not None:
                chosen.append(human)
            chosen.sort(key=lambda row: int(row[0]))
            claimed = []
            # 3. 各输入独立生成 claim_token，同一 checkpoint 交付时逐项复核。
            for row in chosen:
                token = str(uuid4())
                await db.execute(
                    "UPDATE session_inbox SET status = ?, claim_token = ?, "
                    "claimed_at = ? WHERE id = ?",
                    (InboxStatus.CLAIMED.value, token, datetime.now(timezone.utc).isoformat(), row[0]),
                )
                claimed.append(self._from_row((*row[:5], InboxStatus.CLAIMED.value, token, row[7])))
            await db.commit()
            return tuple(claimed)
        # 4. 任一领取失败回滚整批，防止只有部分通知被占用。
        except BaseException:
            await db.rollback()
            raise

    async def release_queued(self) -> None:
        """幂等放行尚未领取的排队输入，停止或异常结束不得调用此入口。

        Scheduler 在现有完成路径请求独立放行；本方法复用事务内放行规则，
        再提交自身连接上的更新。完成 checkpoint 的原子放行由 StateManager
        直接调用模块级事务函数完成，不经此独立提交入口。
        """
        db = await self._get_db()
        # 1. 复用唯一的放行筛选规则，完成后才提交本次独立更新。
        await release_queued_in_transaction(db, self.session_id)
        await db.commit()

    async def claim_task_terminals(self) -> tuple[InboxEnvelope, ...]:
        """在下一次模型调用前领取当前任务终态通知，不领取真人输入。

        Returns:
            tuple[InboxEnvelope, ...]: 当前可交付终态通知的领取集合。
        """
        db = await self._get_db()
        try:
            # 1. 仅冻结当前任务终态通知，供工具协议闭合后的模型安全点使用。
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.execute_fetchall(
                "SELECT id, session_id, kind, origin_channel, payload_json, status, "
                "claim_token, delivery_mode FROM session_inbox "
                "WHERE session_id = ? AND kind = ? "
                "AND (status = ? OR (status = ? AND claimed_at < ?)) "
                "ORDER BY id",
                (self.session_id, InboxKind.TASK_TERMINAL.value, InboxStatus.PENDING.value, InboxStatus.CLAIMED.value,
                 (datetime.now(timezone.utc) - timedelta(seconds=INBOX_CLAIM_LEASE_SECONDS)).isoformat()),
            )
            claimed = []
            # 2. 批量领取各通知，交付令牌由同次 checkpoint 事务核对。
            for row in rows:
                token = str(uuid4())
                await db.execute(
                    "UPDATE session_inbox SET status = ?, claim_token = ?, "
                    "claimed_at = ? WHERE id = ?",
                    (InboxStatus.CLAIMED.value, token, datetime.now(timezone.utc).isoformat(), row[0]),
                )
                claimed.append(self._from_row((*row[:5], InboxStatus.CLAIMED.value, token, row[7])))
            await db.commit()
            return tuple(claimed)
        # 3. 保证异常或取消不会留下部分领取结果。
        except BaseException:
            await db.rollback()
            raise

    async def status(self, envelope_id: int) -> InboxStatus | None:
        """查询指定输入的交付状态，用于确保真人事件在 checkpoint 提交后发布。

        Args:
            envelope_id (int): Inbox 自增输入编号。

        Returns:
            InboxStatus | None: 该输入的交付状态，记录不存在时为空。
        """
        db = await self._get_db()
        async with db.execute(
            "SELECT status FROM session_inbox WHERE session_id = ? AND id = ?",
            (self.session_id, envelope_id),
        ) as cursor:
            row = await cursor.fetchone()
        return InboxStatus(row[0]) if row else None

    async def pending_humans(self, *, origin_channel: str | None = None) -> list[InboxEnvelope]:
        """查询未交付真人输入，按需限定来源以生成接收方快照。

        Args:
            origin_channel (str | None): 输入或本轮回复所属的渠道。

        Returns:
            list[InboxEnvelope]: 按输入序号排序的未交付真人输入。
        """
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT id, session_id, kind, origin_channel, payload_json, status, "
            "claim_token, delivery_mode FROM session_inbox "
            "WHERE session_id = ? AND kind = ? AND status != ? "
            "AND (? IS NULL OR origin_channel = ?) ORDER BY id",
            (self.session_id, InboxKind.HUMAN.value, InboxStatus.DELIVERED.value, origin_channel, origin_channel),
        )
        return [self._from_row(row) for row in rows]

    async def cancel_queued(self, envelope_id: int, origin_channel: str) -> bool:
        """仅删除当前来源尚未领取的排队消息，已经开始的输入不能撤销。

        Args:
            envelope_id (int): Inbox 自增输入编号。
            origin_channel (str): 输入或本轮回复所属的渠道。

        Returns:
            bool: 只有同来源尚未领取的排队记录实际被删除时为真。
        """
        db = await self._get_db()
        # 1. 条件删除同时约束会话、来源、排队模式和 pending，已经领取的输入不可撤销。
        cursor = await db.execute(
            "DELETE FROM session_inbox WHERE session_id = ? AND id = ? "
            "AND origin_channel = ? AND kind = ? "
            "AND delivery_mode = ? AND status = ?",
            (self.session_id, envelope_id, origin_channel, InboxKind.HUMAN.value,
             InboxDeliveryMode.QUEUED.value, InboxStatus.PENDING.value),
        )
        await db.commit()
        return cursor.rowcount == 1

    async def close(self) -> None:
        """关闭实例持有的 SQLite 连接，后续访问可以重新打开。"""
        if self._db is not None:
            await self._db.close()
            self._db = None
