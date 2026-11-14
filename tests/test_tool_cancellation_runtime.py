"""验证用户停止工具时的 Inbox、会话暂停和独立终端任务所有权。"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.models.contracts import InputMessage, StreamChunk, ToolDefinition
from app.agent.plugins.runtime import PluginRuntime
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.runtime.tasks.base import BaseTask
from app.agent.tools.terminal.task import TerminalTask
from app.agent.runtime.turn_control import TurnInterrupted
from app.agent.runtime.agent_execution import AgentExecution
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import ToolCall
from app.agent.state.model import AgentState
from app.agent.tools.core.base import ToolContext
from app.agent.tools.core.runtime import ToolRuntime
from app.agent.tools.exec_command import ExecCommandTool
from app.agent.tools.authorization.manager import ToolAuthorization
from app.crud.sqlite_factory import SqliteFactory
from app.crud.task_record_dao import TaskRecordDao
from app.interaction.interaction_contract import ChannelCommand
from app.schemas.runtime_types import InboxKind, InboxStatus, SessionState, TaskStatus
from app.schemas.runtime_types import AgentRunOutcomeKind
from app.schemas.chat_settings import ChatSettings
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.subagent_types import SubagentAction
from app.agent.state.messages import ToolMessage
from tests.subagent_support import SubagentHarness, wait_for_subagent_worker
from tests.runtime_support import build_test_scheduler, close_test_runtime, configured_settings, make_execution_control
from tests.test_runtime_acceptance import next_kind
from tests.test_tool_cancellation import BarrierTool
from tests.test_checkpoint_recovery import ScriptedLLM

import pytest


def test_stop_overrides_steer_and_keeps_pending_input_until_next_explicit_message(tmp_path: Path) -> None:
    """引导之后停止工具会暂停会话，通知不会唤醒，原引导在下一次明确输入后交付。

    Args:
        tmp_path (Path): 本用例独占的会话数据库。
    """
    async def scenario() -> None:
        """经实际 Scheduler 发送引导、停止和通知，观察公开快照与持久交付结果。"""
        factory = SqliteFactory(tmp_path / "state.sqlite3")
        tool = BarrierTool()
        inputs: list[str] = []

        class Model:
            """首轮调用工具，恢复工作后记录实际收到的输入。"""

            async def astream(self, messages: Sequence[InputMessage], tools: Sequence[ToolDefinition] | None = None) -> AsyncIterator[StreamChunk]:
                """记录每次请求的正文，首轮保持工具等待，之后输出最终回答。

                Args:
                    messages (Sequence[InputMessage]): 本次模型可见的历史及已交付输入。
                    tools (Sequence[ToolDefinition] | None): 本次公开的工具定义。

                Yields:
                    StreamChunk: 首轮工具调用或后续回答正文。
                """
                inputs.append(str(messages))
                if len(inputs) == 1:
                    yield StreamChunk(tool_call=ToolCall("waiting", tool.name, {"label": "waiting"}))
                else:
                    yield StreamChunk(content="新工作已完成")

            async def close(self) -> None:
                """脚本模型没有网络资源。"""

        def make_agent(context: str, manager: SessionStateManager) -> Agent:
            """每轮装配真实 Agent，存储由会话借用，模型只替代外部响应。

            Args:
                context (str): 当前会话的实际上下文编号。
                manager (SessionStateManager): 会话借给本轮的持久化入口。

            Returns:
                Agent: 使用离线模型和真实工具的执行对象。
            """
            agent = Agent(AgentConfig(context, "test", "test", ContextStrategyConfig(compression_trigger_tokens_k=100)),
                factory, llm_client=Model(), session_state_manager=manager, enable_plugin_hooks=False)
            agent.register_tool(tool)
            return agent

        queue = asyncio.Queue()
        runtime = build_test_scheduler(runtime_queue=queue, agent_factory=make_agent,
            session_state_manager_factory=lambda context: SessionStateManager(context, factory),
            chat_settings_dao=configured_settings("s"))
        reader = SessionStateManager("s", factory)
        try:
            await runtime.start()
            assert (await runtime.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "旧工作"}))).accepted
            await asyncio.wait_for(tool.entered.wait(), 5)
            run_id = (await runtime.snapshot("s")).payload["run_id"]
            assert (await runtime.submit(ChannelCommand("s", "desktop", "human_steer", {"message": "保留的引导"},
                target_run_id=run_id))).accepted
            pending = await reader.read_next(kind=InboxKind.HUMAN)
            assert pending is not None and pending.payload["message"] == "保留的引导"
            assert (await runtime.submit(ChannelCommand("s", "desktop", "stop_turn", target_run_id=run_id))).accepted
            await asyncio.wait_for(next_kind(runtime, "run_completed"), 5)
            assert tool.cleaned.is_set()
            assert await reader.status(pending.id) == InboxStatus.PENDING
            assert (await runtime.snapshot("s")).payload["session_state"] == SessionState.PAUSED
            plugin = PluginRuntime("test", "s", run_id, _supervisor=runtime.task_supervisor, _runtime_queue=queue)
            await plugin.notify(name="background", text="后台通知")
            await asyncio.wait_for(queue.join(), 3)
            assert (await runtime.snapshot("s")).payload["session_state"] == SessionState.PAUSED
            assert len(inputs) == 1
            assert await reader.status(pending.id) == InboxStatus.PENDING
            assert (await runtime.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "恢复工作"}))).accepted
            await asyncio.wait_for(next_kind(runtime, "run_completed"), 5)
            await asyncio.wait_for(next_kind(runtime, "run_completed"), 5)
            assert await reader.status(pending.id) == InboxStatus.DELIVERED
            assert len(inputs) == 3
            assert "保留的引导" in inputs[1]
            assert "恢复工作" in inputs[2]
        finally:
            tool.release.set()
            await close_test_runtime(runtime)
            await reader.close()

    asyncio.run(scenario())


def test_stop_during_waiting_cleanup_saves_latest_control_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """等待审批收尾期间停止接管引导，保存时使用状态机最终确定的 STOP 原因。

    Args:
        tmp_path (Path): 本用例独占的审批和会话数据库。
        monkeypatch (pytest.MonkeyPatch): 在取消持久审批的公共入口建立竞争屏障。
    """
    async def scenario() -> None:
        """经实际 Execution 进入等待，再按引导、清理阻塞、停止顺序收尾。"""
        factory = SqliteFactory(tmp_path / "state.sqlite3")
        manager = SessionStateManager("s", factory)
        tool = BarrierTool()
        settings = ChatSettings(session_id="s", model_name="test", openai_api_key="test",
            openai_base_url="http://127.0.0.1:1/v1", temperature=0, system_prompt="test",
            context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
            tool_permissions={"local:barrier": "ask"})

        def make_agent(borrowed: SessionStateManager) -> Agent:
            """为当前执行装配真实统一审批和隔离持久化。

            Args:
                borrowed (SessionStateManager): 会话借给 Agent 的持久化入口。

            Returns:
                Agent: 首次请求工具审批的离线 Agent。
            """
            agent = Agent(AgentConfig("s", "test", "test", settings.context_strategy), factory,
                llm_client=ScriptedLLM([[StreamChunk(tool_call=ToolCall("waiting", tool.name, {"label": "waiting"}))]]),
                tool_authorization=ToolAuthorization(lambda _session: settings), session_state_manager=borrowed)
            agent.register_tool(tool)
            return agent

        async def prepare(run_id: str) -> bool:
            """允许本次选中的输入开始执行。

            Args:
                run_id (str): Execution 为本批输入建立的运行编号。

            Returns:
                bool: 当前会话允许执行。
            """
            return True

        execution = AgentExecution("s", manager=manager, agent_factory=make_agent, events=asyncio.Queue())
        entered, release = asyncio.Event(), asyncio.Event()
        completion = None
        try:
            human = await manager.submit_human("desktop", "执行")
            assert (await execution.execute((human,), prepare=prepare)).kind == AgentRunOutcomeKind.INTERRUPTED
            steer = await manager.submit_human("desktop", "保留的引导")
            assert execution.request_steer(str(steer.id)).accepted
            cancel = manager.cancel_pending_interrupt

            async def delayed_cancel():
                """暂停真实审批取消，使停止能够接管正在收尾的引导。

                Returns:
                    tuple[str, str] | None: 原审批取消结果。
                """
                entered.set()
                await release.wait()
                return await cancel()

            monkeypatch.setattr(manager, "cancel_pending_interrupt", delayed_cancel)
            completion = asyncio.create_task(execution.finish_waiting())
            await asyncio.wait_for(entered.wait(), 3)
            assert execution.request_stop().accepted
            release.set()
            await asyncio.wait_for(completion, 3)
            saved = await manager.load()
            assert [item.text for item in saved.messages if isinstance(item, ToolMessage)] == ["被用户取消，工具调用未完成"]
            assert await manager.status(steer.id) == InboxStatus.PENDING
        finally:
            release.set()
            if completion is not None:
                await asyncio.gather(completion, return_exceptions=True)
            await execution.close("测试结束")
            await manager.close()

    asyncio.run(scenario())


def test_stop_during_prepared_turn_cleanup_returns_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """准备阶段的引导被停止接管后，执行结果与实际保存的停止原因一致。

    Args:
        tmp_path (Path): 本用例独占的会话数据库。
        monkeypatch (pytest.MonkeyPatch): 在等待收尾入口暂停，控制引导与停止的先后顺序。
    """
    async def scenario() -> None:
        """经公开执行入口受理引导，在收尾等待期间停止并检查执行结果和 Inbox。"""
        manager = SessionStateManager("s", SqliteFactory(tmp_path / "state.sqlite3"))

        def make_agent(borrowed: SessionStateManager) -> Agent:
            """执行在准备阶段结束，若启动 Agent 则说明已越过停止边界。

            Args:
                borrowed (SessionStateManager): 当前会话借给执行的持久化入口。

            Raises:
                AssertionError: 已受理停止的执行仍然尝试启动 Agent。
            """
            raise AssertionError("停止后不应启动 Agent")

        execution = AgentExecution("s", manager=manager, agent_factory=make_agent, events=asyncio.Queue())
        entered, release = asyncio.Event(), asyncio.Event()
        cancel = manager.cancel_pending_interrupt
        completion = None
        try:
            steer = await manager.submit_human("desktop", "保留的引导")

            async def prepare(run_id: str) -> bool:
                """准备期间受理引导，进入当前轮次的收尾流程。

                Args:
                    run_id (str): 当前执行已经登记的运行编号。

                Returns:
                    bool: 会话业务准备成功，执行仍需遵循已受理控制。
                """
                assert execution.request_steer(str(steer.id), run_id).accepted
                return True

            async def delayed_cancel() -> tuple[str, str] | None:
                """让用户停止在当前引导的收尾等待期间被受理。

                Returns:
                    tuple[str, str] | None: 真实等待取消结果及来源渠道。
                """
                entered.set()
                await release.wait()
                return await cancel()

            monkeypatch.setattr(manager, "cancel_pending_interrupt", delayed_cancel)
            completion = asyncio.create_task(execution.execute((), prepare=prepare))
            await asyncio.wait_for(entered.wait(), 3)
            assert execution.request_stop().accepted
            release.set()
            outcome = await asyncio.wait_for(completion, 3)
            assert outcome.kind == AgentRunOutcomeKind.STOPPED
            assert await manager.status(steer.id) == InboxStatus.PENDING
            assert not (await manager.load()).pending_tool_calls
        finally:
            release.set()
            if completion is not None:
                await asyncio.gather(completion, return_exceptions=True)
            await execution.close("测试结束")
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("retain", [False, True])
def test_stop_child_cancels_tool_and_saves_child_history(tmp_path: Path, retain: bool) -> None:
    """父停止子执行时取消实际工具，一次性与保留型均先保存工具取消结果。

    Args:
        tmp_path (Path): 本用例独占的父子上下文存储。
        retain (bool): 子执行是否保留上下文，继续使用各自既有结束政策。
    """
    async def scenario() -> None:
        """从父子公开命令停止工具，通过子 Manager 查询持久协议。"""
        tool = BarrierTool()

        async def script(messages: Sequence[InputMessage], tools: Sequence[ToolDefinition] | None) -> AsyncIterator[StreamChunk]:
            """输出一个实际工具调用，供父停止子执行。

            Args:
                messages (Sequence[InputMessage]): 子 Agent 的当前历史和任务正文。
                tools (Sequence[ToolDefinition] | None): 本次允许子调用的工具定义。

            Yields:
                StreamChunk: 进入持续等待的工具调用。
            """
            yield StreamChunk(tool_call=ToolCall("waiting", tool.name, {"label": "waiting"}))

        async with SubagentHarness(tmp_path / "state.sqlite3", script, retain=retain, tools=(tool,)) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(tool.entered.wait(), 3)
            stopped = await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.STOP)
            assert stopped.accepted
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 5)
            assert tool.cleaned.is_set()
            reader = SessionStateManager(receipt.context_id, SqliteFactory(harness.path),
                storage_scope=AgentStorageScope.SUBAGENT, parent_session_id="parent")
            try:
                saved = await reader.load()
                assert [item.text for item in saved.messages if isinstance(item, ToolMessage)] == ["被用户取消，工具调用未完成"]
                assert not saved.pending_tool_calls
            finally:
                await reader.close()

    asyncio.run(scenario())


def test_stopping_terminal_tool_keeps_registered_process_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """取消终端工具的前台等待后，已登记进程由 Supervisor 持续管理。

    Args:
        tmp_path (Path): 本用例隔离的任务数据库及工作目录。
        monkeypatch (pytest.MonkeyPatch): 将工具工作目录限定在测试目录。
    """
    monkeypatch.setattr("app.agent.tools.exec_command.WORKDIR", tmp_path)

    async def scenario() -> None:
        """启动真实长运行进程，取消工具等待，再查询进程记录与执行对象。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(tmp_path / "state.sqlite3")))
        registered = asyncio.Event()
        task_ids: list[str] = []

        class ObservedRuntime(ToolRuntime):
            """在真实任务提交成功后提供测试屏障，后台执行仍由 Supervisor 接管。"""

            async def submit_task(self, task: BaseTask, *, name: str, title: str,
                                  notify_agent: bool, background: bool = True) -> str:
                """提交真实终端任务，回报本次已由管理器持有的任务编号。

                Args:
                    task (BaseTask): 具体终端执行与清理对象。
                    name (str): 当前任务的稳定类型名称。
                    title (str): 当前命令的展示标题。
                    notify_agent (bool): 终态是否通知 Agent。
                    background (bool): 登记时是否在后台列表展示。

                Returns:
                    str: 已保存并由 Supervisor 接管的任务编号。
                """
                task_id = await super().submit_task(task, name=name, title=title,
                    notify_agent=notify_agent, background=background)
                task_ids.append(task_id)
                registered.set()
                return task_id

        runtime = ObservedRuntime("s", "run", "exec_command", "call", supervisor, supervisor.runtime_queue)
        machine, control = make_execution_control("run")
        context = ToolContext("s", AgentState.create_new("s"), runtime=runtime)
        tool = ExecCommandTool()
        invocation = asyncio.create_task(control.run_tool(lambda: tool.execute(
            {"cmd": 'python -c "import time; time.sleep(60)"', "yield_time_ms": 30000}, context)))
        try:
            async with asyncio.timeout(15):
                await registered.wait()
                task_id = task_ids[0]
                task = supervisor.get_task(task_id)
                assert isinstance(task, TerminalTask)
                await task.ready.wait()
            assert machine.request_stop("run").accepted
            control.wake_interrupt()
            with pytest.raises(TurnInterrupted, match="^stop$"):
                await asyncio.wait_for(invocation, 3)
            record = await supervisor.get_task_snapshot(task_id)
            assert record.status == TaskStatus.RUNNING
            assert record.background
            assert supervisor.get_task(task_id) is task
            assert supervisor.active_task_count() == 1
        finally:
            invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)
            await supervisor.close()
            await supervisor.records.close()

    asyncio.run(scenario())
