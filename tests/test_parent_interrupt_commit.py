"""通过状态存储接口验证子暂停与父收件原子提交。"""
from app.crud.sqlite_factory import SqliteFactory

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from app.agent.state.manager import SessionStateManager, RequiredSaveError
from app.agent.state.messages import ToolCall
from app.crud.subagent_dao import SubagentDao
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.runtime_types import InterruptKind
from app.schemas.subagent_types import SubagentDefinition


def test_parent_interrupt_replay_does_not_duplicate_parent_input(tmp_path) -> None:
    """同一问题重提事务后，父只收到一条问题，子保持同一暂停。"""
    async def scenario() -> None:
        """用真实存储的加载、查询及提交接口观察投递结果。"""
        path = str(tmp_path / 'state.sqlite3')
        dao = SubagentDao(SqliteFactory(path))
        parent = SessionStateManager('parent', SqliteFactory(path))
        child = SessionStateManager('child', SqliteFactory(path), storage_scope=AgentStorageScope.SUBAGENT)
        definition = SubagentDefinition('child', 'parent', '助手', '执行任务', frozenset(), True, datetime.now(timezone.utc))
        try:
            await dao.create_with_command(definition, '开始', 'create')
            state = await child.load()
            state.record_assistant_step('', (ToolCall(id='question', name='message_parent', args={'text': '哪个文件？'}),))
            state.set_interrupt({'type': InterruptKind.PARENT_MESSAGE, 'request_id': 'run:question',
                                 'tool_call_id': 'question', 'tool_name': 'message_parent',
                                 'data': {'parent_session_id': 'parent', 'subagent_name': '助手', 'text': '哪个文件？'}}, [])
            state.clear_pending_tool_calls()
            await child.commit_parent_interrupt(state)
            await child.commit_parent_interrupt(state)
            assert len(await parent.read_activation_batch()) == 1
            assert (await child.load()).interrupt_data['request_id'] == 'run:question'
            assert len([item for item in await dao.recent_interactions('child') if item.kind == 'a2a']) == 1
        finally:
            await child.close()
            await parent.close()
            await dao.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('table', ['subagent_checkpoints', 'subagent_conversation_messages', 'session_inbox', 'subagent_interactions'])
def test_parent_interrupt_failure_rolls_back_every_write(tmp_path, table: str) -> None:
    """暂停的任一写入失败时，子保持原进度、父无问题；重试整项成功。"""
    async def scenario() -> None:
        """用隔离库触发器注入失败，通过公开读取核对回滚。"""
        path = str(tmp_path / 'state.sqlite3')
        dao = SubagentDao(SqliteFactory(path))
        parent = SessionStateManager('parent', SqliteFactory(path))
        child = SessionStateManager('child', SqliteFactory(path), storage_scope=AgentStorageScope.SUBAGENT)
        definition = SubagentDefinition('child', 'parent', '助手', '执行任务', frozenset(), True, datetime.now(timezone.utc))
        try:
            await dao.create_with_command(definition, '开始', 'create')
            await parent.read_activation_batch()
            state = await child.load()
            state.record_assistant_step('', (ToolCall(id='question', name='message_parent', args={}),))
            await child.save_progress(state)
            candidate = state.model_copy(deep=True)
            candidate.set_interrupt({'type': InterruptKind.PARENT_MESSAGE, 'request_id': 'question',
                'tool_call_id': 'question', 'tool_name': 'message_parent',
                'data': {'parent_session_id': 'parent', 'subagent_name': '助手', 'text': '哪个文件？'}}, [])
            candidate.clear_pending_tool_calls()
            with sqlite3.connect(path) as db:
                db.execute(f"CREATE TRIGGER fail_pause BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'pause failure'); END")
            with pytest.raises(RequiredSaveError, match='pause failure') as failure:
                await child.commit_parent_interrupt(candidate)
            assert isinstance(failure.value.__cause__, sqlite3.IntegrityError)
            assert (await child.load()).interrupt_data is None
            assert await parent.read_activation_batch() == ()
            assert [item.kind for item in await dao.recent_interactions('child')] == ['task']
            with sqlite3.connect(path) as db:
                db.execute('DROP TRIGGER fail_pause')
            await child.commit_parent_interrupt(candidate)
            assert len(await parent.read_activation_batch()) == 1
        finally:
            await child.close()
            await parent.close()
            await dao.close()
    asyncio.run(scenario())
