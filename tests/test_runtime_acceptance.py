"""补齐步骤 08 的输入竞争、等待保存和资源预算业务验收。"""

import asyncio
from pathlib import Path
import sqlite3

import pytest

from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.models.contracts import StreamChunk
from app.agent.runtime.subagent_requests import CreateSubagentRequest, SubagentSource
from app.agent.runtime.tasks.base import BaseTask
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import ToolCall, ToolMessage
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.core.runtime import ToolRuntime
from app.agent.utils.infra.bounded_operation import close_resources
from app.crud.sqlite_factory import SqliteFactory
from app.interaction.interaction_contract import ChannelCommand
from app.schemas.chat_settings import ChatSettings
from app.schemas.runtime_types import CommandStatus, SessionState
from app.schemas.subagent_types import SubagentAction, SubagentPublicStatus
from app.agent.tools.core.base import BaseTool, ToolResult
from tests.runtime_support import build_test_scheduler, close_test_runtime, configured_settings, next_runtime_event, runtime_hub
from tests.subagent_support import SubagentHarness, wait_for_subagent_status, wait_for_subagent_worker


async def next_kind(runtime, kind):
    """等待目标展示事实，超时由测试调用方限定。

    Args:
        runtime (AgentRuntimeScheduler): 隔离运行时。
        kind (str): 预期展示事件。

    Returns:
        InteractionEvent: 已经过统一队列发布的目标事件。
    """
    while True:
        event = await next_runtime_event(runtime)
        if event.kind == kind:
            return event


def test_parent_new_input_before_old_stop_completion_stays_enabled(tmp_path: Path, monkeypatch) -> None:
    """旧执行停止保存期间受理新输入，旧结束后自动执行新正文。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 在旧执行的协议保存前建立屏障。
    """
    async def scenario() -> None:
        """按模型开始、停止、旧保存、新输入、旧完成的顺序验证两个轮次。"""
        factory = SqliteFactory(tmp_path / "stop-order.sqlite3")
        running, saving, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = SessionStateManager.finish_interrupted_turn
        calls = []

        async def finish(manager, *args, **kwargs):
            """延迟旧执行的真实收尾保存，允许新输入先完成受理。

            Args:
                manager (SessionStateManager): 原执行借用的状态对象。
                args (tuple): 原保存材料。
                kwargs (dict): 原保存选项。
            """
            saving.set()
            await release.wait()
            return await original(manager, *args, **kwargs)

        class Model:
            """第一轮等待停止，第二轮记录新输入并完成。"""
            async def astream(self, messages, tools=None):
                """记录实际模型输入，第一轮保持可停止的分片等待。

                Args:
                    messages (Sequence[BaseMessage | ProjectedMessage]): 本次请求模型使用的已投影对话消息。
                    tools (Sequence[ToolDefinition] | None): 本次请求向模型公开的工具目录。

                Yields:
                    StreamChunk: 用于触发本场景业务路径的离线模型响应。
                """
                calls.append(str(messages))
                if len(calls) == 1:
                    running.set()
                    await asyncio.Event().wait()
                yield StreamChunk(content="新工作已完成")

            async def close(self):
                """脚本模型没有外部资源。"""

        runtime = build_test_scheduler(runtime_queue=asyncio.Queue(),
            agent_factory=lambda context, manager: Agent(AgentConfig(context, "test", "test", ContextStrategyConfig()),
                factory, llm_client=Model(), session_state_manager=manager, enable_plugin_hooks=False),
            session_state_manager_factory=lambda context: SessionStateManager(context, factory),
            chat_settings_dao=configured_settings("s"))
        try:
            await runtime.start()
            await runtime.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "旧工作"}))
            await asyncio.wait_for(running.wait(), 3)
            old_run = (await runtime.snapshot("s")).payload["run_id"]
            monkeypatch.setattr(SessionStateManager, "finish_interrupted_turn", finish)
            assert (await runtime.submit(ChannelCommand("s", "desktop", "stop_turn"))).accepted
            await asyncio.wait_for(saving.wait(), 3)
            assert (await runtime.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "新明确工作"}))).accepted
            assert (await runtime.snapshot("s")).payload["session_state"] == SessionState.ENABLED
            release.set()
            first = await asyncio.wait_for(next_kind(runtime, "run_completed"), 3)
            second = await asyncio.wait_for(next_kind(runtime, "run_completed"), 3)
            assert first.run_id == old_run and second.run_id != old_run
            assert len(calls) == 2 and "新明确工作" in calls[1]
            assert (await runtime.snapshot("s")).payload["session_state"] == SessionState.ENABLED
        finally:
            release.set()
            await close_test_runtime(runtime)
    asyncio.run(scenario())


@pytest.mark.parametrize("failures", [0, 3, 4])
def test_final_checkpoint_attempts_and_failed_close_keep_business_truth(tmp_path: Path, monkeypatch, caplog, failures: int) -> None:
    """最终保存最多四次，耗尽报错暂停；关闭持续失败仍释放会话其他连接。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 注入真实事务内的暂时占用。
        caplog (pytest.LogCaptureFixture): 检查关闭耗尽日志。
        failures (int): 最终保存预定失败次数。
    """
    async def scenario() -> None:
        """统计实际 DAO 尝试、模型调用和关闭次数，核对保存后的历史。"""
        from app.crud.agent_state_dao import AgentStateDao
        factory = SqliteFactory(tmp_path / "final-attempts.sqlite3")
        attempts = calls = closes = 0
        original = AgentStateDao._write_state_in_transaction
        manager_closed = asyncio.Event()
        owned_manager = None

        async def save(dao, db, state, **kwargs):
            """仅在最终事务中注入 BUSY，其他保存继续使用实际 SQLite。

            Args:
                dao (AgentStateDao): 本上下文的 checkpoint 所有者。
                db (aiosqlite.Connection): 当前事务连接。
                state (AgentState): 本次固定保存材料。
                kwargs (dict): checkpoint 类型等保存选项。
            """
            nonlocal attempts
            if kwargs.get("checkpoint_type") == "completed":
                attempts += 1
                if attempts <= failures:
                    error = sqlite3.OperationalError("最终保存暂时占用")
                    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise error
            return await original(dao, db, state, **kwargs)

        class Manager(SessionStateManager):
            """观察会话实际资源关闭，保留真实连接释放。"""
            async def close(self):
                """两个 DAO 关闭后通知观察者。"""
                await super().close()
                if self is owned_manager:
                    manager_closed.set()

        def create_manager(context: str) -> SessionStateManager:
            """记录交给父会话的首个状态对象，短期快照查询各自关闭。

            Args:
                context (str): 当前需要访问的上下文编号。

            Returns:
                SessionStateManager: 使用隔离数据库的状态对象。
            """
            nonlocal owned_manager
            manager = Manager(context, factory)
            if context == "s" and owned_manager is None:
                owned_manager = manager
            return manager

        class Model:
            """最终保存重试始终使用同一次模型输出。"""
            async def astream(self, messages, tools=None):
                """产生一次完整回答并统计外部业务执行。

                Args:
                    messages (Sequence[BaseMessage | ProjectedMessage]): 本次请求模型使用的已投影对话消息。
                    tools (Sequence[ToolDefinition] | None): 本次请求向模型公开的工具目录。

                Yields:
                    StreamChunk: 用于触发本场景业务路径的离线模型响应。
                """
                nonlocal calls
                calls += 1
                yield StreamChunk(content="固定最终正文")

            async def close(self):
                """保存耗尽用例同时注入资源关闭持续失败。"""
                nonlocal closes
                closes += 1
                if failures == 4:
                    raise OSError("模型资源关闭失败")

        runtime = build_test_scheduler(runtime_queue=asyncio.Queue(),
            agent_factory=lambda context, manager: Agent(AgentConfig(context, "test", "test", ContextStrategyConfig()),
                factory, llm_client=Model(), session_state_manager=manager, enable_plugin_hooks=False),
            session_state_manager_factory=create_manager,
            chat_settings_dao=configured_settings("s"))
        try:
            await runtime.start()
            monkeypatch.setattr(AgentStateDao, "_write_state_in_transaction", save)
            await runtime.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "保存固定材料"}))
            events = []
            ending = "history_restored" if failures == 4 else "run_completed"
            while not events or events[-1].kind != ending:
                events.append(await asyncio.wait_for(next_runtime_event(runtime), 3))
            assert attempts == min(failures + 1, 4) and calls == 1
            assert len([item for item in events if item.kind == "error"]) == int(failures == 4)
            assert len([item for item in events if item.kind == "run_completed"]) == int(failures != 4)
            snapshot = (await runtime.snapshot("s")).payload
            assert snapshot["session_state"] == (SessionState.PAUSED if failures == 4 else SessionState.ENABLED)
            await runtime.begin_shutdown()
            await asyncio.wait_for(runtime.wait_for_workers(), 3)
            assert manager_closed.is_set()
            assert closes == (4 if failures == 4 else 1)
            reader = SessionStateManager("s", factory)
            try:
                state = await reader.load()
                assert ("固定最终正文" in str(state.messages)) == (failures != 4)
            finally:
                await reader.close()
            if failures == 4:
                assert any("释放资源失败" in item.message for item in caplog.records)
        finally:
            await close_test_runtime(runtime)
    asyncio.run(scenario())


def test_approval_notice_and_queue_events_share_order_without_slow_observer_blocking(tmp_path: Path) -> None:
    """审批保留期间保存通知和排队消息，三类事件有序发布，慢连接独立结束。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """真实执行产生审批、会话产生排队、Supervisor 产生终态，再验证停止。"""
        from app.schemas.task_types import PluginRequest, PluginRequestKind
        factory = SqliteFactory(tmp_path / "output-order.sqlite3")
        model_calls = 0
        settings = ChatSettings(session_id="s", model_name="test", openai_api_key="test",
            openai_base_url="http://unused", temperature=0, system_prompt="test", context_strategy=ContextStrategyConfig())

        class Protected(BaseTool):
            """审批尚未决定时禁止实际副作用。"""
            name, description = "ordered_protected", "验证审批保持"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args, context):
                """任何实际调用均意味着通知错误替代了审批。

                Args:
                    args (dict[str, object]): 模型提供的工具业务参数。
                    context (ToolContext): 框架绑定的工具来源和任务提交能力。
                """
                raise AssertionError("审批工具提前执行")

        class Model:
            """启动后只请求一次审批。"""
            async def astream(self, messages, tools=None):
                """统计通知是否意外重新启动了模型。

                Args:
                    messages (Sequence[BaseMessage | ProjectedMessage]): 本次请求模型使用的已投影对话消息。
                    tools (Sequence[ToolDefinition] | None): 本次请求向模型公开的工具目录。

                Yields:
                    StreamChunk: 用于触发本场景业务路径的离线模型响应。
                """
                nonlocal model_calls
                model_calls += 1
                yield StreamChunk(tool_call=ToolCall("ordered-call", Protected.name, {}))

            async def close(self):
                """脚本模型没有外部资源。"""

        class Work(BaseTask):
            """审批期间独立完成一次普通后台业务。"""
            async def run(self, runtime):
                """返回将与唯一 Inbox 通知原子保存的结果。

                Args:
                    runtime (TaskRuntime): Supervisor 绑定的本次任务来源及通知能力。
                """
                return "审批期间的后台结果"

        def create(context, manager):
            """装配真实权限检查及模型工具流程。

            Args:
                context (str): 父上下文编号。
                manager (SessionStateManager): 父会话借用状态对象。

            Returns:
                Agent: 使用受审批保护工具的实际 Agent。
            """
            agent = Agent(AgentConfig(context, "test", "test", ContextStrategyConfig()), factory,
                llm_client=Model(), session_state_manager=manager, enable_plugin_hooks=False,
                tool_authorization=ToolAuthorization(lambda _: settings))
            agent.register_tool(Protected())
            return agent

        runtime = build_test_scheduler(runtime_queue=asyncio.Queue(), agent_factory=create,
            session_state_manager_factory=lambda context: SessionStateManager(context, factory), chat_settings_dao=configured_settings("s"))
        slow = None
        try:
            await runtime.start()
            slow = await runtime_hub(runtime).register("s", "desktop")
            await runtime.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "等待审批"}))
            events = []
            while not events or events[-1].kind != "interrupt":
                events.append(await asyncio.wait_for(next_runtime_event(runtime), 3))
            approval = events[-1]
            assert (await runtime.submit(ChannelCommand("s", "desktop", "queue_turn", {"message": "审批后的队列"}))).accepted
            task_runtime = ToolRuntime("s", approval.run_id, "work", "call", runtime.task_supervisor, runtime.runtime_queue)
            task_id = await task_runtime.submit_task(Work(), name="work", title="审批独立任务", notify_agent=True)
            assert (await runtime.task_supervisor.wait(task_id, timeout_ms=2000)).status == "completed"
            await runtime.runtime_queue.join()
            while not runtime_hub(runtime).watermark("s")[1] == events[-1].event_sequence:
                events.append(await asyncio.wait_for(next_runtime_event(runtime), 3))
            kinds = [item.kind for item in events]
            assert kinds.index("interrupt") < kinds.index("queued_message") < len(kinds) - 1
            assert len([item for item in events if item.kind == "task_status" and item.payload["status"] == "completed"]) == 1
            assert [item.event_sequence for item in events] == list(range(1, len(events) + 1))
            snapshot = (await runtime.snapshot("s")).payload
            assert snapshot["interrupt"]["request_id"] == approval.request_id and model_calls == 1
            reader = SessionStateManager("s", factory)
            try:
                assert len(await reader.read_task_terminals()) == 1
            finally:
                await reader.close()
            for index in range(260):
                runtime.runtime_queue.put_nowait(PluginRequest(kind=PluginRequestKind.PUBLISH_EVENT, session_id="s",
                    run_id=approval.run_id, plugin_name="acceptance", name="notice", payload={"text": str(index)}))
            await asyncio.wait_for(runtime.runtime_queue.join(), 3)
            assert slow is not None and slow.queue.get_nowait() is None
            assert (await asyncio.wait_for(runtime.submit(ChannelCommand("s", "desktop", "stop_turn")), 1)).accepted
            await asyncio.wait_for(next_kind(runtime, "authorization_resolved"), 3)
            assert model_calls == 1
        finally:
            if slow is not None:
                runtime_hub(runtime).unsubscribe(slow)
            await close_test_runtime(runtime)
    asyncio.run(scenario())


@pytest.mark.parametrize("waiting_kind", ["approval", "parent"])
def test_stop_while_wait_transaction_commits_cannot_reenter_waiting(tmp_path: Path, monkeypatch, waiting_kind: str) -> None:
    """等待事务已保存但回调尚未返回时停止，旧等待和协议只结束一次。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 延迟真实等待保存返回。
        waiting_kind (str): 人工审批或父消息等待。
    """
    async def scenario() -> None:
        """真实 Pipeline 保存等待后接受 stop，再放行迟到保存回调。"""
        from app.schemas.agent_storage import AgentStorageScope
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        method = "save_progress" if waiting_kind == "approval" else "commit_parent_interrupt"
        original = getattr(SessionStateManager, method)

        async def save(manager, state, *args, **kwargs):
            """等待材料实际落库后设置屏障，保留真实持久事务。

            Args:
                manager (SessionStateManager): 子会话状态对象。
                state (AgentState): 包含待保存中断的候选状态。
                args (tuple): 原保存参数。
                kwargs (dict): 原保存选项。
            """
            result = await original(manager, state, *args, **kwargs)
            if manager.session_id != "parent" and state.interrupt_data and not entered.is_set():
                entered.set()
                await release.wait()
            return result

        class Protected(BaseTool):
            """审批尚未批准，实际执行会使验收失败。"""
            name, description = "acceptance_protected", "受审批保护的验收操作"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args, context) -> ToolResult:
                """记录意外执行的工具副作用。

                Args:
                    args (dict[str, object]): 模型提供的工具业务参数。
                    context (ToolContext): 框架绑定的工具来源和任务提交能力。
                """
                raise AssertionError("停止后仍执行了审批工具")

        async def script(messages, tools):
            """仅请求一次等待，停止后不会再次调用模型。

            Args:
                messages (Sequence[BaseMessage | ProjectedMessage]): 本次请求模型使用的已投影对话消息。
                tools (Sequence[ToolDefinition] | None): 本次请求向模型公开的工具目录。

            Yields:
                StreamChunk: 用于触发本场景业务路径的离线模型响应。
            """
            nonlocal calls
            calls += 1
            yield StreamChunk(tool_call=ToolCall("wait-call", Protected.name if waiting_kind == "approval" else "message_parent",
                {} if waiting_kind == "approval" else {"text": "需要父确认", "expect_reply": True}))

        settings = ChatSettings(session_id="parent", model_name="test", openai_api_key="test",
            openai_base_url="http://unused", temperature=0, system_prompt="test",
            context_strategy=ContextStrategyConfig())
        authorization = ToolAuthorization(lambda _: settings)
        monkeypatch.setattr(SessionStateManager, method, save)
        async with SubagentHarness(tmp_path / "wait-save.sqlite3", script, tools=(Protected(),),
                authorization=authorization if waiting_kind == "approval" else None) as harness:
            child = await harness.create()
            try:
                await asyncio.wait_for(entered.wait(), 3)
                assert (await harness.scheduler.interact_subagent("parent", child.context_id, SubagentAction.STOP)).accepted
            finally:
                release.set()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, child.context_id), 3)
            detail = await harness.scheduler.get_subagent_details("parent", child.context_id)
            assert [item["status"] for item in detail["results"]] == ["stopped"]
            assert calls == 1
            assert (await harness.scheduler.snapshot_subagents("parent"))[0].status == SubagentPublicStatus.IDLE
            manager = SessionStateManager(child.context_id, SqliteFactory(harness.path), storage_scope=AgentStorageScope.SUBAGENT)
            try:
                state = await manager.load()
                assert state.interrupt_data is None and not state.pending_tool_calls
                assert len([item for item in state.messages if isinstance(item, ToolMessage) and item.tool_call_id == "wait-call"]) == 1
            finally:
                await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("retain", [False, True])
@pytest.mark.parametrize("message_first", [False, True])
def test_child_completion_and_message_have_explicit_order(tmp_path: Path, retain: bool, message_first: bool) -> None:
    """新消息先受理便继续处理，关闭先完成的一次性子明确拒绝。

    Args:
        tmp_path (Path): 隔离数据库目录。
        retain (bool): 是否保留子身份。
        message_first (bool): 是否让新消息先于原模型完成受理。
    """
    async def scenario() -> None:
        """用模型屏障固定两种顺序，核对回执、实际输入和父报告。"""
        started, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def script(messages, tools):
            """第一段可控等待，后续明确输入在同一子历史中处理。

            Args:
                messages (Sequence[BaseMessage | ProjectedMessage]): 本次请求模型使用的已投影对话消息。
                tools (Sequence[ToolDefinition] | None): 本次请求向模型公开的工具目录。

            Yields:
                StreamChunk: 用于触发本场景业务路径的离线模型响应。
            """
            seen.append(str(messages))
            if len(seen) == 1:
                started.set()
                await release.wait()
            yield StreamChunk(content="后续已处理" if len(seen) > 1 else "首次已完成")

        async with SubagentHarness(tmp_path / "finish-message.sqlite3", script, retain=retain) as harness:
            child = await harness.create()
            await asyncio.wait_for(started.wait(), 3)
            try:
                if not message_first:
                    release.set()
                    await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, child.context_id), 3)
                receipt = await harness.scheduler.interact_subagent("parent", child.context_id,
                    SubagentAction.COMMAND, "明确后续消息")
                assert receipt.accepted == (message_first or retain)
                if not receipt.accepted:
                    assert receipt.status == CommandStatus.CLOSED
            finally:
                release.set()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, child.context_id), 3)
            detail = await harness.scheduler.get_subagent_details("parent", child.context_id)
            if receipt.accepted:
                assert len(seen) == 2 and "明确后续消息" in seen[1]
                assert detail["results"][0]["result"] == "后续已处理"
            else:
                assert len(seen) == 1 and len(detail["results"]) == 1
            assert len(detail["results"]) == (2 if retain and not message_first else 1)
    asyncio.run(scenario())


def test_parent_accepts_input_while_close_budget_expires(tmp_path: Path, monkeypatch, caplog) -> None:
    """关闭预算耗尽后已受理的新输入自动执行，关闭错误保留日志。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 缩短实际 Agent 关闭总预算。
        caplog (pytest.LogCaptureFixture): 核对关闭耗尽的日志。
    """
    async def scenario() -> None:
        """第一模型关闭挂起，第二输入在预算到期前完成受理。"""
        factory = SqliteFactory(tmp_path / "close-budget.sqlite3")
        closing, second = asyncio.Event(), asyncio.Event()
        created = 0

        async def bounded(resources, **kwargs):
            """保留真实关闭机制，仅把测试预算缩短到半秒。

            Args:
                resources (tuple): Agent 实际拥有的待关闭资源。
                kwargs (dict): 原关闭日志与重试策略。
            """
            await close_resources(resources, timeout_seconds=0.5, **kwargs)

        class Model:
            """按实例区分首次关闭与新输入产生的后续执行。"""
            def __init__(self, index: int):
                """记录本模型对应的创建顺序。

                Args:
                    index (int): 当前模型实例序号。
                """
                self.index = index

            async def astream(self, messages, tools=None):
                """第二实例核验新输入，向观察者报告业务已推进。

                Args:
                    messages (Sequence[BaseMessage | ProjectedMessage]): 本次请求模型使用的已投影对话消息。
                    tools (Sequence[ToolDefinition] | None): 本次请求向模型公开的工具目录。

                Yields:
                    StreamChunk: 用于触发本场景业务路径的离线模型响应。
                """
                if self.index == 2:
                    assert "关闭期间的新输入" in str(messages)
                    second.set()
                yield StreamChunk(content="已完成")

            async def close(self):
                """首次关闭持续等待，实际关闭工具负责预算耗尽。"""
                if self.index == 1:
                    closing.set()
                    await asyncio.Event().wait()

        def agent(context, manager):
            """为后续工作创建新的模型资源，Manager 继续由会话拥有。

            Args:
                context (str): 父上下文编号。
                manager (SessionStateManager): 会话借给 Agent 的状态对象。

            Returns:
                Agent: 使用真实 Pipeline 的独立执行实例。
            """
            nonlocal created
            created += 1
            return Agent(AgentConfig(context, "test", "test", ContextStrategyConfig()), factory,
                session_state_manager=manager, llm_client=Model(created), enable_plugin_hooks=False)

        monkeypatch.setattr("app.agent.agent.close_resources", bounded)
        runtime = build_test_scheduler(runtime_queue=asyncio.Queue(), agent_factory=agent,
            session_state_manager_factory=lambda context: SessionStateManager(context, factory),
            chat_settings_dao=configured_settings("s"))
        try:
            await runtime.start()
            await runtime.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "首次工作"}))
            await asyncio.wait_for(closing.wait(), 3)
            assert (await runtime.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "关闭期间的新输入"}))).accepted
            await asyncio.wait_for(second.wait(), 3)
            assert created == 2
            assert any("释放资源失败" in item.message for item in caplog.records)
        finally:
            await close_test_runtime(runtime)
    asyncio.run(scenario())


def test_shutdown_rejects_new_input_child_and_ordinary_task(tmp_path: Path) -> None:
    """应用关闭入口生效后，三种新工作均明确拒绝且没有新业务副作用。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """按应用退出次序封闭 Scheduler 与 Supervisor，再尝试三种入口。"""
        calls = []

        async def script(messages, tools):
            """只允许最初一次子工作完成。

            Args:
                messages (Sequence[BaseMessage | ProjectedMessage]): 本次请求模型使用的已投影对话消息。
                tools (Sequence[ToolDefinition] | None): 本次请求向模型公开的工具目录。

            Yields:
                StreamChunk: 用于触发本场景业务路径的离线模型响应。
            """
            calls.append(messages)
            yield StreamChunk(content="已有工作完成")

        class Work(BaseTask):
            """关闭后提交失败，业务入口不应调用。"""
            async def run(self, runtime):
                """任何任务业务调用均说明关闭受理失效。

                Args:
                    runtime (TaskRuntime): Supervisor 绑定的本次任务来源及通知能力。
                """
                raise AssertionError("关闭后启动了新任务")

        async with SubagentHarness(tmp_path / "shutdown.sqlite3", script) as harness:
            child = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, child.context_id), 3)
            await harness.scheduler.begin_shutdown()
            await harness.supervisor.begin_shutdown()
            assert (await harness.scheduler.submit(ChannelCommand("parent", "desktop", "human_turn", {"message": "迟到输入"}))).status == CommandStatus.CLOSED
            future = asyncio.get_running_loop().create_future()
            request = CreateSubagentRequest(SubagentSource("parent", "old-run", "late-create"),
                "迟到子", "测试", "迟到指令", frozenset(), True, future)
            harness.queue.put_nowait(request)
            with pytest.raises(RuntimeError, match="关闭"):
                await asyncio.wait_for(future, 3)
            runtime = ToolRuntime("parent", "old-run", "work", "call", harness.supervisor, harness.queue)
            with pytest.raises(RuntimeError, match="关闭"):
                await runtime.submit_task(Work(), name="late", title="迟到任务", notify_agent=True)
            assert len(calls) == 1
            assert len(await harness.supervisor.records.list("parent")) == 1
    asyncio.run(scenario())


def test_shutdown_ends_persisted_parent_message_wait_without_retiring_retained_child(tmp_path: Path) -> None:
    """应用退出结束已持久保存的父消息等待，并保留保留型子身份。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """等待公开名单确认持久等待，再退出并读取最终协议及定义。"""
        from app.schemas.agent_storage import AgentStorageScope

        async def script(messages, tools):
            """子请求父消息后等待应用退出。

            Args:
                messages (Sequence[BaseMessage | ProjectedMessage]): 本次请求模型使用的已投影对话消息。
                tools (Sequence[ToolDefinition] | None): 本次请求向模型公开的工具目录。

            Yields:
                StreamChunk: 用于触发本场景业务路径的离线模型响应。
            """
            yield StreamChunk(tool_call=ToolCall("shutdown-question", "message_parent", {"text": "等待父回复", "expect_reply": True}))

        async with SubagentHarness(tmp_path / "shutdown-wait.sqlite3", script) as harness:
            child = await harness.create()
            await asyncio.wait_for(wait_for_subagent_status(harness.scheduler, child.context_id, SubagentPublicStatus.WAITING_PARENT), 3)
            await harness.scheduler.begin_shutdown()
            await asyncio.wait_for(harness.scheduler.wait_for_workers(), 3)
            await harness.queue.join()
            detail = await harness.scheduler.get_subagent_details("parent", child.context_id)
            assert detail["context_available"]
            assert [item["status"] for item in detail["results"]] == ["stopped"]
            manager = SessionStateManager(child.context_id, SqliteFactory(harness.path), storage_scope=AgentStorageScope.SUBAGENT)
            try:
                state = await manager.load()
                assert state.interrupt_data is None and not state.pending_tool_calls
                assert len([item for item in state.messages if isinstance(item, ToolMessage) and item.tool_call_id == "shutdown-question"]) == 1
            finally:
                await manager.close()
    asyncio.run(scenario())


def test_retained_child_stop_keeps_task_across_explicit_reuse(tmp_path: Path) -> None:
    """保留型子停止后独立任务继续，复用同一子时消费积压结果。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """真实工具启动任务，子停止、任务结束、再发明确消息依次发生。"""
        from app.schemas.agent_storage import AgentStorageScope
        release, running = asyncio.Event(), asyncio.Event()
        task_ids = []
        calls = 0

        class Work(BaseTask):
            """后台资源与子模型分别存活。"""
            async def run(self, runtime):
                """等待子停止之后再完成独立业务。

                Args:
                    runtime (TaskRuntime): Supervisor 绑定的本次任务来源及通知能力。
                """
                await release.wait()
                return "停止之后完成的独立结果"

        class Start(BaseTool):
            """通过真实工具运行时直接提交普通任务。"""
            name, description = "acceptance_start", "启动可跨工作继续的任务"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args, context):
                """登记独立任务并返回工具结果。

                Args:
                    args (dict[str, object]): 模型提供的工具业务参数。
                    context (ToolContext): 框架绑定的工具来源和任务提交能力。
                """
                task_ids.append(await context.runtime.submit_task(Work(), name="independent", title="跨停止工作", notify_agent=True))
                return ToolResult.success("任务已启动")

        async def script(messages, tools):
            """工具后等待停止，下一次明确指令同时读取旧历史和独立结果。

            Args:
                messages (Sequence[BaseMessage | ProjectedMessage]): 本次请求模型使用的已投影对话消息。
                tools (Sequence[ToolDefinition] | None): 本次请求向模型公开的工具目录。

            Yields:
                StreamChunk: 用于触发本场景业务路径的离线模型响应。
            """
            nonlocal calls
            calls += 1
            if calls == 1:
                yield StreamChunk(tool_call=ToolCall("start-independent", Start.name, {}))
            elif calls == 2:
                running.set()
                await asyncio.Event().wait()
            else:
                assert "下一次明确指令" in str(messages) and "停止之后完成的独立结果" in str(messages)
                yield StreamChunk(content="复用已完成")

        async with SubagentHarness(tmp_path / "retained-task.sqlite3", script, tools=(Start(),)) as harness:
            child = await harness.create()
            await asyncio.wait_for(running.wait(), 3)
            assert (await harness.scheduler.interact_subagent("parent", child.context_id, SubagentAction.STOP)).accepted
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, child.context_id), 3)
            assert (await harness.supervisor.get_task_snapshot(task_ids[0])).status == "running"
            release.set()
            assert (await harness.supervisor.wait(task_ids[0], timeout_ms=2000)).status == "completed"
            await harness.queue.join()
            assert calls == 2
            reader = SessionStateManager(child.context_id, SqliteFactory(harness.path), storage_scope=AgentStorageScope.SUBAGENT)
            try:
                assert len(await reader.read_task_terminals()) == 1
            finally:
                await reader.close()
            assert (await harness.scheduler.interact_subagent("parent", child.context_id, SubagentAction.COMMAND, "下一次明确指令")).accepted
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, child.context_id), 3)
            assert calls == 3 and len(task_ids) == 1
    asyncio.run(scenario())
