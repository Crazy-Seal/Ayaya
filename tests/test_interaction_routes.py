from tests.runtime_support import RuntimeAgentStub
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from tests.runtime_support import configured_settings
from contextlib import asynccontextmanager
from functools import partial
from threading import Event
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import (
    get_runtime_scheduler, get_interaction_event_hub, get_channel_subscription_service,
)
from app.agent.core.events import AgentEvent, EventType
from app.agent.state.manager import SessionStateManager
from app.interaction.interaction_event_hub import InteractionEventHub
from app.services.channel_subscription_service import ChannelSubscriptionService
from app.routes.agent import router as agent_router
from app.routes.tools import router as tools_router
from app.interaction.interaction_contract import CommandReceipt
from app.interaction.interaction_contract import InteractionEvent
from app.agent.state.messages import ToolCall
from app.agent.tools.core.base import InterruptEvent
from app.routes.agent import subscribe_events
from app.routes import agent as agent_routes
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler


@pytest.mark.parametrize("delivery_failure", [False, True])
def test_http_command_reaches_real_scheduler_and_sse_subscriber(tmp_path, delivery_failure) -> None:
    """通过真实 HTTP 命令、持久交付和 SSE 响应验证单向接线。

    Args:
        tmp_path (Path): 隔离 SQLite 数据库目录。
        delivery_failure (bool): 是否让输入交付始终失败，验证真实错误帧。
    """
    path = str(tmp_path / "state.sqlite3")
    snapshot_sent = Event()
    completed_sent = Event()

    class Agent(RuntimeAgentStub):
        """只替代模型执行，保留真人输入的真实保存和交付通知。"""

        async def run(self, message, *, origin_channel, inbox_input, **kwargs):
            """保存输入后通知界面，再保存正常结果并结束。

            Args:
                message (str): HTTP 提交的用户正文。
                origin_channel (str): 输入来源渠道。
                inbox_input (InboxEnvelope): Scheduler 读取的输入。
                **kwargs (dict): 本轮控制等未使用参数。

            Yields:
                AgentEvent: 文字及正常完成事件。
            """
            yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": origin_channel, "resumed": False})
            if delivery_failure:
                raise RuntimeError("交付失败")
            manager = SessionStateManager("s", path)
            try:
                # 1. checkpoint 与 Inbox 交付同事务保存后，才通知显示真人消息。
                state = await manager.load()
                human = state.add_user_message(message, origin_channel=origin_channel)
                await manager.save(state, checkpoint_type="intermediate", inbox_inputs=(inbox_input,))
                yield AgentEvent(EventType.HUMAN_INPUT_COMMITTED, {"message": human, "input_sequence": inbox_input.id})
                yield AgentEvent(EventType.TEXT_CHUNK, "收到：" + message)
                # 2. 模拟真实 Agent 的正常完成事务，队列仅在这里放行。
                await manager.save(state, checkpoint_type="completed", release_queued=True)
                yield AgentEvent(EventType.DONE, None)
            finally:
                await manager.close()

        async def close(self):
            """替身无额外资源，存储连接由 run 清理。"""

    def create_agent(session_id, _manager):
        """为真实 Scheduler 提供测试执行对象。

        Args:
            _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
            session_id (str): 被调度的会话。

        Returns:
            Agent: 不访问模型的执行替身。
        """
        assert session_id == "s"
        return Agent()

    subscriptions = ChannelSubscriptionService(path)
    scheduler = AgentRuntimeScheduler(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(),
        agent_factory=create_agent, subscriptions=subscriptions,
        session_state_manager_factory=partial(SessionStateManager, db_path=path),
    )
    hub = InteractionEventHub(scheduler, subscriptions=subscriptions)

    @asynccontextmanager
    async def lifespan(app):
        """在 TestClient 事件循环中启动并关闭真实调度与分发对象。

        Args:
            app (FastAPI): 当前测试应用。

        Yields:
            None: 请求可进入的生命周期区间。
        """
        await scheduler.start()
        await hub.start()
        try:
            yield
        finally:
            try:
                await hub.close()
            finally:
                await scheduler.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(agent_router)

    def runtime_override():
        """返回测试的真实 Scheduler。

        Returns:
            AgentRuntimeScheduler: 测试实例。
        """
        return scheduler

    def hub_override():
        """返回测试的真实分发对象。

        Returns:
            InteractionEventHub: 测试实例。
        """
        return hub

    def subscriptions_override():
        """返回命令与分发共享的配置。

        Returns:
            ChannelSubscriptionService: 测试实例。
        """
        return subscriptions

    app.dependency_overrides[get_runtime_scheduler] = runtime_override
    app.dependency_overrides[get_interaction_event_hub] = hub_override
    app.dependency_overrides[get_channel_subscription_service] = subscriptions_override

    @app.post("/test/end-events")
    async def end_events():
        """结束长连接，让 TestClient 返回完整 SSE 响应。

        Returns:
            dict: 关闭操作已完成。
        """
        await hub.close()
        return {"closed": True}

    async def observed_app(scope, receive, send):
        """透传 ASGI 请求，在实际响应体发出时通知测试线程。

        Args:
            scope (dict): 请求或生命周期信息。
            receive (Callable): ASGI 输入读取函数。
            send (Callable): TestClient 的响应发送函数。
        """
        async def observed_send(message):
            """原样发送响应，并按实际 SSE 帧唤醒测试线程。

            Args:
                message (dict): 应用发出的 ASGI 消息。
            """
            await send(message)
            if message["type"] == "http.response.body":
                for frame in message.get("body", b"").decode().split("\n\n"):
                    if frame.startswith("data: "):
                        kind = json.loads(frame[6:])["kind"]
                        if kind == "session_snapshot":
                            snapshot_sent.set()
                        elif kind in {"run_completed", "error"}:
                            completed_sent.set()
        await app(scope, receive, observed_send)

    with TestClient(observed_app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        # 1. TestClient 缓存流响应；独立线程发起 GET，等待实际初始帧后再提交命令。
        response_future = pool.submit(client.get, "/agent/events/s")
        try:
            assert snapshot_sent.wait(3), "SSE 未交付初始状态"
            response = client.post("/agent/commands", json={
                "session_id": "s", "origin_channel": "desktop", "kind": "human_turn",
                "payload": {"message": "你好"},
            })
            assert response.status_code == 200
            assert response.json() == {"accepted": True, "reason": None, "run_id": None}
            assert completed_sent.wait(3), "命令未通过 Scheduler 和 Hub 到达 SSE"
        finally:
            # 2. 即使断言失败也结束长连接，避免线程池等待一个永不返回的 GET。
            assert client.post("/test/end-events").status_code == 200
        events_response = response_future.result(timeout=3)
    assert events_response.status_code == 200
    assert events_response.headers["content-type"].startswith("text/event-stream")
    frames = [json.loads(line[6:]) for line in events_response.iter_lines() if line.startswith("data: ")]
    assert frames[0]["payload"]["phase"] == "idle"
    assert frames[1]["run_id"] and frames[1]["run_id"] == frames[-1]["run_id"]
    assert len({frame["stream_epoch"] for frame in frames}) == 1
    if delivery_failure:
        assert [frame["kind"] for frame in frames] == ["session_snapshot", "run_started", "error"]
        assert frames[-1]["payload"]["failed_input"] == {"input_sequence": 1, "message": "你好", "images": []}
        assert [frame["event_sequence"] for frame in frames] == [0, 1, 2]
    else:
        assert [frame["kind"] for frame in frames] == [
            "session_snapshot", "run_started", "human_message", "text_chunk", "run_completed",
        ]
        assert frames[2]["payload"]["message_id"]
        assert frames[3]["payload"]["value"] == "收到：你好"
        assert [frame["event_sequence"] for frame in frames] == list(range(5))


def test_unknown_command_keeps_http_200_business_rejection() -> None:
    """验证类型整理没有把未知命令提前变成 HTTP 422 或启动 Agent。"""
    def unexpected_agent(session_id: str, _manager):
        """禁止未知命令触发真实执行。

        Args:
            _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
            session_id (str): 测试的共享会话标识。

        Raises:
            AssertionError: 未知命令错误地启动了 Agent。
        """
        raise AssertionError("未知命令不能启动 Agent")

    scheduler = AgentRuntimeScheduler(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(),  agent_factory=unexpected_agent)

    @asynccontextmanager
    async def lifespan(_app):
        """同一事件循环启动并关闭 Scheduler，释放其持有的状态及结果查询连接。"""
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(agent_router)

    def runtime_override():
        """提供真实调度器，但不启动应用生命周期或生产存储。

        Returns:
            AgentRuntimeScheduler: 测试专用调度器。
        """
        return scheduler

    app.dependency_overrides[get_runtime_scheduler] = runtime_override
    with TestClient(app) as client:
        response = client.post("/agent/commands", json={
            "session_id": "s", "origin_channel": "desktop", "kind": "future_command", "payload": {},
        })
    assert response.status_code == 200
    assert response.json() == {"accepted": False, "reason": "当前命令尚未接入运行时", "run_id": None}


def test_idle_sse_heartbeats_preserve_pending_event_and_close_source(monkeypatch) -> None:
    """验证两条空闲 SSE 发送注释心跳，既不取消等待事件也不泄漏订阅。

    Args:
        monkeypatch (pytest.MonkeyPatch): 缩短测试心跳周期的隔离配置替身。
        execution (bool): 是否检查独立屏幕执行流。
    """
    async def scenario() -> None:
        """跨越两次心跳后继续接收同一来源事件，并在关闭时验证订阅释放。"""
        monkeypatch.setattr(agent_routes, "SSE_HEARTBEAT_SECONDS", 0.01, raising=False)
        release = asyncio.Event()
        closed = asyncio.Event()
        cancelled = asyncio.Event()

        class Source:
            """用可控屏障模拟没有业务数据的长期事件来源。"""
            def validate_source(self, channel: str) -> None:
                """接受测试渠道。

                Args:
                    channel (str): 测试渠道标识。
                """

            async def is_enabled(self, session_id: str, channel: str) -> bool:
                """返回固定启用状态。

                Args:
                    session_id (str): 测试会话标识。
                    channel (str): 测试渠道标识。

                Returns:
                    bool: 测试订阅允许建立。
                """
                return True

            async def subscribe(self, *identity):
                """等待测试放行才产生数据，显式观测取消和订阅清理。

                Args:
                    identity (tuple): 会话展示流的身份；执行流不需要参数。

                Yields:
                    InteractionEvent: 放行后的实际业务数据。
                """
                try:
                    await release.wait()
                    yield InteractionEvent("s", "text_chunk", {"value": "继续"})
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                finally:
                    closed.set()

        source = Source()
        response = await subscribe_events("s", "desktop", source, source)
        stream = response.body_iterator
        try:
            # 1. 连续空闲周期只发送注释，不生成假事件，也不能取消来源的 queue.get。
            assert await asyncio.wait_for(anext(stream), 0.5) == ": ping\n\n"
            assert await asyncio.wait_for(anext(stream), 0.5) == ": ping\n\n"
            assert not cancelled.is_set()
            # 2. 放行后仍取得原本等待的数据，不因心跳丢失事件或身份。
            release.set()
            frame = await asyncio.wait_for(anext(stream), 0.5)
            data = json.loads(frame[6:])
            assert data["kind"] == "text_chunk"
            # 3. 再进入空闲后关闭响应，必须取消等待并释放来源订阅。
            assert await asyncio.wait_for(anext(stream), 0.5) == ": ping\n\n"
        finally:
            await stream.aclose()
        assert closed.is_set()
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_approval_arguments_are_json_objects_in_increment_and_snapshot() -> None:
    """验证不可变工具参数在实时审批和重连快照中都保持 JSON 对象。"""
    async def scenario() -> None:
        """消费真实 SSE 路由生成的帧，检查前端实际收到的参数类型和内容。"""
        call = ToolCall(id="call", name="exec_command", args={
            "cmd": "echo test", "options": {"values": [1, 2]},
        })
        approval = InterruptEvent.create(
            "authorization_request", "确认命令", {"arguments": call.args},
        ).to_client()

        class Events:
            """为路由提供有限的实时审批和重连快照，不连接生产运行时。"""
            def validate_source(self, channel: str) -> None:
                """接受测试的桌面渠道。

                Args:
                    channel (str): 路由请求的来源渠道。
                """
                assert channel == "desktop"

            async def is_enabled(self, session_id: str, channel: str) -> bool:
                """返回测试渠道的固定启用状态。

                Args:
                    session_id (str): 测试会话身份。
                    channel (str): 测试渠道身份。

                Returns:
                    bool: 测试渠道已启用。
                """
                return True

            async def subscribe(self, session_id: str, channel: str):
                """按线路发送重连快照和实时审批。

                Args:
                    session_id (str): 测试会话身份。
                    channel (str): 测试渠道身份。

                Yields:
                    InteractionEvent: 包含相同不可变参数的两类事件。
                """
                yield InteractionEvent(session_id, "session_snapshot", {"interrupt": approval})
                yield InteractionEvent(session_id, "interrupt", {"value": approval})

        # 1. 只替换事件来源，路由和不可变消息容器使用实际实现。
        events = Events()
        response = await subscribe_events("s", "desktop", events, events)
        frames = [json.loads(frame[6:]) async for frame in response.body_iterator]
        # 2. 实时与重连不得将参数退化为 Python 对象地址字符串。
        for frame, key in zip(frames, ("interrupt", "value"), strict=True):
            assert frame["payload"][key]["data"]["arguments"] == {
                "cmd": "echo test", "options": {"values": [1, 2]},
            }

    asyncio.run(scenario())


class _Scheduler:
    """提供隔离测试替身，用于验证：_Scheduler。

    Attributes:
        commands (list[ChannelCommand]): 测试观测到的已提交命令。
    """
    def __init__(self) -> None:
        """初始化测试替身的观测记录。"""
        self.commands = []

    async def submit(self, command):
        """记录测试命令并返回受理回执。

        Args:
            command (ChannelCommand): 统一渠道命令或需要执行的命令正文，类型决定其含义。
        """
        self.commands.append(command)
        return CommandReceipt(True, run_id="run-1")


def test_command_route_requires_session_and_source() -> None:
    """验证HTTP 命令必须包含会话与来源。"""
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    app = FastAPI()
    app.include_router(agent_router)
    app.dependency_overrides[get_runtime_scheduler] = _Scheduler
    with TestClient(app) as client:
        response = client.post("/agent/commands", json={"kind": "human_turn", "payload": {}})
    # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
    assert response.status_code == 422


def test_command_route_forwards_approval_identity() -> None:
    """验证HTTP 入口保留审批请求身份。"""
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    app = FastAPI()
    app.include_router(agent_router)
    app.include_router(tools_router)
    scheduler = _Scheduler()
    app.dependency_overrides[get_runtime_scheduler] = lambda: scheduler
    with TestClient(app) as client:
        response = client.post("/agent/commands", json={
            "session_id": "session-a", "origin_channel": "desktop",
            "kind": "resolve_authorization", "request_id": "interrupt-123",
            "payload": {"kind": "reject"},
        })
        assert client.post("/chat", json={"message": "旧入口"}).status_code == 404
        assert client.post("/tool-authorization/respond", json={}).status_code == 404
        assert client.post("/tool-continuation/respond", json={}).status_code == 404
    # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
    assert response.status_code == 200
    assert response.json() == {"accepted": True, "reason": None, "run_id": "run-1"}
    assert len(scheduler.commands) == 1
    assert scheduler.commands[0].request_id == "interrupt-123"
    assert scheduler.commands[0].payload == {"kind": "reject"}
