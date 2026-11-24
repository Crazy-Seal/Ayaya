from tests.runtime_support import make_execution_control
from app.crud.sqlite_factory import SqliteFactory
import asyncio
from pathlib import Path
import pytest

from app.agent.agent import Agent
from app.agent.core.events import AgentEvent, EventType
from app.agent.runtime.turn_control import TurnControl, TurnInterrupted
from app.agent.state import AgentState, TemporaryContextItem
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import AssistantMessage, ToolCall, ToolMessage
from app.crud.chat_history_dao import ChatHistoryDao
from app.schemas.runtime_types import TurnInterruptReason


def test_turn_reason_uses_protocol_value_in_interrupt_text() -> None:
    """已受理的停止原因在安全点以协议值传递给回答收尾。"""
    execution_state, control = make_execution_control("run")
    assert execution_state.request_stop("run").accepted
    with pytest.raises(TurnInterrupted, match="^stop$"):
        control.check()


def test_stopping_pure_tool_turn_closes_call_without_fake_text(tmp_path) -> None:
    """验证停止纯工具轮次闭合协议且不伪造文本回复。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：停止纯工具轮次闭合协议且不伪造文本回复。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        agent = Agent.__new__(Agent)
        agent.session_state_manager = SessionStateManager("s", SqliteFactory(str(tmp_path / "state.sqlite3")))
        state = AgentState.create_new("s")
        state.set_temporary_context("plugin.context", TemporaryContextItem(title="插件背景", text="本轮有效"))
        call = ToolCall(id="tool-1", name="example", args={})
        state.record_assistant_step("", (call,))
        execution_state, control = make_execution_control("run-1")
        control.partial_persisted = True
        execution_state.request_stop(control.run_id)
        control.wake_interrupt()

        async def interrupted():
            """产生需要停止收尾的工具调用场景。

            Yields:
                AgentEvent: 当前测试场景的预设分片或事件。
            """
            raise TurnInterrupted("stop")
            yield AgentEvent(EventType.DONE, None)

        try:
            events = []
            async def receive(event):
                """记录已保存停止协议之后的执行事件。"""
                events.append(event)
            outcome = await agent._stream_and_finalize(
                state, interrupted(), failure_message="failed",
                turn_control=control, emit=receive,
            )
            assert outcome.kind == "stopped"
            assert [event.type for event in events] == [EventType.DONE]
            assert len([item for item in state.messages if isinstance(item, AssistantMessage)]) == 1
            assert any(isinstance(item, ToolMessage) for item in state.messages)
            assert (await agent.session_state_manager.load()).temporary_context == {}
            assert all("被用户打断" not in item.text for item in state.messages if isinstance(item, AssistantMessage))
        finally:
            await agent.session_state_manager.close()

    asyncio.run(scenario())


def test_stopping_after_completed_text_preserves_history_and_cancels_tool(tmp_path) -> None:
    """验证停止工具调用时保留已输出的正文，并为工具记录取消结果。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证已输出正文及工具取消结果。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        agent = Agent.__new__(Agent)
        agent.session_state_manager = SessionStateManager("s", SqliteFactory(db_path))
        state = AgentState.create_new("s")
        call = ToolCall(id="tool-1", name="example", args={})
        state.record_assistant_step("正在操作", (call,))
        await agent.session_state_manager.save(state, checkpoint_type="intermediate")
        execution_state, control = make_execution_control("run-1")
        control.partial = "正在操作"
        control.partial_persisted = True
        execution_state.request_stop(control.run_id)
        control.wake_interrupt()
        try:
            await agent.session_state_manager.finish_interrupted_turn(
                state, reason=TurnInterruptReason.STOP, partial=control.partial, partial_persisted=control.partial_persisted,
            )
            assistant = [item for item in state.messages if isinstance(item, AssistantMessage)]
            assert len(assistant) == 1
            assert assistant[0].text == "正在操作"
            restored = await agent.session_state_manager.load()
            results = [item for item in restored.messages if isinstance(item, ToolMessage)]
            assert [(item.tool_call_id, item.text) for item in results] == [(call.id, "被用户取消，工具调用未完成")]
            history = await ChatHistoryDao(sqlite_factory=SqliteFactory(Path(db_path))).list_chat_history_last_n_async("s")
            visible = [item for item in history if item["role"] == "AI"]
            assert len(visible) == 1
            assert visible[0]["content"] == assistant[0].text
        finally:
            await agent.session_state_manager.close()

    asyncio.run(scenario())
