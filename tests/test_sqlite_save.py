"""验证共享关键保存按错误性质有限尝试，并在取消后确认实际回滚。"""

import asyncio
from pathlib import Path
import sqlite3

import aiosqlite
import pytest

from app.crud.sqlite_save import SqliteRollbackError, run_sqlite_save, sqlite_transaction


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY_SNAPSHOT])
@pytest.mark.parametrize("failures", [0, 3, 4])
def test_retryable_save_reuses_material_after_confirmed_rollback(
    tmp_path: Path, code: int, failures: int,
) -> None:
    """首次或第四次成功只留一条资料，四次失败保留原始错误且整笔回滚。

    Args:
        tmp_path (Path): 隔离 SQLite 文件目录。
        code (int): SQLite 基础或扩展的暂时占用错误码。
        failures (int): 前几次事务在写入后明确失败。
    """
    async def scenario() -> None:
        """每次写入同一固定编号，上一笔回滚后才允许重新尝试。"""
        async with aiosqlite.connect(tmp_path / "save.sqlite3") as db:
            await db.execute("CREATE TABLE materials(id TEXT PRIMARY KEY, body TEXT)")
            await db.commit()
            attempts = 0
            failure = sqlite3.OperationalError("暂时占用")
            failure.sqlite_errorcode = code
            material = ("fixed-id", "同一份结果")

            async def save() -> str:
                """复用业务准备的编号和正文保存一笔资料。

                Returns:
                    str: 本次确认保存的固定编号。
                """
                nonlocal attempts
                attempts += 1
                async with sqlite_transaction(db):
                    await db.execute("INSERT INTO materials VALUES (?, ?)", material)
                    if attempts <= failures:
                        raise failure
                    return material[0]

            if failures == 4:
                with pytest.raises(sqlite3.OperationalError) as raised:
                    await run_sqlite_save(save, timeout_seconds=2)
                assert raised.value is failure
            else:
                assert await run_sqlite_save(save, timeout_seconds=2) == material[0]
            assert attempts == min(failures + 1, 4)
            rows = await db.execute_fetchall("SELECT id, body FROM materials")
            assert rows == ([] if failures == 4 else [material])

    asyncio.run(scenario())


def test_failed_connection_close_has_finite_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事务回滚已明确失败时，故障连接关闭超时后仍向业务层返回错误。

    Args:
        tmp_path (Path): 隔离 SQLite 文件目录。
        monkeypatch (pytest.MonkeyPatch): 缩短故障连接关闭预算并阻塞实际关闭。
    """
    async def scenario() -> None:
        """使用可释放屏障延迟关闭，核对有限返回和迟到关闭完成。"""
        db = await aiosqlite.connect(tmp_path / "save.sqlite3")
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_close = db.close
        attempts = 0

        async def rollback() -> None:
            """报告回滚失败。

            Raises:
                sqlite3.OperationalError: 本次回滚实际失败。
            """
            raise sqlite3.OperationalError("rollback failed")

        async def close() -> None:
            """模拟收到取消后仍在结束的实际连接关闭。"""
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            await original_close()
            closed.set()

        async def save() -> None:
            """启动一笔明确失败的事务。"""
            nonlocal attempts
            attempts += 1
            async with sqlite_transaction(db):
                raise sqlite3.OperationalError("write failed")

        monkeypatch.setattr(db, "rollback", rollback)
        monkeypatch.setattr(db, "close", close)
        monkeypatch.setattr("app.crud.sqlite_save.FAILED_CONNECTION_CLOSE_TIMEOUT_SECONDS", 0.05)
        saving = asyncio.create_task(run_sqlite_save(save, timeout_seconds=2))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            done, _ = await asyncio.wait({saving}, timeout=0.5)
            assert done and not closed.is_set()
            with pytest.raises(SqliteRollbackError):
                await saving
            assert attempts == 1
        finally:
            release.set()
            await asyncio.gather(saving, return_exceptions=True)
            await asyncio.wait_for(closed.wait(), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("code", [sqlite3.SQLITE_IOERR, None])
def test_nonretryable_storage_error_is_returned_once(tmp_path: Path, code: int | None) -> None:
    """真实事务中出现不可重试错误时立即返回原始原因并回滚写入。

    Args:
        tmp_path (Path): 隔离 SQLite 文件目录。
        code (int | None): I/O 错误或只有锁定字样、缺少错误码的异常。
    """
    async def scenario() -> None:
        """确认错误码分类，异常正文里的锁定字样只作为错误说明。"""
        async with aiosqlite.connect(tmp_path / "save.sqlite3") as db:
            await db.execute("CREATE TABLE materials(body TEXT)")
            await db.commit()
            attempts = 0
            failure = sqlite3.OperationalError("database is locked")
            if code is not None:
                failure.sqlite_errorcode = code

            async def save() -> None:
                """写入后触发本次不可重试错误。"""
                nonlocal attempts
                attempts += 1
                async with sqlite_transaction(db):
                    await db.execute("INSERT INTO materials VALUES ('结果')")
                    raise failure

            with pytest.raises(sqlite3.OperationalError) as raised:
                await run_sqlite_save(save, timeout_seconds=2)
            assert raised.value is failure and attempts == 1
            assert await db.execute_fetchall("SELECT * FROM materials") == []

    asyncio.run(scenario())


def test_repeated_cancellation_waits_for_rollback_and_returns_storage_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回滚确认期间反复取消，保存仍等待回滚并返回原始存储错误。

    Args:
        tmp_path (Path): 隔离 SQLite 文件目录。
        monkeypatch (pytest.MonkeyPatch): 给实际回滚设置可释放屏障。
    """
    async def scenario() -> None:
        """写入失败后阻塞回滚，重复取消和超预算均先等待实际确认。"""
        async with aiosqlite.connect(tmp_path / "save.sqlite3") as db:
            await db.execute("CREATE TABLE materials(body TEXT)")
            await db.commit()
            entered, release = asyncio.Event(), asyncio.Event()
            failure = sqlite3.OperationalError("disk I/O error")
            original_rollback = db.rollback
            attempts = 0

            async def rollback() -> None:
                """放行后实际回滚当前事务。"""
                entered.set()
                await release.wait()
                await original_rollback()

            async def save() -> None:
                """写入一次业务材料后报告原始存储错误。"""
                nonlocal attempts
                attempts += 1
                async with sqlite_transaction(db):
                    await db.execute("INSERT INTO materials VALUES ('结果')")
                    raise failure

            monkeypatch.setattr(db, "rollback", rollback)
            saving = asyncio.create_task(run_sqlite_save(save, timeout_seconds=0.05))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                done, _ = await asyncio.wait({saving}, timeout=0.1)
                assert not done
                for _ in range(3):
                    saving.cancel()
                    await asyncio.sleep(0)
                assert not saving.done()
                release.set()
                with pytest.raises(sqlite3.OperationalError) as raised:
                    await saving
                assert raised.value is failure and attempts == 1
                assert await db.execute_fetchall("SELECT * FROM materials") == []
            finally:
                release.set()
                await asyncio.gather(saving, return_exceptions=True)

    asyncio.run(scenario())
