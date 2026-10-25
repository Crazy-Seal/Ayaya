"""由一个领取任务读取 Scheduler 输出，分发到各渠道连接。"""

import asyncio
from collections import defaultdict
from dataclasses import replace
import logging
from typing import AsyncIterator

from app.interaction.interaction_contract import InteractionEvent
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.schemas.runtime_types import DESKTOP_CHANNEL, SessionPhase
from app.services.channel_subscription_service import ChannelSubscriptionService


logger = logging.getLogger(__name__)
INTERACTION_SUBSCRIBER_QUEUE_SIZE = 256


class InteractionEventHub:
    """管理连接及慢接收方，不修改会话执行状态。

    Attributes:
        _scheduler (AgentRuntimeScheduler): 状态和临时事件的唯一来源。
        _subscriptions (ChannelSubscriptionService): 与命令入口共享的配置。
        _queue_size (int): 每个连接允许积压的事件数。
        _subscribers (dict): 按会话、渠道保存独立连接队列。
        _reader (asyncio.Task | None): 唯一事件领取任务。
        _closed (bool): 关闭后禁止新连接。
    """

    def __init__(
        self, scheduler: AgentRuntimeScheduler, *, subscriptions: ChannelSubscriptionService,
        queue_size: int = INTERACTION_SUBSCRIBER_QUEUE_SIZE,
    ) -> None:
        """保存依赖，不自动启动任务。

        Args:
            scheduler (AgentRuntimeScheduler): 事件和状态来源。
            subscriptions (ChannelSubscriptionService): 共享配置对象。
            queue_size (int): 单个订阅积压容量，必须为正。

        Raises:
            ValueError: 容量不是正数。
        """
        if queue_size < 1:
            raise ValueError("订阅队列容量必须为正数")
        self._scheduler = scheduler
        self._subscriptions = subscriptions
        self._queue_size = queue_size
        self._subscribers: dict[tuple[str, str], set[asyncio.Queue]] = defaultdict(set)
        self._reader: asyncio.Task | None = None
        self._closed = False

    async def start(self) -> None:
        """应用启动时创建唯一领取任务，重复调用不增加消费者。

        Raises:
            RuntimeError: 已关闭实例不能重启。
        """
        if self._closed:
            raise RuntimeError("已关闭的事件分发不能重新启动")
        if self._reader is None:
            self._reader = asyncio.create_task(self._receive())

    def _end_subscription(self, key: tuple[str, str], queue: asyncio.Queue) -> None:
        """移除连接，将积压替换为结束标记。

        Args:
            key (tuple[str, str]): 会话及接收渠道。
            queue (asyncio.Queue): 本连接的队列。
        """
        self._subscribers[key].discard(queue)
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(None)

    async def _receive(self) -> None:
        """等待输出并同步分发，绝不等待 Adapter 网络发送。"""
        try:
            while True:
                event = await self._scheduler.next_event()
                if event is None:
                    return
                # 1. 领取后的分发段不等待，接收方快慢由其独立队列隔离。
                for key, queues in tuple(self._subscribers.items()):
                    session_id, channel = key
                    if session_id != event.session_id:
                        continue
                    if channel != DESKTOP_CHANNEL and channel != event.origin_channel:
                        continue
                    if not self._subscriptions.enabled_for_delivery(session_id, channel):
                        continue
                    for queue in tuple(queues):
                        try:
                            queue.put_nowait(event)
                        except asyncio.QueueFull:
                            self._end_subscription(key, queue)
        except Exception:
            logger.exception("领取会话事件失败，结束现有订阅")
            raise
        finally:
            # 2. 来源结束或任务取消均结束连接，不改变 Scheduler 的执行。
            self._closed = True
            for key, queues in tuple(self._subscribers.items()):
                for queue in tuple(queues):
                    self._end_subscription(key, queue)

    def begin_shutdown(self) -> None:
        """拒绝新连接并结束现有订阅，让服务器先完成 SSE 请求。

        应用在请求关闭时调用。内部领取任务继续读取 Scheduler 输出，
        等生产者收尾后由 close 统一释放。
        """
        self._closed = True
        for key, queues in tuple(self._subscribers.items()):
            for queue in tuple(queues):
                self._end_subscription(key, queue)

    async def close(self) -> None:
        """结束所有连接，取消并等待领取任务，允许重复清理。"""
        self.begin_shutdown()
        if self._reader is not None:
            if not self._reader.done():
                self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)

    @staticmethod
    def _visible_snapshot(event: InteractionEvent, channel: str) -> InteractionEvent:
        """筛选接收渠道可见的数据，保留原状态编号。

        Args:
            event (InteractionEvent): 未筛选的当前状态。
            channel (str): 接收渠道。

        Returns:
            InteractionEvent: 桌面完整状态或仅本渠道可见的内容。
        """
        if channel == DESKTOP_CHANNEL:
            return event
        payload = event.payload
        queued = [item for item in payload["queued_inputs"] if item["origin_channel"] == channel]
        if payload["origin_channel"] != channel:
            return replace(event, payload={"phase": SessionPhase.IDLE, "queued_inputs": queued})
        return replace(event, payload={**payload, "queued_inputs": queued, "tasks": [],
            "pending_inputs": [item for item in payload["pending_inputs"] if item["origin_channel"] == channel]})

    async def subscribe(self, session_id: str, channel: str) -> AsyncIterator[InteractionEvent]:
        """先登记再查询状态，跳过状态已包含的事件。

        Args:
            session_id (str): 共享会话编号。
            channel (str): 接收渠道。

        Yields:
            InteractionEvent: 首次状态和编号更大的可见事件。

        Raises:
            ValueError: 渠道未注册或本会话未启用。
        """
        self._subscriptions.validate_source(channel)
        if not await self._subscriptions.is_enabled(session_id, channel):
            raise ValueError("该会话未订阅此渠道")
        if self._closed:
            return
        queue: asyncio.Queue = asyncio.Queue(self._queue_size)
        key = (session_id, channel)
        self._subscribers[key].add(queue)
        try:
            # 1. 查询期间的新事件进入已登记队列，查询失败时也由 finally 清理。
            snapshot = await self._scheduler.snapshot(session_id)
            if self._closed or queue not in self._subscribers[key]:
                return
            yield self._visible_snapshot(snapshot, channel)
            # 2. 过滤查询前已发生但尚未分发的事件，仅交付当前状态之后的增量。
            sequence = snapshot.event_sequence
            while True:
                event = await queue.get()
                if event is None:
                    return
                if event.event_sequence <= sequence:
                    continue
                sequence = event.event_sequence
                yield event
        finally:
            self._subscribers[key].discard(queue)
