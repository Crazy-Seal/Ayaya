"""为 Hub 的公开读取接口提供可控制事件源，不启动模型或数据库。"""

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol
from dataclasses import replace

from app.interaction.interaction_contract import InteractionEvent
from app.interaction.interaction_event_hub import InteractionEventHub
from app.routes.agent import observe_runtime


class SnapshotSource(Protocol):
    """提供测试客户端恢复当前会话所需的状态查询。"""

    async def snapshot(self, session_id: str) -> InteractionEvent:
        """取得待展示会话的快照。

        Args:
            session_id (str): 测试客户端选择展示的主会话。

        Returns:
            InteractionEvent: 带输出水位的会话快照。
        """
        ...


async def observe_session(session_id: str, hub: InteractionEventHub,
                          source: SnapshotSource) -> AsyncIterator[InteractionEvent]:
    """模拟客户端连接全局流后查询当前快照，再衔接该会话增量。

    Args:
        session_id (str): 测试客户端正在展示的主会话。
        hub (InteractionEventHub): 真实的全局事件分发对象。
        source (SnapshotSource): 调度器或可控快照源。

    Yields:
        InteractionEvent: 当前会话快照和后续可见事件。
    """
    stream = observe_runtime(hub)
    try:
        await anext(stream)
        snapshot = await source.snapshot(session_id)
        yield snapshot
        sequence = snapshot.event_sequence
        async for event in stream:
            if isinstance(event, InteractionEvent) and event.session_id == session_id and event.event_sequence > sequence:
                sequence = event.event_sequence
                yield event
    finally:
        await stream.aclose()


class EventSource:
    """模拟 Scheduler 的状态查询和单消费者输出接口。

    Attributes:
        events (asyncio.Queue): 测试显式产生的输出。
        sequence (int): 已产生事件的编号。
        fail_snapshot (bool): 是否模拟状态查询失败。
        snapshot_entered (asyncio.Event): 状态查询已到达等待位置。
        release_snapshot (asyncio.Event | None): 指定时暂停查询，测试负责放行。
    """

    def __init__(self, hub) -> None:
        """创建空输出，默认状态查询直接成功。"""
        self.hub = hub
        self.fail_snapshot = False
        self.snapshot_entered = asyncio.Event()
        self.release_snapshot = None

    async def publish(self, event: InteractionEvent) -> None:
        """测试生产者放入事件，不调用 Hub。

        Args:
            event (InteractionEvent): 测试预设事件。
        """
        self.hub.publish(event)

    async def snapshot(self, session_id: str) -> InteractionEvent:
        """复制编号后按需等待，让测试插入查询交错或失败。

        Args:
            session_id (str): 当前会话。

        Returns:
            InteractionEvent: 初始空闲状态。

        Raises:
            RuntimeError: 测试要求模拟查询失败。
        """
        epoch, sequence = self.hub.watermark(session_id)
        self.snapshot_entered.set()
        if self.release_snapshot is not None:
            await self.release_snapshot.wait()
        if self.fail_snapshot:
            raise RuntimeError("模拟快照查询失败")
        return InteractionEvent(session_id, "session_snapshot", {
            "phase": "idle", "run_id": None, "interrupt": None,
            "pending_inputs": [], "queued_inputs": [], "tasks": [],
        }, stream_epoch=epoch, event_sequence=sequence)
