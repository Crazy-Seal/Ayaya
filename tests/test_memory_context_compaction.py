"""验证日记存在与否均保留旧摘要，并将任务和聊天梗概交给下一次压缩。"""

import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.agent.agent import Agent, AgentConfig
from app.agent.context._compaction import CONTEXT_SUMMARY_PREFIX
from app.agent.memory.config import MemoryConfig
from app.agent.memory.manager import MemoryManager
from app.agent.memory.store.chat_history_store import ChatHistoryStore
from app.agent.models.contracts import InputMessage, StreamChunk, ToolDefinition
from app.agent.plugins.memory.memory import MemoryPlugin
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import ContextSummaryMessage, HumanMessage, text_content
from app.agent.state.model import AgentState
from app.schemas.chat_settings import AgentPluginSettings, ChatSettings
from tests.model_capture import capture_input


@pytest.mark.parametrize("has_diary", [False, True])
@pytest.mark.parametrize("compact", [False, True])
def test_diary_preserves_summary_and_tasks_during_model_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_diary: bool, compact: bool,
) -> None:
    """通过真实执行和存储验证普通请求及压缩请求均保留任务和聊天梗概。

    Args:
        tmp_path (Path): 本例独占的检查点、协议历史和日记目录。
        monkeypatch (pytest.MonkeyPatch): 将插件连接到测试记忆管理器，并隔离日记生成。
        has_diary (bool): 摘要覆盖的聊天日是否已有日记。
        compact (bool): 是否用较长的已完成对话触发上下文压缩。
    """
    async def scenario() -> None:
        """交付新输入，检查摘要模型输入、主模型输入和最终检查点。"""
        path = tmp_path / "state.sqlite3"
        manager = SessionStateManager("session", str(path))
        settings = ChatSettings(
            session_id="session", model_name="test", openai_api_key="test",
            openai_base_url="http://unused", temperature=0, system_prompt="测试",
            context_strategy={"compression_trigger_tokens_k": 10},
            agent_plugins={"memory": AgentPluginSettings(config={
                "enable_diary": True, "enable_episodic": False, "enable_semantic": False,
            })},
        )
        memory = MemoryManager("session", MemoryConfig(
            sqlite_path=str(tmp_path / "diary.sqlite3"), timezone=ZoneInfo("Asia/Shanghai"),
        ), settings)
        memory.chat_history_store = ChatHistoryStore(path)
        diary_date = date(2026, 8, 29)
        if has_diary:
            await memory.diary_memory.store.add("session", diary_date, "当天讨论了上下文整理。")

        async def no_generation(*, max_history_id: int | None = None) -> None:
            """本例验证已有日记与摘要，后续日期的日记生成由其他测试覆盖。

            Args:
                max_history_id (int | None): 本轮输入前已保存的历史范围。
            """

        monkeypatch.setattr(memory, "prepare_diary", no_generation)
        monkeypatch.setattr("app.agent.plugins.memory.memory.get_memory_manager", lambda _: memory)
        payload = {
            "active_tasks": [{
                "topic": "整理上下文", "current_state": "已确认旧方案",
                "constraints": ["保留未完成工作"], "decisions": ["任务与聊天分开记录"],
                "pending": ["继续实现"], "references": ["app/agent/context/coordinator.py"],
            }],
            "summary": "之前讨论了上下文整理，随后聊了当天的生活。",
        }
        old_summary = ContextSummaryMessage(
            content=CONTEXT_SUMMARY_PREFIX + "\n" + json.dumps(payload, ensure_ascii=False),
            summary_schema_version=1, covered_through_message_id="source", compression_ratio=0.5,
        )
        summarized: list[dict] = []
        requested: list[dict] = []

        class Model:
            """观察真实执行送来的模型输入，以结构化结果代替外部模型。"""

            async def ainvoke_structured(
                self, messages: list[InputMessage], schema: dict, **kwargs: object,
            ) -> dict:
                """确认旧任务和聊天正文完整进入压缩请求，再返回合并后的摘要。

                Args:
                    messages (list[InputMessage]): 待压缩的前段历史及摘要指令。
                    schema (dict): 结构化摘要的输出约束。
                    **kwargs (object): 摘要生成参数。

                Returns:
                    dict: 同时保留任务与聊天梗概的合并结果。
                """
                assert old_summary.content in [item["content"] for item in capture_input(messages)]
                summarized.append(payload)
                return payload

            async def astream(
                self, messages: list[InputMessage], *, tools: list[ToolDefinition] | None = None,
            ):
                """观察主模型接收的唯一摘要，并确认对应检查点已经保存。

                Args:
                    messages (list[InputMessage]): 准备完成的主模型输入。
                    tools (list[ToolDefinition] | None): 本轮可用工具。

                Yields:
                    StreamChunk: 本轮最终回答。
                """
                summaries = [item["content"] for item in capture_input(messages)
                             if isinstance(item.get("content"), str)
                             and item["content"].startswith(CONTEXT_SUMMARY_PREFIX + "\n")]
                assert len(summaries) == 1
                requested.append(json.loads(summaries[0].split("\n", 1)[1]))
                saved = await manager.load()
                assert sum(isinstance(item, ContextSummaryMessage) for item in saved.messages) == 1
                yield StreamChunk(content="继续完成")

            async def close(self) -> None:
                """测试模型的响应保存在内存中。"""

        agent = Agent(
            AgentConfig(session_id="session", model_name="test", api_key="test",
                        context_strategy=settings.context_strategy),
            llm_client=Model(), session_state_manager=manager, chat_settings=settings,
        )
        agent.register_plugin(MemoryPlugin(enable_diary=True, enable_episodic=False, enable_semantic=False))
        try:
            # 1. 保存真实原消息，再以一条摘要替换，使已有日记能对应到被压缩的历史。
            state = AgentState.create_new("session")
            state.append_input(HumanMessage(
                content=text_content("旧对话"), message_id="source",
                created_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
            ))
            await manager.save(state, checkpoint_type="completed")
            state.commit_compaction((old_summary,))
            if compact:
                state.append_input(HumanMessage(content=text_content("继续之前的工作")))
                state.add_assistant_message("此前完成的工作记录。" * 8_000)
            await manager.save(state, checkpoint_type="completed")

            # 2. 新输入触发记忆召回和模型准备，旧摘要在两条执行路径中都保留。
            async for _ in agent.run("继续"):
                pass
            assert requested == [payload]
            assert summarized == ([payload] if compact else [])
            saved = await manager.load()
            summaries = [item for item in saved.messages if isinstance(item, ContextSummaryMessage)]
            assert len(summaries) == 1
            assert json.loads(summaries[0].content.split("\n", 1)[1]) == payload
            assert await memory.diary_memory.has_diary(diary_date) == has_diary
        finally:
            await agent.close()
            await manager.close()
            await memory.diary_memory.llm.close()

    asyncio.run(scenario())
