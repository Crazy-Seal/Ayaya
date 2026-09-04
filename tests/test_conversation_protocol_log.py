import asyncio
import json
from pathlib import Path

import aiosqlite
import pytest

from app.agent.core.state_manager import StateManager
from app.agent.state import AgentState
from app.crud.chat_history_dao import ChatHistoryDao


def test_checkpoint_and_protocol_log_are_saved_atomically(tmp_path: Path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("session", db_path=str(db_path))
        try:
            state = AgentState.create_new("session")
            state.add_user_message("你好")
            state.messages.append({
                "role": "assistant",
                "content": "我来读取",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }],
            })
            state.add_tool_message("结果", "read_file", "call-1", compressible=True)
            state.add_assistant_message("完成")
            await manager.save(state, checkpoint_type="completed")

            db = await manager._get_db()
            rows = await db.execute_fetchall(
                "SELECT role, message_json FROM conversation_messages ORDER BY id"
            )
            assert [row[0] for row in rows] == ["user", "assistant", "tool", "assistant"]
            assistant = json.loads(rows[1][1])
            assert assistant["content"] == "我来读取"
            assert assistant["tool_calls"][0]["id"] == "call-1"

            await db.execute("""
                CREATE TRIGGER reject_protocol_insert BEFORE INSERT ON conversation_messages
                BEGIN SELECT RAISE(ABORT, 'reject protocol'); END
            """)
            await db.commit()
            failed = AgentState.from_checkpoint(state.to_checkpoint())
            failed.add_user_message("不应提交")
            with pytest.raises(aiosqlite.IntegrityError, match="reject protocol"):
                await manager.save(failed, checkpoint_type="intermediate")
            count = (await db.execute_fetchall("SELECT COUNT(*) FROM checkpoints"))[0][0]
            assert count == 1
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_history_dao_projects_visible_rows_before_pagination(tmp_path: Path) -> None:
    async def scenario() -> list[dict]:
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("session", db_path=str(db_path))
        try:
            state = AgentState.create_new("session")
            state.add_user_message("问题")
            state.messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            })
            state.add_tool_message("隐藏结果", "read_file", "call-1", compressible=True)
            state.add_assistant_message("回答")
            await manager.save(state, checkpoint_type="completed")
            return await ChatHistoryDao(db_path=db_path).list_chat_history_async(
                "session", start=1, limit=2
            )
        finally:
            await manager.close()

    rows = asyncio.run(scenario())
    assert [row["role"] for row in rows] == ["AI_Tool_Calling", "AI"]


def test_tool_image_protocol_row_references_its_tool_message(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str | None]:
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("session", db_path=str(db_path))
        try:
            state = AgentState.create_new("session")
            state.add_tool_message("截屏成功", "screenshot", "call-shot")
            tool_message_id = state.messages[-1]["_message_id"]
            state.deferred_tool_images.append({
                "tool_call_id": "call-shot",
                "tool_name": "screenshot",
                "image_message_name": "system_screenshot",
                "image_urls": ["data:image/png;base64,AAA"],
            })
            state.flush_deferred_tool_images()
            await manager.save(state, checkpoint_type="completed")

            db = await manager._get_db()
            row = (await db.execute_fetchall(
                """
                SELECT record_kind, associated_message_id
                FROM conversation_messages
                WHERE record_kind = 'tool_image'
                """
            ))[0]
            assert row[0] == "tool_image"
            return tool_message_id, row[1]
        finally:
            await manager.close()

    tool_message_id, associated_message_id = asyncio.run(scenario())
    assert associated_message_id == tool_message_id


def test_tool_image_without_tool_call_id_is_not_misassociated() -> None:
    state = AgentState.create_new("session")
    state.add_tool_message("旧工具结果", "legacy_tool")
    state.deferred_tool_images.append({
        "tool_name": "unknown",
        "image_message_name": "mcp_tool_image",
        "image_urls": ["data:image/png;base64,AAA"],
    })

    state.flush_deferred_tool_images()

    assert "_associated_message_id" not in state.messages[-1]


def test_history_dao_does_not_fall_back_to_retired_chat_history_table(
    tmp_path: Path,
) -> None:
    async def scenario() -> list[dict]:
        db_path = tmp_path / "agent.sqlite3"
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE chat_history (
                    id INTEGER PRIMARY KEY,
                    thread_id TEXT,
                    role TEXT,
                    content TEXT
                )
                """
            )
            await db.execute(
                "INSERT INTO chat_history VALUES (1, 'session', 'Human', '旧记录')"
            )
            await db.commit()
        return await ChatHistoryDao(db_path=db_path).list_chat_history_async("session")

    assert asyncio.run(scenario()) == []
