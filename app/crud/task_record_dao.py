"""后台任务记录与终态 Inbox 通知的 SQLite 持久化。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import os

import aiosqlite

from app.agent.runtime.session_inbox import append_task_terminal_in_transaction, ensure_inbox_schema
from app.runtime import get_checkpoint_db
from app.schemas.runtime_types import TaskStatus


TASK_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_records (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'stopped', 'orphaned')),
    pid INTEGER,
    exit_code INTEGER,
    output_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT,
    origin_run_id TEXT,
    origin_tool_call_id TEXT,
    output_ref TEXT,
    background INTEGER NOT NULL DEFAULT 1,
    tool_name TEXT
)
"""


@dataclass(frozen=True)
class TaskRecord:
    """保存进程任务的持久状态、输出引用及发起工具调用的关联信息。

    Attributes:
        task_id (str): 受管进程任务标识。
        session_id (str): 共享 Ayaya 会话标识。
        command (str): 原始 Shell 命令或任务说明，供任务详情展示。
        status (TaskStatus): 任务终态或查询状态。
        pid (int | None): 需要加入 Job 或恢复线程的进程标识。
        exit_code (int | None): 进程退出码，状态未知时为空。
        output_path (str): 追加式完整输出文件路径。
        created_at (str): 原消息 UTC 创建时间。
        ended_at (str | None): 任务终态提交时间，运行中为空。
        origin_run_id (str | None): 发起进程任务的运行轮次标识。
        origin_tool_call_id (str | None): 发起任务的工具调用标识。
        output_ref (str | None): 稳定输出引用，当前使用 task_id。
        background (bool): 是否已显式或超时转入后台，前台记录不进入任务入口。
        tool_name (str | None): 创建任务的实际工具名；旧记录为空。
    """
    task_id: str
    session_id: str
    command: str
    status: TaskStatus
    pid: int | None
    exit_code: int | None
    output_path: str
    created_at: str
    ended_at: str | None
    origin_run_id: str | None = None
    origin_tool_call_id: str | None = None
    output_ref: str | None = None
    background: bool = True
    tool_name: str | None = None


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
        """在初始化锁内创建任务及 Inbox 表，幂等补齐工具名、来源及输出字段。

        Returns:
            aiosqlite.Connection: 已初始化任务及 Inbox 的共享实例连接。
        """
        # 1. 初始化锁防止首次并发查询创建多个连接。
        async with self._db_lock:
            if self._db is None:
                os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
                db = await aiosqlite.connect(self.db_path)
                await db.execute(TASK_SCHEMA_SQL)
                columns = {
                    str(row[1]) for row in await db.execute_fetchall("PRAGMA table_info(task_records)")
                }
                # 2. 幂等补齐来源列及稳定输出引用，不删除已有任务记录。
                for column in ("origin_run_id", "origin_tool_call_id", "output_ref", "tool_name"):
                    if column not in columns:
                        await db.execute(f"ALTER TABLE task_records ADD COLUMN {column} TEXT")
                # 2.1. 旧记录无法判定前后台，保留为可见任务；新启动记录写入真实运行模式。
                if "background" not in columns:
                    await db.execute("ALTER TABLE task_records ADD COLUMN background INTEGER NOT NULL DEFAULT 1")
                await db.execute(
                    "UPDATE task_records SET output_ref = task_id WHERE output_ref IS NULL"
                )
                # 3. 任务表与 Inbox 共用连接和数据库，为终态通知原子事务做准备。
                await ensure_inbox_schema(db)
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_records_session_created "
                    "ON task_records(session_id, created_at DESC)"
                )
                await db.commit()
                self._db = db
            return self._db

    async def running_ids(self) -> list[str]:
        """列出仍标记运行中的任务，以便启动时确认其已失去进程所有权。

        Returns:
            list[str]: 后端重启后需要标记 orphaned 的运行记录标识。
        """
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT task_id FROM task_records WHERE status = ?", (TaskStatus.RUNNING.value,),
        )
        return [str(row[0]) for row in rows]

    async def insert_running(
        self, task_id: str, session_id: str, command: str,
        pid: int, output_path: str, *,
        origin_run_id: str | None = None,
        origin_tool_call_id: str | None = None,
        background: bool = True,
        tool_name: str | None = None,
    ) -> None:
        """保存已经创建的进程任务和输出路径，记录发起 run 与工具调用。

        Args:
            task_id (str): 受管进程任务标识。
            session_id (str): 共享 Ayaya 会话标识。
            command (str): 需要执行或展示的 Shell 命令或任务说明。
            pid (int): 需要加入 Job 或恢复线程的进程标识。
            output_path (str): 追加式完整输出文件路径。
            origin_run_id (str | None): 发起进程任务的运行轮次标识。
            origin_tool_call_id (str | None): 发起任务的工具调用标识。
            background (bool): 是否显式后台启动，前台等待时不展示后台入口。
            tool_name (str | None): 创建工具名；兼容旧数据调用时可为空。
        """
        db = await self._get_db()
        # 1. 同一 DAO 写锁覆盖插入与提交，避免和终态通知事务交错。
        async with self._write_lock:
            try:
                await db.execute(
                    "INSERT INTO task_records "
                    "(task_id, session_id, command, status, pid, output_path, created_at, "
                    "origin_run_id, origin_tool_call_id, output_ref, background, tool_name) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, session_id, command, TaskStatus.RUNNING.value, pid, output_path, self._now(),
                     origin_run_id, origin_tool_call_id, task_id, int(background), tool_name),
                )
                await db.commit()
            except BaseException:
                # 1. 取消或写入失败时回滚，后续查询不能读到半笔登记或后台移交。
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

    async def finish_once(
        self, task_id: str, status: TaskStatus, exit_code: int | None,
        result: str = "",
    ) -> bool:
        """条件更新首次任务终态，并在同一事务插入唯一终态通知。

        仅条件更新成功时调用 Inbox 的通知写入函数；该函数不单独提交。写锁
        覆盖整笔事务，失败或取消时回滚任务和通知，成功提交后才向调用方返回。

        Args:
            task_id (str): 受管进程任务标识。
            status (TaskStatus): 调用方确认的任务终态，不允许使用 running。
            exit_code (int | None): 进程退出码，状态未知时为空。
            result (str): 仅兼容旧调用的参数，新通知不包含输出正文。

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
                    "UPDATE task_records SET status = ?, exit_code = ?, ended_at = ? "
                    "WHERE task_id = ? AND status = ?",
                    (status.value if isinstance(status, TaskStatus) else status,
                     exit_code, self._now(), task_id, TaskStatus.RUNNING.value),
                )
                if cursor.rowcount != 1:
                    await db.commit()
                    return False
                row = await db.execute_fetchall(
                    "SELECT session_id, command, tool_name FROM task_records WHERE task_id = ?",
                    (task_id,),
                )
                session_id, command, tool_name = row[0]
                # 3. 将任务事实交给 Inbox 编码，复用当前连接，通知与任务终态一起提交。
                await append_task_terminal_in_transaction(
                    db, session_id=session_id, task_id=task_id, task_name=command,
                    status=status.value if isinstance(status, TaskStatus) else status, result="",
                    tool_name=tool_name, exit_code=exit_code, output_ref=f"task-output://{task_id}",
                )
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
        async with self._write_lock:
            async with db.execute(
                "SELECT task_id, session_id, command, status, pid, exit_code, "
                "output_path, created_at, ended_at, origin_run_id, origin_tool_call_id, "
                "output_ref, background, tool_name FROM task_records WHERE task_id = ?",
                (task_id,),
            ) as cursor:
                row = await cursor.fetchone()
            return self._from_row(row) if row else None

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
                "output_ref, background, tool_name FROM task_records "
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
        values = list(row[:-2])
        values[3] = TaskStatus(values[3])
        return TaskRecord(*values, background=bool(row[-2]), tool_name=row[-1])

    async def close(self) -> None:
        """关闭 DAO 持有的 SQLite 连接。"""
        if self._db is not None:
            await self._db.close()
            self._db = None
