"""统一本地 SQLite 连接配置，连接和业务表由实际使用者拥有。"""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from enum import StrEnum
from pathlib import Path
import sqlite3
import logging

import aiosqlite

from app.environment import get_checkpoint_db
from app.agent.utils.infra.bounded_operation import close_resources


SQLITE_LOCK_TIMEOUT_SECONDS = 0.1
SQLITE_SAVE_TIMEOUT_SECONDS = 5.0
logger = logging.getLogger(__name__)


class SqliteDatabase(StrEnum):
    """标识应用共享的检查点、历史和运行记录数据库。"""

    MAIN = "main"


class SqliteFactory:
    """提供已配置连接，将 schema、事务和连接关闭交给 DAO／Store。

    Attributes:
        main_path (Path): 当前应用或隔离测试使用的共享数据库路径。
    """

    def __init__(self, main_path: str | Path | None = None) -> None:
        """绑定当前环境的数据库位置，构造时保持存储未打开。

        Args:
            main_path (str | Path | None): 隔离实例覆盖路径，省略时读取环境路径。
        """
        self.main_path = Path(main_path) if main_path is not None else get_checkpoint_db()

    def path(self, db_name: SqliteDatabase | str | Path = SqliteDatabase.MAIN) -> Path:
        """解析共享库或记忆配置指定的独立数据库。

        Args:
            db_name (SqliteDatabase | str | Path): 共享库标识或独立库路径。

        Returns:
            Path: 本次连接使用的文件位置。
        """
        return self.main_path if db_name == SqliteDatabase.MAIN else Path(db_name)

    async def connect(self, db_name: SqliteDatabase | str | Path = SqliteDatabase.MAIN) -> aiosqlite.Connection:
        """打开并配置连接，配置失败时释放尚未交付的连接。

        Args:
            db_name (SqliteDatabase | str | Path): 共享库标识或独立库路径。

        Returns:
            aiosqlite.Connection: 由调用者持有和关闭的连接。
        """
        path = self.path(db_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(str(path), timeout=SQLITE_LOCK_TIMEOUT_SECONDS)
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            return db
        except BaseException:
            await close_resources(((str(path), db.close),), logger=logger)
            raise

    def connect_sync(self, db_name: SqliteDatabase | str | Path = SqliteDatabase.MAIN) -> sqlite3.Connection:
        """为同步建表提供相同连接配置，调用者负责提交和关闭。

        Args:
            db_name (SqliteDatabase | str | Path): 共享库标识或独立库路径。

        Returns:
            sqlite3.Connection: 已配置的同步连接。
        """
        path = self.path(db_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), timeout=SQLITE_LOCK_TIMEOUT_SECONDS)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            return db
        except BaseException:
            db.close()
            raise

    @asynccontextmanager
    async def connection(self, db_name: SqliteDatabase | str | Path = SqliteDatabase.MAIN) -> AsyncIterator[aiosqlite.Connection]:
        """让单次查询的 Store 在离开查询范围时释放自身连接。

        Args:
            db_name (SqliteDatabase | str | Path): 共享库标识或独立库路径。

        Yields:
            aiosqlite.Connection: 当前查询独占的已配置连接。
        """
        db = await self.connect(db_name)
        try:
            yield db
        finally:
            await close_resources(((str(self.path(db_name)), db.close),), logger=logger)
