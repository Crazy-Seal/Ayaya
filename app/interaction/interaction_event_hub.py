"""为应用输出编号并分发到独立连接，网络消费和运行快照由入口组织。"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, replace
from uuid import uuid4

from app.interaction.interaction_contract import InteractionEvent


INTERACTION_SUBSCRIBER_QUEUE_SIZE = 256


@dataclass(frozen=True, slots=True)
class InteractionSubscription:
    """持有一个连接的缓冲，结束连接只影响本连接的观察。"""

    queue: asyncio.Queue[InteractionEvent | None]


class InteractionEventHub:
    """拥有输出 epoch、每会话序号和订阅缓冲，通过同步发布隔离慢网络。

    Attributes:
        _subscribers (set[InteractionSubscription]): 各观察连接的独立有界缓冲。
        _epoch (str): 本次应用输出编号的命名空间。
        _sequences (dict[str, int]): 每会话最后一次发布的序号。
    """

    def __init__(self, *, queue_size: int = INTERACTION_SUBSCRIBER_QUEUE_SIZE) -> None:
        """创建事件编号及观察连接集合，运行查询由业务入口负责。

        Args:
            queue_size (int): 单连接最多积压的事件数量。
        """
        if queue_size < 1:
            raise ValueError("订阅队列容量必须为正数")
        self._queue_size = queue_size
        self._subscribers: set[InteractionSubscription] = set()
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
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                self.unsubscribe(subscriber)
        return event

    async def register(self) -> InteractionSubscription | None:
        """登记全局观察连接，开始缓冲各会话事件。

        Returns:
            InteractionSubscription | None: 已登记连接，Hub 关闭时为空。
        """
        if self._closed:
            return None
        subscriber = InteractionSubscription(asyncio.Queue(self._queue_size))
        self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: InteractionSubscription) -> None:
        """移除本连接并释放积压，唤醒仍在等待的网络消费者。

        Args:
            subscriber (InteractionSubscription): 需要释放的连接。
        """
        self._subscribers.discard(subscriber)
        while not subscriber.queue.empty():
            subscriber.queue.get_nowait()
        subscriber.queue.put_nowait(None)

    def begin_shutdown(self) -> None:
        """结束网络观察，运行生产者和队列按应用生命周期顺序收尾。"""
        self._closed = True
        for subscriber in tuple(self._subscribers):
            self.unsubscribe(subscriber)

    async def close(self) -> None:
        """释放应用拥有的全部连接缓冲，允许重复关闭。"""
        self.begin_shutdown()
