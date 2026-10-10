"""每会话渠道订阅配置；桌面始终启用。"""

from __future__ import annotations

from app.schemas.runtime_types import (
    DESKTOP_CHANNEL,
)
from app.crud.channel_subscription_dao import ChannelSubscriptionDao
from app.services.interaction_contract import ChannelDescriptor


class ChannelSubscriptionService:
    """实现每会话渠道启用规则，桌面永久启用。

    Attributes:
        _dao (ChannelSubscriptionDao): 渠道配置的持久访问对象。
    """
    def __init__(self, db_path: str | None = None) -> None:
        """创建可替换数据库路径的订阅 DAO。

        Args:
            db_path (str | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
        """
        self._dao = ChannelSubscriptionDao(db_path)

    async def is_enabled(
        self, session_id: str, channel: str,
        channels: tuple[ChannelDescriptor, ...],
    ) -> bool:
        """优先读取已保存配置，桌面固定开启，未知渠道不可用。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            channel (str): 已注册渠道标识。
            channels (tuple[ChannelDescriptor, ...]): 当前注册的渠道描述集合。

        Returns:
            bool: 桌面固定为真，可选渠道采用保存值或注册默认值。
        """
        descriptor = next((item for item in channels if item.channel == channel), None)
        if descriptor is None:
            return False
        # 1. 桌面固定启用；可选渠道优先采用持久设置，未保存时使用注册默认值。
        if channel == DESKTOP_CHANNEL:
            return True
        saved = await self._dao.get(session_id, channel)
        return saved if saved is not None else descriptor.enabled

    async def set_enabled(
        self, session_id: str, channel: str, enabled: bool,
        channels: tuple[ChannelDescriptor, ...],
    ) -> None:
        """拒绝修改桌面或未注册渠道，然后持久保存可选渠道设置。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            channel (str): 已注册渠道标识。
            enabled (bool): 渠道是否参与该会话的双向互通。
            channels (tuple[ChannelDescriptor, ...]): 当前注册的渠道描述集合。

        Raises:
            ValueError: 桌面渠道不能修改。
        """
        # 1. 桌面和未知渠道不可修改，验证通过后才交给 DAO 持久保存。
        if channel == DESKTOP_CHANNEL:
            raise ValueError("桌面渠道不能修改")
        if not any(item.channel == channel for item in channels):
            raise ValueError("渠道未注册")
        await self._dao.set(session_id, channel, enabled)

    async def list(
        self, session_id: str, channels: tuple[ChannelDescriptor, ...]
    ) -> list[dict]:
        """合并注册描述与会话配置，返回设置页所需渠道列表。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            channels (tuple[ChannelDescriptor, ...]): 当前注册的渠道描述集合。

        Returns:
            list[dict]: 设置页需要的渠道身份、名称、启用和可用状态。
        """
        return [{
            "channel": item.channel,
            "display_name": item.display_name,
            "enabled": await self.is_enabled(session_id, item.channel, channels),
            "ready": item.ready,
        } for item in channels]
