import asyncio
from pathlib import Path
import pytest

from app.agent.agent import Agent
from app.agent.core.event_router import AgentEvent, EventType
from app.agent.runtime.turn_control import TurnControl, TurnInterrupted
from app.agent.state import AgentState
from app.agent.state.manager import StateManager
from app.agent.state.messages import AssistantMessage, ToolCall, ToolMessage
from app.crud.chat_history_dao import ChatHistoryDao


def test_string_turn_reason_keeps_original_interrupt_text() -> None:
    """验证既有字符串初始化仍能打断，异常不泄漏枚举名称或属性错误。
    """
    # 保留原始字符串作为旧调用样例，不让测试随实现一起改成枚举。
    control = TurnControl("run", reason="stop")
    with pytest.raises(TurnInterrupted, match="^stop$"):
        control.check()


def test_stopping_pure_tool_turn_closes_call_without_fake_text(tmp_path) -> None:
    """验证停止纯工具轮次闭合协议且不伪造文本回复。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：停止纯工具轮次闭合协议且不伪造文本回复。

        """
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        agent = Agent.__new__(Agent)
        agent.state_manager = StateManager("s", str(tmp_path / "state.sqlite3"))
        state = AgentState.create_new("s")
        call = ToolCall(id="tool-1", name="example", args={})
        state.record_assistant_step("", (call,))
        control = TurnControl("run-1")
        control.partial_persisted = True
        control.request("stop")

        async def interrupted():
            """产生需要停止收尾的工具调用场景。

            Yields:
                AgentEvent: 当前测试场景的预设分片或事件。
            """
            raise TurnInterrupted("stop")
            yield AgentEvent(EventType.DONE, None)

        try:
            events = [event async for event in agent._stream_and_finalize(
                state, interrupted(), failure_message="failed",
                turn_control=control,
            )]
            assert [event.type for event in events] == [EventType.DONE]
            assert len([item for item in state.messages if isinstance(item, AssistantMessage)]) == 1
            assert any(isinstance(item, ToolMessage) for item in state.messages)
            assert all("被用户打断" not in item.text for item in state.messages if isinstance(item, AssistantMessage))
        finally:
            await agent.state_manager.close()

    asyncio.run(scenario())


def test_stopping_after_text_and_tool_keeps_one_assistant_history_message(tmp_path) -> None:
    """验证工具前文本被打断后仍只保留一个 Assistant 历史项。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：工具前文本被打断后仍只保留一个 Assistant 历史项。

        """
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        agent = Agent.__new__(Agent)
        agent.state_manager = StateManager("s", db_path)
        state = AgentState.create_new("s")
        call = ToolCall(id="tool-1", name="example", args={})
        state.record_assistant_step("正在操作", (call,))
        state.record_tool_result(call, "工具完成")
        await agent.state_manager.save(state, checkpoint_type="intermediate")
        control = TurnControl("run-1")
        control.partial = "正在操作"
        control.partial_persisted = True
        control.request("stop")
        try:
            await agent._finish_interrupted_turn(state, control, "desktop")
            assistant = [item for item in state.messages if isinstance(item, AssistantMessage)]
            assert len(assistant) == 1
            assert assistant[0].text == "正在操作……（被用户打断）"
            history = await ChatHistoryDao(db_path=Path(db_path)).list_chat_history_last_n_async("s")
            visible = [item for item in history if item["role"] == "AI"]
            assert len(visible) == 1
            assert visible[0]["content"] == assistant[0].text
        finally:
            await agent.state_manager.close()

    asyncio.run(scenario())
