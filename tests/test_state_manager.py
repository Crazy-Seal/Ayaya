import asyncio
import json
from datetime import timedelta
from pathlib import Path

import aiosqlite
import pytest

from app.agent.state.manager import LATEST_CHECKPOINT_SQL, StateManager
from app.agent.memory.store.chat_history_store import ChatHistoryStore
from app.crud.chat_history_dao import ChatHistoryDao
from app.agent.state import AgentState, TokenBaseline
import app.agent.state.manager as state_manager_module
from app.runtime import get_agent_state_db, get_chat_history_db, get_checkpoint_db


async def checkpoint_rows(manager: StateManager) -> list[tuple[int, str, str]]:
    db = await manager._get_db()
    async with db.execute(
        """
        SELECT id, checkpoint_type, state_json FROM checkpoints
        WHERE session_id = ? ORDER BY id
        """,
        (manager.session_id,),
    ) as cursor:
        return await cursor.fetchall()


def test_latest_checkpoint_query_does_not_require_temp_sort(tmp_path: Path) -> None:
    async def scenario() -> list[str]:
        manager = StateManager(
            "test-session",
            db_path=str(tmp_path / "checkpoints.sqlite3"),
        )
        try:
            db = await manager._get_db()
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
    async def scenario() -> None:
        manager = StateManager("test-session", db_path=str(tmp_path / "checkpoints.sqlite3"))
        try:
            first = AgentState.create_new("test-session")
            first.add_assistant_message("第一个中间态")
            first_id = await manager.save(first, checkpoint_type="intermediate")

            db = await manager._get_db()
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
    async def scenario() -> None:
        manager = StateManager("test-session", db_path=str(tmp_path / "checkpoints.sqlite3"))
        try:
            state = AgentState.create_new("test-session")
            await manager.save(state, checkpoint_type="intermediate")

            total = StateManager.MAX_COMPLETED_CHECKPOINTS_PER_SESSION + 5
            for index in range(total):
                state = AgentState.create_new("test-session")
                state.add_assistant_message(f"完成态 {index}")
                await manager.save(state, checkpoint_type="completed")

            rows = await checkpoint_rows(manager)
            assert len(rows) == StateManager.MAX_COMPLETED_CHECKPOINTS_PER_SESSION
            assert {row[1] for row in rows} == {"completed"}
            first_retained = AgentState.from_checkpoint(json.loads(rows[0][2]))
            assert first_retained.messages[-1]["content"] == "完成态 5"

            intermediate = AgentState.create_new("test-session")
            intermediate.add_assistant_message("最新中间态")
            await manager.save(intermediate, checkpoint_type="intermediate")
            rows = await checkpoint_rows(manager)
            assert len(rows) == StateManager.MAX_COMPLETED_CHECKPOINTS_PER_SESSION + 1
            assert [row[1] for row in rows].count("intermediate") == 1
            assert (await manager.load()).messages[-1]["content"] == "最新中间态"
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_runtime_uses_one_database_for_checkpoint_and_chat_history() -> None:
    assert get_checkpoint_db() == get_agent_state_db()
    assert get_chat_history_db() == get_agent_state_db()


def test_token_baseline_is_persisted_in_checkpoint(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = StateManager("test-session", db_path=str(tmp_path / "agent.sqlite3"))
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
    async def scenario() -> None:
        manager = StateManager("test-session", db_path=str(tmp_path / "agent.sqlite3"))
        try:
            state = AgentState.create_new("test-session")
            state.add_user_message("你好")
            state.add_assistant_message("你好呀")
            await manager.save(state, checkpoint_type="completed")

            db = await manager._get_db()
            rows = await db.execute_fetchall(
                "SELECT role,message_id FROM conversation_messages WHERE session_id=? ORDER BY id",
                ("test-session",),
            )
            assert [row[0] for row in rows] == ["user", "assistant"]
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
    async def scenario() -> None:
        manager = StateManager("test-session", db_path=str(tmp_path / "agent.sqlite3"))
        try:
            state = AgentState.create_new("test-session")
            state.add_user_message("真实问题")
            state.messages.append({
                "role": "user",
                "name": "mcp_tool_image",
                "content": [
                    {"type": "text", "text": "[系统消息]工具返回图片"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,MQ=="}},
                ],
            })
            state.add_assistant_message("完成")
            await manager.save(state, checkpoint_type="completed")

            db = await manager._get_db()
            rows = await ChatHistoryDao(db_path=tmp_path / "agent.sqlite3").list_chat_history_async(
                "test-session"
            )
            assert [row["role"] for row in rows] == ["Human", "AI", "AI"]
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_state_manager_does_not_access_retired_legacy_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_if_called():
        raise AssertionError("不应再访问旧聊天记录数据库")

    monkeypatch.setattr(
        state_manager_module,
        "get_legacy_chat_history_db",
        fail_if_called,
        raising=False,
    )

    async def scenario() -> None:
        manager = StateManager(
            "test-session",
            db_path=str(tmp_path / "agent_checkpoints.sqlite3"),
        )
        try:
            await manager._get_db()
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_fresh_state_database_does_not_create_retired_chat_history_table(
    tmp_path: Path,
) -> None:
    async def scenario() -> set[str]:
        manager = StateManager(
            "test-session",
            db_path=str(tmp_path / "agent.sqlite3"),
        )
        try:
            db = await manager._get_db()
            rows = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            return {row[0] for row in rows}
        finally:
            await manager.close()

    tables = asyncio.run(scenario())
    assert "conversation_messages" in tables
    assert "chat_history" not in tables


def test_chat_history_store_has_no_independent_write_api() -> None:
    assert not hasattr(ChatHistoryStore, "save_chat_message")


def test_chat_history_store_honors_previous_input_high_watermark(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, list[str]]:
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("test-session", db_path=str(db_path))
        store = ChatHistoryStore(db_path=db_path)
        try:
            state = AgentState.create_new("test-session")
            state.add_user_message("上一轮输入")
            state.add_assistant_message("上一轮回复")
            await manager.save(state, checkpoint_type="completed")
            cutoff = await store.get_latest_message_id("test-session")

            state.add_user_message("当前可修改输入")
            await manager.save(state, checkpoint_type="intermediate")
            effective_date = store._utc_to_local_date(state.messages[0]["_created_at"])
            messages = await store.get_messages_by_date(
                "test-session",
                effective_date,
                max_history_id=cutoff,
            )
            human_count = sum(
                1 for message in messages
                if message.get("role") == "user"
            )
            return human_count, [message["content"] for message in messages]
        finally:
            await manager.close()

    assert asyncio.run(scenario()) == (1, ["上一轮输入", "上一轮回复"])


def test_latest_protocol_id_uses_session_scoped_monotonic_high_watermark(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int]:
        db_path = tmp_path / "agent.sqlite3"
        first = StateManager("first", db_path=str(db_path))
        second = StateManager("second", db_path=str(db_path))
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
    async def scenario() -> tuple[int, int, int]:
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("session", db_path=str(db_path))
        try:
            state = AgentState.create_new("session")
            for index in range(15):
                state.add_user_message(f"human-{index}")
                state.add_assistant_message(f"assistant-{index}")
            await manager.save(state, checkpoint_type="completed")

            store = ChatHistoryStore(db_path=db_path)

            async def reject_full_row_loader(*_args, **_kwargs):
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
    db_path = tmp_path / "agent_checkpoints.sqlite3"

    ChatHistoryStore(db_path=db_path)

    assert not db_path.exists()
    assert not hasattr(ChatHistoryStore, "_ensure_table")
    assert not hasattr(ChatHistoryStore, "list_chat_history")
    assert not hasattr(ChatHistoryStore, "has_chat_on_date")
