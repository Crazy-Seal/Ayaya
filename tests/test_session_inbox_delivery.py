import asyncio
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from app.agent.state import AgentState
from app.agent.state.manager import SessionStateManager
from app.crud.task_record_dao import TaskRecordDao


def test_inbox_input_and_checkpoint_commit_as_one_delivery(tmp_path) -> None:
    """验证输入交付与 checkpoint 在同一事务提交或回滚。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：输入交付与 checkpoint 在同一事务提交或回滚。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        manager = SessionStateManager("shared", db_path=db_path)
        inbox = SessionStateManager("shared", db_path=db_path)
        try:
            envelope = await inbox.submit_human("qq", "请截图")
            input_record = await inbox.read_next()
            assert input_record is not None and input_record.id == envelope.id
            state = AgentState.create_new("shared")
            state.add_user_message(input_record.payload["message"], origin_channel=input_record.origin_channel)
            await manager.save(state, checkpoint_type="intermediate", inbox_inputs=(input_record,))
            assert await inbox.status(envelope.id) == "delivered"
            # 2. 从独立连接确认 checkpoint、协议历史和交付记录均已持久提交。
            with closing(sqlite3.connect(db_path)) as inspection:
                assert inspection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
                assert inspection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 1
                status, delivered_at = inspection.execute(
                    "SELECT status, delivered_at FROM session_inbox WHERE id = ?",
                    (envelope.id,),
                ).fetchone()
                assert status == "delivered" and delivered_at is not None
            restored = await manager.load()
            assert restored.messages[-1].origin_channel == "qq"
        finally:
            await manager.close()
            await inbox.close()

    asyncio.run(scenario())


def test_later_cancelled_input_rolls_back_checkpoint_protocol_and_earlier_delivery(tmp_path: Path) -> None:
    """验证同批后一条输入撤销时，前一条交付和状态保存一起回滚，并可重试。

    Args:
        tmp_path (Path): 测试专用 SQLite 目录。
    """
    async def scenario() -> None:
        """通过公开读取和保存入口，检查部分交付失败后的持久状态。"""
        path = str(tmp_path / "state.sqlite3")
        inbox = SessionStateManager("shared", path)
        manager = SessionStateManager("shared", path)
        try:
            # 1. 保存空的旧 checkpoint，然后读取两条尚未写入协议历史的真人输入。
            state = AgentState.create_new("shared")
            baseline_id = await manager.save(state, checkpoint_type="intermediate")
            await inbox.submit_human("desktop", "第一条")
            first = await inbox.read_next()
            second = await inbox.submit_human("desktop", "第二条", queued=True)
            assert first is not None and second is not None
            state.add_user_message("第一条")
            state.add_user_message("第二条")
            # 2. 第一条允许更新，第二条已经撤销，数据库必须撤销前面的全部写入。
            assert await inbox.cancel_queued(second.id, "desktop")
            with pytest.raises(RuntimeError, match="输入已失效"):
                await manager.save(
                    state, checkpoint_type="intermediate",
                    inbox_inputs=(first, second),
                )
            with closing(sqlite3.connect(path)) as inspection:
                assert inspection.execute("SELECT id FROM checkpoints").fetchall() == [(baseline_id,)]
                assert inspection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
                assert inspection.execute(
                    "SELECT status, delivered_at FROM session_inbox ORDER BY id",
                ).fetchall() == [
                    ("pending", None),
                ]
            assert not (await manager.load()).messages
            # 3. 重新加载状态后，只保存第一条仍然有效的输入。
            state = await manager.load()
            state.add_user_message("第一条")
            await manager.save(state, checkpoint_type="intermediate", inbox_inputs=(first,))
            assert await inbox.status(first.id) == "delivered"
            assert await inbox.status(second.id) is None
            with closing(sqlite3.connect(path)) as inspection:
                assert inspection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 1
            assert len((await manager.load()).messages) == 1
        finally:
            await manager.close()
            await inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_field, invalid_value", [("session_id", "other")])
def test_invalid_input_session_leaves_no_checkpoint(
    tmp_path: Path, invalid_field: str, invalid_value: str | None,
) -> None:
    """验证错误会话时保存失败，且原始读取仍可成功交付。

    Args:
        tmp_path (Path): 测试专用 SQLite 目录。
        invalid_field (str): 需要破坏的读取快照字段。
        invalid_value (str | None): 错误会话。
    """
    async def scenario() -> None:
        """分别注入非法读取元数据，核对失败和重试的公开结果。"""
        path = str(tmp_path / "state.sqlite3")
        inbox = SessionStateManager("shared", path)
        manager = SessionStateManager("shared", path)
        try:
            # 1. 保留数据库中的有效读取，只破坏交给保存入口的快照。
            await inbox.submit_human("desktop", "合法输入")
            input_record = await inbox.read_next()
            assert input_record is not None
            state = AgentState.create_new("shared")
            state.add_user_message("合法输入")
            with pytest.raises(ValueError, match="不属于当前会话"):
                await manager.save(
                    state, checkpoint_type="intermediate",
                    inbox_inputs=(replace(input_record, **{invalid_field: invalid_value}),),
                )
            # 2. checkpoint 和协议历史均未保存，输入仍保持pending 状态。
            with closing(sqlite3.connect(path)) as inspection:
                assert inspection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
                assert inspection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
            assert await inbox.status(input_record.id) == "pending"
            await manager.save(state, checkpoint_type="intermediate", inbox_inputs=(input_record,))
            assert await inbox.status(input_record.id) == "delivered"
            assert len((await manager.load()).messages) == 1
        finally:
            await manager.close()
            await inbox.close()

    asyncio.run(scenario())


def test_queued_release_failure_rolls_back_completed_checkpoint_and_delivery(tmp_path: Path) -> None:
    """验证排队放行失败回滚完成状态、协议历史和输入交付，移除故障后可重试。

    Args:
        tmp_path (Path): 测试专用 SQLite 目录。
    """
    async def scenario() -> None:
        """使用真实 SQLite 触发器拒绝事务最后一步，核对整笔回滚。"""
        path = str(tmp_path / "state.sqlite3")
        inbox = SessionStateManager("shared", path)
        manager = SessionStateManager("shared", path)
        try:
            # 1. 保留旧 checkpoint，安排本轮输入和等待本轮完成的下一条输入。
            state = AgentState.create_new("shared")
            baseline_id = await manager.save(state, checkpoint_type="intermediate")
            await inbox.submit_human("desktop", "本轮输入")
            input_record = await inbox.read_next()
            assert input_record is not None
            queued = await inbox.submit_human("desktop", "下一轮输入", queued=True)
            state.add_user_message("本轮输入")
            with closing(sqlite3.connect(path)) as inspection:
                inspection.execute(
                    "CREATE TRIGGER reject_release BEFORE UPDATE OF queue_ready ON session_inbox "
                    "WHEN NEW.queue_ready = 1 BEGIN SELECT RAISE(ABORT, 'reject release'); END",
                )
                inspection.commit()
            # 2. 放行失败发生在交付更新之后，不能留下新 checkpoint 或已交付标记。
            with pytest.raises(sqlite3.IntegrityError, match="reject release"):
                await manager.save(
                    state, checkpoint_type="completed", inbox_inputs=(input_record,), release_queued=True,
                )
            with closing(sqlite3.connect(path)) as inspection:
                assert inspection.execute("SELECT id FROM checkpoints").fetchall() == [(baseline_id,)]
                assert inspection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
                assert inspection.execute(
                    "SELECT status, delivered_at FROM session_inbox WHERE id = ?", (input_record.id,),
                ).fetchone() == ("pending", None)
                assert inspection.execute(
                    "SELECT queue_ready FROM session_inbox WHERE id = ?", (queued.id,),
                ).fetchone() == (0,)
                inspection.execute("DROP TRIGGER reject_release")
                inspection.commit()
            assert [item.id for item in await inbox.read_activation_batch()] == [input_record.id]
            # 3. 去掉故障后，同一读取可完成提交，排队输入才获得执行资格。
            await manager.save(
                state, checkpoint_type="completed", inbox_inputs=(input_record,), release_queued=True,
            )
            assert await inbox.status(input_record.id) == "delivered"
            with closing(sqlite3.connect(path)) as inspection:
                assert inspection.execute("SELECT checkpoint_type FROM checkpoints").fetchall() == [("completed",)]
                assert inspection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 1
            batch = await inbox.read_activation_batch()
            assert [item.id for item in batch] == [queued.id]
        finally:
            await manager.close()
            await inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("through_checkpoint", [False, True])
def test_release_only_touches_pending_queued_humans_in_current_session(
    tmp_path: Path, through_checkpoint: bool,
) -> None:
    """验证独立放行和完成保存采用相同筛选规则，重复调用不触及其他记录。

    Args:
        tmp_path (Path): 测试专用 SQLite 目录。
        through_checkpoint (bool): 是否通过完成 checkpoint 请求放行。
    """
    async def scenario() -> None:
        """安排不同会话、类别和交付阶段，并用触发器禁止更新不符合条件的记录。"""
        path = str(tmp_path / "state.sqlite3")
        inbox = SessionStateManager("shared", path)
        other = SessionStateManager("other", path)
        manager = SessionStateManager("shared", path)
        records = TaskRecordDao(path)
        try:
            # 1. 先形成已交付和已读取的排队输入，再加入仍在等待的各类输入。
            delivered = await inbox.submit_human("desktop", "已交付", queued=True)
            await inbox.release_queued()
            input_record = await inbox.read_next()
            assert input_record is not None
            state = AgentState.create_new("shared")
            state.add_user_message("已交付")
            await manager.save(state, checkpoint_type="intermediate", inbox_inputs=(input_record,))
            input_record = await inbox.submit_human("desktop", "已读取", queued=True)
            await inbox.release_queued()
            assert (await inbox.read_next()).id == input_record.id
            target = await inbox.submit_human("desktop", "应放行", queued=True)
            await inbox.submit_human("desktop", "立即输入")
            await other.submit_human("desktop", "其他会话", queued=True)
            await records.insert_running("task", "shared", "任务", 1, str(tmp_path / "out"))
            assert await records.finish_once("task", "completed", 0)
            # 2. 即使重复更新不会改变值，也禁止放行 SQL 触及非目标记录。
            with closing(sqlite3.connect(path)) as inspection:
                before = inspection.execute(
                    "SELECT id, queue_ready, status FROM session_inbox ORDER BY id",
                ).fetchall()
                inspection.execute(
                    "CREATE TRIGGER guard_release BEFORE UPDATE OF queue_ready ON session_inbox "
                    "WHEN NOT (OLD.session_id = 'shared' AND OLD.kind = 'human' "
                    "AND OLD.delivery_mode = 'queued' AND OLD.status = 'pending') "
                    "BEGIN SELECT RAISE(ABORT, 'unexpected release'); END",
                )
                inspection.commit()
            for _ in range(2):
                if through_checkpoint:
                    await manager.save(state, checkpoint_type="completed", release_queued=True)
                else:
                    await inbox.release_queued()
            # 3. 两次放行只改变目标记录的资格，交付状态保持原值。
            with closing(sqlite3.connect(path)) as inspection:
                after = inspection.execute(
                    "SELECT id, queue_ready, status FROM session_inbox ORDER BY id",
                ).fetchall()
            assert after == [
                (row[0], 1 if row[0] == target.id else row[1], row[2]) for row in before
            ]
            assert await inbox.status(delivered.id) == "delivered"
            assert await inbox.status(input_record.id) == "pending"
        finally:
            await records.close()
            await manager.close()
            await other.close()
            await inbox.close()

    asyncio.run(scenario())


def test_queued_input_survives_connection_reopen_and_task_terminal_does_not_release_it(tmp_path) -> None:
    """验证同一进程重开 Inbox 连接时排队输入仍保留且不被任务通知放行。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：同一进程重开 Inbox 连接时排队输入仍保留且不被任务通知放行。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        inbox = SessionStateManager("shared", db_path)
        records = TaskRecordDao(db_path)
        try:
            queued = await inbox.submit_human("desktop", "稍后执行", queued=True)
            await records.insert_running("task-1", "shared", "任务", 1, str(tmp_path / "out"))
            assert await records.finish_once("task-1", "completed", 0)
            batch = await inbox.read_activation_batch()
            assert [item.kind for item in batch] == ["task_terminal"]
            manager = SessionStateManager("shared", db_path)
            try:
                await manager.save(
                    AgentState.create_new("shared"), checkpoint_type="completed",
                    inbox_inputs=batch,
                )
            finally:
                await manager.close()
        finally:
            await inbox.close()
            await records.close()
        restarted = SessionStateManager("shared", db_path)
        try:
            assert await restarted.read_activation_batch() == ()
            assert await restarted.status(queued.id) == "pending"
            await restarted.release_queued()
            released = await restarted.read_activation_batch()
            assert len(released) == 1 and released[0].id == queued.id
        finally:
            await restarted.close()

    asyncio.run(scenario())


def test_activation_batch_groups_later_task_terminal_before_human_projection(tmp_path) -> None:
    """验证同次激活的任务终态先于真人输入投影。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：同次激活的任务终态先于真人输入投影。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        inbox = SessionStateManager("shared", db_path)
        records = TaskRecordDao(db_path)
        try:
            human = await inbox.submit_human("desktop", "用户指令")
            await records.insert_running("task-1", "shared", "任务", 1, str(tmp_path / "out"))
            assert await records.finish_once("task-1", "completed", 0)
            batch = await inbox.read_activation_batch()
            assert {item.id for item in batch} == {human.id, human.id + 1}
            assert [item.kind for item in batch] == ["human", "task_terminal"]
        finally:
            await inbox.close()
            await records.close()

    asyncio.run(scenario())


def test_completed_checkpoint_releases_queued_input_in_same_transaction(tmp_path) -> None:
    """验证正常完成 checkpoint 原子放行排队消息。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：正常完成 checkpoint 原子放行排队消息。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        inbox = SessionStateManager("shared", db_path)
        manager = SessionStateManager("shared", db_path)
        try:
            queued = await inbox.submit_human("desktop", "随后执行", queued=True)
            await manager.save(
                AgentState.create_new("shared"), checkpoint_type="completed",
                release_queued=True,
            )
        finally:
            await manager.close()
            await inbox.close()
        restarted = SessionStateManager("shared", db_path)
        try:
            input_record = await restarted.read_activation_batch()
            assert len(input_record) == 1 and input_record[0].id == queued.id
        finally:
            await restarted.close()

    asyncio.run(scenario())
