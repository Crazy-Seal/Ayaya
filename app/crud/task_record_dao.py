"""后台任务记录与终态 Inbox 通知的 SQLite 持久化。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os

import aiosqlite

from app.schemas.tool_types import TASK_OUTPUT_URI_PREFIX
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.subagent_types import TaskRecordKind, SubagentInteractionKind
from app.schemas.subagent_types import SubagentDefinition
from app.crud.subagent_sql import ensure_subagent_schema, append_interaction_in_transaction
from app.crud.subagent_sql import require_subagent_in_transaction, subagent_available_in_transaction
from app.crud.session_inbox_dao import append_task_terminal_in_transaction, ensure_inbox_schema
from app.environment import get_checkpoint_db
from app.schemas.runtime_types import TaskStatus, TurnInterruptReason
from app.schemas.task_types import TaskRecord, TERMINAL_TASK_NAME


TASK_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_records (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    command TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'stopped', 'orphaned')),
    pid INTEGER,
    exit_code INTEGER,
    output_path TEXT,
    created_at TEXT NOT NULL,
    ended_at TEXT,
    origin_run_id TEXT,
    origin_tool_call_id TEXT,
    output_ref TEXT,
    background INTEGER NOT NULL DEFAULT 1,
    tool_name TEXT,
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    result TEXT,
    error TEXT,
    plugin_name TEXT,
    notify_agent INTEGER NOT NULL,
    subagent_id TEXT, notification_context_id TEXT,
    notification_scope TEXT NOT NULL DEFAULT 'main' CHECK(notification_scope IN ('main','subagent')),
    kind TEXT NOT NULL DEFAULT 'ordinary' CHECK(kind IN ('ordinary','subagent')), end_reason TEXT
)
"""


class TaskRecordDao:
    """拥有任务记录和首次终态规则，协调任务终态与 Inbox 通知的原子提交。

    TaskSupervisor 判断任务已结束后调用 finish_once。本类条件更新任务终态，
    将任务事实交给 Inbox 模块编码通知，并负责共用连接上的统一提交；任一步
    失败或取消时回滚全部数据库写入，不留下缺少通知的任务终态。

    Attributes:
        db_path (str | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
        _db (aiosqlite.Connection | None): 参与当前原子持久化操作的 SQLite 连接。
        _db_lock (asyncio.Lock): 避免并发初始化重复创建连接的锁。
        _write_lock (asyncio.Lock): 防止同一连接上的写事务交错。
    """
    def __init__(self, db_path: str | None = None) -> None:
        """准备延迟连接和写入锁，避免同一连接的任务事务交错。

        Args:
            db_path (str | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
        """
        self.db_path = str(db_path or get_checkpoint_db())
        self._db: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _now() -> str:
        """生成用于任务记录的 UTC 时间字符串。

        Returns:
            str: UTC ISO 8601 时间字符串。
        """
        return datetime.now(timezone.utc).isoformat()

    async def _get_db(self) -> aiosqlite.Connection:
        """在初始化锁内创建当前任务及 Inbox 表，已有数据库由离线迁移升级。

        Returns:
            aiosqlite.Connection: 已初始化任务及 Inbox 的共享实例连接。
        """
        # 1. 初始化锁防止首次并发查询创建多个连接。
        async with self._db_lock:
            if self._db is None:
                os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
                db = await aiosqlite.connect(self.db_path)
                try:
                    # 2. 已有表必须先显式迁移，在创建新表或查询新字段前给出可操作的错误。
                    columns = {str(row[1]) for row in await db.execute_fetchall("PRAGMA table_info(task_records)")}
                    if columns and not {"subagent_id", "notification_context_id", "notification_scope", "kind", "end_reason"} <= columns:
                        raise RuntimeError("task_records 需要先执行 scripts/migrate_subagents.py 离线迁移")
                    if "lifetime" in columns:
                        raise RuntimeError("task_records 需要先执行 scripts/migrate_subagents.py 离线迁移以删除 lifetime 列")
                    await db.execute(TASK_SCHEMA_SQL)
                    # 3. 任务表与 Inbox 共用连接和数据库，为终态通知原子事务做准备。
                    await ensure_inbox_schema(db)
                    await ensure_inbox_schema(db, storage_scope=AgentStorageScope.SUBAGENT)
                    await ensure_subagent_schema(db)
                    await db.execute(
                        "CREATE INDEX IF NOT EXISTS idx_task_records_session_created "
                        "ON task_records(session_id, created_at DESC)"
                    )
                    await db.commit()
                except BaseException:
                    await db.close()
                    raise
                self._db = db
            return self._db

    async def subagent_available(self, session_id: str, subagent_id: str) -> bool:
        """按父子归属和废弃时间读取子身份资格，不推断执行状态。"""
        db = await self._get_db()
        async with self._write_lock:
            return await subagent_available_in_transaction(db, session_id, subagent_id)

    async def running_ids(self, kind: TaskRecordKind = TaskRecordKind.ORDINARY) -> list[str]:
        """列出仍标记运行中的任务，以便启动时确认其已失去进程所有权。

        Returns:
            list[str]: 后端重启后需要标记 orphaned 的运行记录标识。
        """
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT task_id FROM task_records WHERE status = ? AND kind = ?", (TaskStatus.RUNNING.value, kind.value),
        )
        return [str(row[0]) for row in rows]

    async def insert_running(
        self, task_id: str, session_id: str, command: str | None = None,
        pid: int | None = None, output_path: str | None = None, *,
        origin_run_id: str | None = None,
        origin_tool_call_id: str | None = None,
        background: bool = True,
        tool_name: str | None = None,
        name: str = TERMINAL_TASK_NAME, title: str | None = None,
        plugin_name: str | None = None,
        notify_agent: bool = True,
        subagent_id: str | None = None, notification_context_id: str | None = None,
        notification_scope: AgentStorageScope = AgentStorageScope.MAIN,
        kind: TaskRecordKind = TaskRecordKind.ORDINARY,
    ) -> None:
        """保存任务初始记录及来源，具体资源由任务开始执行后创建。

        Args:
            task_id (str): 统一管理的任务编号。
            session_id (str): 共享 Ayaya 会话标识。
            command (str | None): 终端命令，尚未创建终端或通用任务为空。
            pid (int | None): 已创建的进程编号，尚未创建终端或通用任务为空。
            output_path (str | None): 终端完整输出路径，通用任务为空。
            origin_run_id (str | None): 发起任务的来源轮次编号。
            origin_tool_call_id (str | None): 发起任务的工具调用标识。
            background (bool): 是否显式后台启动，前台等待时不展示后台入口。
            tool_name (str | None): 创建工具名；插件任务为空。
            name (str): 供查询或识别本次内容用途的稳定名称。
            title (str | None): 供用户阅读的任务说明。
            plugin_name (str | None): 发出内容或创建任务的插件稳定名称。
            notify_agent (bool): 终态保存时是否生成给 Agent 的结果通知。
        """
        db = await self._get_db()
        # 1. 同一 DAO 写锁覆盖插入与提交，避免和终态通知事务交错。
        async with self._write_lock:
            try:
                # 1. 子普通任务登记与废弃使用数据库写事务串行，历史补记仍可写入。
                await db.execute("BEGIN IMMEDIATE")
                if subagent_id is not None and kind == TaskRecordKind.ORDINARY:
                    await require_subagent_in_transaction(db, session_id, subagent_id)
                await db.execute(
                    "INSERT INTO task_records "
                    "(task_id, session_id, command, status, pid, output_path, created_at, "
                    "origin_run_id, origin_tool_call_id, output_ref, background, tool_name, "
                    "name, title, plugin_name, notify_agent, subagent_id, notification_context_id, notification_scope, kind) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, session_id, command, TaskStatus.RUNNING.value, pid, output_path, self._now(),
                     origin_run_id, origin_tool_call_id, task_id if output_path else None, int(background), tool_name,
                     name, title if title is not None else command, plugin_name, int(notify_agent), subagent_id, notification_context_id, notification_scope.value, kind.value),
                )
                await db.commit()
            except BaseException:
                # 1. 取消或写入失败时回滚，后续查询不能读到半笔登记或后台移交。
                await db.rollback()
                raise

    async def start_subagent_run(self, definition: SubagentDefinition, run_id: str, previous_run_id: str | None) -> None:
        """原子结束被引导的旧段并建立新段，重试沿用同一个新轮次编号。

        Args:
            definition (SubagentDefinition): 固定子身份和父归属。
            run_id (str): 本次准备开始的新段编号。
            previous_run_id (str | None): 同一委派上一段，首次运行为空。
        """
        db = await self._get_db()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                existing = await db.execute_fetchall("SELECT session_id,subagent_id FROM task_records WHERE task_id=?", (run_id,))
                if existing:
                    if existing[0] != (definition.session_id, definition.subagent_id):
                        raise ValueError("运行编号已属于其他上下文")
                    await db.commit()
                    return
                if previous_run_id is not None:
                    cursor = await db.execute(
                        "UPDATE task_records SET status=?,ended_at=?,end_reason=? WHERE task_id=? AND session_id=? AND subagent_id=? AND kind=? AND status=?",
                        (TaskStatus.STOPPED.value, self._now(), TurnInterruptReason.STEER.value, previous_run_id,
                         definition.session_id, definition.subagent_id, TaskRecordKind.SUBAGENT.value, TaskStatus.RUNNING.value))
                    if cursor.rowcount != 1:
                        raise ValueError("原子执行段已经结束，不能改写为引导")
                await db.execute(
                    "INSERT INTO task_records(task_id,session_id,status,created_at,background,name,title,notify_agent,subagent_id,notification_context_id,notification_scope,kind) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, definition.session_id, TaskStatus.RUNNING.value, self._now(), 0, "subagent",
                     f"{definition.name} ({definition.subagent_id})", 1,
                     definition.subagent_id, definition.session_id, AgentStorageScope.MAIN.value, TaskRecordKind.SUBAGENT.value))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def mark_background(self, task_id: str) -> None:
        """前台等待超时后持久标记同一进程已移交后台，不更改执行状态和任务身份。

        Args:
            task_id (str): 原前台进程的稳定任务标识。
        """
        db = await self._get_db()
        # 1. 模式移交与终态写入共用写锁，避免同一连接上的事务交错。
        async with self._write_lock:
            try:
                await db.execute("UPDATE task_records SET background = 1 WHERE task_id = ?", (task_id,))
                await db.commit()
            except BaseException:
                # 1. 取消或写入失败时回滚，后续查询不能读到半笔登记或后台移交。
                await db.rollback()
                raise

    async def attach_terminal(self, task_id: str, *, command: str, pid: int,
                              output_path: str) -> TaskRecord:
        """终端模块向已登记任务补充实际进程信息，不登记任务或改变终态。

        Args:
            task_id (str): Supervisor 已登记的任务编号。
            command (str): 实际终端命令。
            pid (int): 已启动的系统进程编号。
            output_path (str): 完整输出文件路径。

        Returns:
            TaskRecord: 进程信息提交后的任务记录。

        Raises:
            KeyError: 任务不存在或已结束，不能附加运行中的终端。
        """
        db = await self._get_db()
        async with self._write_lock:
            try:
                cursor = await db.execute(
                    "UPDATE task_records SET command=?,pid=?,output_path=?,output_ref=? "
                    "WHERE task_id=? AND status=?",
                    (command, pid, output_path, task_id, task_id, TaskStatus.RUNNING.value))
                if cursor.rowcount != 1:
                    raise KeyError("终端任务不存在或已结束")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get(task_id)

    async def save_terminal_exit_code(self, task_id: str, exit_code: int | None) -> None:
        """由终端模块保存实际退出码，随后 Supervisor 独立提交通用终态。

        Args:
            task_id (str): 已登记的终端任务编号。
            exit_code (int | None): 实际进程退出码，无法确定时为空。
        """
        db = await self._get_db()
        async with self._write_lock:
            try:
                await db.execute("UPDATE task_records SET exit_code=? WHERE task_id=? AND status=?",
                    (exit_code, task_id, TaskStatus.RUNNING.value))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def finish_once(
        self, task_id: str, status: TaskStatus, exit_code: int | None = None, *,
        result: str | None = None, error: str | None = None,
        notify: bool = True, end_reason: str | None = None,
    ) -> bool:
        """条件更新首次任务终态，并在同一事务插入唯一终态通知。

        仅条件更新成功时调用 Inbox 的通知写入函数；该函数不单独提交。写锁
        覆盖整笔事务，失败或取消时回滚任务和通知，成功提交后才向调用方返回。

        Args:
            task_id (str): 受管进程任务标识。
            status (TaskStatus): 调用方确认的任务终态，不允许使用 running。
            exit_code (int | None): 进程退出码，状态未知时为空。
            result (str | None): 任务正常返回的结果正文，无正文时为空。
            error (str | None): 执行或清理失败的错误摘要。

        Returns:
            bool: 首次终态与通知均已提交时为真，重复或不存在的记录为假。

        Raises:
            ValueError: 终态不能是 running。
        """
        # 1. 只接受终态；同一连接的写锁覆盖整个条件更新和通知事务。
        if status == TaskStatus.RUNNING:
            raise ValueError("终态不能是 running")
        db = await self._get_db()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                # 2. 仅 running 记录可迁移，重复终态回调不产生第二条通知。
                cursor = await db.execute(
                    "UPDATE task_records SET status = ?, exit_code = COALESCE(?, exit_code), ended_at = ?, result = ?, error = ?, end_reason = ? "
                    "WHERE task_id = ? AND status = ?",
                    (status.value if isinstance(status, TaskStatus) else status,
                     exit_code, self._now(), result, error, end_reason, task_id, TaskStatus.RUNNING.value),
                )
                if cursor.rowcount != 1:
                    await db.commit()
                    return False
                row = (await db.execute_fetchall(
                    "SELECT session_id, title, tool_name, output_ref, notify_agent, exit_code, subagent_id, notification_context_id, notification_scope, kind FROM task_records WHERE task_id = ?",
                    (task_id,),
                ))[0]
                session_id, title, tool_name, output_ref, notify_agent, saved_exit_code, subagent_id, recipient, scope, kind = row
                scope = AgentStorageScope(scope)
                if scope == AgentStorageScope.SUBAGENT:
                    notify = notify and await subagent_available_in_transaction(db, session_id, recipient)
                # 3. 静默任务只保存终态；需要通知时使用同一事务写入唯一结果。
                if notify_agent and notify:
                    await append_task_terminal_in_transaction(
                        db, session_id=recipient or session_id, task_id=task_id, task_name=title,
                        status=status.value if isinstance(status, TaskStatus) else status, result=result or "", error=error,
                        tool_name=tool_name, exit_code=saved_exit_code,
                        output_ref=f"{TASK_OUTPUT_URI_PREFIX}{output_ref}" if output_ref else None,
                        storage_scope=scope,
                    )
                if kind == TaskRecordKind.SUBAGENT and end_reason != TurnInterruptReason.STEER:
                    await append_interaction_in_transaction(db, subagent_id, SubagentInteractionKind.FINAL, result or error or "", task_id)
                await db.commit()
                return True
            # 4. 任一数据库操作失败或任务取消都回滚整笔交付。
            except BaseException:
                await db.rollback()
                raise

    async def get(self, task_id: str) -> TaskRecord | None:
        """按任务标识查询持久记录。

        Args:
            task_id (str): 受管进程任务标识。

        Returns:
            TaskRecord | None: 持久任务记录，任务不存在时为空。
        """
        db = await self._get_db()
        # 1. 查询与写事务串行，终态和通知提交前不向外暴露中间状态。
        # 1.1. 在线程内一次取完结果，父 worker 取消时也不会遗留占读锁的游标。
        async with self._write_lock:
            rows = await db.execute_fetchall(
                "SELECT task_id, session_id, command, status, pid, exit_code, "
                "output_path, created_at, ended_at, origin_run_id, origin_tool_call_id, "
                "output_ref, background, tool_name, name, title, result, error, plugin_name, notify_agent, subagent_id, notification_context_id, notification_scope, kind, end_reason FROM task_records WHERE task_id = ?",
                (task_id,),
            )
            return self._from_row(rows[0]) if rows else None

    async def list(self, session_id: str) -> list[TaskRecord]:
        """按创建时间倒序查询会话任务记录。

        Args:
            session_id (str): 共享 Ayaya 会话标识。

        Returns:
            list[TaskRecord]: 该会话按创建时间倒序排列的持久任务记录。
        """
        db = await self._get_db()
        # 1. 查询与写事务串行，终态和通知提交前不向外暴露中间状态。
        async with self._write_lock:
            rows = await db.execute_fetchall(
                "SELECT task_id, session_id, command, status, pid, exit_code, "
                "output_path, created_at, ended_at, origin_run_id, origin_tool_call_id, "
                "output_ref, background, tool_name, name, title, result, error, plugin_name, notify_agent, subagent_id, notification_context_id, notification_scope, kind, end_reason FROM task_records "
                "WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            )
            return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: tuple) -> TaskRecord:
        """还原合法的 SQLite 任务状态与布尔标记，不修改原始记录。

        Args:
            row (tuple): 按任务查询固定列顺序取得的数据库行。

        Returns:
            TaskRecord: 状态为 TaskStatus 的不可变记录。
        """
        values = list(row)
        values[3] = TaskStatus(values[3])
        values[12] = bool(values[12])
        values[19] = bool(values[19])
        values[22] = AgentStorageScope(values[22])
        values[23] = TaskRecordKind(values[23])
        return TaskRecord(*values)

    async def close(self) -> None:
        """关闭 DAO 持有的 SQLite 连接。"""
        if self._db is not None:
            await self._db.close()
            self._db = None
