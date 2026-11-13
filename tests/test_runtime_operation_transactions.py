"""通过业务提交与查询 Interface 验证引导事务和创建幂等。"""
from app.crud.sqlite_factory import SqliteFactory

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import ToolCall
from app.crud.subagent_dao import SubagentDao
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.runtime_types import TaskStatus, TurnInterruptReason
from app.schemas.subagent_types import SubagentDefinition
from app.agent.models.contracts import StreamChunk
from tests.subagent_support import SubagentHarness, wait_for_subagent_worker


def test_create_replay_returns_original_identity(tmp_path) -> None:
    """创建响应丢失后重试仍返回原身份，初始命令只保存一次。"""
    async def scenario() -> None:
        """提供不同候选编号，验证固定创建来源决定实际身份。"""
        dao = SubagentDao(SqliteFactory(str(tmp_path / 'state.sqlite3')))
        try:
            first = SubagentDefinition('first', 'parent', '助手', '任务', frozenset(), True, datetime.now(timezone.utc))
            other = SubagentDefinition('other', 'parent', '助手', '任务', frozenset(), True, datetime.now(timezone.utc))
            assert (await dao.create_with_command(first, '开始', 'create')).subagent_id == 'first'
            assert (await dao.create_with_command(other, '开始', 'create')).subagent_id == 'first'
            assert len(await dao.list_records()) == 1
        finally:
            await dao.close()
    asyncio.run(scenario())


def test_final_response_save_retries_without_model_reexecution(tmp_path, monkeypatch) -> None:
    """正常回答提交暂时失败时，DAO 重试固定材料，模型只执行一次。"""
    async def scenario() -> None:
        """通过真实会话执行观察模型次数及唯一最终报告。"""
        calls = 0
        failures = 0
        from app.crud.agent_state_dao import AgentStateDao
        from app.schemas.runtime_types import CheckpointType
        original = AgentStateDao._write_state_in_transaction

        async def save_once(dao, db, state, **kwargs):
            """在具名事务内部拒绝一次暂时锁冲突，随后保存相同响应。"""
            nonlocal failures
            if dao.session_id != 'parent' and kwargs.get('checkpoint_type') == CheckpointType.COMPLETED and failures == 0:
                failures += 1
                error = sqlite3.OperationalError('暂时无法保存')
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            return await original(dao, db, state, **kwargs)

        async def script(messages, tools):
            """产生完整回答，重试阶段不应再次调用。"""
            nonlocal calls
            calls += 1
            yield StreamChunk(content='已完成，保存后交还')

        monkeypatch.setattr(AgentStateDao, '_write_state_in_transaction', save_once)
        async with SubagentHarness(tmp_path / 'state.sqlite3', script) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            detail = await harness.scheduler.get_subagent_details('parent', receipt.context_id)
            assert calls == 1
            assert len(detail['results']) == 1
            assert detail['results'][0]['status'] == 'completed'
            assert detail['results'][0]['result'] == '已完成，保存后交还'
    asyncio.run(scenario())


def test_steer_run_switch_rolls_back_old_end_if_new_insert_fails(tmp_path) -> None:
    """新段建立失败时旧段仍可报告；重试不会改写已正常结束的结果。"""
    async def scenario() -> None:
        """用真实记录查询核对旧段与新段的原子切换。"""
        path = str(tmp_path / 'state.sqlite3')
        dao = TaskRecordDao(SqliteFactory(path))
        definition = SubagentDefinition('child', 'parent', '助手', '任务', frozenset(), True, datetime.now(timezone.utc))
        try:
            await dao.start_subagent_run(definition, 'old', None)
            with sqlite3.connect(path) as db:
                db.execute("CREATE TRIGGER fail_run BEFORE INSERT ON task_records WHEN NEW.task_id='new' BEGIN SELECT RAISE(ABORT, 'run failure'); END")
            with pytest.raises(sqlite3.IntegrityError, match='run failure'):
                await dao.start_subagent_run(definition, 'new', 'old')
            assert (await dao.get('old')).status == TaskStatus.RUNNING
            assert await dao.get('new') is None
            with sqlite3.connect(path) as db:
                db.execute('DROP TRIGGER fail_run')
            await dao.start_subagent_run(definition, 'new', 'old')
            await dao.start_subagent_run(definition, 'new', 'old')
            assert (await dao.get('old')).end_reason == TurnInterruptReason.STEER
            await dao.finish_once('new', TaskStatus.COMPLETED)
            with pytest.raises(ValueError):
                await dao.start_subagent_run(definition, 'third', 'new')
            assert (await dao.get('new')).status == TaskStatus.COMPLETED
        finally:
            await dao.close()
    asyncio.run(scenario())


def test_authorization_steer_does_not_leave_input_without_decision(tmp_path) -> None:
    """审批引导决定保存失败时，新输入也回滚，旧请求仍可再次处理。"""
    async def scenario() -> None:
        """通过 Manager 的决定、状态和待交付查询接口验证事务。"""
        path = str(tmp_path / 'state.sqlite3')
        manager = SessionStateManager('parent', SqliteFactory(path))
        try:
            state = await manager.load()
            state.record_assistant_step('', (ToolCall(id='tool', name='test_tool', args={}),))
            state.clear_pending_tool_calls()
            state.set_interrupt({'type': 'authorization_request', 'request_id': 'approval',
                                 'tool_call_id': 'tool', 'tool_name': 'test_tool'})
            await manager.save_progress(state)
            with sqlite3.connect(path) as db:
                db.execute("CREATE TRIGGER fail_decision BEFORE INSERT ON checkpoints BEGIN SELECT RAISE(ABORT, 'decision failure'); END")
            with pytest.raises(sqlite3.IntegrityError, match='decision failure'):
                await manager.record_authorization_decision('approval', {'kind': 'steer', 'message': '改做这项'})
            assert not (await manager.load()).interrupt_data.get('runtime_decision')
            assert await manager.pending_humans() == []
            with sqlite3.connect(path) as db:
                db.execute('DROP TRIGGER fail_decision')
            await manager.record_authorization_decision('approval', {'kind': 'steer', 'message': '改做这项'})
            assert len(await manager.pending_humans()) == 1
        finally:
            await manager.close()
    asyncio.run(scenario())
