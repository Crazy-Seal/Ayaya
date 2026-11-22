from dataclasses import asdict
from tests.runtime_support import runtime_hub, close_test_runtime
from app.routes.agent import observe_runtime
from app.schemas.runtime_types import AgentRunOutcome, AgentRunOutcomeKind
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory
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
from app.http_errors import install_http_error_handlers
from app.agent.core.events import AgentEvent, EventType
from app.agent.state.manager import SessionStateManager
from app.interaction.interaction_event_hub import InteractionEventHub
from app.services.channel_subscription_service import ChannelSubscriptionService
from app.routes.agent import router as agent_router
from app.routes.tools import router as tools_router
from app.schemas.runtime_types import CommandReceipt, CommandStatus
from app.interaction.interaction_contract import InteractionEvent
from app.agent.state.messages import ToolCall
from app.agent.tools.core.base import InterruptEvent
from app.routes.agent import subscribe_events, get_session_snapshot
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

        async def run(self, message, *, origin_channel, inbox_input, emit, **kwargs):
            """保存输入后通知界面，再保存正常结果并结束。

            Args:
                message (str): HTTP 提交的用户正文。
                origin_channel (str): 输入来源渠道。
                inbox_input (InboxEnvelope): Scheduler 读取的输入。
                **kwargs (dict): 本轮控制等未使用参数。

            Yields:
                AgentEvent: 文字及正常完成事件。
            """
            outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED)
            await emit(AgentEvent(EventType.RUN_STARTED, {"origin_channel": origin_channel, "resumed": False}))
            if delivery_failure:
                raise OSError("交付失败")
            manager = SessionStateManager("s", SqliteFactory(path))
            try:
                # 1. checkpoint 与 Inbox 交付同事务保存后，才通知显示真人消息。
                state = await manager.load()
                human = state.add_user_message(message, origin_channel=origin_channel)
                await manager.save(state, checkpoint_type="intermediate", inbox_inputs=(inbox_input,))
                await emit(AgentEvent(EventType.HUMAN_INPUT_COMMITTED, {"message": human, "input_sequence": inbox_input.id}))
                await emit(AgentEvent(EventType.TEXT_CHUNK, "收到：" + message))           # 2. 模拟真实 Agent 的正常完成事务，队列仅在这里放行。
                await manager.save(state, checkpoint_type="completed", release_queued=True)
                await emit(AgentEvent(EventType.DONE, None))
            finally:
                await manager.close()

            return outcome

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

    subscriptions = ChannelSubscriptionService(SqliteFactory(path))
    scheduler = build_test_scheduler(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(),
        agent_factory=create_agent, subscriptions=subscriptions,
        session_state_manager_factory=partial(SessionStateManager, sqlite_factory=SqliteFactory(path)),
    )
    hub = runtime_hub(scheduler)

    @asynccontextmanager
    async def lifespan(app):
        """在 TestClient 事件循环中启动并关闭真实调度与分发对象。

        Args:
            app (FastAPI): 当前测试应用。

        Yields:
            None: 请求可进入的生命周期区间。
        """
        await scheduler.start()
        pass
        try:
            yield
        finally:
            try:
                await hub.close()
            finally:
                await close_test_runtime(scheduler)

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
                        if kind == "stream_ready":
                            snapshot_sent.set()
                        elif kind in {"run_completed", "error"}:
                            completed_sent.set()
        await app(scope, receive, observed_send)

    with TestClient(observed_app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        # 1. TestClient 缓存流响应；独立线程发起 GET，等待实际初始帧后再提交命令。
        response_future = pool.submit(client.get, "/agent/events")
        try:
            assert snapshot_sent.wait(3), "SSE 未交付初始状态"
            snapshot_response = client.get("/agent/snapshot/s")
            assert snapshot_response.status_code == 200
            assert snapshot_response.json()["payload"]["phase"] == "idle"
            response = client.post("/agent/commands", json={
                "session_id": "s", "origin_channel": "desktop", "kind": "human_turn",
                "payload": {"message": "你好"},
            })
            assert response.status_code == 200
            assert response.json() == {"accepted": True, "status": "accepted", "context_id": "s", "detail": None, "run_id": None}
            assert completed_sent.wait(3), "命令未通过 Scheduler 和 Hub 到达 SSE"
        finally:
            # 2. 即使断言失败也结束长连接，避免线程池等待一个永不返回的 GET。
            assert client.post("/test/end-events").status_code == 200
        events_response = response_future.result(timeout=3)
    assert events_response.status_code == 200
    assert events_response.headers["content-type"].startswith("text/event-stream")
    frames = [json.loads(line[6:]) for line in events_response.iter_lines() if line.startswith("data: ")]
    assert frames[0]["kind"] == "stream_ready"
    frames = frames[1:]
    assert frames[0]["run_id"] and frames[0]["run_id"] == frames[-1]["run_id"]
    assert len({frame["stream_epoch"] for frame in frames}) == 1
    if delivery_failure:
        assert [frame["kind"] for frame in frames] == ["run_started", "error"]
        assert frames[-1]["payload"]["save_failed"] is True
        assert [frame["event_sequence"] for frame in frames] == [1, 2]
    else:
        assert [frame["kind"] for frame in frames] == [
            "run_started", "human_message", "text_chunk", "run_completed",
        ]
        assert frames[1]["payload"]["message_id"]
        assert frames[2]["payload"]["value"] == "收到：你好"
        assert [frame["event_sequence"] for frame in frames] == list(range(1, 5))


@pytest.mark.parametrize("origin,status,detail", [
    ("desktop", "invalid", "未知运行命令"),
    ("unknown", "failed", "交互渠道未注册或已关闭"),
])
def test_unknown_command_keeps_http_200_business_rejection(origin: str, status: str, detail: str) -> None:
    """未知命令和未注册渠道仍由调度器返回 HTTP 200 拒绝回执。

    Args:
        origin (str): 提交命令的渠道。
        status (str): 调度器原有的拒绝状态。
        detail (str): 调度器原有的错误正文。
    """
    def unexpected_agent(session_id: str, _manager):
        """禁止未知命令触发真实执行。

        Args:
            _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
            session_id (str): 测试的共享会话标识。

        Raises:
            AssertionError: 未知命令错误地启动了 Agent。
        """
        raise AssertionError("未知命令不能启动 Agent")

    scheduler = build_test_scheduler(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(),  agent_factory=unexpected_agent, session_state_manager_factory=lambda context: SessionStateManager(context, SqliteFactory()))

    @asynccontextmanager
    async def lifespan(_app):
        """同一事件循环启动并关闭 Scheduler，释放其持有的状态及结果查询连接。"""
        await scheduler.start()
        try:
            yield
        finally:
            await close_test_runtime(scheduler)

    app = FastAPI(lifespan=lifespan)
    app.include_router(agent_router)
    install_http_error_handlers(app)

    def runtime_override():
        """提供真实调度器，但不启动应用生命周期或生产存储。

        Returns:
            AgentRuntimeScheduler: 测试专用调度器。
        """
        return scheduler

    app.dependency_overrides[get_runtime_scheduler] = runtime_override
    with TestClient(app) as client:
        response = client.post("/agent/commands", json={
            "session_id": "s", "origin_channel": origin, "kind": "future_command", "payload": {},
        })
    assert response.status_code == 200
    assert response.json() == {"accepted": False, "status": status, "context_id": "s", "detail": detail, "run_id": None}


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
            """用可控屏障模拟快照读取期间的空闲连接。"""

            async def snapshot(self, session_id):
                """等待采样放行，返回与实际 Hub 对应的水位。"""
                await release.wait()
                epoch, sequence = hub.watermark(session_id)
                return InteractionEvent(session_id, "session_snapshot", {}, stream_epoch=epoch, event_sequence=sequence)

        class Hub(InteractionEventHub):
            """观察真实订阅句柄的回收。"""

            def unsubscribe(self, subscriber):
                """释放连接并通知测试清理完成。"""
                super().unsubscribe(subscriber)
                closed.set()

        subscriptions = ChannelSubscriptionService(SqliteFactory())
        hub = Hub(subscriptions=subscriptions)
        source = Source()
        response = await subscribe_events("desktop", hub, subscriptions)
        stream = response.body_iterator
        try:
            # 1. 就绪帧之后空闲两次，心跳保持订阅缓冲有效。
            assert json.loads((await anext(stream))[6:])["kind"] == "stream_ready"
            assert await asyncio.wait_for(anext(stream), 0.5) == ": ping\n\n"
            assert await asyncio.wait_for(anext(stream), 0.5) == ": ping\n\n"
            # 2. 快照后的增量仍从同一个订阅缓冲读取。
            hub.publish(InteractionEvent("s", "text_chunk", {"value": "继续"}))
            assert json.loads((await anext(stream))[6:])["kind"] == "text_chunk"
            assert await asyncio.wait_for(anext(stream), 0.5) == ": ping\n\n"
        finally:
            await stream.aclose()
        assert closed.is_set()
        assert not hub._subscribers

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

        subscriptions = ChannelSubscriptionService(SqliteFactory())
        hub = InteractionEventHub(subscriptions=subscriptions)

        class Events:
            """只替换快照查询，线路和订阅缓冲使用真实实现。"""

            async def snapshot(self, session_id):
                """返回包含不可变参数的实际重连快照。"""
                epoch, sequence = hub.watermark(session_id)
                return InteractionEvent(session_id, "session_snapshot", {"interrupt": approval},
                    stream_epoch=epoch, event_sequence=sequence)

        response = await subscribe_events("desktop", hub, subscriptions)
        stream = response.body_iterator
        try:
            assert json.loads((await anext(stream))[6:])["kind"] == "stream_ready"
            from app.agent.state.messages import to_json_value
            frames = [to_json_value(asdict(await get_session_snapshot("s", "desktop", hub, subscriptions, Events())))]
            hub.publish(InteractionEvent("s", "interrupt", {"value": approval}))
            frames.append(json.loads((await anext(stream))[6:]))
        finally:
            await stream.aclose()
            await hub.close()
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
        return CommandReceipt(CommandStatus.ACCEPTED, run_id="run-1")


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
    assert response.json() == {"accepted": True, "status": "accepted", "context_id": None, "detail": None, "run_id": "run-1"}
    assert len(scheduler.commands) == 1
    assert scheduler.commands[0].request_id == "interrupt-123"
    assert scheduler.commands[0].payload == {"kind": "reject"}
