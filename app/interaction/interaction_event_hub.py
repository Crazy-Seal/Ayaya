"""为应用输出编号并分发到独立连接，网络消费和运行快照由入口组织。"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, replace
from uuid import uuid4

from app.interaction.interaction_contract import InteractionEvent
from app.schemas.runtime_types import DESKTOP_CHANNEL, SessionPhase
from app.services.channel_subscription_service import ChannelSubscriptionService


INTERACTION_SUBSCRIBER_QUEUE_SIZE = 256


@dataclass(frozen=True, slots=True)
class InteractionSubscription:
    """持有一个连接的缓冲，结束连接只影响本连接的观察。"""

    channel: str
    queue: asyncio.Queue[InteractionEvent | None]


class InteractionEventHub:
    """拥有输出 epoch、每会话序号和订阅缓冲，通过同步发布隔离慢网络。

    Attributes:
        _subscriptions (ChannelSubscriptionService): 命令和观察共用的渠道配置。
        _subscribers (dict): 每个渠道各连接的有界缓冲。
        _epoch (str): 本次应用输出编号的命名空间。
        _sequences (dict[str, int]): 每会话最后一次发布的序号。
    """

    def __init__(self, *, subscriptions: ChannelSubscriptionService,
                 queue_size: int = INTERACTION_SUBSCRIBER_QUEUE_SIZE) -> None:
        """保存渠道配置并创建输出身份，运行查询保持在业务入口。

        Args:
            subscriptions (ChannelSubscriptionService): 应用共享的渠道配置。
            queue_size (int): 单连接最多积压的事件数量。
        """
        if queue_size < 1:
            raise ValueError("订阅队列容量必须为正数")
        self._subscriptions, self._queue_size = subscriptions, queue_size
        self._subscribers: dict[str, set[InteractionSubscription]] = defaultdict(set)
        self._epoch = uuid4().hex
        self._sequences: dict[str, int] = defaultdict(int)
        self._closed = False

    def watermark(self, session_id: str) -> tuple[str, int]:
        """在队列采样位置取得与业务摘要对应的输出水位。

        Args:
            session_id (str): 需要采样的父会话编号。

        Returns:
            tuple[str, int]: 输出命名空间和已发布序号。
        """
        return self._epoch, self._sequences[session_id]

    @property
    def stream_epoch(self) -> str:
        """取得全局连接就绪时使用的后端启动身份。

        Returns:
            str: 当前后端的事件编号命名空间。
        """
        return self._epoch

    def publish(self, event: InteractionEvent) -> InteractionEvent:
        """同步编号并投递可见连接，连接溢出时只结束该连接。

        Args:
            event (InteractionEvent): Scheduler 已路由的展示事实。

        Returns:
            InteractionEvent: 已分配 epoch 和序号的事件。
        """
        self._sequences[event.session_id] += 1
        event = replace(event, stream_epoch=self._epoch, event_sequence=self._sequences[event.session_id])
        for channel, subscribers in tuple(self._subscribers.items()):
            if channel not in {DESKTOP_CHANNEL, event.origin_channel}:
                continue
            if not self._subscriptions.enabled_for_delivery(event.session_id, channel):
                continue
            for subscriber in tuple(subscribers):
                try:
                    subscriber.queue.put_nowait(event)
                except asyncio.QueueFull:
                    self.unsubscribe(subscriber)
        return event

    async def register(self, channel: str) -> InteractionSubscription | None:
        """加载渠道配置后登记全局观察连接，开始缓冲各会话事件。

        Args:
            channel (str): 已注册的接收渠道。

        Returns:
            InteractionSubscription | None: 已登记连接，Hub 关闭时为空。
        """
        await self._subscriptions.load_channel(channel)
        if self._closed:
            return None
        subscriber = InteractionSubscription(channel, asyncio.Queue(self._queue_size))
        self._subscribers[channel].add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: InteractionSubscription) -> None:
        """移除本连接并释放积压，唤醒仍在等待的网络消费者。

        Args:
            subscriber (InteractionSubscription): 需要释放的连接。
        """
        key = subscriber.channel
        self._subscribers[key].discard(subscriber)
        if not self._subscribers[key]:
            self._subscribers.pop(key, None)
        while not subscriber.queue.empty():
            subscriber.queue.get_nowait()
        subscriber.queue.put_nowait(None)

    def begin_shutdown(self) -> None:
        """结束网络观察，运行生产者和队列按应用生命周期顺序收尾。"""
        self._closed = True
        for subscribers in tuple(self._subscribers.values()):
            for subscriber in tuple(subscribers):
                self.unsubscribe(subscriber)

    async def close(self) -> None:
        """释放应用拥有的全部连接缓冲，允许重复关闭。"""
        self.begin_shutdown()

    @staticmethod
    def visible_snapshot(event: InteractionEvent, channel: str) -> InteractionEvent:
        """按渠道筛选快照内容，保留与运行采样对应的输出水位。

        Args:
            event (InteractionEvent): 业务查询入口生成的完整快照。
            channel (str): 本连接接收渠道。

        Returns:
            InteractionEvent: 当前渠道可以展示的快照。
        """
        if channel == DESKTOP_CHANNEL:
            return event
        payload = event.payload
        queued = [item for item in payload["queued_inputs"] if item["origin_channel"] == channel]
        if payload["origin_channel"] != channel:
            return replace(event, payload={"phase": SessionPhase.IDLE, "queued_inputs": queued})
        return replace(event, payload={**payload, "queued_inputs": queued, "tasks": [],
            "pending_inputs": [item for item in payload["pending_inputs"] if item["origin_channel"] == channel]})
