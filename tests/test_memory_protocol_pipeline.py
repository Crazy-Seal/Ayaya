import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.agent.context import HookContext, PluginHook
from app.agent.core.state_manager import StateManager
from app.agent.memory.manager import MemoryManager
from app.agent.memory.memories.diary import DiaryMemory
from app.agent.memory.memories.episodic import EpisodicMemory
from app.agent.memory.memories.semantic_mem0 import MEM0_CUSTOM_INSTRUCTIONS
from app.agent.memory.store.chat_history_store import ChatHistoryStore
from app.agent.memory.store.diary_sqlite_store import DiarySqliteStore
from app.agent.message import messages_from_openai_format
from app.agent.plugins.memory import MemoryPlugin
from app.agent.state import AgentState
import app.agent.plugins.memory as memory_plugin_module


TOOL_MEMORY_CONSTRAINT = (
    "工具调用参数与结果只用于理解对话上下文，不是用户陈述，禁止单独提取为记忆。"
)


def test_protocol_store_freezes_ten_complete_turns_and_five_history_turns(tmp_path: Path) -> None:
    async def scenario():
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("session", db_path=str(db_path))
        try:
            state = AgentState.create_new("session")
            for index in range(15):
                state.add_user_message(f"human-{index}")
                if index == 14:
                    state.messages.append({
                        "role": "assistant", "content": "",
                        "tool_calls": [{
                            "id": "call-14", "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                        }],
                    })
                    state.add_tool_message("file-content", "read_file", "call-14", compressible=True)
                state.add_assistant_message(f"assistant-{index}")
            await manager.save(state, checkpoint_type="completed")
            store = ChatHistoryStore(db_path=db_path)
            store.READ_CHUNK_SIZE = 3
            return await store.get_recent_completed_turns("session")
        finally:
            await manager.close()

    history, current = asyncio.run(scenario())
    assert sum(1 for message in history if message.get("role") == "user") == 5
    assert sum(1 for message in current if message.get("role") == "user") == 10
    assert any(message.get("role") == "tool" for message in current)


def test_completed_turn_query_does_not_accept_a_chunk_head_fragment(tmp_path: Path) -> None:
    async def scenario():
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("session", db_path=str(db_path))
        try:
            state = AgentState.create_new("session")
            state.add_user_message("请读取文件")
            state.messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            })
            state.add_tool_message("file-content", "read_file", "call-1")
            state.add_assistant_message("读取完成")
            await manager.save(state, checkpoint_type="completed")

            store = ChatHistoryStore(db_path=db_path)
            store.READ_CHUNK_SIZE = 2
            return await store.get_recent_completed_turns(
                "session",
                current_turns=1,
                history_turns=0,
            )
        finally:
            await manager.close()

    history, current = asyncio.run(scenario())
    assert history == []
    assert [message["role"] for message in current] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


def test_long_term_memory_format_has_full_current_tools_and_light_history_tools() -> None:
    manager = MemoryManager.__new__(MemoryManager)
    manager.chat_settings = SimpleNamespace(address="用户", name="AI")
    raw = [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
        }]},
        {"role": "tool", "name": "read_file", "tool_call_id": "c", "content": "result"},
    ]
    messages = messages_from_openai_format(raw)

    current = manager._format_messages(messages)
    history = manager._format_messages(messages, mark_as_history=True)

    assert 'AI：调用工具 read_file，参数：{"path":"a.py"}' in current
    assert "工具结果：result" in current
    assert history == "AI：调用了工具 read_file"


def test_diary_format_skips_system_injected_tool_images() -> None:
    formatted = DiaryMemory._format_messages([
        {"role": "user", "content": "请看一下屏幕"},
        {
            "role": "tool",
            "name": "screenshot",
            "tool_call_id": "call-shot",
            "content": "截屏成功",
        },
        {
            "role": "user",
            "name": "system_screenshot",
            "content": [
                {"type": "text", "text": "[系统消息]工具 screenshot 返回图片: "},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ],
        },
        {
            "role": "user",
            "name": "mcp_tool_image",
            "content": [
                {"type": "text", "text": "[系统消息]工具 draw 返回图片: "},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBB"}},
            ],
        },
        {
            "role": "user",
            "name": "system_tool_image",
            "content": [
                {"type": "text", "text": "[系统消息]工具 draw 返回图片: "},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,CCC"}},
            ],
        },
    ])

    assert "用户：请看一下屏幕" in formatted
    assert "工具结果：截屏成功" in formatted
    assert "[系统消息]工具" not in formatted


def test_diary_generation_restores_previous_diaries_and_conversation_context() -> None:
    class ChatStore:
        def __init__(self) -> None:
            self.before_calls: list[dict] = []

        async def get_last_chat_date(self, *_args, **_kwargs):
            return date(2026, 9, 1)

        async def get_messages_by_date(self, *_args, **_kwargs):
            return [{"role": "user", "content": "今天继续昨天的设计"}]

        async def get_messages_before_date(
            self, session_id, before_date, limit=10, *, max_history_id=None
        ):
            self.before_calls.append({
                "session_id": session_id,
                "before_date": before_date,
                "limit": limit,
                "max_history_id": max_history_id,
            })
            return [
                {"role": "user", "content": "昨天先确定两个字段"},
                {"role": "assistant", "content": "已经确定 active_tasks 和 summary"},
            ]

    class DiaryStore:
        def __init__(self) -> None:
            self.added: list[tuple[str, date, str]] = []

        async def exists(self, *_args):
            return False

        async def get_recent_before_date(self, session_id, before_date, n=2):
            assert session_id == "session"
            assert before_date == date(2026, 9, 1)
            assert n == 2
            return [
                (date(2026, 8, 31), "昨天讨论了上下文摘要。"),
                (date(2026, 8, 30), "前天开始审核上下文系统。"),
            ]

        async def add(self, session_id, effective_date, content):
            self.added.append((session_id, effective_date, content))
            return 1

    class LLM:
        def __init__(self) -> None:
            self.calls: list[list[dict]] = []

        async def ainvoke(self, messages):
            self.calls.append(messages)
            return SimpleNamespace(content="生成的日记")

    async def scenario():
        memory = DiaryMemory.__new__(DiaryMemory)
        memory.session_id = "session"
        memory.config = SimpleNamespace(recent_diaries_count=2)
        memory.chat_settings = SimpleNamespace(
            name="Ayaya",
            feature="温柔",
            character="助手",
            address="用户",
            characteristic="忠实记录共同经历。",
        )
        memory.chat_history_store = ChatStore()
        memory.store = DiaryStore()
        memory.llm = LLM()

        await memory.check_and_generate(date(2026, 9, 2), max_history_id=42)
        return memory

    memory = asyncio.run(scenario())

    assert memory.chat_history_store.before_calls == [{
        "session_id": "session",
        "before_date": date(2026, 9, 1),
        "limit": 10,
        "max_history_id": 42,
    }]
    assert len(memory.llm.calls) == 1
    assert [message["role"] for message in memory.llm.calls[0]] == ["user"]
    prompt = memory.llm.calls[0][0]["content"]
    assert prompt.index("【2026-08-30】") < prompt.index("【2026-08-31】")
    assert prompt.index("之前的日记：") < prompt.index("2026-09-01之前最后的对话记录：")
    assert prompt.index("2026-09-01之前最后的对话记录：") < prompt.index("2026-09-01的对话内容（这是需要你记录的部分）：")
    assert "昨天先确定两个字段" in prompt
    assert "今天继续昨天的设计" in prompt
    assert "只是为了让你更好的理解上下文，不要将这些内容重复总结" in prompt
    assert "没有与用户交互" not in prompt
    assert memory.store.added == [("session", date(2026, 9, 1), "生成的日记")]


def test_diary_prompt_marks_missing_reference_context_as_unavailable() -> None:
    memory = DiaryMemory.__new__(DiaryMemory)
    memory.chat_settings = SimpleNamespace(
        name="Ayaya",
        feature="温柔",
        character="助手",
        address="用户",
        characteristic="忠实记录共同经历。",
    )

    prompt = memory._build_diary_prompt(
        [{"role": "user", "content": "今天第一次聊天"}],
        date(2026, 9, 1),
        previous_diaries=[],
        previous_messages=[],
    )

    assert "暂无以前的日记" in prompt
    assert "此前暂无对话记录" in prompt
    assert "今天第一次聊天" in prompt
    assert "None" not in prompt


def test_long_term_memory_is_scheduled_after_completed_response(monkeypatch) -> None:
    scheduled: list[str] = []

    def capture(coro, *, logger, task_name):
        scheduled.append(task_name)
        coro.close()

    monkeypatch.setattr(memory_plugin_module, "create_background_task", capture)

    async def scenario():
        plugin = MemoryPlugin(enable_diary=False)
        state = AgentState.create_new("session")
        state.long_term_memory_turn_counter = 10
        commit_context = {}
        await plugin.execute(HookContext.create(
            PluginHook.BEFORE_RESPONSE_COMMIT, state, data=commit_context
        ))
        assert scheduled == []
        await plugin.execute(HookContext.create(
            PluginHook.AFTER_RESPONSE_COMMIT, state, data=commit_context
        ))
        return state

    state = asyncio.run(scenario())
    assert state.long_term_memory_turn_counter == 0
    assert scheduled == ["memory.persist_long_term"]


def test_memory_plugin_does_not_own_completed_turn_counting() -> None:
    async def scenario():
        plugin = MemoryPlugin(
            enable_diary=False,
            enable_episodic=False,
            enable_semantic=False,
        )
        state = AgentState.create_new("session")
        state.add_user_message("尚未完成的真人输入")
        before = state.long_term_memory_turn_counter
        await plugin.execute(HookContext.create(
            PluginHook.BEFORE_RESPONSE_COMMIT, state, data={}
        ))
        return before, state.long_term_memory_turn_counter

    assert asyncio.run(scenario()) == (0, 0)


def test_diaries_have_a_separate_effective_date_unique_table(tmp_path: Path) -> None:
    store = DiarySqliteStore(str(tmp_path / "memory.sqlite3"))
    assert store.TABLE_NAME == "diaries"


def test_memory_prompts_only_add_the_tool_evidence_constraint() -> None:
    assert TOOL_MEMORY_CONSTRAINT in MEM0_CUSTOM_INSTRUCTIONS
    memory = EpisodicMemory.__new__(EpisodicMemory)
    memory.chat_settings = SimpleNamespace(
        name="AI", feature="温柔", character="助手", address="用户", characteristic="忠实"
    )
    assert TOOL_MEMORY_CONSTRAINT in memory._build_extraction_prompt("当前", "前情")
