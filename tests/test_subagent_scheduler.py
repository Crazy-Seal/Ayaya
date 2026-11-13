"""从公开命令、专用请求和持久详情验证 Scheduler 管理子 Agent。"""
from tests.runtime_support import runtime_hub, close_test_runtime
from app.routes.agent import observe_runtime
from app.schemas.runtime_types import AgentRunOutcome, AgentRunOutcomeKind
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory

from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator
from tests.runtime_support import RuntimeAgentStub

import asyncio
from pathlib import Path
from tests.runtime_support import configured_settings

import pytest

from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.core.events import AgentEvent, EventType
from app.agent.models.llm_client import StreamChunk
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.runtime.subagent_requests import CreateSubagentRequest, SubagentSource, InteractSubagentRequest
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.state.manager import SessionStateManager
from app.interaction.interaction_contract import ChannelCommand
from app.schemas.subagent_types import SubagentAction
from app.schemas.runtime_types import TaskStatus
from tests.subagent_support import SubagentHarness, wait_for_subagent_worker, subagent_worker_running


@pytest.mark.parametrize("retain", [False, True])
def test_child_final_report_and_context_retention(tmp_path: Path, retain: bool) -> None:
    """任务有限观察后继续；完成报告唯一入父 Inbox，保留选项决定可否再次受派。

    Args:
        tmp_path (Path): 隔离数据库目录。
        retain (bool): 本例是否保留子上下文。
    """
    async def scenario() -> None:
        """用真实子 Agent 和 SQLite，只替换模型及父侧发起动作。"""
        path = str(tmp_path / "state.sqlite3")
        queue = asyncio.Queue()
        created = asyncio.Queue()
        release = asyncio.Event()
        requests: list[list] = []
        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(path)))

        class ChildLLM:
            """用事件控制子模型，记录每次收到的真实业务消息。"""
            async def astream(self, messages, **kwargs):
                """等待测试放行后产生最终文本。"""
                requests.append(list(messages))
                await release.wait()
                yield StreamChunk(content="子任务已完成")

            async def close(self) -> None:
                """测试模型没有外部连接。"""

        class Parent(RuntimeAgentStub):
            """模拟父工具发起请求，借用父 worker 的 Manager 保存输入和结果。

            Attributes:
                manager (SessionStateManager): Scheduler 提供并负责关闭的父会话状态管理对象。
            """

            def __init__(self, manager: SessionStateManager) -> None:
                """接收父 worker 使用的状态管理对象。

                Args:
                    manager (SessionStateManager): 由 Scheduler 创建并在 worker 结束后关闭的对象。
                """
                self.manager = manager

            async def run(self, message, *, turn_control, inbox_inputs, emit, **kwargs):
                """保存父真人输入并通过共享队列创建一个子 Agent。"""
                outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED)
                await emit(AgentEvent(EventType.RUN_STARTED, {"origin_channel": "desktop", "resumed": False}))
                state = await self.manager.load()
                await self.manager.commit_human_input(state, message, None, inputs=inbox_inputs)
                source = SubagentSource("parent", turn_control.run_id, "create-call")
                reply = asyncio.get_running_loop().create_future()
                await queue.put(CreateSubagentRequest(source, "测试子助手", "完成任务", "第一项任务", frozenset(), retain, reply))
                await created.put((await reply, source))
                await emit(AgentEvent(EventType.DONE, None))

                return outcome

            async def run_runtime_inputs(self, *, inbox_inputs, emit, **kwargs):
                """接收子完成报告，保持父不重新创建子 Agent。"""
                outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED)
                await emit(AgentEvent(EventType.RUN_STARTED, {"origin_channel": "desktop", "resumed": False}))
                state = await self.manager.load()
                await self.manager.commit_runtime_inputs(state, None, inbox_inputs)
                await emit(AgentEvent(EventType.DONE, None))

                return outcome

            async def close(self) -> None:
                """父 worker 在执行结束后统一关闭借出的 Manager。"""

        def child_factory(definition, manager) -> Agent:
            """组合现有 Agent，关闭自动插件钩子并使用独立上下文。"""
            return Agent(AgentConfig(session_id=definition.subagent_id, system_prompt=definition.system_prompt,
                                    model_name="test", api_key="test",
                                    context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)),
                         session_state_manager=manager, llm_client=ChildLLM(), enable_plugin_hooks=False,
                         permission_session_id=definition.session_id, tool_whitelist=definition.tool_whitelist, sqlite_factory=SqliteFactory())

        scheduler = build_test_scheduler(runtime_queue=queue, agent_factory=lambda _session, manager: Parent(manager),
            subagent_factory=child_factory, task_supervisor=supervisor,
            session_state_manager_factory=lambda context: SessionStateManager(context, SqliteFactory(path)), chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        try:
            await scheduler.start()
            assert (await scheduler.submit(ChannelCommand("parent", "desktop", "human_turn", {"message": "开始"}))).accepted
            receipt, source = await asyncio.wait_for(created.get(), 3)
            assert subagent_worker_running(scheduler, receipt.context_id)
            assert len(await scheduler.snapshot_subagents("parent")) == 1
            release.set()
            await asyncio.wait_for(wait_for_subagent_worker(scheduler, receipt.context_id), 3)
            detail = await scheduler.get_subagent_details("parent", receipt.context_id)
            assert detail["context_available"] == retain
            assert len(detail["results"]) == 1
            assert detail["results"][0]["result"] == "子任务已完成"
            assert len(await scheduler.snapshot_subagents("parent")) == int(retain)
            assert "第一项任务" in str(requests[0])
        finally:
            release.set()
            try:
                await close_test_runtime(scheduler)
            finally:
                await supervisor.close()
                await supervisor.records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("retain", [False, True])
def test_child_factory_failure_finishes_once_after_delivery_retries(tmp_path: Path, retain: bool) -> None:
    """子装配失败后保存一次失败报告，保留型可在修正配置后再次明确受派。

    Args:
        tmp_path (Path): 隔离数据库目录。
        retain (bool): 失败后是否保留可再次受派的子身份。
    """
    async def scenario() -> None:
        """通过创建回执、父 Inbox 和详情验证失败收尾及下一次明确委派。"""
        attempts = 0
        broken = True

        async def script(messages, tools):
            """工厂恢复后正常完成下一次委派。"""
            yield StreamChunk(content="配置修正后完成")

        class Harness(SubagentHarness):
            """只替换子装配故障，执行、重试和失败保存沿用真实运行时。"""
            def _child(self, definition, manager):
                """记录装配次数，模拟配置导致的持续装配异常。"""
                nonlocal attempts
                attempts += 1
                if broken:
                    raise RuntimeError("测试子 Agent 装配失败")
                return super()._child(definition, manager)

        async with Harness(tmp_path / "state.sqlite3", script, retain=retain) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            assert attempts == 1
            detail = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
            assert len(detail["results"]) == 1
            assert detail["results"][0]["status"] == TaskStatus.FAILED
            assert detail["results"][0]["error"]
            assert detail["context_available"] == retain
            assert len([item for item in detail["recent_interactions"] if item["kind"] == "final"]) == 1
            notifications = await asyncio.wait_for(harness.received.get(), 3)
            assert len(notifications) == 1
            assert notifications[0].payload["status"] == TaskStatus.FAILED
            assert len(await harness.scheduler.snapshot_subagents("parent")) == int(retain)
            if retain:
                broken = False
                next_task = await harness.scheduler.interact_subagent(
                    "parent", receipt.context_id, SubagentAction.COMMAND, "配置已修正，开始下一项任务",
                )
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, next_task.context_id), 3)
                detail = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
                assert attempts == 2
                assert [item["status"] for item in detail["results"]] == [TaskStatus.COMPLETED, TaskStatus.FAILED]
    asyncio.run(scenario())
