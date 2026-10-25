"""每会话渠道订阅的 SQLite 存取。"""

from __future__ import annotations

import os

import aiosqlite

from app.environment import get_checkpoint_db


CHANNEL_SUBSCRIPTION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS channel_subscriptions (
    session_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    PRIMARY KEY (session_id, channel)
)
"""


class ChannelSubscriptionDao:
    """持久保存每会话每渠道的启用设置，每次操作独立打开和关闭连接。

    Attributes:
        db_path (str | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
    """
    def __init__(self, db_path: str | None = None) -> None:
        """保存订阅配置的数据库路径。

        Args:
            db_path (str | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
        """
        self.db_path = str(db_path or get_checkpoint_db())

    async def _connect(self) -> aiosqlite.Connection:
        """打开 SQLite 并幂等创建渠道订阅表。

        Returns:
            aiosqlite.Connection: 已初始化渠道配置表的独立连接，调用方必须关闭。
        """
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        # 1. 在当前环境数据库幂等建表，调用方负责在操作 finally 关闭这次连接。
        db = await aiosqlite.connect(self.db_path)
        await db.execute(CHANNEL_SUBSCRIPTION_SCHEMA_SQL)
        await db.commit()
        return db

    async def get(self, session_id: str, channel: str) -> bool | None:
        """读取指定会话渠道的已保存设置，未设置时由服务决定默认值。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            channel (str): 已注册渠道标识。

        Returns:
            bool | None: 已保存的启用值，未设置时为空。
        """
        # 1. 独立连接查询会话渠道配置，未保存设置返回 None；finally 始终关闭连接。
        db = await self._connect()
        try:
            async with db.execute(
                "SELECT enabled FROM channel_subscriptions "
                "WHERE session_id = ? AND channel = ?",
                (session_id, channel),
            ) as cursor:
                row = await cursor.fetchone()
            return bool(row[0]) if row else None
        finally:
            await db.close()

    async def set(self, session_id: str, channel: str, enabled: bool) -> None:
        """按会话渠道主键插入或更新启用状态。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            channel (str): 已注册渠道标识。
            enabled (bool): 渠道是否参与该会话的双向互通。
        """
        # 1. 按会话渠道主键 upsert，提交后关闭连接，其他会话设置不受影响。
        db = await self._connect()
        try:
            await db.execute(
                "INSERT INTO channel_subscriptions (session_id, channel, enabled) "
                "VALUES (?, ?, ?) ON CONFLICT(session_id, channel) "
                "DO UPDATE SET enabled = excluded.enabled",
                (session_id, channel, int(enabled)),
            )
            await db.commit()
        finally:
            await db.close()
