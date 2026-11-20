from tests.memory_support import memory_settings
"""用真实 SQLite 和执行调用链验证批次 B 的保存、恢复及输入政策。"""

import asyncio
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest
import aiosqlite

from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.models.contracts import StreamChunk
from app.agent.runtime.tasks.base import BaseTask
from app.agent.tools.core.base import BaseTool, ToolResult
from app.agent.tools.core.runtime import ToolRuntime
from app.agent.state.messages import ToolCall
from app.agent.state.manager import SessionStateManager
from app.agent.state.model import AgentState
from app.crud.agent_state_dao import AgentStateDao
from app.crud.session_inbox_dao import SessionInboxDao
from app.crud.sqlite_factory import SqliteFactory
from app.crud.subagent_dao import SubagentDao
from app.interaction.interaction_contract import ChannelCommand
from app.schemas.runtime_types import CheckpointType, CommandKind, InboxKind, SessionState
from app.schemas.subagent_types import SubagentDefinition
from tests.runtime_support import build_test_scheduler, close_test_runtime, configured_settings, finish_task_record, next_runtime_event
from tests.test_authorization_state import approval_state


@pytest.mark.parametrize("owner", ["state", "inbox", "subagent"])
@pytest.mark.parametrize("failures", [0, 3, 4])
def test_connection_configuration_uses_same_save_attempt_budget_and_releases_failures(tmp_path: Path, monkeypatch,
                                                                                      owner: str, failures: int) -> None:
    """连接准备错误由具名保存内部统一判断，至多四次尝试，未交付连接全部关闭。"""
    async def scenario() -> None:
        """在实际连接配置处注入 BUSY，确认最终记录只写入一次。"""
        factory = SqliteFactory(tmp_path / "connection-failure.sqlite3")
        connect = aiosqlite.connect
        attempts, closed = 0, []
        error = sqlite3.OperationalError("配置时 SQLite 暂时占用")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY

        async def open_connection(*args, **kwargs):
            """使用真实连接，只让指定的配置步骤失败并观察资源释放。"""
            nonlocal attempts
            attempts += 1
            current = attempts
            db = await connect(*args, **kwargs)
            execute, close = db.execute, db.close

            async def configure(*sql_args, **sql_kwargs):
                """失败发生在连接交付前，业务 SQL 尚未开始。"""
                if current <= failures:
                    raise error
                return await execute(*sql_args, **sql_kwargs)

            async def release() -> None:
                """记录实际关闭，验证每份失败连接恰好释放一次。"""
                closed.append(current)
                await close()

            db.execute, db.close = configure, release
            return db

        monkeypatch.setattr(aiosqlite, "connect", open_connection)
        dao = (AgentStateDao("s", factory) if owner == "state" else
               SessionInboxDao("s", factory) if owner == "inbox" else SubagentDao(factory))
        if owner == "state":
            state = AgentState.create_new("s")
            state.add_user_message("相同材料")
            operation = dao.save(state, checkpoint_type=CheckpointType.INTERMEDIATE)
            table = "checkpoints"
        elif owner == "inbox":
            operation = dao.submit_human("desktop", "相同材料")
            table = "session_inbox"
        else:
            definition = SubagentDefinition("child", "s", "助手", "任务", frozenset(), True, datetime.now(timezone.utc))
            operation = dao.create_with_command(definition, "相同材料", "source")
            table = "subagents"
        try:
            if failures == 4:
                with pytest.raises(sqlite3.OperationalError) as failure:
                    await operation
                assert failure.value is error
            else:
                await operation
                with closing(factory.connect_sync()) as inspection:
                    assert inspection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (1,)
            assert attempts == min(failures + 1, 4)
            assert closed == list(range(1, min(failures, 4) + 1))
        finally:
            await dao.close()
    asyncio.run(scenario())


def test_sqlite_factory_applies_same_configuration_to_sync_and_async_connections(tmp_path: Path) -> None:
    """同步和异步连接使用相同的文件、WAL、锁预算及外键配置。"""
    factory = SqliteFactory(tmp_path / "configured.sqlite3")
    with closing(factory.connect_sync()) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert db.execute("PRAGMA busy_timeout").fetchone() == (100,)
        assert db.execute("PRAGMA foreign_keys").fetchone() == (1,)

    async def scenario() -> None:
        """通过真实连接读取实际生效配置，退出查询范围后连接由使用者关闭。"""
        async with factory.connection() as db:
            assert tuple(await db.execute_fetchall("PRAGMA journal_mode")) == (("wal",),)
            assert tuple(await db.execute_fetchall("PRAGMA busy_timeout")) == ((100,),)
    asyncio.run(scenario())


def test_final_save_fault_preserves_committed_tools_and_independent_task(tmp_path: Path) -> None:
    """最终提交失败保留工具进展和普通任务，不重跑副作用，通知不能解除暂停。"""
    async def scenario() -> None:
        """真实工具先保存进展，再拒绝最终 checkpoint，并通过下一条明确输入继续。"""
        factory = SqliteFactory(tmp_path / "final-fault.sqlite3")
        task_release = asyncio.Event()
        side_effects, task_ids = [], []
        model_calls = 0

        class IndependentTask(BaseTask):
            """独立工作在执行故障和资源释放后仍由 Supervisor 继续持有。"""
            async def run(self, runtime):
                """等待显式放行，再形成普通任务结果。"""
                await task_release.wait()
                return "后台任务已经完成"

        class StartWork(BaseTool):
            """记录一次外部操作并创建一个独立普通任务。"""
            name, description = "batch_b_start_work", "启动测试工作"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args, context):
                """只运行一次的工具副作用和任务登记。"""
                side_effects.append("executed")
                task_ids.append(await context.runtime.submit_task(IndependentTask(), name="independent",
                    title="独立工作", notify_agent=True))
                return ToolResult.success("已保存的工具进展")

        class Model:
            """第一轮使用工具，第二段正文触发保存故障，第三次调用只响应新输入。"""
            async def astream(self, messages, tools=None):
                """按实际请求次数返回工具或正文，重复业务会让断言失败。"""
                nonlocal model_calls
                model_calls += 1
                if model_calls == 1:
                    yield StreamChunk(content="工具之前的已保存正文")
                    yield StreamChunk(tool_call=ToolCall("start-work", "batch_b_start_work", {}))
                else:
                    yield StreamChunk(content="尚未提交的最终正文" if model_calls == 2 else "新的工作完成")

            async def close(self) -> None:
                """脚本模型保持外部资源为空。"""

        def create_agent(context_id, manager):
            """通过真实 Agent、Pipeline 和 ToolRuntime 运行已装配的测试工具。"""
            agent = Agent(AgentConfig(context_id, "test", "test", ContextStrategyConfig()), factory,
                session_state_manager=manager, llm_client=Model(),
                tool_runtime_factory=lambda name, run, call: ToolRuntime(context_id, run, name, call,
                    runtime.task_supervisor, runtime.runtime_queue))
            agent.register_tool(StartWork())
            return agent

        runtime = build_test_scheduler(runtime_queue=asyncio.Queue(), agent_factory=create_agent,
            session_state_manager_factory=lambda context: SessionStateManager(context, factory),
            chat_settings_dao=configured_settings("s"))
        try:
            await runtime.start()
            setup = SessionStateManager("s", factory)
            await setup.load()
            await setup.close()
            with closing(factory.connect_sync()) as inspection:
                inspection.execute("CREATE TRIGGER reject_final BEFORE INSERT ON checkpoints "
                    "WHEN NEW.checkpoint_type='completed' BEGIN SELECT RAISE(ABORT, 'final rejected'); END")
                inspection.commit()
            assert (await runtime.submit(ChannelCommand("s", "desktop", CommandKind.HUMAN_TURN, {"message": "执行工作"}))).accepted
            events = []
            while not any(event.kind == "history_restored" for event in events):
                events.append(await asyncio.wait_for(next_runtime_event(runtime), 2))
            assert side_effects == ["executed"] and model_calls == 2
            assert len(task_ids) == 1
            assert (await runtime.task_supervisor.get_task_snapshot(task_ids[0])).status == "running"
            snapshot = await runtime.snapshot("s")
            assert snapshot.payload["session_state"] == "paused"
            assert snapshot.payload["execution"]["phase"] == "idle"
            reader = SessionStateManager("s", factory)
            try:
                state = await reader.load()
                texts = [message.text for message in state.messages]
                assert "工具之前的已保存正文" in texts
                assert "尚未提交的最终正文" not in texts
                assert "已保存的工具进展" in texts
            finally:
                await reader.close()
            task_release.set()
            await runtime.task_supervisor.wait(task_ids[0], timeout_ms=1000)
            await runtime.runtime_queue.join()
            assert model_calls == 2
            with closing(factory.connect_sync()) as inspection:
                inspection.execute("DROP TRIGGER reject_final")
                inspection.commit()
            assert (await runtime.submit(ChannelCommand("s", "desktop", CommandKind.HUMAN_TURN, {"message": "继续新的工作"}))).accepted
            while (await asyncio.wait_for(next_runtime_event(runtime), 2)).kind != "run_completed":
                pass
            assert side_effects == ["executed"] and model_calls == 3
        finally:
            task_release.set()
            await close_test_runtime(runtime)
    asyncio.run(scenario())


@pytest.mark.parametrize("owner", ["state", "inbox", "subagent"])
def test_dao_commit_after_budget_and_repeated_cancellation_returns_actual_success(tmp_path: Path, monkeypatch, owner: str) -> None:
    """相关 DAO 仅等待一次真实提交，超预算和重复取消均不制造失败结论。"""
    async def scenario() -> None:
        """阻塞实际提交，观察预算耗尽后仍等待，再放行并核对唯一保存。"""
        factory = SqliteFactory(tmp_path / "confirm.sqlite3")
        dao = (AgentStateDao("s", factory) if owner == "state" else
               SessionInboxDao("s", factory) if owner == "inbox" else SubagentDao(factory))
        db = await dao._get_db()
        entered, release = asyncio.Event(), asyncio.Event()
        original = db.commit

        async def commit() -> None:
            """让真实 SQLite 提交等待测试放行，保存本身仍只提交一次。"""
            entered.set()
            await release.wait()
            await original()

        monkeypatch.setattr(db, "commit", commit)
        module = __import__(dao.__class__.__module__, fromlist=["SQLITE_SAVE_TIMEOUT_SECONDS"])
        monkeypatch.setattr(module, "SQLITE_SAVE_TIMEOUT_SECONDS", 1.0)
        if owner == "state":
            state = AgentState.create_new("s")
            state.add_user_message("固定输入")
            operation = dao.save(state, checkpoint_type=CheckpointType.INTERMEDIATE)
            table = "checkpoints"
        elif owner == "inbox":
            operation = dao.submit_human("desktop", "固定输入")
            table = "session_inbox"
        else:
            definition = SubagentDefinition("child", "s", "助手", "任务", frozenset(), True, datetime.now(timezone.utc))
            operation = dao.create_with_command(definition, "固定输入", "create-source")
            table = "subagents"
        saving = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(saving), 1.05)
            saving.cancel()
            saving.cancel()
            assert not saving.done()
            release.set()
            await asyncio.wait_for(saving, 1)
            with closing(factory.connect_sync()) as inspection:
                assert inspection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (1,)
        finally:
            release.set()
            await asyncio.gather(saving, return_exceptions=True)
            await dao.close()
    asyncio.run(scenario())


def test_authorization_first_wins_across_independent_managers(tmp_path: Path) -> None:
    """两个持久入口同时决定同一审批，数据库只保存首个决定及其引导输入。"""
    async def scenario() -> None:
        """让两个候选状态都来自同一审批，再并发提交到实际独立连接。"""
        factory = SqliteFactory(tmp_path / "approval.sqlite3")
        first, second = AgentStateDao("s", factory), AgentStateDao("s", factory)
        state = approval_state()
        try:
            await first.save(state, checkpoint_type=CheckpointType.INTERMEDIATE)
            approved, steered = state.model_copy(deep=True), state.model_copy(deep=True)
            approved.record_authorization_decision("approval", {"kind": "approve"})
            steered.record_authorization_decision("approval", {"kind": "steer", "message": "新指令"})
            results = await asyncio.gather(first.commit_interrupt_decision(approved, origin_channel="desktop"),
                second.commit_interrupt_decision(steered, origin_channel="desktop"))
            assert sorted(results) == [False, True]
            saved = await first.load()
            with closing(factory.connect_sync()) as inspection:
                count = inspection.execute("SELECT COUNT(*) FROM session_inbox WHERE kind='human'").fetchone()[0]
            assert count == int(saved.interrupt_data["runtime_decision"]["kind"] == "steer")
        finally:
            await first.close()
            await second.close()
    asyncio.run(scenario())


def test_failed_input_is_not_replayed_by_notice_or_next_explicit_message(tmp_path: Path) -> None:
    """输入交付故障后保持暂停，通知不执行，下一条明确输入只处理新正文。"""
    async def scenario() -> None:
        """拒绝真实输入 checkpoint，再恢复存储，验证模型只收到下一条明确消息。"""
        factory = SqliteFactory(tmp_path / "input-fault.sqlite3")
        setup = SessionStateManager("s", factory)
        await setup.load()
        await setup.close()
        seen = []

        class Model:
            """记录实际模型输入，返回正常正文。"""
            async def astream(self, messages, tools=None):
                """只有输入成功交付后才可调用此模型。"""
                seen.append(messages)
                yield StreamChunk(content="新输入已处理")

            async def close(self) -> None:
                """脚本模型没有外部资源。"""

        def create_agent(context_id, manager):
            """使用真实 Agent、共享 Manager 和离线模型。"""
            return Agent(AgentConfig(context_id, "test", "test", ContextStrategyConfig()), factory,
                         session_state_manager=manager, llm_client=Model())

        runtime = build_test_scheduler(runtime_queue=asyncio.Queue(), agent_factory=create_agent,
            session_state_manager_factory=lambda context: SessionStateManager(context, factory),
            chat_settings_dao=configured_settings("s"))
        try:
            await runtime.start()
            with closing(factory.connect_sync()) as inspection:
                inspection.execute("CREATE TRIGGER reject_input BEFORE INSERT ON checkpoints "
                    "WHEN NEW.checkpoint_type='intermediate' BEGIN SELECT RAISE(ABORT, 'input rejected'); END")
                inspection.commit()
            assert (await runtime.submit(ChannelCommand("s", "desktop", CommandKind.HUMAN_TURN, {"message": "失败旧输入"}))).accepted
            events = []
            while not any(event.kind == "history_restored" for event in events):
                events.append(await asyncio.wait_for(next_runtime_event(runtime), 2))
            assert [event.kind for event in events].count("error") == 1
            assert not any(event.kind == "human_message" for event in events)
            assert not seen
            assert (await runtime.snapshot("s")).payload["session_state"] == SessionState.PAUSED
            records = runtime.task_supervisor.records
            await records.insert_running("notice", "s", title="通知")
            await finish_task_record(runtime.task_supervisor, "notice", "completed")
            await runtime.runtime_queue.join()
            assert not seen
            with closing(factory.connect_sync()) as inspection:
                inspection.execute("DROP TRIGGER reject_input")
                inspection.commit()
            assert (await runtime.submit(ChannelCommand("s", "desktop", CommandKind.HUMAN_TURN, {"message": "新的明确输入"}))).accepted
            while (await asyncio.wait_for(next_runtime_event(runtime), 2)).kind != "run_completed":
                pass
            assert len(seen) == 1
            manager = SessionStateManager("s", factory)
            try:
                texts = [message.text for message in (await manager.load()).messages]
                assert "新的明确输入" in texts and "失败旧输入" not in texts
                assert not await manager.pending_humans()
            finally:
                await manager.close()
        finally:
            await close_test_runtime(runtime)
    asyncio.run(scenario())

@pytest.mark.parametrize("read_step", ["checkpoint", "history"])
def test_faulted_restore_requires_both_reads_and_only_explicit_input_retries(tmp_path: Path, read_step: str) -> None:
    """恢复 checkpoint 或协议历史读取失败时保持 FAULTED，通知始终保留为待处理。"""
    async def scenario() -> None:
        """阻止一次真实保存，再分别阻止恢复查询，确认下一条明确输入才再次尝试。"""
        factory = SqliteFactory(tmp_path / "restore.sqlite3")
        blocked = False
        reads = 0
        model_calls = 0

        class Manager(SessionStateManager):
            """只在故障发生后拒绝指定的恢复查询。"""

            async def load(self):
                """按测试屏障读取最后成功保存的 checkpoint。"""
                nonlocal reads
                if blocked and read_step == "checkpoint":
                    reads += 1
                    raise OSError("checkpoint 读取暂时失败")
                return await super().load()

            async def committed_history_position(self):
                """确认历史读取成功与 checkpoint 恢复具有相同的成功条件。"""
                nonlocal reads
                if blocked and read_step == "history":
                    reads += 1
                    raise OSError("历史读取暂时失败")
                return await super().committed_history_position()

        class Model:
            """记录明确恢复后的实际业务执行次数。"""

            async def astream(self, messages, tools=None):
                """保存最终回答前允许测试阻止接下来的恢复读取。"""
                nonlocal model_calls, blocked
                model_calls += 1
                if model_calls == 1:
                    blocked = True
                yield StreamChunk(content="正常正文")

            async def close(self) -> None:
                """离线模型没有独立资源。"""

        def create(context, manager):
            """为本上下文创建真实 Agent，恢复查询由同一个 Manager 完成。"""
            return Agent(AgentConfig(context, "test", "test", ContextStrategyConfig()), factory,
                         session_state_manager=manager, llm_client=Model())

        runtime = build_test_scheduler(runtime_queue=asyncio.Queue(), agent_factory=create,
            session_state_manager_factory=lambda context: Manager(context, factory), chat_settings_dao=configured_settings("s"))
        try:
            await runtime.start()
            with closing(factory.connect_sync()) as db:
                db.execute("CREATE TRIGGER reject_final BEFORE INSERT ON checkpoints WHEN NEW.checkpoint_type='completed' "
                           "BEGIN SELECT RAISE(ABORT,'final rejected'); END")
                db.commit()
            await runtime.submit(ChannelCommand("s", "desktop", CommandKind.HUMAN_TURN, {"message": "失败轮"}))
            while (await asyncio.wait_for(next_runtime_event(runtime), 3)).kind != "error":
                pass
            for _ in range(100):
                snapshot = (await runtime.snapshot("s")).payload
                if snapshot["session_state"] == "paused":
                    break
                await asyncio.sleep(0.01)
            assert snapshot["execution"]["phase"] == "faulted"
            assert reads == 1 and model_calls == 1
            await runtime.task_supervisor.records.insert_running("notice", "s", title="恢复期间通知")
            await finish_task_record(runtime.task_supervisor, "notice", "completed")
            await runtime.runtime_queue.join()
            assert reads == 1 and model_calls == 1
            with closing(factory.connect_sync()) as db:
                db.execute("DROP TRIGGER reject_final")
                db.commit()
            blocked = False
            await runtime.submit(ChannelCommand("s", "desktop", CommandKind.HUMAN_TURN, {"message": "新的明确输入"}))
            kinds = []
            while "run_completed" not in kinds:
                kinds.append((await asyncio.wait_for(next_runtime_event(runtime), 3)).kind)
            assert "history_restored" in kinds and model_calls == 2
        finally:
            await close_test_runtime(runtime)
    asyncio.run(scenario())


@pytest.mark.parametrize("retain", [False, True])
def test_child_report_failure_preserves_history_and_never_resends_normal_report(tmp_path: Path, retain: bool) -> None:
    """报告保存失败单独显示交付错误，成功保存的子对话保留且原报告只尝试一次。"""
    async def scenario() -> None:
        """用真实 SQLite 触发器拒绝报告终态，随后观察保留政策和下一次明确消息。"""
        from tests.subagent_support import SubagentHarness, wait_for_subagent_worker
        from app.schemas.agent_storage import AgentStorageScope
        from app.schemas.subagent_types import SubagentAction
        calls = 0

        async def script(messages, tools):
            """每次明确输入只运行一次模型，最终正文均先保存到子历史。"""
            nonlocal calls
            calls += 1
            yield StreamChunk(content=f"已保存的子正文 {calls}")

        async with SubagentHarness(tmp_path / "reports.sqlite3", script, retain=retain) as harness:
            with closing(sqlite3.connect(harness.path)) as db:
                db.execute("CREATE TRIGGER reject_report BEFORE UPDATE OF status ON task_records WHEN OLD.kind='subagent' "
                           "BEGIN SELECT RAISE(ABORT,'report rejected'); END")
                db.commit()
            receipt = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            while True:
                event = await asyncio.wait_for(next_runtime_event(harness.scheduler), 3)
                if event.kind == "error" and event.payload.get("report_failed"):
                    break
            assert event.payload["subagent_id"] == receipt.context_id
            assert harness.received.empty() and calls == 1
            manager = SessionStateManager(receipt.context_id, SqliteFactory(harness.path), storage_scope=AgentStorageScope.SUBAGENT)
            try:
                assert (await manager.load()).messages[-1].text == "已保存的子正文 1"
            finally:
                await manager.close()
            details = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
            assert details["context_available"] == retain
            with closing(sqlite3.connect(harness.path)) as db:
                db.execute("DROP TRIGGER reject_report")
                db.commit()
            await harness.scheduler.runtime_queue.join()
            assert harness.received.empty() and calls == 1
            if retain:
                next_receipt = await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.COMMAND, "下一项工作")
                assert next_receipt.accepted
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
                notices = await asyncio.wait_for(harness.received.get(), 3)
                assert len(notices) == 1 and notices[0].payload["result"] == "已保存的子正文 2"
                assert calls == 2
    asyncio.run(scenario())


def test_parent_and_two_children_control_independent_contexts(tmp_path: Path) -> None:
    """父与两个子真实并发执行时，停止子和停止父分别只改变目标上下文。"""
    async def scenario() -> None:
        """通过真实创建工具建立两个子，分别从模型取消及持久结果验证控制范围。"""
        from app.agent.tools.create_subagent import CreateSubagentTool
        from app.schemas.subagent_types import SubagentAction
        from tests.subagent_support import wait_for_subagent_worker
        factory = SqliteFactory(tmp_path / "independent.sqlite3")
        queue = asyncio.Queue()
        parent_running = asyncio.Event()
        children_running = asyncio.Event()
        child_running = {}
        child_cancelled = set()
        parent_calls = 0
        supervisor = None

        class ParentModel:
            """首个模型步骤创建两个子，后续步骤保持父正文流等待。"""

            async def astream(self, messages, tools=None):
                """创建子完成后保留父运行，供测试独立控制。"""
                nonlocal parent_calls
                parent_calls += 1
                if parent_calls == 1:
                    for index in range(2):
                        yield StreamChunk(tool_call=ToolCall(f"create-{index}", "create_subagent", {
                            "name": f"子{index}", "system_prompt": "独立工作", "prompt": "开始",
                            "if_retain_context": True}))
                else:
                    yield StreamChunk(content="父仍在处理")
                    parent_running.set()
                    await asyncio.Event().wait()

            async def close(self) -> None:
                """离线父模型没有资源。"""

        class ChildModel:
            """每个实际子编号保留各自的模型流。"""

            def __init__(self, context_id):
                """绑定测试观察用的实际子编号。"""
                self.context_id = context_id

            async def astream(self, messages, tools=None):
                """记录哪个上下文被取消，其他子流持续等待。"""
                event = child_running.setdefault(self.context_id, asyncio.Event())
                yield StreamChunk(content="子仍在处理")
                event.set()
                if len(child_running) == 2 and all(item.is_set() for item in child_running.values()):
                    children_running.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    child_cancelled.add(self.context_id)

            async def close(self) -> None:
                """离线子模型没有资源。"""

        def parent_agent(context, manager):
            """注入真实创建工具及同一应用后台任务端口。"""
            agent = Agent(AgentConfig(context, "test", "test", ContextStrategyConfig()), factory,
                session_state_manager=manager, llm_client=ParentModel(),
                tool_runtime_factory=lambda tool, run, call: ToolRuntime(context, run, tool, call, supervisor, queue))
            agent.register_tool(CreateSubagentTool({}))
            return agent

        def child_agent(definition, manager):
            """每个子使用自己独立的 AgentExecution 和实际历史上下文。"""
            return Agent(AgentConfig(definition.subagent_id, "test", "test", ContextStrategyConfig()), factory,
                         session_state_manager=manager, llm_client=ChildModel(definition.subagent_id))

        runtime = build_test_scheduler(runtime_queue=queue, agent_factory=parent_agent, subagent_factory=child_agent,
            session_state_manager_factory=lambda context: SessionStateManager(context, factory), chat_settings_dao=configured_settings("s"))
        supervisor = runtime.task_supervisor
        try:
            await runtime.start()
            await runtime.submit(ChannelCommand("s", "desktop", CommandKind.HUMAN_TURN, {"message": "同时执行"}))
            await asyncio.wait_for(parent_running.wait(), 3)
            await asyncio.wait_for(children_running.wait(), 3)
            assert len(child_running) == 2
            first, second = tuple(child_running)
            parent_run = (await runtime.snapshot("s")).payload["run_id"]
            assert (await runtime.interact_subagent("s", first, SubagentAction.STOP)).accepted
            await asyncio.wait_for(wait_for_subagent_worker(runtime, first), 3)
            assert child_cancelled == {first}
            parent = (await runtime.snapshot("s")).payload
            assert parent["run_id"] == parent_run and parent["phase"] == "running"
            assert (await runtime.submit(ChannelCommand("s", "desktop", CommandKind.STOP_TURN, target_run_id=parent_run))).accepted
            for _ in range(100):
                if (await runtime.snapshot("s")).payload["phase"] == "idle":
                    break
                await asyncio.sleep(0.01)
            assert (await runtime.snapshot("s")).payload["session_state"] == "paused"
            assert child_cancelled == {first}
            summaries = await runtime.snapshot_subagents("s")
            assert next(item for item in summaries if item.subagent_id == second).status == "running"
            assert parent_calls == 2
        finally:
            await close_test_runtime(runtime)
    asyncio.run(scenario())


def test_model_business_failure_commits_final_failure_without_replaying_model(tmp_path: Path) -> None:
    """模型失败保存明确失败结果和已显示片段，下一次启动整理不会把它冒充完成回答。"""
    async def scenario() -> None:
        """在实际模型流中抛出业务错误，检查一次错误输出及最终持久位置。"""
        factory = SqliteFactory(tmp_path / "model-failure.sqlite3")
        calls = 0

        class Model:
            """输出可见正文后模拟模型连接协议失败。"""

            async def astream(self, messages, tools=None):
                """同一业务只执行一次，失败由 Agent 保存收尾。"""
                nonlocal calls
                calls += 1
                yield StreamChunk(content="失败前已显示的片段")
                raise RuntimeError("模型协议失败")

            async def close(self) -> None:
                """脚本模型没有外部资源。"""

        runtime = build_test_scheduler(runtime_queue=asyncio.Queue(),
            agent_factory=lambda context, manager: Agent(AgentConfig(context, "test", "test", ContextStrategyConfig()), factory,
                session_state_manager=manager, llm_client=Model()),
            session_state_manager_factory=lambda context: SessionStateManager(context, factory), chat_settings_dao=configured_settings("s"))
        try:
            await runtime.start()
            await runtime.submit(ChannelCommand("s", "desktop", CommandKind.HUMAN_TURN, {"message": "开始"}))
            events = []
            while not any(event.kind == "error" for event in events):
                events.append(await asyncio.wait_for(next_runtime_event(runtime), 3))
            assert calls == 1 and len([event for event in events if event.kind == "error"]) == 1
            assert not events[-1].payload.get("save_failed")
            manager = SessionStateManager("s", factory)
            try:
                state = await manager.load()
                assert manager.last_loaded_checkpoint_type == CheckpointType.COMPLETED
                assert state.messages[-1].text == "失败前已显示的片段\n\n本轮执行失败：模型协议失败"
                assert not state.pending_tool_calls and not state.interrupt_data
            finally:
                await manager.close()
            assert (await runtime.snapshot("s")).payload["session_state"] == SessionState.PAUSED
        finally:
            await close_test_runtime(runtime)
    asyncio.run(scenario())


def test_direct_agent_initializes_memory_plugin_and_its_tools_with_explicit_factory(tmp_path: Path) -> None:
    """独立 Agent 与应用 Agent 共用插件装配，启用记忆会注册实际搜索工具。"""
    async def scenario() -> None:
        """只初始化插件和工具，保持模型及记忆提取未运行。"""
        config = AgentConfig("direct", "test", "test", ContextStrategyConfig(), plugins=[{
            "name": "memory", "config": {"enable_diary": False, "enable_episodic": False, "enable_semantic": False}}])
        agent = Agent(config, SqliteFactory(tmp_path / "direct.sqlite3"), chat_settings=memory_settings("direct"))
        try:
            await agent.initialize()
            assert agent.get_plugins() == ["memory"]
            identities = agent.tool_manager.get_visible_tool_identities()
            assert {identity.name for identity in identities.values() if identity.plugin_name == "memory"} == {"search_memory", "search_diary"}
        finally:
            await agent.close()
    asyncio.run(scenario())


def test_explicit_queue_rejects_idle_and_committing_without_saving_stranded_input(tmp_path: Path) -> None:
    """空闲及最终提交阶段拒绝显式排队，普通明确消息仍能受理并在提交后执行。"""
    async def scenario() -> None:
        """保持真实最终保存等待，从输入持久状态和模型次数验证排队资格。"""
        factory = SqliteFactory(tmp_path / "queue-eligibility.sqlite3")
        committing, release = asyncio.Event(), asyncio.Event()
        calls = 0

        class Manager(SessionStateManager):
            """第一次最终提交等待测试观察排队回执。"""

            async def complete_turn(self, state, **kwargs):
                """保存已经开始，控制操作只能接续下一条普通输入。"""
                if calls == 1:
                    committing.set()
                    await release.wait()
                return await super().complete_turn(state, **kwargs)

        class Model:
            """每个被交付的明确输入只执行一次。"""

            async def astream(self, messages, tools=None):
                """正常完成，供最后保存屏障控制消息时序。"""
                nonlocal calls
                calls += 1
                yield StreamChunk(content="完成")

            async def close(self) -> None:
                """离线模型没有外部资源。"""

        runtime = build_test_scheduler(runtime_queue=asyncio.Queue(),
            agent_factory=lambda context, manager: Agent(AgentConfig(context, "test", "test", ContextStrategyConfig()), factory,
                session_state_manager=manager, llm_client=Model()),
            session_state_manager_factory=lambda context: Manager(context, factory), chat_settings_dao=configured_settings("s"))
        try:
            await runtime.start()
            queued = ChannelCommand("s", "desktop", CommandKind.QUEUE_TURN, {"message": "无法执行的队列"})
            assert not (await runtime.submit(queued)).accepted
            await runtime.submit(ChannelCommand("s", "desktop", CommandKind.HUMAN_TURN, {"message": "第一条"}))
            await asyncio.wait_for(committing.wait(), 3)
            assert not (await runtime.submit(queued)).accepted
            assert (await runtime.submit(ChannelCommand("s", "desktop", CommandKind.HUMAN_TURN, {"message": "第二条"}))).accepted
            release.set()
            completed = 0
            while completed < 2:
                if (await asyncio.wait_for(next_runtime_event(runtime), 3)).kind == "run_completed":
                    completed += 1
            assert calls == 2
            manager = SessionStateManager("s", factory)
            try:
                assert not await manager.pending_humans()
                assert "无法执行的队列" not in str((await manager.load()).messages)
            finally:
                await manager.close()
        finally:
            release.set()
            await close_test_runtime(runtime)
    asyncio.run(scenario())


def test_queue_save_delayed_before_final_commit_is_released_and_executed(tmp_path: Path, monkeypatch) -> None:
    """已经受理的排队保存先完成，最终回答随后原子放行它并执行一次。"""
    from app.crud.session_inbox_dao import SessionInboxDao

    async def scenario() -> None:
        """延迟真实 Inbox 保存，在此期间放行模型完成并观察提交顺序。"""
        factory = SqliteFactory(tmp_path / "queue-completion-race.sqlite3")
        modeled, finish_model = asyncio.Event(), asyncio.Event()
        saving_queue, release_queue, committing = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0
        original = SessionInboxDao.submit_human

        async def submit(dao, *args, **kwargs):
            """输入排序已经生效，实际 SQLite 写入在此等待受控放行。"""
            if kwargs.get("queued"):
                saving_queue.set()
                await release_queue.wait()
            return await original(dao, *args, **kwargs)

        class Manager(SessionStateManager):
            """观察 Agent 已进入最终提交，持久化排序由父类负责。"""

            async def complete_turn(self, state, **kwargs):
                """记录最终提交已被调用，再等待真实输入和回答事务完成。"""
                committing.set()
                return await super().complete_turn(state, **kwargs)

        class Model:
            """第一轮等待排队受理，第二轮只处理已保存的排队消息。"""

            async def astream(self, messages, tools=None):
                """每条明确输入执行一次，最终保存负责放行下一条排队输入。"""
                nonlocal calls
                calls += 1
                if calls == 1:
                    modeled.set()
                    await finish_model.wait()
                else:
                    assert "稍慢的排队消息" in str(messages)
                yield StreamChunk(content="已完成")

            async def close(self) -> None:
                """脚本模型没有外部资源。"""

        monkeypatch.setattr(SessionInboxDao, "submit_human", submit)
        runtime = build_test_scheduler(runtime_queue=asyncio.Queue(),
            agent_factory=lambda context, manager: Agent(AgentConfig(context, "test", "test", ContextStrategyConfig()), factory,
                session_state_manager=manager, llm_client=Model()),
            session_state_manager_factory=lambda context: Manager(context, factory), chat_settings_dao=configured_settings("s"))
        pending = None
        try:
            await runtime.start()
            await runtime.submit(ChannelCommand("s", "desktop", CommandKind.HUMAN_TURN, {"message": "第一条"}))
            await asyncio.wait_for(modeled.wait(), 3)
            pending = asyncio.create_task(runtime.submit(ChannelCommand("s", "desktop", CommandKind.QUEUE_TURN, {"message": "稍慢的排队消息"})))
            await asyncio.wait_for(saving_queue.wait(), 3)
            finish_model.set()
            await asyncio.wait_for(committing.wait(), 3)
            release_queue.set()
            assert (await asyncio.wait_for(pending, 3)).accepted
            completed = 0
            while completed < 2:
                if (await asyncio.wait_for(next_runtime_event(runtime), 3)).kind == "run_completed":
                    completed += 1
            assert calls == 2
            manager = SessionStateManager("s", factory)
            try:
                assert not await manager.pending_humans()
                assert "稍慢的排队消息" in str((await manager.load()).messages)
            finally:
                await manager.close()
        finally:
            finish_model.set()
            release_queue.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await close_test_runtime(runtime)
    asyncio.run(scenario())
