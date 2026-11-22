"""验证插件新通知的激活意图只在保存成功后改变会话资格。"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.models.llm_client import StreamChunk
from app.agent.plugins.runtime import PluginRuntime
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.state.manager import SessionStateManager
from app.crud.sqlite_factory import SqliteFactory
from app.crud.task_record_dao import TaskRecordDao
from app.interaction.interaction_contract import InteractionCommand
from app.schemas.runtime_types import CommandKind
from tests.runtime_support import build_test_scheduler, close_test_runtime, configured_settings, next_runtime_event


@pytest.mark.parametrize("steer", [False, True])
def test_new_plugin_input_activates_only_when_requested(tmp_path: Path, steer: bool) -> None:
    """默认通知在暂停时留存，新激活通知可以启动首轮及用户停止后的执行。

    Args:
        tmp_path (Path): 隔离配置、消息和任务数据库目录。
        steer (bool): 通过普通通知或引导入口提交激活请求。
    """
    async def scenario() -> None:
        """运行真实输入交付和模型投影，只替代外部模型回答。"""
        requests: list[object] = []

        class Model:
            """立即回答以便核对首次与再次激活的实际轮次数。"""

            async def astream(self, messages, tools=None):
                """记录实际投影后的模型输入并生成回复。

                Args:
                    messages (Sequence[InputMessage]): 本轮已交付的实际输入。
                    tools (Sequence[ToolDefinition] | None): 本轮可调用工具。

                Yields:
                    StreamChunk: 固定的最终回答。
                """
                requests.append(messages)
                yield StreamChunk(content="主动回复")

            async def close(self) -> None:
                """模型替身没有外部连接。"""

        queue = asyncio.Queue()
        factory = SqliteFactory(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(factory))
        scheduler = build_test_scheduler(runtime_queue=queue, task_supervisor=supervisor,
            agent_factory=lambda session, manager: Agent(
                AgentConfig(session_id=session, model_name="test", api_key="test",
                    context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)),
                llm_client=Model(), session_state_manager=manager, sqlite_factory=factory),
            session_state_manager_factory=lambda session: SessionStateManager(session, factory),
            chat_settings_dao=configured_settings("s"))
        runtime = PluginRuntime("timer", "s", _supervisor=supervisor, _runtime_queue=queue)
        manager = SessionStateManager("s", factory)
        try:
            await scheduler.start()
            await runtime.notify(name="quiet", text="普通通知")
            assert (await scheduler.snapshot("s")).payload["session_state"] == "paused"
            pending = await manager.read_activation_batch()
            assert [item.payload["text"] for item in pending] == ["普通通知"]
            assert requests == []
            # 1. 激活内容写入失败时保持暂停，已有普通通知仍在等待交付。
            with sqlite3.connect(factory.main_path) as db:
                db.execute("CREATE TRIGGER reject_plugin BEFORE INSERT ON session_inbox "
                    "WHEN NEW.kind = 'plugin_notification' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
            if steer:
                await runtime.steer_turn("保存失败", activate_session=True)
            else:
                await runtime.notify(name="failed", text="保存失败", activate_session=True)
            assert (await scheduler.snapshot("s")).payload["session_state"] == "paused"
            assert requests == []
            assert [item.payload["text"] for item in await manager.read_activation_batch()] == ["普通通知"]
            with sqlite3.connect(factory.main_path) as db:
                db.execute("DROP TRIGGER reject_plugin")
            # 2. 新请求保存成功后恢复执行，用户停止后仍能由下一条显式通知激活。
            for expected in (1, 2):
                if steer:
                    await runtime.steer_turn("主动引导", activate_session=True)
                else:
                    await runtime.notify(name="hello", text="主动聊天", activate_session=True)
                async with asyncio.timeout(3):
                    while (await next_runtime_event(scheduler)).kind != "run_completed":
                        pass
                assert len(requests) == expected
                assert (await scheduler.snapshot("s")).payload["session_state"] == "enabled"
                assert await manager.read_activation_batch() == ()
                receipt = await scheduler.submit(InteractionCommand("s", CommandKind.STOP_TURN))
                assert receipt.accepted
                assert (await scheduler.snapshot("s")).payload["session_state"] == "paused"
            state = await manager.load()
            assert all("activate_session" not in message.to_storage_format() for message in state.messages)
        finally:
            await manager.close()
            await close_test_runtime(scheduler)
    asyncio.run(scenario())
