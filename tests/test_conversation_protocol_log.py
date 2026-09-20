import asyncio
import json
from pathlib import Path

import aiosqlite
import pytest
from fastapi import HTTPException

from app.agent.context._compaction import ContextCompressionPolicy
from app.agent.state.manager import StateManager
from app.agent.state import AgentState
from app.agent.state.messages import (
    AvailableMedia,
    CompactedMedia,
    MessageContractError,
    ToolCall,
    ToolMediaMessage,
)
from app.agent.state.message_names import (
    SCREENSHOT_COMPRESSED_NAME,
    SCREENSHOT_MESSAGE_NAME,
)
from app.crud.chat_history_dao import ChatHistoryDao
from app.routes.chat_history import list_chat_history, list_chat_history_last_n


def test_checkpoint_and_protocol_log_are_saved_atomically(tmp_path: Path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("session", db_path=str(db_path))
        try:
            state = AgentState.create_new("session")
            state.add_user_message("你好")
            state.record_assistant_step("我来读取", (
                ToolCall(id="call-1", name="read_file", args={"path": "a.py"}),
            ))
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
            state.record_assistant_step("", (
                ToolCall(id="call-1", name="read_file", args={}),
            ))
            state.add_tool_message("隐藏结果", "read_file", "call-1", compressible=True)
            state.add_assistant_message("回答")
            await manager.save(state, checkpoint_type="completed")
            dao = ChatHistoryDao(db_path=db_path)

            async def reject_full_history_load(_session_id: str) -> list[dict]:
                raise AssertionError("分页查询不应先加载并投影整个会话")

            dao._visible_protocol_rows = reject_full_history_load  # type: ignore[method-assign]
            return await dao.list_chat_history_async(
                "session", start=1, limit=2
            )
        finally:
            await manager.close()

    rows = asyncio.run(scenario())
    assert [row["role"] for row in rows] == ["AI_Tool_Calling", "AI"]


def test_history_dao_keeps_projection_order_across_small_query_chunks(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[dict], list[dict]]:
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("session", db_path=str(db_path))
        try:
            state = AgentState.create_new("session")
            state.add_user_message("question")
            state.record_assistant_step("working", (
                ToolCall(id="call-1", name="read_file", args={}),
            ))
            state.add_tool_message("hidden", "read_file", "call-1", compressible=True)
            state.add_assistant_message("answer")
            await manager.save(state, checkpoint_type="completed")

            dao = ChatHistoryDao(db_path=db_path)
            dao.READ_CHUNK_SIZE = 1
            return (
                await dao.list_chat_history_async("session", start=1, limit=2),
                await dao.list_chat_history_last_n_async("session", n=2),
            )
        finally:
            await manager.close()

    page, tail = asyncio.run(scenario())
    assert [(row["role"], row["content"]) for row in page] == [
        ("AI", "working"),
        ("AI_Tool_Calling", "调用了工具: read_file"),
    ]
    assert [(row["role"], row["content"]) for row in tail] == [
        ("AI_Tool_Calling", "调用了工具: read_file"),
        ("AI", "answer"),
    ]


def test_tool_image_protocol_row_references_its_tool_message(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str | None]:
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("session", db_path=str(db_path))
        try:
            state = AgentState.create_new("session")
            call = ToolCall(id="call-shot", name="screenshot", args={})
            state.record_assistant_step("", (call,))
            tool_message = state.record_tool_result(
                call,
                "截屏成功",
                image_urls=("data:image/png;base64,AAA",),
                image_message_name=SCREENSHOT_MESSAGE_NAME,
            )
            tool_message_id = tool_message.message_id
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


def test_compressed_checkpoint_preserves_externalized_screenshot_history(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[dict, dict, AgentState]:
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("session", db_path=str(db_path))
        try:
            state = AgentState.create_new("session")
            call = ToolCall(id="call-shot", name="screenshot", args={})
            state.record_assistant_step("", (call,))
            state.record_tool_result(
                call,
                "截屏成功",
                image_urls=(
                    "data:image/png;base64,"
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
                ),
                image_message_name=SCREENSHOT_MESSAGE_NAME,
            )
            screenshot_id = state.messages[-1].message_id
            await manager.save(state, checkpoint_type="completed")

            db = await manager._get_db()
            before_row = (await db.execute_fetchall(
                """
                SELECT message_json, attachments_json
                FROM conversation_messages
                WHERE message_id = ?
                """,
                (screenshot_id,),
            ))[0]

            candidate, changed = ContextCompressionPolicy().compress_screenshots(
                state.messages,
                ttl_human_messages=0,
                max_screenshots=2,
            )
            assert changed == 1
            state.commit_compaction(candidate)
            await manager.save(state, checkpoint_type="intermediate")

            after_row = (await db.execute_fetchall(
                """
                SELECT message_json, attachments_json
                FROM conversation_messages
                WHERE message_id = ?
                """,
                (screenshot_id,),
            ))[0]
            restored = await manager.load()
            return (
                {
                    "message": json.loads(before_row[0]),
                    "attachments": json.loads(before_row[1]),
                },
                {
                    "message": json.loads(after_row[0]),
                    "attachments": json.loads(after_row[1]),
                },
                restored,
            )
        finally:
            await manager.close()

    before, after, restored = asyncio.run(scenario())

    assert before == after
    assert before["message"]["name"] == SCREENSHOT_MESSAGE_NAME
    assert before["attachments"][0].startswith("screenshot/")
    restored_screenshot = next(
        message
        for message in restored.messages
        if isinstance(message, ToolMediaMessage)
        and isinstance(message.payload, CompactedMedia)
    )
    assert "base64" not in json.dumps(restored_screenshot.to_storage_format())


def test_tool_image_without_tool_call_id_is_not_misassociated() -> None:
    state = AgentState.create_new("session")
    message = ToolMediaMessage(
        tool_name="unknown",
        media_kind="mcp",
        text="[系统消息]工具返回图片",
        payload=AvailableMedia(urls=("data:image/png;base64,AAA",)),
    )
    checkpoint = state.to_checkpoint()
    checkpoint["messages"] = [message.to_storage_format()]
    state = AgentState.from_checkpoint(checkpoint)

    assert state.messages[-1].associated_message_id is None


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


def test_history_dao_raises_for_missing_message_type_but_keeps_legacy_notice(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[MessageContractError, list[dict]]:
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("session", db_path=str(db_path))
        try:
            db = await manager._get_db()
            await db.execute(
                """
                INSERT INTO conversation_messages (
                    session_id, message_id, created_at, role, message_json,
                    record_kind, fidelity, message_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    "broken",
                    "missing-type",
                    "2026-09-20T00:00:00+00:00",
                    "user",
                    json.dumps({"role": "user", "content": "坏记录"}),
                    "protocol",
                    "exact",
                ),
            )
            await db.execute(
                """
                INSERT INTO conversation_messages (
                    session_id, message_id, created_at, role, message_json,
                    record_kind, fidelity, message_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    "legacy",
                    "legacy-notice",
                    "2026-09-20T00:00:00+00:00",
                    "assistant",
                    json.dumps({"role": "assistant", "content": "旧工具提示"}),
                    "legacy_tool_notice",
                    "projected",
                ),
            )
            await db.commit()
        finally:
            await manager.close()

        dao = ChatHistoryDao(db_path=db_path)
        try:
            await dao.list_chat_history_async("broken")
        except MessageContractError as exc:
            error = exc
        else:
            raise AssertionError("缺失 message_type 必须显式失败")
        return error, await dao.list_chat_history_async("legacy")

    error, legacy = asyncio.run(scenario())
    assert "数据库与当前消息契约不兼容" in str(error)
    assert [(item["role"], item["content"]) for item in legacy] == [
        ("AI_Tool_Calling", "旧工具提示")
    ]


@pytest.mark.parametrize("route", [list_chat_history, list_chat_history_last_n])
def test_chat_history_routes_map_contract_errors_to_explicit_500(route) -> None:
    class BrokenHistoryService:
        async def get_chat_history_data(self, **_kwargs):
            raise MessageContractError("数据库与当前消息契约不兼容")

        async def get_chat_history_last_n(self, **_kwargs):
            raise MessageContractError("数据库与当前消息契约不兼容")

    async def invoke() -> None:
        if route is list_chat_history:
            await route("session", 0, 200, BrokenHistoryService())
        else:
            await route("session", 100, BrokenHistoryService())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(invoke())
    assert exc_info.value.status_code == 500
    assert "消息契约不兼容" in str(exc_info.value.detail)
