"""引导、父暂停隔离、保留型再次委派和子后台进程生命周期。"""
from app.crud.sqlite_factory import SqliteFactory

import asyncio
from pathlib import Path

import pytest

from app.agent.models.contracts import StreamChunk
from app.agent.runtime.tasks.base import BaseTask
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import ToolCall, ToolMessage
from app.agent.tools.core.base import BaseTool, ToolResult
from app.crud.session_inbox_dao import SessionInboxDao
from app.crud.subagent_dao import SubagentDao
from app.interaction.interaction_contract import InteractionCommand
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.runtime_types import CommandKind, TaskStatus
from app.schemas.subagent_types import SubagentAction, SubagentPublicStatus
from tests.subagent_support import SubagentHarness, wait_for_subagent_worker, subagent_worker_running


@pytest.mark.parametrize("retain,finish", [(False, "completed"), (False, "failed"), (False, "stop"),
    (False, "destroy"), (True, "stop"), (True, "destroy"), (True, "idle_destroy")])
def test_child_end_keeps_tool_history_and_retired_identity_rejects_commands(
    tmp_path: Path, retain: bool, finish: str,
) -> None:
    """不同结束方式均保留工具过程，废弃后移出名单且原身份无法再次受派。

    Args:
        tmp_path (Path): 隔离数据库目录。
        retain (bool): 子 Agent 最初是否允许跨任务复用。
        finish (str): 正常完成、失败、停止、运行中销毁或空闲销毁。
    """
    async def scenario() -> None:
        """让真实工具先保存结果，再在模型步骤触发指定结束方式。"""
        entered = asyncio.Event()
        rounds = 0

        class TraceTool(BaseTool):
            """产生应当在废弃后保留的真实工具结果。"""
            name, description = "trace_operation", "记录操作"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args, context) -> ToolResult:
                """返回可在历史中识别的结果。"""
                return ToolResult.success("应当保留的工具结果")

        async def script(messages, tools):
            """第一步调用工具，下一步按测试场景完成、失败或等待停止。"""
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield StreamChunk(tool_call=ToolCall("trace", "trace_operation", {}))
                return
            entered.set()
            if finish == "failed":
                raise RuntimeError("测试模型失败")
            if finish in {"completed", "idle_destroy"}:
                yield StreamChunk(content="最终回答")
            else:
                await asyncio.Event().wait()

        async with SubagentHarness(tmp_path / "state.sqlite3", script, retain=retain, tools=(TraceTool(),)) as harness:
            receipt = await harness.create()
            identifier = receipt.context_id
            await asyncio.wait_for(entered.wait(), 3)
            if finish == "idle_destroy":
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
                receipt = await harness.scheduler.interact_subagent("parent", identifier, SubagentAction.DESTROY)
            elif finish in {"stop", "destroy"}:
                action = SubagentAction.STOP if finish == "stop" else SubagentAction.DESTROY
                await harness.scheduler.interact_subagent("parent", identifier, action)
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            retired = not retain or finish in {"destroy", "idle_destroy"}
            dao = SubagentDao(SqliteFactory(harness.path))
            manager = SessionStateManager(identifier, SqliteFactory(harness.path), storage_scope=AgentStorageScope.SUBAGENT)
            try:
                record = await dao.get("parent", identifier)
                assert record.definition.if_retain_context == retain
                assert (record.retired_at is not None) == retired
                assert "应当保留的工具结果" in str((await manager.load()).messages)
                details = await harness.scheduler.get_subagent_details("parent", identifier)
                assert details["context_available"] == (not retired)
                assert len(details["results"]) == 1
                assert len(await harness.scheduler.snapshot_subagents("parent")) == int(not retired)
                if retired:
                    response = await harness.scheduler.interact_subagent("parent", identifier, SubagentAction.COMMAND, "再次执行")
                    assert not response.accepted
            finally:
                await manager.close()
                await dao.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("action", [SubagentAction.STOP, SubagentAction.DESTROY])
def test_stop_before_first_input_load_still_commits_one_final_report(tmp_path: Path, monkeypatch, action) -> None:
    """首次读取 Inbox 等待期间已受理停止，也必须有唯一终态和父报告。"""
    async def scenario() -> None:
        """仅在存储读取处设屏障，不替换命令、执行和收尾逻辑。"""
        entered, released = asyncio.Event(), asyncio.Event()
        original = SessionStateManager.read_activation_batch
        blocked = False

        async def read(manager, *args, **kwargs):
            """让子第一次读取输入在停止受理之前挂起。"""
            nonlocal blocked
            if manager.session_id != "parent" and not blocked:
                blocked = True
                entered.set()
                await released.wait()
            return await original(manager, *args, **kwargs)

        async def script(messages, tools):
            """停止先受理时模型不应执行。"""
            raise AssertionError("子已停止，不应开始模型")
            yield StreamChunk(content="不应到达")

        monkeypatch.setattr(SessionStateManager, "read_activation_batch", read)
        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(entered.wait(), 3)
            assert (await harness.scheduler.interact_subagent("parent", receipt.context_id, action)).accepted
            released.set()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            details = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
            assert len(details["results"]) == 1
            assert details["results"][0]["status"] == "stopped"
            assert len([item for item in details["recent_interactions"] if item["kind"] == "final"]) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize(
    "retain,first_action,repeated_action,fail_retry",
    [
        (False, SubagentAction.STOP, SubagentAction.STOP, False),
        (True, SubagentAction.STOP, SubagentAction.STOP, False),
        (True, SubagentAction.STOP, SubagentAction.DESTROY, False),
        (True, SubagentAction.DESTROY, SubagentAction.STOP, False),
        (True, SubagentAction.DESTROY, SubagentAction.DESTROY, False),
        (False, SubagentAction.DESTROY, SubagentAction.DESTROY, True),
    ],
)
def test_model_client_cleanup_retries_share_active_cleanup(
    tmp_path: Path, retain: bool, first_action: SubagentAction,
    repeated_action: SubagentAction, fail_retry: bool,
) -> None:
    """Agent.close 内重试客户端关闭，期间重复停止或销毁共用原关闭流程。

    Args:
        tmp_path (Path): 当前用例独占的数据库目录。
        retain (bool): 普通停止后是否保留子上下文。
        first_action (SubagentAction): 客户端关闭首次失败后收到的管理操作。
        repeated_action (SubagentAction): 重试等待期间再次受理的操作。
        fail_retry (bool): 第一次重试是否失败，以验证预算内的第二次重试。
    """
    async def scenario() -> None:
        """通过模型客户端关闭、命令和最终结果观察清理并发及销毁升级。"""
        attempts = 0
        active = 0
        maximum_active = 0
        retry_entered, retry_released, concurrent_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        failed = asyncio.Event()

        async def close_model_client() -> None:
            """记录模型客户端关闭的并发数，按事件控制重试完成及再次失败。"""
            nonlocal attempts, active, maximum_active
            attempts += 1
            if attempts == 1:
                failed.set()
                raise RuntimeError("测试模型客户端暂时无法关闭")
            active += 1
            maximum_active = max(maximum_active, active)
            if active > 1:
                concurrent_cleanup.set()
            current_attempt = attempts
            retry_entered.set()
            try:
                await retry_released.wait()
                if fail_retry and current_attempt == 2:
                    raise RuntimeError("测试模型客户端重试关闭仍失败")
            finally:
                active -= 1

        class Harness(SubagentHarness):
            """在真实子工厂中替换模型客户端的关闭方法。"""
            def _child(self, definition, manager):
                """让模型正常执行，在关闭模型客户端时注入可重试的异常。"""
                agent = super()._child(definition, manager)
                agent.llm_client.close = close_model_client
                return agent

        async def script(messages, tools):
            """正常完成，资源错误发生在最终收尾。"""
            yield StreamChunk(content="业务已完成")

        async with Harness(tmp_path / "state.sqlite3", script, retain=retain) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(failed.wait(), 3)
            assert subagent_worker_running(harness.scheduler, receipt.context_id)

            # 1. 让第一次重试停在模型客户端关闭中，再受理重复停止或销毁。
            try:
                first = await harness.scheduler.interact_subagent("parent", receipt.context_id, first_action)
                assert first.accepted == (retain or first_action == SubagentAction.DESTROY)
                await asyncio.wait_for(retry_entered.wait(), 3)
                repeated = await harness.scheduler.interact_subagent("parent", receipt.context_id, repeated_action)
                assert repeated.accepted == (retain and first_action != SubagentAction.DESTROY or repeated_action == SubagentAction.DESTROY)
                assert subagent_worker_running(harness.scheduler, receipt.context_id)
                # 1.1. 等待可能出现的并发关闭，确认当前只有一个重试在执行。
                try:
                    await asyncio.wait_for(concurrent_cleanup.wait(), 0.1)
                except asyncio.TimeoutError:
                    pass
                assert maximum_active == 1
                if not retain or SubagentAction.DESTROY in (first_action, repeated_action):
                    assert not await harness.scheduler.snapshot_subagents("parent")
            finally:
                retry_released.set()

            # 2. 释放客户端关闭屏障，Agent.close 在有限预算内完成剩余尝试。
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            assert maximum_active == 1
            assert attempts == (3 if fail_retry else 2)

            # 3. 收尾报告只保存一次，销毁升级生效，普通停止保留可复用上下文。
            details = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
            destroyed = not retain or SubagentAction.DESTROY in (first_action, repeated_action)
            assert details["context_available"] == (not destroyed)
            assert len(details["results"]) == 1
            assert details["results"][0]["status"] == TaskStatus.COMPLETED
            assert len([item for item in details["recent_interactions"] if item["kind"] == "final"]) == 1
            summaries = await harness.scheduler.snapshot_subagents("parent")
            if destroyed:
                assert not summaries
                manager = SessionStateManager(receipt.context_id, SqliteFactory(harness.path), storage_scope=AgentStorageScope.SUBAGENT)
                try:
                    assert not await manager.read_activation_batch()
                finally:
                    await manager.close()
            else:
                assert summaries[0].status == SubagentPublicStatus.IDLE
                assert summaries[0].error is None
    asyncio.run(scenario())


@pytest.mark.parametrize("retain,destroy", [(False, False), (True, False), (True, True)])
def test_last_connection_close_failure_finishes_child_execution(
    tmp_path: Path, monkeypatch, caplog, retain: bool, destroy: bool,
) -> None:
    """最后关闭连接失败仍结束完成等待，销毁身份移除，保留身份可以再次受派。

    Args:
        tmp_path (Path): 当前用例独占的数据库目录。
        monkeypatch (pytest.MonkeyPatch): 在子 Inbox 的最后连接关闭处注入一次异常。
        caplog (pytest.LogCaptureFixture): 确认连接关闭失败留有异常日志。
        retain (bool): 正常结束后是否保留子上下文。
        destroy (bool): 是否先完成保留型任务，再显式销毁空闲子身份。
    """
    async def scenario() -> None:
        """保留真实执行和销毁，仅让一次子 Inbox 关闭在释放连接前失败。"""
        original_close = SessionInboxDao.close
        fail_close = not destroy
        failed_stores: list[SessionInboxDao] = []

        async def close(store: SessionInboxDao) -> None:
            """对子身份注入一次关闭异常，其余连接按原流程释放。"""
            nonlocal fail_close
            if fail_close and store.storage_scope == AgentStorageScope.SUBAGENT:
                fail_close = False
                failed_stores.append(store)
                raise RuntimeError("测试子 Inbox 连接关闭失败")
            await original_close(store)

        async def script(messages, tools):
            """每次明确受派都产出可保存的最终报告。"""
            yield StreamChunk(content="业务已完成")

        monkeypatch.setattr(SessionInboxDao, "close", close)
        try:
            async with SubagentHarness(tmp_path / "state.sqlite3", script, retain=retain) as harness:
                receipt = await harness.create()
                if destroy:
                    # 1. 先正常完成保留型任务，故障仅发生在显式销毁的最后关闭阶段。
                    await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
                    fail_close = True
                    receipt = await harness.scheduler.interact_subagent(
                        "parent", receipt.context_id, SubagentAction.DESTROY,
                    )
                    assert receipt.accepted

                # 2. 关闭异常仍完成执行回执，结果及父通知对应的最终交互只保存一次。
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
                expected_closed = int(not retain or destroy)
                assert len(failed_stores) == expected_closed
                if expected_closed:
                    assert any(record.exc_info is not None for record in caplog.records)
                details = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
                assert len(details["results"]) == 1
                assert details["results"][0]["status"] == TaskStatus.COMPLETED
                assert len([item for item in details["recent_interactions"] if item["kind"] == "final"]) == 1
                summaries = await harness.scheduler.snapshot_subagents("parent")
                if not retain or destroy:
                    assert not summaries
                    assert not details["context_available"]
                else:
                    # 3. 保留型已结束本次执行，后续命令能开始新委派。
                    assert summaries[0].status == SubagentPublicStatus.IDLE
                    next_task = await harness.scheduler.interact_subagent(
                        "parent", receipt.context_id, SubagentAction.COMMAND, "继续下一项任务",
                    )
                    assert next_task.accepted
                    await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, next_task.context_id), 3)
        finally:
            # 4. 测试主动释放故障注入留下的连接，避免影响临时数据库清理。
            for store in failed_stores:
                await original_close(store)
    asyncio.run(scenario())


def test_guidance_continues_same_child_without_intermediate_report(tmp_path: Path) -> None:
    """引导结束旧模型等待，保留原指令并只交付一次最终报告。"""
    async def scenario() -> None:
        """用模型事件屏障让父命令在第一段等待时到达。"""
        entered = asyncio.Event()
        async def script(messages, tools):
            """新指令到达前持续等待，下一段从保存的子历史继续。"""
            if "新方向" not in str(messages):
                entered.set()
                await asyncio.Event().wait()
            assert "第一项任务" in str(messages)
            yield StreamChunk(content="按新方向完成")
        async with SubagentHarness(tmp_path / "state.sqlite3", script, retain=False) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(entered.wait(), 3)
            await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.COMMAND, "新方向")
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            detail = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
            assert len(detail["results"]) == 1
            assert detail["results"][0]["result"] == "按新方向完成"
            assert not detail["context_available"]
    asyncio.run(scenario())


def test_application_shutdown_cancels_a_child_waiting_inside_a_tool(tmp_path: Path) -> None:
    """应用退出按主 Agent 的取消路径结束挂起工具，不能无限等待子 worker。"""
    async def scenario() -> None:
        """让实际工具停在 await，退出后核验工具和子最终结果。"""
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class WaitingTool(BaseTool):
            """持有一次可取消的等待，不启动后台工作。"""
            name, description = "waiting_tool", "等待测试事件"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args, context) -> ToolResult:
                """等待应用关闭取消，并在退出时释放工具资源。"""
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
                return ToolResult.success("结束")

        async def script(messages, tools):
            """在首个模型步骤请求挂起工具。"""
            yield StreamChunk(tool_call=ToolCall(id="waiting", name="waiting_tool", args={}))

        async with SubagentHarness(tmp_path / "state.sqlite3", script, tools=(WaitingTool(),)) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(harness.scheduler.begin_shutdown(), 3)
            await asyncio.wait_for(harness.scheduler.wait_for_workers(), 3)
            assert cancelled.is_set()
            assert not subagent_worker_running(harness.scheduler, receipt.context_id)
    asyncio.run(scenario())


def test_stopped_retained_child_can_be_explicitly_reused(tmp_path: Path) -> None:
    """stop 不销毁保留型身份，新 command 使用已有子历史开始新任务。"""
    async def scenario() -> None:
        """第一段停止后，以同一子 ID 完成下一项任务。"""
        entered = asyncio.Event()
        async def script(messages, tools):
            """第一次等待停止，重用时检查旧上下文。"""
            if "第二项" not in str(messages):
                entered.set()
                await asyncio.Event().wait()
            assert "第一项任务" in str(messages)
            yield StreamChunk(content="第二项完成")
        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            first = await harness.create()
            await asyncio.wait_for(entered.wait(), 3)
            await harness.scheduler.interact_subagent("parent", first.context_id, SubagentAction.STOP)
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, first.context_id), 3)
            assert (await harness.scheduler.snapshot_subagents("parent"))[0].status == SubagentPublicStatus.IDLE
            second = await harness.scheduler.interact_subagent("parent", first.context_id, SubagentAction.COMMAND, "第二项")
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, second.context_id), 3)
            detail = await harness.scheduler.get_subagent_details("parent", first.context_id)
            assert [record["status"] for record in reversed(detail["results"])] == ["stopped", "completed"]
    asyncio.run(scenario())


def test_new_child_message_during_wait_cleanup_keeps_accepted_input_enabled(tmp_path: Path, monkeypatch) -> None:
    """子停止正在保存等待协议时，新明确消息继续交付并形成唯一最终报告。"""
    from app.agent.runtime.agent_execution import AgentExecution

    async def scenario() -> None:
        """在真实等待结束后设置屏障，交错停止、新消息与原 worker 收尾。"""
        entered, released = asyncio.Event(), asyncio.Event()
        original = AgentExecution.finish_waiting
        calls = 0

        async def finish(execution):
            """原等待已结束，输入受理仍可在资源交还前更新会话政策。"""
            await original(execution)
            if execution.context_id != "parent" and not entered.is_set():
                entered.set()
                await released.wait()

        async def script(messages, tools):
            """先等待父回复，新明确消息受理后完成第二项指令。"""
            nonlocal calls
            calls += 1
            if calls == 1:
                yield StreamChunk(tool_call=ToolCall("ask", "message_parent", {"text": "请确认下一项", "expect_reply": True}))
            else:
                assert "第二项明确消息" in str(messages)
                yield StreamChunk(content="第二项已完成")

        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            receipt = await harness.create()
            identifier = receipt.context_id
            from tests.subagent_support import wait_for_subagent_status
            await asyncio.wait_for(wait_for_subagent_status(harness.scheduler, identifier, SubagentPublicStatus.WAITING_PARENT), 3)
            monkeypatch.setattr(AgentExecution, "finish_waiting", finish)
            try:
                assert (await harness.scheduler.interact_subagent("parent", identifier, SubagentAction.STOP)).accepted
                await asyncio.wait_for(entered.wait(), 3)
                assert (await harness.scheduler.interact_subagent("parent", identifier, SubagentAction.COMMAND, "第二项明确消息")).accepted
            finally:
                released.set()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, identifier), 3)
            assert calls == 2
            details = await harness.scheduler.get_subagent_details("parent", identifier)
            assert len(details["results"]) == 1
            assert details["results"][0]["status"] == "completed"
            assert details["results"][0]["result"] == "第二项已完成"
    asyncio.run(scenario())


def test_parent_stop_does_not_stop_child_or_allow_its_result_to_wake_parent(tmp_path: Path) -> None:
    """父暂停不级联子停止，子结果留父 Inbox 而不恢复自动处理。"""
    async def scenario() -> None:
        """控制父和子各自等待，分别观察停止和结果交付资格。"""
        parent_release, child_release, child_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def script(messages, tools):
            """等待父已经暂停后再完成。"""
            child_entered.set()
            await child_release.wait()
            yield StreamChunk(content="父暂停期间完成")
        async with SubagentHarness(tmp_path / "state.sqlite3", script, parent_release=parent_release) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(child_entered.wait(), 3)
            assert (await harness.scheduler.submit(InteractionCommand("parent", CommandKind.STOP_TURN))).accepted
            parent_release.set()
            assert subagent_worker_running(harness.scheduler, receipt.context_id)
            child_release.set()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            assert harness.received.empty()
            manager = SessionStateManager("parent", SqliteFactory(harness.path))
            try:
                notices = await manager.read_task_terminals()
                assert len(notices) == 1
                assert notices[0].payload["result"] == "父暂停期间完成"
            finally:
                await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("retain", [False, True])
def test_background_task_lifetime_and_notification_recipient(tmp_path: Path, retain: bool) -> None:
    """保留型独立工作可跨轮存活，一次性结束全量清理，结果只交发起子 Inbox。"""
    async def scenario() -> None:
        """由真实工具提交一个持有独立资源的任务，观察 Supervisor 和 Manager。"""
        released, cleaned = asyncio.Event(), asyncio.Event()
        task_ids: list[str] = []
        rounds = 0

        class Work(BaseTask):
            """自己持有等待资源，不借用 Agent 或插件客户端。"""
            async def run(self, runtime) -> str:
                """在测试放行后报告自己的结果。"""
                await released.wait()
                return "后台结果"

            async def cleanup(self) -> None:
                """释放由任务自己持有的资源。"""
                cleaned.set()

        class StartWork(BaseTool):
            """模拟获准插件工具创建非终端独立任务。"""
            name, description = "start_work", "启动测试后台工作"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args, context) -> ToolResult:
                """通过原 ToolRuntime 提交，不通过子管理请求代理 Supervisor。"""
                task_ids.append(await context.runtime.submit_task(Work(), name="work", title="测试工作",
                                                                  notify_agent=True))
                return ToolResult.success("已启动")

        async def script(messages, tools):
            """先启动工作并完成委派，下一次明确唤醒再读取积压结果。"""
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield StreamChunk(tool_call=ToolCall(id="start", name="start_work", args={}))
            else:
                if rounds > 2:
                    assert "后台结果" in str(messages)
                yield StreamChunk(content="委派完成")

        async with SubagentHarness(tmp_path / "state.sqlite3", script, retain=retain, tools=(StartWork(),)) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            record = await harness.supervisor.get_task_snapshot(task_ids[0])
            assert record.subagent_id == receipt.context_id
            if not retain:
                assert record.status == TaskStatus.STOPPED
                assert cleaned.is_set()
            else:
                assert record.status == TaskStatus.RUNNING
                assert not cleaned.is_set()
                released.set()
                await harness.supervisor.wait(task_ids[0], timeout_ms=3000)
                manager = SessionStateManager(receipt.context_id, SqliteFactory(harness.path), storage_scope=AgentStorageScope.SUBAGENT)
                try:
                    assert len(await manager.read_task_terminals()) == 1
                finally:
                    await manager.close()
                assert (await harness.scheduler.snapshot_subagents("parent"))[0].status == SubagentPublicStatus.IDLE
                next_task = await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.COMMAND, "读后台结果")
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, next_task.context_id), 3)
    asyncio.run(scenario())
