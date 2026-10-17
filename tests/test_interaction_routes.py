import asyncio
import json
from types import SimpleNamespace
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_runtime_scheduler
from app.routes.agent import router as agent_router
from app.routes.tools import router as tools_router
from app.services.interaction_contract import CommandReceipt
from app.services.interaction_contract import InteractionEvent
from app.agent.state.messages import ToolCall
from app.agent.tools.core.base import InterruptEvent
from app.routes.agent import subscribe_events
from app.routes import agent as agent_routes
from app.services.agent_runtime_scheduler import AgentRuntimeScheduler
from app.services.interaction_event_hub import InteractionEventHub


def test_unknown_command_keeps_http_200_business_rejection() -> None:
    """验证类型整理没有把未知命令提前变成 HTTP 422 或启动 Agent。"""
    def unexpected_agent(session_id: str):
        """禁止未知命令触发真实执行。

        Args:
            session_id (str): 测试的共享会话标识。

        Raises:
            AssertionError: 未知命令错误地启动了 Agent。
        """
        raise AssertionError("未知命令不能启动 Agent")

    scheduler = AgentRuntimeScheduler(InteractionEventHub(), agent_factory=unexpected_agent)
    asyncio.run(scheduler.start())
    app = FastAPI()
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

            async def channel_enabled(self, session_id: str, channel: str) -> bool:
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
        scheduler = SimpleNamespace(events=source)
        response = await subscribe_events("s", "desktop", scheduler)
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
        call = ToolCall(id="call", name="run_ps", args={
            "command": "Write-Output test", "options": {"values": [1, 2]},
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

            async def channel_enabled(self, session_id: str, channel: str) -> bool:
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
        response = await subscribe_events("s", "desktop", SimpleNamespace(events=Events()))
        frames = [json.loads(frame[6:]) async for frame in response.body_iterator]
        # 2. 实时与重连不得将参数退化为 Python 对象地址字符串。
        for frame, key in zip(frames, ("interrupt", "value"), strict=True):
            assert frame["payload"][key]["data"]["arguments"] == {
                "command": "Write-Output test", "options": {"values": [1, 2]},
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
