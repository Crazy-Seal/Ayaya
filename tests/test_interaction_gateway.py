from tests.runtime_support import runtime_hub, close_test_runtime
from app.routes.agent import observe_runtime
from app.crud.sqlite_factory import SqliteFactory
from tests.runtime_event_source import EventSource
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from app.interaction.interaction_contract import InteractionEvent
from app.interaction.interaction_event_hub import InteractionEventHub
from app.interaction.interaction_contract import InteractionCommand
from app.schemas.runtime_types import CommandReceipt, CommandStatus








def test_slow_subscriber_reconnects_from_snapshot_watermark() -> None:
    """验证慢订阅者断开后可按快照水位恢复。"""
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：慢订阅者断开后可按快照水位恢复。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。

        hub = InteractionEventHub(queue_size=1)
        source = EventSource(hub)
        pass
        stream = observe_runtime(hub)
        initial = await anext(stream)
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert initial.kind == "stream_ready"
        await source.publish(InteractionEvent("s", "text_chunk", {"value": "a"}))
        await source.publish(InteractionEvent("s", "text_chunk", {"value": "b"}))
        try:
            await anext(stream)
        except StopAsyncIteration:
            pass
        else:
            raise AssertionError("慢订阅者应断开并从快照恢复")
        replacement = observe_runtime(hub)
        ready = await anext(replacement)
        snapshot = await source.snapshot("s")
        assert snapshot.kind == "session_snapshot"
        assert snapshot.stream_epoch == ready.stream_epoch == initial.stream_epoch
        assert snapshot.event_sequence == 2
        await replacement.aclose()
        await hub.close()

    asyncio.run(scenario())
