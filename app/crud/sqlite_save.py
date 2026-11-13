"""统一 SQLite 关键保存的有限尝试、实际事务确认和取消保护。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
import sqlite3
from typing import TypeVar

import aiosqlite

from app.agent.utils.infra.bounded_operation import OperationTimeoutError, run_bounded


T = TypeVar("T")
FAILED_CONNECTION_CLOSE_TIMEOUT_SECONDS = 5.0


class _RetryableSqliteSaveError(Exception):
    """本次事务已结束，SQLite 暂时占用允许重新保存相同材料。"""


class SqliteRollbackError(RuntimeError):
    """事务回滚失败，保存调用结束并保留存储错误供业务层报告。"""


async def _confirm(task: asyncio.Task[T]) -> T:
    """等待已开始的提交、回滚或保存尝试，取得实际结果。

    Args:
        task (asyncio.Task[T]): 已开始且必须完成确认的存储操作。

    Returns:
        T: 存储操作的实际结果。

    Raises:
        BaseException: 存储操作实际失败或自身取消。
    """
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


@asynccontextmanager
async def sqlite_transaction(db: aiosqlite.Connection) -> AsyncIterator[None]:
    """按 DAO 指定范围提交关键保存，失败时确认整笔事务回滚。

    DAO 在自身写锁内使用本事务范围，写入任务结果及其通知等关联数据。
    已开始的提交和回滚允许超过保存预算；确认期间的取消先等待实际结果。

    Args:
        db (aiosqlite.Connection): DAO 持有并串行使用的连接。

    Yields:
        None: DAO 执行一次业务 SQL 的事务范围。

    Raises:
        SqliteRollbackError: 回滚实际失败，当前连接的事务结果无法安全重试。
        BaseException: 业务写入、提交或取消的原始错误，回滚已完成。
    """
    current = asyncio.current_task()
    if current is not None and current.cancelling():
        raise asyncio.CancelledError
    try:
        # 1. DAO 将关联写入放入同一事务，开始失败也先结束本次事务。
        await db.execute("BEGIN IMMEDIATE")
        yield
        # 2. 写入完成后确认提交，业务调用者依据实际结果继续。
        await _confirm(asyncio.create_task(db.commit()))
    except BaseException:
        # 3. 写入失败或取消时确认回滚，再允许同一材料进入后续尝试。
        try:
            await _confirm(asyncio.create_task(db.rollback()))
        except BaseException as exc:
            # 3.1. 回滚失败的连接退出使用，后续查询只从重新打开的连接读取已提交事实。
            try:
                await run_bounded(db.close, retryable=(),
                                  timeout_seconds=FAILED_CONNECTION_CLOSE_TIMEOUT_SECONDS)
            except BaseException as close_error:
                raise SqliteRollbackError("SQLite 回滚和连接关闭均失败，已停止重试") from close_error
            raise SqliteRollbackError("SQLite 保存失败且回滚未成功，已停止重试") from exc
        raise


async def run_sqlite_save(
    operation: Callable[[], Awaitable[T]], *, timeout_seconds: float,
) -> T:
    """让 DAO 对已准备的关键保存最多尝试四次，并返回真实事务结果。

    单次操作包含连接准备和由 sqlite_transaction 确认的业务事务。仅明确的
    BUSY／LOCKED 错误允许重试。等锁、尝试和间隔共用预算；超时或取消后
    先等待当前事务实际结束，确认成功返回成功，确认失败保留原始原因。

    Args:
        operation (Callable[[], Awaitable[T]]): 复用同一材料、失败时已结束事务的单次操作。
        timeout_seconds (float): 等锁及后续尝试共享的正数预算，单位为秒。

    Returns:
        T: 已确认的保存结果。

    Raises:
        OperationTimeoutError: 尝试预算耗尽且当前保存已取消并完成回滚。
        asyncio.CancelledError: 调用者取消，当前保存已取消并完成回滚。
        Exception: 不可重试错误或尝试耗尽后的原始存储错误。
    """
    attempt: asyncio.Task[T] | None = None

    async def save_attempt() -> T:
        """执行同一份保存材料，集中判断可安全重试的 SQLite 暂时占用。

        Returns:
            T: 本次保存的实际结果。
        """
        nonlocal attempt
        attempt = asyncio.current_task()
        try:
            return await operation()
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise _RetryableSqliteSaveError(str(exc)) from exc
            raise

    try:
        try:
            return await run_bounded(save_attempt, retryable=(_RetryableSqliteSaveError,),
                                     timeout_seconds=timeout_seconds)
        except (OperationTimeoutError, asyncio.CancelledError):
            # 1. SQLite 工作线程可能仍在写入；当前尝试确认结束后才交回业务层。
            if attempt is not None:
                try:
                    return await _confirm(attempt)
                except asyncio.CancelledError:
                    pass
            raise
    except _RetryableSqliteSaveError as exc:
        assert exc.__cause__ is not None
        raise exc.__cause__
