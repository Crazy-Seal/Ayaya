from app.agent.state.manager import RequiredSaveError
from app.crud.sqlite_factory import SqliteFactory
from tests.model_capture import result_parts
import asyncio
import json
from pathlib import Path

import aiosqlite
import pytest
from fastapi import HTTPException

from app.agent.context._compaction import ContextCompressionPolicy
from app.agent.state.manager import SessionStateManager
from app.agent.state import AgentState
from app.agent.state.messages import (
    MessageContractError,
    ToolCall, ToolMessage, ImageContent, TextContent, text_content,
)
from app.agent.models.openai_message_names import (
    SCREENSHOT_COMPRESSED_NAME,
    SCREENSHOT_MESSAGE_NAME,
)
from app.crud.chat_history_dao import ChatHistoryDao
from app.routes.chat_history import list_chat_history, list_chat_history_last_n


def test_checkpoint_and_protocol_log_are_saved_atomically(tmp_path: Path) -> None:
    """验证checkpoint 和协议日志在同一事务保存。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“checkpoint 和协议日志在同一事务保存”的异步步骤。"""
        db_path = tmp_path / "agent.sqlite3"
        manager = SessionStateManager("session", sqlite_factory=SqliteFactory(str(db_path)))
        try:
            state = AgentState.create_new("session")
            state.add_user_message("你好")
            state.record_assistant_step("我来读取", (
                ToolCall(id="call-1", name="read_file", args={"path": "a.py"}),
            ))
            state.add_tool_message("结果", "read_file", "call-1", compressible=True)
            state.add_assistant_message("完成")
            await manager.save(state, checkpoint_type="completed")

            db = await manager._state_dao._get_db()
            rows = await db.execute_fetchall(
                "SELECT message_type, message_json FROM conversation_messages ORDER BY id"
            )
            assert [row[0] for row in rows] == ["human", "assistant", "tool", "assistant"]
            assistant = json.loads(rows[1][1])
            assert assistant["content"] == [{"type":"text","text":"我来读取"}]
            assert assistant["tool_calls"][0]["id"] == "call-1"

            await db.execute("""
                CREATE TRIGGER reject_protocol_insert BEFORE INSERT ON conversation_messages
                BEGIN SELECT RAISE(ABORT, 'reject protocol'); END
            """)
            await db.commit()
            failed = AgentState.from_checkpoint(state.to_checkpoint())
            failed.add_user_message("不应提交")
            with pytest.raises(RequiredSaveError, match="reject protocol"):
                await manager.save(failed, checkpoint_type="intermediate")
            count = (await db.execute_fetchall("SELECT COUNT(*) FROM checkpoints"))[0][0]
            assert count == 1
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_history_dao_projects_visible_rows_before_pagination(tmp_path: Path) -> None:
    """验证历史先筛选可见行再分页。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> list[dict]:
        """由外层测试调用，完成“历史先筛选可见行再分页”的异步步骤。

        Returns:
            list[dict]: 供外层用例断言使用的预设结果。
        """
        db_path = tmp_path / "agent.sqlite3"
        manager = SessionStateManager("session", sqlite_factory=SqliteFactory(str(db_path)))
        try:
            state = AgentState.create_new("session")
            state.add_user_message("问题")
            state.record_assistant_step("", (
                ToolCall(id="call-1", name="read_file", args={}),
            ))
            state.add_tool_message("隐藏结果", "read_file", "call-1", compressible=True)
            state.add_assistant_message("回答")
            await manager.save(state, checkpoint_type="completed")
            dao = ChatHistoryDao(sqlite_factory=SqliteFactory(db_path))

            async def reject_full_history_load(_session_id: str) -> list[dict]:
                """全量历史读取被调用时主动失败，验证分页流程使用有限查询。

                该定义仅用于外层用例：验证历史先筛选可见行再分页。

                Args:
                    _session_id (str): 当前操作所属的会话编号。
                """
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
    """验证小块查询仍保持可见历史顺序。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> tuple[list[dict], list[dict]]:
        """由外层测试调用，完成“小块查询仍保持可见历史顺序”的异步步骤。

        Returns:
            tuple[list[dict], list[dict]]: 供外层用例断言使用的预设结果。
        """
        db_path = tmp_path / "agent.sqlite3"
        manager = SessionStateManager("session", sqlite_factory=SqliteFactory(str(db_path)))
        try:
            state = AgentState.create_new("session")
            state.add_user_message("question")
            state.record_assistant_step("working", (
                ToolCall(id="call-1", name="read_file", args={}),
            ))
            state.add_tool_message("hidden", "read_file", "call-1", compressible=True)
            state.add_assistant_message("answer")
            await manager.save(state, checkpoint_type="completed")

            dao = ChatHistoryDao(sqlite_factory=SqliteFactory(db_path))
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


def test_tool_image_is_archived_inside_its_tool_result(tmp_path: Path) -> None:
    """历史只保存完整工具结果，图片带文件引用并继承调用身份。"""
    async def scenario() -> None:
        """通过保存及历史接口验证图片归属，不产生独立媒体行。"""
        manager=SessionStateManager("s",SqliteFactory(str(tmp_path/"state.sqlite")))
        state=AgentState.create_new("s")
        call=ToolCall("c","read_image",{})
        state.record_assistant_step("",(call,))
        result=state.record_tool_result(call,(TextContent("完成"),ImageContent("data:image/png;base64,AA==",media_kind="tool")))
        try:
            await manager.save(state,checkpoint_type="completed")
            restored=await manager.load()
            assert restored.messages[-1].message_id==result.message_id
            assert restored.messages[-1].content[-1].storage_ref.startswith("tool/")
            history=await ChatHistoryDao(sqlite_factory=SqliteFactory(tmp_path/"state.sqlite")).list_chat_history_async("s")
            assert history[-1]["id"]==f"{result.message_id}:images"
        finally:
            await manager.close()
    asyncio.run(scenario())



def test_screenshot_compaction_preserves_original_history(tmp_path: Path) -> None:
    """checkpoint 图片被压缩后，历史仍保留原文件引用和图片气泡。"""
    async def scenario() -> None:
        """在隔离 SQLite 中比较压缩前后可见历史。"""
        manager=SessionStateManager("s",SqliteFactory(str(tmp_path/"state.sqlite")))
        state=AgentState.create_new("s")
        call=ToolCall("c","screenshot",{})
        state.record_assistant_step("",(call,))
        state.record_tool_result(call,(TextContent("完成"),ImageContent("data:image/png;base64,AA==",media_kind="screenshot")),compressible=True)
        try:
            await manager.save(state,checkpoint_type="completed")
            dao=ChatHistoryDao(sqlite_factory=SqliteFactory(tmp_path/"state.sqlite"))
            before=await dao.list_chat_history_async("s")
            candidate,changed=ContextCompressionPolicy.compress_screenshots(state.messages,ttl_human_messages=0,max_screenshots=2)
            assert changed==1
            state.commit_compaction(candidate)
            await manager.save(state,checkpoint_type="intermediate")
            assert await dao.list_chat_history_async("s")==before
            assert (await manager.load()).messages[-1].content[-1].context_state=="compacted"
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_history_dao_raises_for_missing_message_type_but_keeps_legacy_notice(
    tmp_path: Path,
) -> None:
    """验证消息类型缺失明确报错但保留旧工具提示兼容。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> tuple[MessageContractError, list[dict]]:
        """由外层测试调用，完成“消息类型缺失明确报错但保留旧工具提示兼容”的异步步骤。

        Returns:
            tuple[MessageContractError, list[dict]]: 供外层用例断言使用的预设结果。
        """
        db_path = tmp_path / "agent.sqlite3"
        manager = SessionStateManager("session", sqlite_factory=SqliteFactory(str(db_path)))
        try:
            db = await manager._state_dao._get_db()
            await db.execute(
                """
                INSERT INTO conversation_messages (
                    session_id, message_id, created_at, message_json,
                    record_kind, fidelity, message_type
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    "broken",
                    "missing-type",
                    "2026-09-20T00:00:00+00:00",
                    json.dumps({"role": "user", "content": "坏记录"}),
                    "protocol",
                    "exact",
                ),
            )
            await db.execute(
                """
                INSERT INTO conversation_messages (
                    session_id, message_id, created_at, message_json,
                    record_kind, fidelity, message_type
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    "legacy",
                    "legacy-notice",
                    "2026-09-20T00:00:00+00:00",
                    json.dumps({"role": "assistant", "content": "旧工具提示"}),
                    "legacy_tool_notice",
                    "projected",
                ),
            )
            await db.commit()
        finally:
            await manager.close()

        dao = ChatHistoryDao(sqlite_factory=SqliteFactory(db_path))
        try:
            await dao.list_chat_history_async("broken")
        except MessageContractError as exc:
            error = exc
        else:
            raise AssertionError("缺失 message_type 必须显式失败")
        return error, await dao.list_chat_history_async("legacy")

    error, legacy = asyncio.run(scenario())
    assert "需要先迁移旧格式" in str(error)
    assert [(item["role"], item["content"]) for item in legacy] == [
        ("AI_Tool_Calling", "旧工具提示")
    ]


@pytest.mark.parametrize("route", [list_chat_history, list_chat_history_last_n])
def test_chat_history_routes_map_contract_errors_to_explicit_500(route) -> None:
    """验证历史路由把协议契约错误转为明确 HTTP 500。

    Args:
        route (Any): 本次调用的历史或权限路由函数。
    """
    class BrokenHistoryService:
        """提供当前用例所需的 BrokenHistoryService 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证历史路由把协议契约错误转为明确 HTTP 500。
        """
        async def get_chat_history_data(self, **_kwargs):
            """返回用例指定的历史结果或异常，替代真实历史读取。

            该定义仅用于外层用例：验证历史路由把协议契约错误转为明确 HTTP 500。

            Args:
                _kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            raise MessageContractError("需要先迁移旧格式")

        async def get_chat_history_last_n(self, **_kwargs):
            """返回用例指定的最近历史或异常，替代真实历史读取。

            该定义仅用于外层用例：验证历史路由把协议契约错误转为明确 HTTP 500。

            Args:
                _kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            raise MessageContractError("需要先迁移旧格式")

    async def invoke() -> None:
        """调用当前测试路由，供外层检查状态码或异常转换。

        该定义仅用于外层用例：验证历史路由把协议契约错误转为明确 HTTP 500。
        """
        if route is list_chat_history:
            await route("session", 0, 200, BrokenHistoryService())
        else:
            await route("session", 100, through_message_id=None, chat_history_service=BrokenHistoryService())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(invoke())
    assert exc_info.value.status_code == 500
    assert "消息契约不兼容" in str(exc_info.value.detail)
