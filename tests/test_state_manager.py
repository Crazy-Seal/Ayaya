import asyncio
from pathlib import Path

from app.agent.core.state_manager import StateManager
from app.agent.state import AgentState


def test_rollback_restores_previous_checkpoint(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = StateManager("test-session", db_path=str(tmp_path / "checkpoints.sqlite3"))
        try:
            first = AgentState.create_new("test-session")
            first.add_user_message("first")
            await manager.save(first)
            await manager.load()  # 为下一轮建立良好状态的水位线

            second = AgentState.from_checkpoint(first.to_checkpoint())
            second.add_assistant_message("second")
            await manager.save(second)

            assert await manager.rollback() is True
            restored = await manager.load()
            assert len(restored.messages) == 1
            assert restored.messages[0]["content"] == "first"
            assert await manager.rollback() is False
        finally:
            await manager.close()

    asyncio.run(scenario())
