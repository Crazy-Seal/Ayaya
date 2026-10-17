"""按渠道发布会话视图，慢订阅者不会阻塞 Agent。"""

import asyncio
import inspect
from collections import defaultdict
from dataclasses import replace
from typing import AsyncIterator, Awaitable, Callable
from uuid import uuid4

from app.schemas.runtime_types import (
    DESKTOP_CHANNEL, InteractionEventKind,
)
from app.interaction.interaction_contract import ChannelDescriptor, InteractionEvent
from app.services.channel_subscription_service import ChannelSubscriptionService


INTERACTION_SUBSCRIBER_QUEUE_SIZE = 256


class InteractionEventHub:
    """按会话来源路由观察事件，以有界队列隔离慢订阅者。

    Attributes:
        _snapshot (Callable[[str, str], dict | Awaitable[dict]]): 按会话和接收渠道生成当前视图的回调。
        _queue_size (int): 每个订阅者的有界队列容量。
        _subscriptions (ChannelSubscriptionService): 可替换的会话订阅配置服务。
        _session_enabled (dict[tuple[str, str], bool]): 每会话渠道启用状态缓存。
        _epoch (str): 本次后端启动的事件序号命名空间。
        _channels (dict[str, ChannelDescriptor]): 当前注册的渠道描述集合。
        _subscribers (dict[tuple[str, str], set[asyncio.Queue[InteractionEvent | None]]]): 接收增量的有界订阅队列集合。
        _sequences (dict[str, int]): 按会话递增的界面事件水位。
        _locks (dict[str, asyncio.Lock]): 按会话序列化发布或执行状态迁移的短锁。
        _closed (bool): 已关闭时拒绝新订阅，不再投递执行视图。
    """
    def __init__(
        self,
        snapshot: Callable[[str, str], dict | Awaitable[dict]] | None = None,
        *,
        queue_size: int = INTERACTION_SUBSCRIBER_QUEUE_SIZE,
        subscriptions: ChannelSubscriptionService | None = None,
    ) -> None:
        """创建进程发布 epoch、桌面渠道和会话订阅队列。

        Args:
            snapshot (Callable[[str, str], dict | Awaitable[dict]] | None): 按会话和接收渠道生成当前视图的回调。
            queue_size (int): 每个订阅者的有界队列容量。
            subscriptions (ChannelSubscriptionService | None): 可替换的会话订阅配置服务。
        """
        self._snapshot = snapshot or (lambda _session, _channel: {})
        self._queue_size = queue_size
        self._subscriptions = subscriptions or ChannelSubscriptionService()
        self._session_enabled: dict[tuple[str, str], bool] = {}
        self._epoch = uuid4().hex
        self._channels: dict[str, ChannelDescriptor] = {
            DESKTOP_CHANNEL: ChannelDescriptor(DESKTOP_CHANNEL, "桌面", ready=True),
        }
        self._subscribers: dict[tuple[str, str], set[asyncio.Queue[InteractionEvent | None]]] = defaultdict(set)
        self._sequences: dict[str, int] = defaultdict(int)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._closed = False

    async def close(self) -> None:
        """幂等结束所有订阅，等待正在生成的快照离开发布屏障。"""
        # 1. 先禁止新登记，随后逐会话等待发布或快照完成。
        self._closed = True
        for session_id, lock in tuple(self._locks.items()):
            async with lock:
                for (session, _channel), subscribers in tuple(self._subscribers.items()):
                    if session != session_id:
                        continue
                    for queue in tuple(subscribers):
                        while not queue.empty():
                            queue.get_nowait()
                        queue.put_nowait(None)
                    subscribers.clear()

    def set_snapshot_provider(
        self, snapshot: Callable[[str, str], dict | Awaitable[dict]]
    ) -> None:
        """装入 Scheduler 的快照回调，EventHub 不反向持有 Adapter。

        Args:
            snapshot (Callable[[str, str], dict | Awaitable[dict]]): 按会话和接收渠道生成当前视图的回调。
        """
        self._snapshot = snapshot

    def register_channel(self, channel: str, display_name: str | None = None) -> None:
        """登记渠道描述，不注册渠道实例或后台执行任务。

        Args:
            channel (str): 已注册渠道标识。
            display_name (str | None): 渠道在设置页的展示名称。

        Raises:
            ValueError: 渠道已注册或名称为空。
        """
        if not channel or channel in self._channels:
            raise ValueError("渠道已注册或名称为空")
        self._channels[channel] = ChannelDescriptor(channel, display_name or channel)

    def channels(self) -> tuple[ChannelDescriptor, ...]:
        """返回当前渠道描述的不可变集合。

        Returns:
            tuple[ChannelDescriptor, ...]: 已注册的渠道描述，不包含 Adapter 实例。
        """
        return tuple(self._channels.values())

    def set_enabled(self, channel: str, enabled: bool) -> None:
        """修改渠道默认启用值，测试可用；会话持久配置通过另一入口修改。

        Args:
            channel (str): 已注册渠道标识。
            enabled (bool): 渠道是否参与该会话的双向互通。

        Raises:
            ValueError: 桌面渠道不能关闭。
        """
        if channel == DESKTOP_CHANNEL and not enabled:
            raise ValueError("桌面渠道不能关闭")
        descriptor = self._channels[channel]
        self._channels[channel] = replace(descriptor, enabled=enabled)

    def validate_source(self, channel: str) -> None:
        """拒绝未注册渠道，会话是否启用另由 channel_enabled 检查。

        Args:
            channel (str): 已注册渠道标识。

        Raises:
            ValueError: 交互渠道未注册或已关闭。
        """
        descriptor = self._channels.get(channel)
        if descriptor is None:
            raise ValueError("交互渠道未注册或已关闭")

    async def channel_enabled(self, session_id: str, channel: str) -> bool:
        """读取并缓存指定会话的持久渠道启用设置。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            channel (str): 已注册渠道标识。

        Returns:
            bool: 该渠道在指定会话是否允许提交命令及接收事件。
        """
        self.validate_source(channel)
        key = (session_id, channel)
        # 1. 首次访问读取持久订阅，此后使用缓存；配置写入口同步更新同一缓存。
        if key not in self._session_enabled:
            self._session_enabled[key] = await self._subscriptions.is_enabled(
                session_id, channel, self.channels(),
            )
        return self._session_enabled[key]

    async def set_session_enabled(self, session_id: str, channel: str, enabled: bool) -> None:
        """持久更新渠道订阅并同步进程缓存。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            channel (str): 已注册渠道标识。
            enabled (bool): 渠道是否参与该会话的双向互通。
        """
        # 1. 先确认配置已经持久化，再更新发布路径读取的内存启用状态。
        await self._subscriptions.set_enabled(session_id, channel, enabled, self.channels())
        self._session_enabled[(session_id, channel)] = enabled

    async def list_subscriptions(self, session_id: str) -> list[dict]:
        """合并注册表与持久配置，返回当前会话订阅列表。

        Args:
            session_id (str): 共享 Ayaya 会话标识。

        Returns:
            list[dict]: 渠道描述与会话持久启用设置合并后的展示列表。
        """
        return await self._subscriptions.list(session_id, self.channels())

    async def publish(
        self, event: InteractionEvent, *,
        update_snapshot_state: Callable[[], None] | None = None,
    ) -> InteractionEvent:
        """在会话发布锁内分配序号，只投递桌面及原来源，队列满时断开慢订阅者。

        Args:
            event (InteractionEvent): 待发布或消费的观察事件。
            update_snapshot_state (Callable[[], None] | None): Scheduler 提供的同步回调；取得发布锁后更新快照使用的内存状态，不得等待、访问数据库或取得其他锁。

        Returns:
            InteractionEvent: 已填入进程 epoch 和会话序号的事件，未连接渠道不重放。
        """
        # 1. 同一会话串行分配水位，持有的只是一段发布屏障。
        async with self._locks[event.session_id]:
            if self._closed:
                return event
            # 1.1. 运行状态和事件编号一起前进，快照不能提前显示尚未发布的阶段变化。
            if update_snapshot_state is not None:
                update_snapshot_state()
            self._sequences[event.session_id] += 1
            published = replace(
                event,
                stream_epoch=self._epoch,
                event_sequence=self._sequences[event.session_id],
            )
            # 2. 桌面始终观察共享会话，外部事件只再发给原来源，不向其他渠道广播。
            recipients = {DESKTOP_CHANNEL}
            if event.origin_channel and event.origin_channel != DESKTOP_CHANNEL:
                recipients.add(event.origin_channel)
            for channel in recipients:
                descriptor = self._channels.get(channel)
                if descriptor is None or not self._session_enabled.get(
                    (event.session_id, channel), descriptor.enabled
                ):
                    continue
                for queue in tuple(self._subscribers.get((event.session_id, channel), ())):
                    try:
                        queue.put_nowait(published)
                    # 3. 队列满时清空并放入结束标记，Agent 不等待慢消费者；重连用快照恢复。
                    except asyncio.QueueFull:
                        self._subscribers[(event.session_id, channel)].discard(queue)
                        while not queue.empty():
                            queue.get_nowait()
                        queue.put_nowait(None)
            return published

    async def subscribe(self, session_id: str, channel: str) -> AsyncIterator[InteractionEvent]:
        """在同一发布屏障内登记队列并取得快照水位，随后逐项消费增量。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            channel (str): 已注册渠道标识。

        Yields:
            InteractionEvent: 先提供 session_snapshot，再提供本渠道可见增量。

        Raises:
            ValueError: 该会话未订阅此渠道。
        """
        # 1. 验证会话渠道订阅，为接收方创建独立有界队列。
        self.validate_source(channel)
        if not await self.channel_enabled(session_id, channel):
            raise ValueError("该会话未订阅此渠道")
        queue: asyncio.Queue[InteractionEvent | None] = asyncio.Queue(self._queue_size)
        key = (session_id, channel)
        try:
            # 2. 清理范围从登记前开始；快照查询失败或连接此时取消，也必须移除队列。
            # 2.1. 与运行状态更新、事件编号分配共用发布锁，查询期间的新事件稍后再发布。
            async with self._locks[session_id]:
                if self._closed:
                    return
                self._subscribers[key].add(queue)
                watermark = self._sequences[session_id]
                payload = self._snapshot(session_id, channel)
                if inspect.isawaitable(payload):
                    payload = await payload
                snapshot = InteractionEvent(
                    session_id=session_id,
                    kind=InteractionEventKind.SESSION_SNAPSHOT,
                    payload=payload,
                    stream_epoch=self._epoch,
                    event_sequence=watermark,
                )
            # 3. 先发送完整当前视图，再逐条发送水位后的可见增量。
            yield snapshot
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        # 4. 只移除订阅队列，断线不触碰会话 worker 或 Agent 状态。
        finally:
            self._subscribers[key].discard(queue)
