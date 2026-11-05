"""重启只整理已受理工作，不恢复模型、审批或后台进程。"""

import asyncio
from datetime import datetime, timezone
from tests.runtime_support import configured_settings
from pathlib import Path

import pytest

from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import ToolCall
from app.crud.subagent_dao import SubagentDao
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.runtime_types import CheckpointType
from app.schemas.subagent_types import SubagentDefinition, SubagentPublicStatus, TaskRecordKind


@pytest.mark.parametrize("retain", [False, True])
def test_accepted_command_without_worker_is_reported_and_never_restarted(tmp_path: Path, retain: bool) -> None:
    """创建已提交但 worker 尚未建立时退出，重启保存 orphaned，保留型仅空闲可复用。"""
    async def scenario() -> None:
        """直接准备崩溃前持久事实，再通过公开启动和查询验收。"""
        path = str(tmp_path / "state.sqlite3")
        dao = SubagentDao(path)
        definition = SubagentDefinition("child", "parent", "测试助手", "测试", frozenset(), retain, datetime.now(timezone.utc))
        await dao.create_with_command(definition, "已受理的初始工作", "creation")
        await dao.close()

        def forbidden(*_args):
            """重启不得构造父子 Agent 或调用模型。"""
            raise AssertionError("重启不应开始执行")

        scheduler = AgentRuntimeScheduler(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), agent_factory=forbidden,
            subagent_factory=forbidden, session_state_manager_factory=lambda session: SessionStateManager(session, path))
        try:
            await scheduler.start()
            summaries = await scheduler.snapshot_subagents("parent")
            assert len(summaries) == int(retain)
            if retain:
                assert summaries[0].status == SubagentPublicStatus.IDLE
            details = await scheduler.get_subagent_details("parent", "child")
            assert len(details["results"]) == 1
            assert details["results"][0]["status"] == "orphaned"
            assert details["context_available"] == retain
            manager = SessionStateManager("parent", path)
            try:
                assert len(await manager.read_task_terminals()) == 1
            finally:
                await manager.close()
        finally:
            await scheduler.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("parent_exists", [False, True])
@pytest.mark.parametrize("retain,destroy", [(False, False), (True, False), (True, True)])
def test_restart_preserves_child_progress_and_only_retires_non_reusable_identities(
    tmp_path: Path, retain: bool, destroy: bool, parent_exists: bool,
) -> None:
    """重启整理完整持久进度，废弃一次性和已请求销毁的身份，保留历史但不重跑工具。

    Args:
        tmp_path (Path): 隔离数据库目录。
        retain (bool): 子身份原本是否可复用。
        destroy (bool): 崩溃前是否已受理销毁。
    """
    async def scenario() -> None:
        """保存工具已开始的进度，关闭全部连接后仅启动实际 Scheduler 整理。"""
        path = str(tmp_path / "state.sqlite3")
        dao, tasks = SubagentDao(path), TaskRecordDao(path)
        manager = SessionStateManager("child", path, storage_scope=AgentStorageScope.SUBAGENT)
        try:
            await dao.create_with_command(SubagentDefinition("child", "parent", "助手", "测试", frozenset(),
                retain, datetime.now(timezone.utc)), "崩溃前已交付的任务", "create")
            batch = await manager.read_activation_batch()
            state = await manager.load()
            await manager.commit_runtime_inputs(state, None, batch)
            state.record_assistant_step("崩溃前已开始操作", (ToolCall("old-call", "operation", {}),))
            await manager.save(state, checkpoint_type=CheckpointType.INTERMEDIATE)
            await tasks.insert_running("old-run", "parent", name="subagent", title="旧委派", background=False,
                subagent_id="child", kind=TaskRecordKind.SUBAGENT, notification_context_id="parent")
            if destroy:
                await dao.retire("child")
        finally:
            await manager.close()
            await tasks.close()
            await dao.close()

        def forbidden(*_args):
            """启动只整理历史，不构造 Agent 或执行任何模型和工具。"""
            raise AssertionError("重启不能自动执行")

        settings = configured_settings("parent" if parent_exists else "other")
        scheduler = AgentRuntimeScheduler(chat_settings_dao=settings, runtime_queue=asyncio.Queue(), agent_factory=forbidden,
            subagent_factory=forbidden, session_state_manager_factory=lambda identifier: SessionStateManager(identifier, path))
        reader = SessionStateManager("child", path, storage_scope=AgentStorageScope.SUBAGENT)
        try:
            await scheduler.start()
            state = await reader.load()
            assert "崩溃前已开始操作" in str(state.messages)
            assert not state.pending_tool_calls
            assert reader.last_loaded_checkpoint_type == CheckpointType.COMPLETED
            details = await scheduler.get_subagent_details("parent", "child")
            assert details["context_available"] == (retain and not destroy and parent_exists)
            assert details["results"][0]["status"] == "orphaned"
            assert len(await scheduler.snapshot_subagents("parent")) == int(retain and not destroy and parent_exists)
        finally:
            await reader.close()
            await scheduler.close()
    asyncio.run(scenario())
