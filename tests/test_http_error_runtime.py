"""确认共用方法更换异常后，运行时仍按原格式报告失败。"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from app.agent.state.manager import SessionStateManager
from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.models.contracts import StreamChunk, ToolDefinition
from app.agent.state.messages import BaseMessage, ToolCall, ToolMessage
from app.agent.tools.core.runtime import ToolRuntime
from app.agent.tools.get_subagent_details import GetSubagentDetailsTool
from app.agent.tools.terminal.task import read_terminal_output
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.sqlite_factory import SqliteFactory
from app.errors import InvalidOperationError
from app.interaction.interaction_contract import InteractionCommand
from tests.runtime_event_source import observe_session
from app.schemas.runtime_types import TaskStatus
from app.schemas.task_types import TaskRecord
from app.utils.sse_stream import stream_sse_json
from tests.runtime_support import build_test_scheduler, close_test_runtime, configured_settings, runtime_hub


@pytest.mark.asyncio
async def test_missing_subagent_keeps_tool_error_text(tmp_path: Path) -> None:
    """模型查询不存在的子 Agent 时仍收到原工具结果，轮次继续正常结束。

    Args:
        tmp_path (Path): 当前执行独占的数据库目录。
    """
    sqlite = SqliteFactory(tmp_path / "state.sqlite3")
    queue = asyncio.Queue()
    observed: list[str] = []

    class Model:
        """先查询子 Agent，再读取实际工具结果并结束回答。"""

        async def astream(self, messages: Sequence[BaseMessage], *, tools: Sequence[ToolDefinition] = ()) -> AsyncIterator[StreamChunk]:
            """通过真实 Agent 工具循环观察错误正文。

            Args:
                messages (Sequence[BaseMessage]): 本轮传给模型的业务消息。
                tools (Sequence[ToolDefinition]): 当前模型可用工具。

            Yields:
                StreamChunk: 查询请求或结束文字。
            """
            results = [message.text for message in messages if isinstance(message, ToolMessage)]
            if results:
                observed.extend(results)
                yield StreamChunk(content="已收到查询结果")
            else:
                yield StreamChunk(tool_call=ToolCall("lookup", "get_subagent_details", {"subagent_id": "missing"}))

        async def close(self) -> None:
            """模型替身没有外部连接。"""

    def create_agent(session_id: str, manager: SessionStateManager) -> Agent:
        """为调度器创建使用真实工具队列、借用会话存储的 Agent。

        Args:
            session_id (str): 本次执行的主会话。
            manager (SessionStateManager): worker 提供的独立会话状态对象。

        Returns:
            Agent: 使用离线模型和真实子 Agent 查询工具的执行实例。
        """
        agent = Agent(AgentConfig(session_id=session_id, model_name="test", api_key="test", system_prompt="查询子身份",
                                  context_strategy=ContextStrategyConfig()),
            llm_client=Model(), sqlite_factory=sqlite, session_state_manager=manager,
            tool_runtime_factory=lambda name, run, call: ToolRuntime(session_id, run, name, call, scheduler.task_supervisor, queue))
        agent.register_tool(GetSubagentDetailsTool())
        return agent

    scheduler = build_test_scheduler(chat_settings_dao=configured_settings("session"), runtime_queue=queue,
        agent_factory=create_agent, session_state_manager_factory=lambda sid: SessionStateManager(sid, sqlite))
    hub = runtime_hub(scheduler)
    await scheduler.start()
    stream = observe_session("session", hub, scheduler)
    try:
        await anext(stream)
        receipt = await scheduler.submit(InteractionCommand("session", "human_turn", {"message": "查询"}))
        assert receipt.accepted is True
        while (event := await asyncio.wait_for(anext(stream), 5)).kind != "run_completed":
            assert event.kind != "error", event.payload
        assert observed == ["错误: 子 Agent 不存在或不属于当前会话"]
    finally:
        await stream.aclose()
        await hub.close()
        await close_test_runtime(scheduler)


@pytest.mark.asyncio
async def test_configuration_lookup_failure_keeps_sse_error_text(tmp_path: Path) -> None:
    """创建 Agent 时读取不到配置，原带引号说明仍通过 SSE 错误事件送出。

    Args:
        tmp_path (Path): 本用例隔离的会话数据库目录。
    """
    settings = configured_settings("session")
    sqlite = SqliteFactory(tmp_path / "state.sqlite3")

    def create_agent(session_id: str, _manager: SessionStateManager) -> None:
        """模拟创建 Agent 时，配置读取入口查到了已删除的会话。

        Args:
            session_id (str): 已受理的主会话。
            _manager (SessionStateManager): 当前 worker 持有的状态对象。

        Raises:
            ResourceNotFoundError: 创建 Agent 时配置已被删除。
        """
        settings.get_chat_settings("deleted")

    scheduler = build_test_scheduler(chat_settings_dao=settings, runtime_queue=asyncio.Queue(),
        agent_factory=create_agent, session_state_manager_factory=lambda sid: SessionStateManager(sid, sqlite))
    hub = runtime_hub(scheduler)
    await scheduler.start()
    stream = stream_sse_json(observe_session("session", hub, scheduler))
    try:
        first = await asyncio.wait_for(anext(stream), 3)
        assert json.loads(first.removeprefix("data: "))["kind"] == "session_snapshot"
        receipt = await scheduler.submit(InteractionCommand("session", "human_turn", {"message": "你好"}))
        assert receipt.accepted is True
        frame = json.loads((await asyncio.wait_for(anext(stream), 3)).removeprefix("data: "))
        assert frame["kind"] == "error"
        assert frame["payload"] == {"detail": "'session_id not found: deleted'"}
    finally:
        await stream.aclose()
        await hub.close()
        await close_test_runtime(scheduler)


@pytest.mark.asyncio
async def test_shared_output_reader_preserves_errors_and_value_error_contract() -> None:
    """终端读取仍保留无输出正文，内部非法分页继续抛出普通 ValueError。"""
    record = TaskRecord("task", "session", None, TaskStatus.COMPLETED, None, None, None, "now", "now")
    with pytest.raises(InvalidOperationError) as error:
        await read_terminal_output(record, 0, 1)
    assert str(error.value) == "此任务没有终端输出"
    assert isinstance(error.value, ValueError)
    for offset, limit in [(-1, 1), (0, 0), (0, 65537)]:
        with pytest.raises(ValueError) as invalid:
            await read_terminal_output(record, offset, limit)
        assert type(invalid.value) is ValueError
        assert str(invalid.value) == "无效的输出分页参数"


@pytest.mark.parametrize("session_id", ["missing", "with'quote", 'with"quote', "with\nnewline"])
def test_missing_configuration_preserves_key_error_string(tmp_path: Path, session_id: str) -> None:
    """各种会话编号的不存在说明都保持原 KeyError 的字符串格式。

    Args:
        tmp_path (Path): 隔离的 YAML 目录。
        session_id (str): 包含普通字符或引号的未知编号。
    """
    from app.errors import ResourceNotFoundError

    config = tmp_path / "settings.yaml"
    config.write_text("chat_models: []\n", encoding="utf-8")
    with pytest.raises(ResourceNotFoundError) as error:
        ChatSettingsDao(config).get_chat_settings(session_id)
    assert str(error.value) == str(KeyError(f"session_id not found: {session_id}"))
