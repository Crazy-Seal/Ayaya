"""通过公开状态和输入接口验证子执行存储隔离及历史留档。"""
from app.crud.sqlite_factory import SqliteFactory

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.agent.state.manager import SessionStateManager
from app.agent.state.model import AgentState
from app.agent.state.messages import HumanMessage, text_content
from app.agent.models.openai_chat import OpenAIChatCompletionsAdapter
from app.agent.models.contracts import LLMConfig, StreamChunk
from app.crud.agent_state_dao import AgentStateDao
from app.crud.session_inbox_dao import SessionInboxDao
from app.crud.subagent_dao import SubagentDao
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.runtime_types import CheckpointType, InboxStatus, TaskStatus
from app.schemas.subagent_types import SubagentDefinition, SubagentInteractionKind
from tests.subagent_support import SubagentHarness, wait_for_subagent_worker


def test_a2a_reply_is_delivered_once_as_user_message(tmp_path: Path) -> None:
    """父回复保留交互记录，同时作为普通用户消息交付且重复提交不会重复输入。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """通过 DAO 和 Manager 的公开接口验证回复与普通命令的存储区别。"""
        path = str(tmp_path / "state.sqlite3")
        definition = SubagentDefinition("child", "parent", "代码助手", "检查代码", frozenset(), True, datetime.now(timezone.utc))
        dao = SubagentDao(SqliteFactory(path))
        manager = SessionStateManager("child", SqliteFactory(path), storage_scope=AgentStorageScope.SUBAGENT)
        adapter = OpenAIChatCompletionsAdapter(LLMConfig(model="test", api_key="test"))
        try:
            await dao.create_with_command(definition, "初始任务", "create")
            await manager.discard_pending_commands()
            await dao.append_message(definition, SubagentInteractionKind.REPLY, "处理 app.py", "parent-reply", request_id="question")
            await dao.append_message(definition, SubagentInteractionKind.STEER, "处理 app.py", "parent-reply")
            batch = await manager.read_activation_batch()
            assert len(batch) == 1
            assert batch[0].payload["text"] == "处理 app.py"
            state = await manager.load()
            await manager.commit_runtime_inputs(state, None, batch)
            restored = await manager.load()
            assert len(restored.messages) == 1
            assert isinstance(restored.messages[0], HumanMessage)
            assert restored.messages[0].content == text_content("处理 app.py")
            assert adapter.encode_messages(restored.messages) == [
                {"role": "user", "content": "处理 app.py"},
            ]
            interactions = await dao.recent_interactions("child")
            assert [(item.kind, item.text, item.source_id) for item in interactions] == [
                (SubagentInteractionKind.TASK, "初始任务", "create"),
                (SubagentInteractionKind.REPLY, "处理 app.py", "parent-reply"),
            ]
        finally:
            await adapter.close()
            await manager.close()
            await dao.close()
    asyncio.run(scenario())


def test_parent_and_child_state_and_inbox_are_isolated(tmp_path: Path) -> None:
    """相同上下文键和输入编号在两组表中仍分别交付，重开后保存各自历史。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """用真实 SQLite 对父子 Manager 分别保存及重新加载。"""
        database = str(tmp_path / "state.sqlite3")
        parent = SessionStateManager("same", SqliteFactory(database))
        child = SessionStateManager("same", SqliteFactory(database), storage_scope=AgentStorageScope.SUBAGENT)
        try:
            first = await parent.submit_human("desktop", "父输入")
            second = await child.submit_human("desktop", "子输入")
            parent_state, child_state = await parent.load(), await child.load()
            await parent.commit_human_input(parent_state, "父输入", None, inputs=(first,))
            assert await child.status(second.id) == InboxStatus.PENDING
            await child.commit_human_input(child_state, "子输入", None, inputs=(second,))
            assert "父输入" in str((await parent.load()).messages)
            assert "子输入" not in str((await parent.load()).messages)
            assert "子输入" in str((await child.load()).messages)
        finally:
            await parent.close()
            await child.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("retain", [False, True])
def test_completed_child_history_can_be_reopened_from_database(tmp_path: Path, retain: bool) -> None:
    """两类子 Agent 结束后均可重新读取数据库中的任务和最终回答。

    Args:
        tmp_path (Path): 隔离数据库目录。
        retain (bool): 完成后是否还允许子 Agent 再次受派。
    """
    async def scenario() -> None:
        """通过真实 Scheduler 完成任务，再由独立 Manager 读取已关闭执行的历史。"""
        database = str(tmp_path / "state.sqlite3")

        async def script(messages, tools):
            """返回可在子历史中辨认的最终回答。"""
            yield StreamChunk(content="已完成数据库留档任务")

        async with SubagentHarness(Path(database), script, retain=retain) as harness:
            receipt = await harness.create("请保存这次任务")
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            manager = SessionStateManager(receipt.context_id, SqliteFactory(database), storage_scope=AgentStorageScope.SUBAGENT)
            try:
                state = await manager.load()
                assert "请保存这次任务" in str(state.messages)
                assert "已完成数据库留档任务" in str(state.messages)
            finally:
                await manager.close()
    asyncio.run(scenario())


def test_wrong_inbox_scope_rolls_back_child_checkpoint(tmp_path: Path) -> None:
    """子 Store 拒绝消费主 Inbox 快照，失败后不留下检查点或交付标记。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """提交错误范围输入，观察两个公开持久接口均未发生变化。"""
        database = str(tmp_path / "state.sqlite3")
        inbox = SessionInboxDao("same", SqliteFactory(database))
        child = AgentStateDao("same", SqliteFactory(database), storage_scope=AgentStorageScope.SUBAGENT)
        try:
            item = await inbox.submit_human("desktop", "父输入")
            with pytest.raises(ValueError):
                await child.save(AgentState(session_id="same"), checkpoint_type=CheckpointType.INTERMEDIATE,
                                 inbox_inputs=(item,))
            assert await child.load() is None
            assert await inbox.status(item.id) == InboxStatus.PENDING
        finally:
            await inbox.close()
            await child.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("delivery", ["before", "after", "concurrent"])
def test_retirement_keeps_history_and_blocks_late_task_inputs(tmp_path: Path, delivery: str) -> None:
    """废弃保留历史并清除当时待交付输入，后提交的任务通知继续保存。

    Args:
        tmp_path (Path): 隔离数据库目录。
        delivery (str): 后台结果在废弃前、后或并发提交。
    """
    async def scenario() -> None:
        """通过真实 DAO 和 Manager 观察状态、交互、结果及不同上下文的输入。"""
        path = str(tmp_path / "state.sqlite3")
        definition = SubagentDefinition("child", "parent", "助手", "测试", frozenset(), False, datetime.now(timezone.utc))
        dao, tasks = SubagentDao(SqliteFactory(path)), TaskRecordDao(SqliteFactory(path))
        child = SessionStateManager("child", SqliteFactory(path), storage_scope=AgentStorageScope.SUBAGENT)
        parent = SessionStateManager("child", SqliteFactory(path))
        other = SessionStateManager("other", SqliteFactory(path), storage_scope=AgentStorageScope.SUBAGENT)
        try:
            await dao.create_with_command(definition, "已交付任务", "create")
            initial = (await child.read_activation_batch())[0]
            state = await child.load()
            await child.commit_runtime_inputs(state, None, (initial,))
            state.add_assistant_message("已保存的最终回答")
            await child.save(state, checkpoint_type=CheckpointType.COMPLETED)
            await dao.append_message(definition, SubagentInteractionKind.STEER, "未交付的引导", "steer")
            pending = await child.submit_human("desktop", "未交付输入")
            parent_input = await parent.submit_human("desktop", "父输入")
            other_input = await other.submit_human("desktop", "其他子输入")
            await tasks.insert_running("work", "parent", name="work", title="子后台工作", subagent_id="child",
                notification_context_id="child", notification_scope=AgentStorageScope.SUBAGENT)
            if delivery == "before":
                await tasks.finish_once("work", TaskStatus.COMPLETED, result="后台结果")
                await dao.retire("child")
            elif delivery == "after":
                await dao.retire("child")
                await tasks.finish_once("work", TaskStatus.COMPLETED, result="后台结果")
            else:
                await asyncio.gather(dao.retire("child"), tasks.finish_once("work", TaskStatus.COMPLETED, result="后台结果"))
            record = await dao.get("parent", "child")
            assert record.retired_at is not None
            assert not record.context_available
            assert not await dao.list_available("parent")
            assert await child.status(initial.id) == InboxStatus.DELIVERED
            assert await child.status(pending.id) is None
            notices = await child.read_task_terminals()
            if delivery == "after":
                assert len(notices) == 1 and notices[0].payload["result"] == "后台结果"
            elif delivery == "before":
                assert not notices
            await dao.retire("child")
            assert (await dao.get("parent", "child")).retired_at == record.retired_at
            assert await parent.status(parent_input.id) == InboxStatus.PENDING
            assert await other.status(other_input.id) == InboxStatus.PENDING
            assert "已保存的最终回答" in str((await child.load()).messages)
            assert [item.text for item in await dao.recent_interactions("child")] == ["已交付任务", "未交付的引导"]
            assert (await tasks.get("work")).result == "后台结果"
        finally:
            await child.close()
            await parent.close()
            await other.close()
            await tasks.close()
            await dao.close()
    asyncio.run(scenario())


def test_retirement_rolls_back_marker_when_pending_input_cannot_be_deleted(tmp_path: Path) -> None:
    """输入删除失败时废弃标记也回滚，后续可按原记录再次收尾。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario() -> None:
        """在测试库安装真实失败触发器，通过公开接口检查事务结果。"""
        path = str(tmp_path / "state.sqlite3")
        dao = SubagentDao(SqliteFactory(path))
        child = SessionStateManager("child", SqliteFactory(path), storage_scope=AgentStorageScope.SUBAGENT)
        try:
            await dao.create_with_command(SubagentDefinition("child", "parent", "助手", "测试", frozenset(),
                True, datetime.now(timezone.utc)), "保留待交付任务", "create")
            initial = (await child.read_activation_batch())[0]
            with sqlite3.connect(path) as db:
                db.execute("CREATE TRIGGER fail_retirement BEFORE DELETE ON subagent_session_inbox "
                           "BEGIN SELECT RAISE(ABORT, 'test retirement failure'); END")
            with pytest.raises(sqlite3.IntegrityError, match="test retirement failure"):
                await dao.retire("child")
            assert (await dao.get("parent", "child")).retired_at is None
            assert await child.status(initial.id) == InboxStatus.PENDING
            with sqlite3.connect(path) as db:
                db.execute("DROP TRIGGER fail_retirement")
            await dao.retire("child")
            assert (await dao.get("parent", "child")).retired_at is not None
        finally:
            await child.close()
            await dao.close()
    asyncio.run(scenario())
