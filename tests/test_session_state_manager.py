"""验证会话状态业务入口、恢复核验和借用对象的关闭责任。"""
from tests.runtime_support import collect_agent_events
from app.crud.sqlite_factory import SqliteFactory

import asyncio

import pytest

from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.core.events import EventType
from app.agent.models.llm_client import StreamChunk
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import ToolCall
from tests.test_runtime_input_delivery import ProbeModel


async def save_authorization(manager: SessionStateManager) -> None:
    """保存一个真实工具调用的待审批状态，供公开业务入口处理。

    Args:
        manager (SessionStateManager): 本次测试独有的会话状态业务对象。
    """
    state = await manager.load()
    state.add_user_message("执行工具", origin_channel="qq")
    call = ToolCall(id="call", name="probe", args={})
    state.record_assistant_step("", (call,), origin_channel="qq")
    state.clear_pending_tool_calls()
    state.set_interrupt({"type": "authorization_request", "request_id": "approval",
        "tool_call_id": call.id, "tool_name": call.name, "tool_args": {}, "origin_channel": "qq"})
    await manager.save_progress(state)


def test_authorization_requires_saved_first_decision_and_matching_request(tmp_path) -> None:
    """恢复必须使用匹配请求的首次持久决定，不接受过期编号或覆盖决定。

    Args:
        tmp_path (Path): Pytest 隔离存储目录。
    """
    async def scenario():
        """通过 Manager 写入、核验并重新读取同一个审批决定。"""
        manager = SessionStateManager("s", SqliteFactory(str(tmp_path / "state.sqlite3")))
        try:
            await save_authorization(manager)
            assert await manager.load_for_resume("approval") is None
            assert await manager.record_authorization_decision("old", {"kind": "approve"}) is None
            assert await manager.record_authorization_decision("approval", {"kind": "approve"}) == "qq"
            assert await manager.record_authorization_decision("approval", {"kind": "reject"}) is None
            assert await manager.load_for_resume("old") is None
            restored = await manager.load_for_resume("approval")
            assert restored.interrupt_data["runtime_decision"] == {"kind": "approve"}
            assert await manager.pending_humans() == []
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_closing_agent_stream_waits_for_model_stream_cleanup(tmp_path) -> None:
    """关闭外层输出时立即关闭模型迭代，不依赖垃圾回收触发清理。

    Args:
        tmp_path (Path): Pytest 隔离存储目录。
    """
    async def scenario():
        """在正文已输出但尚未完成时关闭，并核对未误提交完成状态。"""
        released = asyncio.Event()

        class StreamingModel(ProbeModel):
            """在生成器退出时记录模型响应已经释放。"""

            async def astream(self, messages, tools=None):
                """输出正文后暂停，关闭迭代时同步执行 finally。

                Args:
                    messages (list): 模型输入消息。
                    tools (list | None): 本次公开工具。

                Yields:
                    StreamChunk: 本次可见的正文片段。
                """
                try:
                    yield StreamChunk(content="尚未完成")
                    await asyncio.Event().wait()
                finally:
                    released.set()

        agent = Agent(AgentConfig(session_id="s", model_name="probe", api_key="test",
            context_strategy=ContextStrategyConfig()), sqlite_factory=SqliteFactory(str(tmp_path / "state.sqlite3")),
            llm_client=StreamingModel())
        shown = asyncio.Event()

        async def receive(event):
            """正文到达后保留执行等待，供测试取消一次真实 Agent 调用。"""
            if event.type == EventType.TEXT_CHUNK:
                shown.set()
                await asyncio.Event().wait()

        running = asyncio.create_task(agent.run("开始", emit=receive))
        try:
            await asyncio.wait_for(shown.wait(), 2)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert released.is_set()
            await agent.session_state_manager.load()
            assert agent.session_state_manager.last_loaded_checkpoint_type == "intermediate"
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await agent.close()

    asyncio.run(scenario())


def test_steer_accepts_input_without_marking_it_delivered(tmp_path) -> None:
    """审批引导保存新正文和旧请求决定，新正文仍等待下一轮交付。

    Args:
        tmp_path (Path): Pytest 隔离存储目录。
    """
    async def scenario():
        """检查引导决定和 Inbox 输入各自保持既有业务含义。"""
        manager = SessionStateManager("s", SqliteFactory(str(tmp_path / "state.sqlite3")))
        try:
            await save_authorization(manager)
            decision = {"kind": "steer", "message": "改做另一件事"}
            assert await manager.record_authorization_decision("approval", decision, "desktop") == "qq"
            pending = await manager.pending_humans()
            assert len(pending) == 1 and pending[0].origin_channel == "desktop"
            assert pending[0].payload["message"] == decision["message"]
            assert await manager.status(pending[0].id) == "pending"
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_agent_borrows_manager_and_stale_resume_does_not_start(tmp_path) -> None:
    """Agent 关闭不释放 worker 的 Manager，过期恢复不产生开始事件或模型调用。

    Args:
        tmp_path (Path): Pytest 隔离存储目录。
    """
    async def scenario():
        """借用真实 Manager 完成一次执行并核对连接责任。"""
        manager = SessionStateManager("s", SqliteFactory(str(tmp_path / "state.sqlite3")))
        model = ProbeModel()
        agent = Agent(AgentConfig(session_id="s", model_name="probe", api_key="test",
            context_strategy=ContextStrategyConfig()), llm_client=model, session_state_manager=manager, sqlite_factory=SqliteFactory())
        try:
            assert [event for event in await collect_agent_events(agent.resume, 'expired')] == []
            assert model.calls == 0
            events = [event for event in await collect_agent_events(agent.run, '你好')]
            assert events[0].type == EventType.RUN_STARTED
            assert events[1].type == EventType.HUMAN_INPUT_COMMITTED
            connection = manager._state_dao._db
            await agent.close()
            assert manager._state_dao._db is connection
            assert (await manager.load()).messages[0].text == "你好"
        finally:
            await manager.close()
        assert manager._state_dao._db is None and manager._inbox_dao._db is None

    asyncio.run(scenario())


def test_close_attempts_inbox_cleanup_when_state_close_fails(tmp_path, monkeypatch) -> None:
    """状态连接关闭失败时仍关闭输入连接，不遗留另一条数据库连接。

    Args:
        tmp_path (Path): Pytest 隔离存储目录。
        monkeypatch (pytest.MonkeyPatch): 注入关闭故障。
    """
    async def scenario():
        """关闭真实连接后报告状态关闭故障，验证 finally 中的输入清理。"""
        manager = SessionStateManager("s", SqliteFactory(str(tmp_path / "state.sqlite3")))
        await manager.load()
        await manager.submit_human("desktop", "待处理")
        original = manager._state_dao.close

        async def fail_close():
            """先释放实际状态连接，再模拟关闭操作报告错误。"""
            await original()
            raise RuntimeError("close failure")

        monkeypatch.setattr(manager._state_dao, "close", fail_close)
        await manager.close()
        assert manager._state_dao._db is None and manager._inbox_dao._db is None

    asyncio.run(scenario())
