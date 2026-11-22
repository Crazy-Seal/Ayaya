"""验证全局观察连接和按需快照的公开行为。"""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.crud.sqlite_factory import SqliteFactory
from app.dependencies import get_runtime_scheduler, get_interaction_event_hub
from app.http_errors import install_http_error_handlers
from app.interaction.interaction_contract import InteractionEvent, InteractionStreamReady
from app.interaction.interaction_event_hub import InteractionEventHub
from app.routes.agent import observe_runtime, router
from app.agent.state.messages import ToolCall
from tests.runtime_event_source import EventSource


def test_global_observers_receive_all_sessions_with_independent_buffers() -> None:
    """各观察连接接收所有会话，慢连接溢出后快速连接继续工作。"""
    async def scenario() -> None:
        """让快速连接持续读取，同时保留慢连接积压。"""
        hub = InteractionEventHub(queue_size=2)
        fast, slow = await hub.register(), await hub.register()
        assert fast is not None and slow is not None
        first = hub.publish(InteractionEvent("A", "text_chunk", {"value": "A"}))
        assert await fast.queue.get() == first
        second = hub.publish(InteractionEvent("B", "text_chunk", {"value": "B"}))
        assert await fast.queue.get() == second
        third = hub.publish(InteractionEvent("A", "run_completed", {}))
        assert await fast.queue.get() == third
        assert await slow.queue.get() is None
        assert (first.event_sequence, second.event_sequence, third.event_sequence) == (1, 1, 2)
        hub.unsubscribe(fast)
        assert await fast.queue.get() is None
        await hub.close()
    asyncio.run(scenario())


def test_global_stream_interleaves_sessions_and_includes_future_sessions() -> None:
    """A 的较大序号与新会话的较小序号均能通过同一连接。"""
    async def scenario() -> None:
        """登记全局流后发布三组独立会话事件。"""
        hub = InteractionEventHub()
        stream = observe_runtime(hub)
        try:
            ready = await anext(stream)
            assert isinstance(ready, InteractionStreamReady)
            for index in range(20):
                hub.publish(InteractionEvent("A", "text_chunk", {"value": str(index)}))
            hub.publish(InteractionEvent("B", "text_chunk", {"value": "B"}))
            hub.publish(InteractionEvent("new-session", "text_chunk", {"value": "新会话"}))
            events = [await asyncio.wait_for(anext(stream), 1) for _ in range(22)]
            assert [(event.session_id, event.event_sequence) for event in events if isinstance(event, InteractionEvent)][-3:] == [
                ("A", 20), ("B", 1), ("new-session", 1),
            ]
        finally:
            await stream.aclose()
            await hub.close()
    asyncio.run(scenario())




def test_snapshot_http_preserves_approval_arguments_without_publishing_events() -> None:
    """按需快照能序列化审批参数，查询不会推进事件编号或要求全局订阅。"""

    hub = InteractionEventHub()
    call = ToolCall("call", "exec_command", {"cmd": "echo test", "options": {"values": [1, 2]}})

    class Source(EventSource):
        """在真实快照结构中加入不可变工具参数。"""

        async def snapshot(self, session_id: str) -> InteractionEvent:
            """返回已等待人工审批的会话状态。

            Args:
                session_id (str): 当前页面请求展示的会话编号。

            Returns:
                InteractionEvent: 带有不可变工具参数的审批快照。
            """
            snapshot = await super().snapshot(session_id)
            snapshot.payload["interrupt"] = {"type": "authorization_request", "request_id": "request", "data": {"arguments": call.args}}
            return snapshot

    app = FastAPI()
    app.include_router(router)
    install_http_error_handlers(app)
    app.dependency_overrides[get_interaction_event_hub] = lambda: hub
    app.dependency_overrides[get_runtime_scheduler] = lambda: Source(hub)
    before = hub.watermark("A")
    with TestClient(app) as client:
        response = client.get("/agent/snapshot/A")
        assert response.status_code == 200
        assert response.json()["payload"]["interrupt"]["data"]["arguments"] == {"cmd": "echo test", "options": {"values": [1, 2]}}
        assert client.get("/agent/events/A").status_code == 404
        assert client.get("/agent/channels/A").status_code == 404
        assert client.put("/agent/channels/A/desktop", json={"enabled": True}).status_code == 404
    assert hub.watermark("A") == before
