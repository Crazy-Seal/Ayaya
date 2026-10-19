"""统一保存渠道描述，读取并缓存每会话的持久启用配置。"""

from dataclasses import replace

from app.crud.channel_subscription_dao import ChannelSubscriptionDao
from app.interaction.interaction_contract import ChannelDescriptor
from app.schemas.runtime_types import DESKTOP_CHANNEL


class ChannelSubscriptionService:
    """向命令入口和事件分发提供同一份渠道配置。

    Attributes:
        _dao (ChannelSubscriptionDao): 持久配置访问对象。
        _channels (dict[str, ChannelDescriptor]): 已注册渠道描述。
        _saved (dict[tuple[str, str], bool | None]): 持久覆盖值缓存；None 表示采用默认值。
    """

    def __init__(self, db_path: str | None = None) -> None:
        """创建访问对象并登记桌面，不读取数据库。

        Args:
            db_path (str | None): 测试可替换的数据库路径。
        """
        self._dao = ChannelSubscriptionDao(db_path)
        self._channels = {DESKTOP_CHANNEL: ChannelDescriptor(DESKTOP_CHANNEL, "桌面", ready=True)}
        self._saved: dict[tuple[str, str], bool | None] = {}

    def register_channel(
        self, channel: str, display_name: str | None = None, *, enabled: bool = True, ready: bool = False,
    ) -> None:
        """登记描述，不创建 Adapter 或外部连接。

        Args:
            channel (str): 唯一标识。
            display_name (str | None): 设置页名称，缺省采用标识。
            enabled (bool): 未保存会话设置时的默认值。
            ready (bool): 保留的展示状态，不进行连接探测。

        Raises:
            ValueError: 标识为空或已注册。
        """
        if not channel or channel in self._channels:
            raise ValueError("渠道已注册或名称为空")
        self._channels[channel] = ChannelDescriptor(channel, display_name or channel, enabled, ready)

    def channels(self) -> tuple[ChannelDescriptor, ...]:
        """取得已注册描述。

        Returns:
            tuple[ChannelDescriptor, ...]: 当前描述集合。
        """
        return tuple(self._channels.values())

    def validate_source(self, channel: str) -> None:
        """检查注册，启用值另由 is_enabled 检查。

        Args:
            channel (str): 待检查标识。

        Raises:
            ValueError: 标识未注册。
        """
        if channel not in self._channels:
            raise ValueError("交互渠道未注册或已关闭")

    def set_default_enabled(self, channel: str, enabled: bool) -> None:
        """修改注册默认值，已有持久覆盖值保持有效。

        Args:
            channel (str): 已注册标识。
            enabled (bool): 新默认值。

        Raises:
            ValueError: 未注册或试图禁用桌面。
        """
        self.validate_source(channel)
        if channel == DESKTOP_CHANNEL and not enabled:
            raise ValueError("桌面渠道不能关闭")
        self._channels[channel] = replace(self._channels[channel], enabled=enabled)

    def enabled_for_delivery(self, session_id: str, channel: str) -> bool:
        """分发时同步读取配置，订阅入口已加载覆盖值。

        Args:
            session_id (str): 会话编号。
            channel (str): 接收渠道。

        Returns:
            bool: 按覆盖值或默认值启用，未知渠道为 False。
        """
        descriptor = self._channels.get(channel)
        if descriptor is None:
            return False
        if channel == DESKTOP_CHANNEL:
            return True
        saved = self._saved.get((session_id, channel))
        return descriptor.enabled if saved is None else saved

    async def is_enabled(self, session_id: str, channel: str) -> bool:
        """首次读取持久设置，此后与分发共用缓存。

        Args:
            session_id (str): 会话编号。
            channel (str): 渠道标识。

        Returns:
            bool: 会话启用值，桌面固定启用。
        """
        if channel not in self._channels:
            return False
        key = (session_id, channel)
        if channel != DESKTOP_CHANNEL and key not in self._saved:
            # 1. 查询期间写入口可能已更新缓存，不能用旧查询覆盖新保存值。
            saved = await self._dao.get(session_id, channel)
            self._saved.setdefault(key, saved)
        return self.enabled_for_delivery(session_id, channel)

    async def set_enabled(self, session_id: str, channel: str, enabled: bool) -> None:
        """保存成功后更新命令和分发共用的缓存。

        Args:
            session_id (str): 会话编号。
            channel (str): 可选渠道。
            enabled (bool): 本会话启用值。

        Raises:
            ValueError: 未注册或试图修改桌面。
        """
        self.validate_source(channel)
        if channel == DESKTOP_CHANNEL:
            raise ValueError("桌面渠道不能修改")
        await self._dao.set(session_id, channel, enabled)
        self._saved[(session_id, channel)] = enabled

    async def list(self, session_id: str) -> list[dict]:
        """读取设置页的描述和会话启用值。

        Args:
            session_id (str): 会话编号。

        Returns:
            list[dict]: 渠道名称、启用值和原有展示状态。
        """
        return [{"channel": item.channel, "display_name": item.display_name,
                 "enabled": await self.is_enabled(session_id, item.channel), "ready": item.ready}
                for item in self.channels()]
