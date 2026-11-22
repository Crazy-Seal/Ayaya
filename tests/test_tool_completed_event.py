"""验证工具完成事件、历史图片展示及运行中历史查询终点。"""
from tests.runtime_support import collect_agent_events
from tests.runtime_support import runtime_hub, close_test_runtime
from app.routes.agent import observe_runtime
from tests.runtime_support import next_runtime_event
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory

import asyncio
from pathlib import Path
from tests.runtime_support import configured_settings

import pytest

from app.agent.core.events import EventType
from app.agent.models.contracts import StreamChunk
from app.agent.state import AgentState
from app.agent.state.manager import RequiredSaveError, SessionStateManager
from app.schemas.runtime_types import AgentRunOutcomeKind
from app.agent.state.messages import ToolCall, ToolMessage
from app.crud.chat_history_dao import ChatHistoryDao
from app.routes.chat_history import list_chat_history_last_n
from app.services.chat_history_service import ChatHistoryService
from app.errors import ResourceNotFoundError
from tests.test_checkpoint_recovery import ImageStepTool, make_agent


@pytest.mark.parametrize("resumed", [False, True])
def test_tool_completed_is_emitted_after_each_durable_result(tmp_path: Path, resumed: bool) -> None:
    """普通执行和审批恢复逐个报告已保存结果，后续工具尚未执行时图片已可查询。

    Args:
        tmp_path (Path): 本用例专属的隔离存储目录。
        resumed (bool): 是否从审批恢复入口执行同一批工具。
    """
    async def scenario() -> None:
        """在消费完成事件的当下重新读取 SQLite，核对结果和未完成调用。"""
        path = tmp_path / "state.sqlite3"
        calls = (ToolCall("a", "image_step", {"label": "A"}), ToolCall("b", "image_step", {"label": "B"}))
        scripts = [[StreamChunk(content="继续回答")]]
        if not resumed:
            scripts.insert(0, [StreamChunk(content="准备操作"), *(StreamChunk(tool_call=call) for call in calls)])
        agent = await make_agent(path, scripts, ImageStepTool())
        manager = agent.session_state_manager
        dao = ChatHistoryDao(SqliteFactory(path))
        try:
            if resumed:
                state = AgentState.create_new("checkpoint-test")
                state.add_user_message("执行")
                state.record_assistant_step("准备操作", calls)
                state.set_interrupt({
                    "type": "authorization_request", "request_id": "approval",
                    "tool_name": "image_step", "tool_call_id": "a", "tool_args": {"label": "A"},
                    "resume_state": {},
                }, pending_actions=(calls[1],))
                state.clear_pending_tool_calls()
                await manager.save_progress(state)
                stream = agent.pipeline.resume_tools(state, {"kind": "approve"}, session_state_manager=manager)
            completed = []
            async def receive(event):
                """在每次完成事件交付时读取真实已保存工具结果，验证下一项尚未执行。"""
                if event.type != EventType.TOOL_COMPLETED:
                    return
                completed.append(event.data)
                saved = await manager.load()
                results = [message for message in saved.messages if isinstance(message, ToolMessage)]
                assert event.data == {
                    "tool_call_id": results[-1].tool_call_id, "tool_name": "image_step",
                    "message_id": results[-1].message_id,
                }
                assert len(results) == len(completed)
                assert [call.id for call in saved.pending_tool_calls] == (["b"] if len(completed) == 1 else [])
                history = await dao.list_chat_history_last_n_async(
                    "checkpoint-test", through_message_id=event.data["message_id"],
                )
                assert history[-1]["images"]
                assert history[-1]["content"] == ""
                assert history[-1]["id"] == f"{event.data['message_id']}:images"
            if resumed:
                async for event in stream:
                    await receive(event)
            else:
                outcome = await agent.run("执行两个图片工具", emit=receive)
                assert outcome.kind == AgentRunOutcomeKind.COMPLETED
            assert [entry["tool_call_id"] for entry in completed] == ["a", "b"]
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_failed_result_save_does_not_emit_tool_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """工具已执行但结果保存失败时，事件流不能报告完成或让界面查询未提交结果。

    Args:
        tmp_path (Path): 本用例专属数据库目录。
        monkeypatch (pytest.MonkeyPatch): 临时替换保存方法，制造工具结果事务失败。
    """
    async def scenario() -> None:
        """保留真人和工具调用的初始保存，只让工具结果保存失败。"""
        path = tmp_path / "state.sqlite3"
        agent = await make_agent(path, [[StreamChunk(tool_call=ToolCall("a", "image_step", {}))]], ImageStepTool())
        original = agent.session_state_manager.save_progress

        async def fail_result(state: AgentState) -> int:
            """结果进入内存后模拟磁盘写入失败。

            Args:
                state (AgentState): 当前要保存的会话状态。
            Returns:
                int: 尚无工具结果时真实保存返回的 checkpoint 编号。
            """
            if any(isinstance(message, ToolMessage) for message in state.messages):
                raise RequiredSaveError("工具结果保存失败")
            return await original(state)

        monkeypatch.setattr(agent.session_state_manager, "save_progress", fail_result)
        try:
            events = []
            async def receive_agent_event(event):
                """在原事件位置执行此场景的观察和控制。"""
                events.append(event)
            outcome = await agent.run('执行', emit=receive_agent_event)
            assert outcome.kind == AgentRunOutcomeKind.SAVE_FAILED
            assert outcome.error == "工具结果保存失败"
            assert not any(event.type == EventType.TOOL_COMPLETED for event in events)
            saved = await agent.session_state_manager.load()
            assert not any(isinstance(message, ToolMessage) for message in saved.messages)
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_scheduler_forwards_tool_completed_before_following_reply(tmp_path: Path) -> None:
    """真实 Agent 和调度器将工具完成转换为同名交互事件，保留调用身份及顺序。

    Args:
        tmp_path (Path): 当前调度器和 Agent 共用的隔离数据库目录。
    """
    from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
    from app.interaction.interaction_contract import InteractionCommand
    from app.schemas.runtime_types import InteractionEventKind

    async def scenario() -> None:
        """通过公开命令提交一次工具调用，消费渠道事件直到本轮结束。"""
        path = tmp_path / "scheduler.sqlite3"
        agent = await make_agent(path, [
            [StreamChunk(tool_call=ToolCall("call", "image_step", {}))],
            [StreamChunk(content="后续回答")],
        ], ImageStepTool(), session_id="s")
        scheduler = build_test_scheduler(chat_settings_dao=configured_settings(),
            runtime_queue=asyncio.Queue(), agent_factory=lambda _session, _manager: agent,
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(str(path))),
        )
        try:
            await scheduler.start()
            assert (await scheduler.submit(InteractionCommand("s", "human_turn", {"message": "执行"}))).accepted
            events = []
            while True:
                event = await asyncio.wait_for(next_runtime_event(scheduler), 3)
                assert event is not None
                events.append(event)
                if event.kind == InteractionEventKind.RUN_COMPLETED:
                    break
            kinds = [event.kind for event in events]
            assert kinds.index(InteractionEventKind.TOOL_CALL) < kinds.index(InteractionEventKind.TOOL_COMPLETED)
            assert kinds.index(InteractionEventKind.TOOL_COMPLETED) < kinds.index(InteractionEventKind.TEXT_CHUNK)
            completed = next(event for event in events if event.kind == InteractionEventKind.TOOL_COMPLETED)
            assert completed.payload["value"]["tool_call_id"] == "call"
            assert completed.payload["value"]["tool_name"] == "image_step"
            history = await ChatHistoryDao(SqliteFactory(path)).list_chat_history_last_n_async(
                "s", through_message_id=completed.payload["value"]["message_id"],
            )
            assert history[-1]["images"] and history[-1]["content"] == ""
        finally:
            await close_test_runtime(scheduler)

    asyncio.run(scenario())


def test_history_stops_at_tool_result_even_when_later_reply_is_saved(tmp_path: Path) -> None:
    """运行中刷新排除已抢先保存的后续回答，同时保留正确分页和会话范围。

    Args:
        tmp_path (Path): 本用例专属数据库目录。
    """
    async def scenario() -> None:
        """先保存工具和后续回复，再按工具消息编号查询其前面的历史。"""
        path = tmp_path / "state.sqlite3"
        manager = SessionStateManager("s", SqliteFactory(str(path)))
        try:
            state = AgentState.create_new("s")
            state.add_user_message("开始")
            call = ToolCall("a", "read_file", {})
            state.record_assistant_step("操作前的说明", (call,))
            tool = state.record_tool_result(call, ())
            state.add_assistant_message("工具之后已经保存的回答")
            await manager.save_progress(state)
            dao = ChatHistoryDao(SqliteFactory(path))
            dao.READ_CHUNK_SIZE = 1
            limited = await dao.list_chat_history_last_n_async("s", n=1, through_message_id=tool.message_id)
            assert len(limited) == 1 and limited[0]["role"] == "AI_Tool_Calling"
            history = await dao.list_chat_history_last_n_async("s", through_message_id=tool.message_id)
            assert [item["content"] for item in history] == ["开始", "操作前的说明", "调用了工具: read_file"]
            assert (await dao.list_chat_history_last_n_async("s"))[-1]["content"] == "工具之后已经保存的回答"
            # 1. 路由和服务必须传递相同终点，不能退化为无范围的最新历史查询。
            response = await list_chat_history_last_n(
                "s", n=100, through_message_id=tool.message_id, chat_history_service=ChatHistoryService(dao),
            )
            assert [item.content for item in response.data] == [item["content"] for item in history]
            with pytest.raises(ResourceNotFoundError):
                await dao.list_chat_history_last_n_async("other", through_message_id=tool.message_id)
            # 2. 其他会话的消息编号不能作为本会话的历史终点。
            with pytest.raises(ResourceNotFoundError, match="历史查询终点不存在于当前会话"):
                await list_chat_history_last_n(
                    "other", n=100, through_message_id=tool.message_id, chat_history_service=ChatHistoryService(dao),
                )
        finally:
            await manager.close()

    asyncio.run(scenario())
