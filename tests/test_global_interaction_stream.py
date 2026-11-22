"""验证全局观察连接和按需快照的公开行为。"""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.crud.sqlite_factory import SqliteFactory
from app.dependencies import get_runtime_scheduler, get_interaction_event_hub, get_channel_subscription_service
from app.http_errors import install_http_error_handlers
from app.interaction.interaction_contract import InteractionEvent, InteractionStreamReady
from app.interaction.interaction_event_hub import InteractionEventHub
from app.routes.agent import observe_runtime, router
from app.services.channel_subscription_service import ChannelSubscriptionService
from app.agent.state.messages import ToolCall
from tests.runtime_event_source import EventSource


def test_global_stream_interleaves_sessions_and_includes_future_sessions() -> None:
    """A 的较大序号与新会话的较小序号均能通过同一连接。"""
    async def scenario() -> None:
        """登记全局流后发布三组独立会话事件。"""
        hub = InteractionEventHub(subscriptions=ChannelSubscriptionService(SqliteFactory()))
        stream = observe_runtime("desktop", hub)
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


def test_global_channel_loads_saved_settings_and_applies_live_changes(tmp_path) -> None:
    """首次订阅时读取持久禁用设置，写入配置后立即改变投递范围。"""
    async def scenario() -> None:
        """先保存配置，再由全新服务接收多会话事件。"""
        factory = SqliteFactory(str(tmp_path / "channels.sqlite3"))
        writer = ChannelSubscriptionService(factory)
        writer.register_channel("qq")
        await writer.set_enabled("disabled", "qq", False)
        service = ChannelSubscriptionService(factory)
        service.register_channel("qq")
        hub = InteractionEventHub(subscriptions=service)
        stream = observe_runtime("qq", hub)
        try:
            assert isinstance(await anext(stream), InteractionStreamReady)
            hub.publish(InteractionEvent("disabled", "text_chunk", {}, origin_channel="qq"))
            hub.publish(InteractionEvent("other-origin", "text_chunk", {}, origin_channel="telegram"))
            hub.publish(InteractionEvent("enabled", "text_chunk", {}, origin_channel="qq"))
            event = await asyncio.wait_for(anext(stream), 1)
            assert isinstance(event, InteractionEvent) and event.session_id == "enabled"
            await service.set_enabled("disabled", "qq", True)
            hub.publish(InteractionEvent("disabled", "text_chunk", {}, origin_channel="qq"))
            event = await asyncio.wait_for(anext(stream), 1)
            assert isinstance(event, InteractionEvent) and event.session_id == "disabled"
        finally:
            await stream.aclose()
            await hub.close()
    asyncio.run(scenario())


def test_snapshot_http_preserves_approval_arguments_without_publishing_events() -> None:
    """按需快照能序列化审批参数，查询不会推进事件编号或要求全局订阅。"""
    service = ChannelSubscriptionService(SqliteFactory())
    hub = InteractionEventHub(subscriptions=service)
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
    app.dependency_overrides[get_channel_subscription_service] = lambda: service
    app.dependency_overrides[get_runtime_scheduler] = lambda: Source(hub)
    before = hub.watermark("A")
    with TestClient(app) as client:
        response = client.get("/agent/snapshot/A")
        assert response.status_code == 200
        assert response.json()["payload"]["interrupt"]["data"]["arguments"] == {"cmd": "echo test", "options": {"values": [1, 2]}}
        assert client.get("/agent/snapshot/A?channel=unknown").status_code == 400
        assert client.get("/agent/events/A").status_code == 404
    assert hub.watermark("A") == before
