"""验证工具图片立即参与 checkpoint 与历史原子保存。"""

import asyncio
from pathlib import Path

from app.agent.state import AgentState
from app.agent.state.manager import StateManager
from app.agent.state.messages import TextContent, ImageContent, ToolCall, ToolMessage
from app.crud.chat_history_dao import ChatHistoryDao


def test_first_tool_image_is_durable_while_next_tool_remains_pending(tmp_path: Path) -> None:
    """第一项工具完成后可恢复其图片，后续未执行调用仍保持待处理。"""
    async def scenario() -> None:
        """使用隔离 SQLite 保存并重新载入未完成批次。"""
        manager = StateManager("session", str(tmp_path / "state.sqlite"))
        state = AgentState.create_new("session")
        state.add_user_message("读取")
        a, b = ToolCall("a", "read_image", {}), ToolCall("b", "read_file", {})
        state.record_assistant_step("", (a, b))
        state.record_tool_result(a, (TextContent("图片"), ImageContent("data:image/png;base64,AA==", media_kind="tool")))
        try:
            await manager.save(state, checkpoint_type="intermediate")
            restored = await manager.load()
            assert restored.pending_tool_calls == (b,)
            result = restored.messages[-1]
            assert isinstance(result, ToolMessage) and isinstance(result.content[1], ImageContent)
            assert "_deferred_tool_media" not in restored.to_checkpoint()
            history = await ChatHistoryDao(db_path=tmp_path / "state.sqlite").list_chat_history_async("session")
            assert history[-1]["images"] and history[-1]["id"] == f"{result.message_id}:images"
        finally:
            await manager.close()
    asyncio.run(scenario())
