from app.agent.models.openai_message_names import MCP_TOOL_IMAGE_MESSAGE_NAME
from tests.model_capture import result_parts
import asyncio
import json
import re
from datetime import timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.agent.state.manager import SessionStateManager
from app.crud.agent_state_dao import AgentStateDao, LATEST_CHECKPOINT_SQL
from app.agent.memory.store.chat_history_store import ChatHistoryStore
from app.crud.chat_history_dao import ChatHistoryDao
from app.agent.state import AgentState, TokenBaseline
from app.agent.state.messages import (HumanMessage, PROTOCOL_MESSAGE_TYPES, ToolCall, ImageContent, ToolMessage)
from app.environment import get_agent_state_db, get_chat_history_db, get_checkpoint_db


async def checkpoint_rows(manager: SessionStateManager) -> list[tuple[int, str, str]]:
    """只读查询临时数据库中的 checkpoint 行，供验证清理和保留规则。

    Args:
        manager (SessionStateManager): 本用例的临时数据库状态管理器。

    Returns:
        list[tuple[int, str, str]]: 供外层用例断言使用的预设结果。
    """
    db = await manager._state_dao._get_db()
    async with db.execute(
        """
        SELECT id, checkpoint_type, state_json FROM checkpoints
        WHERE session_id = ? ORDER BY id
        """,
        (manager.session_id,),
    ) as cursor:
        return await cursor.fetchall()


def test_latest_checkpoint_query_does_not_require_temp_sort(tmp_path: Path) -> None:
    """验证最新 checkpoint 查询不使用临时排序。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> list[str]:
        """由外层测试调用，完成“最新 checkpoint 查询不使用临时排序”的异步步骤。

        Returns:
            list[str]: 供外层用例断言使用的预设结果。
        """
        manager = SessionStateManager(
            "test-session",
            db_path=str(tmp_path / "checkpoints.sqlite3"),
        )
        try:
            db = await manager._state_dao._get_db()
            rows = await db.execute_fetchall(
                f"EXPLAIN QUERY PLAN {LATEST_CHECKPOINT_SQL}",
                ("test-session",),
            )
            return [str(row[3]) for row in rows]
        finally:
            await manager.close()

    plan = asyncio.run(scenario())

    assert not any("TEMP B-TREE" in detail for detail in plan)


def test_intermediate_checkpoint_replaces_previous_one_atomically(tmp_path: Path) -> None:
    """验证中间 checkpoint 在事务中替换上一份中间记录。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“中间 checkpoint 在事务中替换上一份中间记录”的异步步骤。"""
        manager = SessionStateManager("test-session", db_path=str(tmp_path / "checkpoints.sqlite3"))
        try:
            first = AgentState.create_new("test-session")
            first.add_assistant_message("第一个中间态")
            first_id = await manager.save(first, checkpoint_type="intermediate")

            db = await manager._state_dao._get_db()
            await db.execute("""
                CREATE TRIGGER prevent_intermediate_delete
                BEFORE DELETE ON checkpoints
                WHEN OLD.checkpoint_type = 'intermediate'
                BEGIN
                    SELECT RAISE(ABORT, '模拟删除旧中间态失败');
                END
            """)
            await db.commit()

            failed = AgentState.from_checkpoint(first.to_checkpoint())
            failed.add_assistant_message("不应提交的中间态")
            with pytest.raises(aiosqlite.IntegrityError, match="模拟删除旧中间态失败"):
                await manager.save(failed, checkpoint_type="intermediate")

            rows_after_failure = await checkpoint_rows(manager)
            assert [row[0] for row in rows_after_failure] == [first_id]

            await db.execute("DROP TRIGGER prevent_intermediate_delete")
            await db.commit()
            second = AgentState.from_checkpoint(first.to_checkpoint())
            second.add_assistant_message("第二个中间态")
            second_id = await manager.save(second, checkpoint_type="intermediate")

            rows = await checkpoint_rows(manager)
            assert [(row[0], row[1]) for row in rows] == [(second_id, "intermediate")]
        finally:
            await manager.close()

    asyncio.run(scenario())

def test_completed_checkpoint_removes_intermediate_and_keeps_latest_thirty(
    tmp_path: Path,
) -> None:
    """验证完成保存删除中间记录并保留最近三十份完成记录。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“完成保存删除中间记录并保留最近三十份完成记录”的异步步骤。"""
        manager = SessionStateManager("test-session", db_path=str(tmp_path / "checkpoints.sqlite3"))
        try:
            state = AgentState.create_new("test-session")
            await manager.save(state, checkpoint_type="intermediate")

            total = AgentStateDao.MAX_COMPLETED_CHECKPOINTS_PER_SESSION + 5
            for index in range(total):
                state = AgentState.create_new("test-session")
                state.add_assistant_message(f"完成态 {index}")
                await manager.save(state, checkpoint_type="completed")

            rows = await checkpoint_rows(manager)
            assert len(rows) == AgentStateDao.MAX_COMPLETED_CHECKPOINTS_PER_SESSION
            assert {row[1] for row in rows} == {"completed"}
            first_retained = AgentState.from_checkpoint(json.loads(rows[0][2]))
            assert first_retained.messages[-1].text == "完成态 5"

            intermediate = AgentState.create_new("test-session")
            intermediate.add_assistant_message("最新中间态")
            await manager.save(intermediate, checkpoint_type="intermediate")
            rows = await checkpoint_rows(manager)
            assert len(rows) == AgentStateDao.MAX_COMPLETED_CHECKPOINTS_PER_SESSION + 1
            assert [row[1] for row in rows].count("intermediate") == 1
            assert (await manager.load()).messages[-1].text == "最新中间态"
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_runtime_uses_one_database_for_checkpoint_and_chat_history() -> None:
    """验证checkpoint 和聊天历史使用同一数据库。"""
    assert get_checkpoint_db() == get_agent_state_db()
    assert get_chat_history_db() == get_agent_state_db()


def test_token_baseline_is_persisted_in_checkpoint(tmp_path: Path) -> None:
    """验证真实 token 基线保存到 checkpoint。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“真实 token 基线保存到 checkpoint”的异步步骤。"""
        manager = SessionStateManager("test-session", db_path=str(tmp_path / "agent.sqlite3"))
        try:
            state = AgentState.create_new("test-session")
            state.token_baseline = TokenBaseline(
                accounting_key="openai-chat:model-a",
                estimator_version=1,
                static_fingerprint="abc",
                rough_prompt_tokens=900,
                actual_prompt_tokens=1_000,
            )
            await manager.save(state, checkpoint_type="completed")

            restored = await manager.load()

            assert restored.token_baseline == state.token_baseline
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_checkpoint_and_visible_history_are_saved_atomically(tmp_path: Path) -> None:
    """验证checkpoint 与可见历史原子保存。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“checkpoint 与可见历史原子保存”的异步步骤。"""
        manager = SessionStateManager("test-session", db_path=str(tmp_path / "agent.sqlite3"))
        try:
            state = AgentState.create_new("test-session")
            state.add_user_message("你好")
            state.add_assistant_message("你好呀")
            await manager.save(state, checkpoint_type="completed")

            db = await manager._state_dao._get_db()
            rows = await db.execute_fetchall(
                "SELECT message_type,message_id FROM conversation_messages WHERE session_id=? ORDER BY id",
                ("test-session",),
            )
            assert [row[0] for row in rows] == ["human", "assistant"]
            assert all(row[1] for row in rows)

            await db.execute(
                """
                CREATE TRIGGER reject_history_insert
                BEFORE INSERT ON conversation_messages
                BEGIN
                    SELECT RAISE(ABORT, '模拟聊天记录写入失败');
                END
                """
            )
            await db.commit()

            failed = AgentState.from_checkpoint(state.to_checkpoint())
            failed.add_user_message("不应提交")
            with pytest.raises(aiosqlite.IntegrityError, match="模拟聊天记录写入失败"):
                await manager.save(failed, checkpoint_type="intermediate")

            checkpoint_count = (
                await db.execute_fetchall(
                    "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?",
                    ("test-session",),
                )
            )[0][0]
            history_count = (
                await db.execute_fetchall(
                    "SELECT COUNT(*) FROM conversation_messages WHERE session_id = ?",
                    ("test-session",),
                )
            )[0][0]
            assert checkpoint_count == 1
            assert history_count == 2
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_mcp_tool_image_is_not_persisted_as_human_history(tmp_path: Path) -> None:
    """验证MCP 工具图片不会保存为真人历史。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“MCP 工具图片不会保存为真人历史”的异步步骤。"""
        manager = SessionStateManager("test-session", db_path=str(tmp_path / "agent.sqlite3"))
        try:
            state = AgentState.create_new("test-session")
            state.add_user_message("真实问题")
            call = ToolCall(id="call-mcp", name="mcp_tool", args={})
            state.record_assistant_step("", (call,))
            state.record_tool_result(call, result_parts('完成', ('data:image/png;base64,MQ==',), 'mcp'))
            state.add_assistant_message("完成")
            await manager.save(state, checkpoint_type="completed")

            db = await manager._state_dao._get_db()
            rows = await ChatHistoryDao(db_path=tmp_path / "agent.sqlite3").list_chat_history_async(
                "test-session"
            )
            assert [row["role"] for row in rows] == [
                "Human",
                "AI_Tool_Calling",
                "AI",
                "AI",
            ]
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_fresh_state_database_creates_checkpoint_and_protocol_log_tables(
    tmp_path: Path,
) -> None:
    """验证新数据库建立 checkpoint 和协议日志表。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> set[str]:
        """由外层测试调用，完成“新数据库建立 checkpoint 和协议日志表”的异步步骤。

        Returns:
            set[str]: 供外层用例断言使用的预设结果。
        """
        manager = SessionStateManager(
            "test-session",
            db_path=str(tmp_path / "agent.sqlite3"),
        )
        try:
            db = await manager._state_dao._get_db()
            rows = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            return {row[0] for row in rows}
        finally:
            await manager.close()

    tables = asyncio.run(scenario())
    assert {"checkpoints", "conversation_messages"} <= tables


def test_fresh_database_message_type_check_uses_shared_protocol_types(
    tmp_path: Path,
) -> None:
    """验证消息类型约束使用共用协议类型集合。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> str:
        """由外层测试调用，完成“消息类型约束使用共用协议类型集合”的异步步骤。

        Returns:
            str: 供外层用例断言使用的预设结果。
        """
        manager = SessionStateManager(
            "test-session",
            db_path=str(tmp_path / "agent.sqlite3"),
        )
        try:
            db = await manager._state_dao._get_db()
            rows = await db.execute_fetchall(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='conversation_messages'"
            )
            return str(rows[0][0])
        finally:
            await manager.close()

    table_sql = asyncio.run(scenario())
    match = re.search(
        r"message_type\s+TEXT\s+CHECK\s*\(message_type\s+IN\s*\((.*?)\)\)",
        table_sql,
        re.DOTALL,
    )
    assert match is not None
    allowed_types = tuple(re.findall(r"'([^']+)'", match.group(1)))
    assert allowed_types == PROTOCOL_MESSAGE_TYPES


def test_chat_history_store_honors_previous_input_high_watermark(tmp_path: Path) -> None:
    """验证历史读取遵守上一次输入捕获的编号边界。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> tuple[int, list[str]]:
        """由外层测试调用，完成“历史读取遵守上一次输入捕获的编号边界”的异步步骤。

        Returns:
            tuple[int, list[str]]: 供外层用例断言使用的预设结果。
        """
        db_path = tmp_path / "agent.sqlite3"
        manager = SessionStateManager("test-session", db_path=str(db_path))
        store = ChatHistoryStore(db_path=db_path)
        try:
            state = AgentState.create_new("test-session")
            state.add_user_message("上一轮输入")
            state.add_assistant_message("上一轮回复")
            await manager.save(state, checkpoint_type="completed")
            cutoff = await store.get_latest_message_id("test-session")

            state.add_user_message("当前可修改输入")
            await manager.save(state, checkpoint_type="intermediate")
            effective_date = store._utc_to_local_date(state.messages[0].created_at)
            messages = await store.get_messages_by_date(
                "test-session",
                effective_date,
                max_history_id=cutoff,
            )
            human_count = sum(
                1 for message in messages
                if isinstance(message, HumanMessage)
            )
            return human_count, [message.text for message in messages]
        finally:
            await manager.close()

    assert asyncio.run(scenario()) == (1, ["上一轮输入", "上一轮回复"])


def test_latest_protocol_id_uses_session_scoped_monotonic_high_watermark(tmp_path: Path) -> None:
    """验证最新协议编号按会话查询且单调递增。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> tuple[int, int]:
        """由外层测试调用，完成“最新协议编号按会话查询且单调递增”的异步步骤。

        Returns:
            tuple[int, int]: 供外层用例断言使用的预设结果。
        """
        db_path = tmp_path / "agent.sqlite3"
        first = SessionStateManager("first", db_path=str(db_path))
        second = SessionStateManager("second", db_path=str(db_path))
        try:
            first_state = AgentState.create_new("first")
            first_state.add_user_message("first-1")
            await first.save(first_state, checkpoint_type="completed")

            second_state = AgentState.create_new("second")
            second_state.add_user_message("second-1")
            await second.save(second_state, checkpoint_type="completed")

            first_state.add_assistant_message("first-2")
            await first.save(first_state, checkpoint_type="completed")
            await first.save(first_state, checkpoint_type="completed")

            store = ChatHistoryStore(db_path=db_path)
            return (
                await store.get_latest_message_id("first"),
                await store.get_latest_message_id("second"),
            )
        finally:
            await first.close()
            await second.close()

    first_id, second_id = asyncio.run(scenario())
    assert first_id > second_id > 0


def test_narrow_history_queries_do_not_use_full_row_loader(tmp_path: Path) -> None:
    """验证有限历史查询不调用全量记录读取。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> tuple[int, int, int]:
        """由外层测试调用，完成“有限历史查询不调用全量记录读取”的异步步骤。

        Returns:
            tuple[int, int, int]: 供外层用例断言使用的预设结果。
        """
        db_path = tmp_path / "agent.sqlite3"
        manager = SessionStateManager("session", db_path=str(db_path))
        try:
            state = AgentState.create_new("session")
            for index in range(15):
                state.add_user_message(f"human-{index}")
                state.add_assistant_message(f"assistant-{index}")
            await manager.save(state, checkpoint_type="completed")

            store = ChatHistoryStore(db_path=db_path)

            async def reject_full_row_loader(*_args, **_kwargs):
                """全量行读取被调用时主动失败，验证有限查询没有绕过范围。

                该定义仅用于外层用例：验证有限历史查询不调用全量记录读取。

                Args:
                    _args (dict[str, Any]): 当前工具调用或被包装函数的参数。
                    _kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
                """
                raise AssertionError("窄查询不应加载整个会话的 message_json")

            store._rows = reject_full_row_loader  # type: ignore[method-assign]
            latest_id = await store.get_latest_message_id("session")
            last_date = await store.get_last_chat_date("session")
            previous = await store.get_messages_before_date(
                "session",
                last_date + timedelta(days=1),
                limit=10,
                max_history_id=latest_id,
            )
            history, current = await store.get_recent_completed_turns(
                "session",
                max_history_id=latest_id,
            )
            return len(previous), len(history), len(current)
        finally:
            await manager.close()

    assert asyncio.run(scenario()) == (10, 10, 20)


def test_chat_history_store_constructor_does_not_initialize_schema(tmp_path: Path) -> None:
    """验证历史存储构造时不初始化表结构。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    db_path = tmp_path / "agent_checkpoints.sqlite3"

    ChatHistoryStore(db_path=db_path)

    assert not db_path.exists()
