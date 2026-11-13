from app.crud.sqlite_factory import SqliteFactory
import asyncio

from app.services.channel_subscription_service import ChannelSubscriptionService


def test_subscriptions_are_per_session_and_desktop_is_always_enabled(tmp_path) -> None:
    """验证渠道配置按会话隔离且桌面始终开启。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：渠道配置按会话隔离且桌面始终开启。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        service = ChannelSubscriptionService(SqliteFactory(str(tmp_path / "state.sqlite3")))
        service.register_channel("qq", "QQ", enabled=False)
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert not await service.is_enabled("one", "qq")
        await service.set_enabled("one", "qq", True)
        assert await service.is_enabled("one", "qq")
        assert not await service.is_enabled("two", "qq")
        assert await service.is_enabled("two", "desktop")
        try:
            await service.set_enabled("one", "desktop", False)
        except ValueError:
            pass
        else:
            raise AssertionError("桌面渠道不能关闭")

    asyncio.run(scenario())
