from tests.model_capture import result_parts
from tests.model_capture import capture_input
from app.agent.state.messages import text_content
import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.agent.plugins.base import HookContext, PluginHook
from app.agent.state.manager import StateManager
from app.agent.memory.memories.diary import DiaryMemory
from app.agent.memory.memories.episodic import EpisodicMemory
from app.agent.memory.memories.semantic_mem0 import MEM0_CUSTOM_INSTRUCTIONS
from app.agent.memory.store.chat_history_store import ChatHistoryStore
from app.agent.memory.store.diary_sqlite_store import DiarySqliteStore
from app.agent.state.messages import (
    AssistantMessage,
    HumanMessage,
    ToolCall,
    ImageContent,
    ToolMessage,
)
from app.agent.plugins.memory import MemoryPlugin
from app.agent.state import AgentState
import app.agent.plugins.memory as memory_plugin_module


TOOL_MEMORY_CONSTRAINT = (
    "工具调用参数与结果只用于理解对话上下文，不是用户陈述，禁止单独提取为记忆。"
)


def test_protocol_store_freezes_ten_complete_turns_and_five_history_turns(tmp_path: Path) -> None:
    """验证协议历史按边界选十轮当前批次及五轮前情。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario():
        """由外层测试调用，完成“协议历史按边界选十轮当前批次及五轮前情”的异步步骤。"""
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("session", db_path=str(db_path))
        try:
            state = AgentState.create_new("session")
            for index in range(15):
                state.add_user_message(f"human-{index}")
                if index == 14:
                    call = ToolCall(id="call-14", name="read_file", args={"path": "a.py"})
                    state.record_assistant_step("", (call,))
                    state.record_tool_result(call, result_parts('file-content', (), 'tool'), compressible=True)
                state.add_assistant_message(f"assistant-{index}")
            await manager.save(state, checkpoint_type="completed")
            store = ChatHistoryStore(db_path=db_path)
            store.READ_CHUNK_SIZE = 3
            return await store.get_recent_completed_turns("session")
        finally:
            await manager.close()

    history, current = asyncio.run(scenario())
    assert sum(1 for message in history if isinstance(message, HumanMessage)) == 5
    assert sum(1 for message in current if isinstance(message, HumanMessage)) == 10
    assert any(isinstance(message, ToolMessage) for message in current)

def test_completed_turn_query_does_not_accept_a_chunk_head_fragment(tmp_path: Path) -> None:
    """验证分块开头不完整轮次不会当作完整历史。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario():
        """由外层测试调用，完成“分块开头不完整轮次不会当作完整历史”的异步步骤。"""
        db_path = tmp_path / "agent.sqlite3"
        manager = StateManager("session", db_path=str(db_path))
        try:
            state = AgentState.create_new("session")
            state.add_user_message("请读取文件")
            call = ToolCall(id="call-1", name="read_file", args={})
            state.record_assistant_step("", (call,))
            state.record_tool_result(call, text_content("file-content"))
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
    assert [type(message) for message in current] == [
        HumanMessage,
        AssistantMessage,
        ToolMessage,
        AssistantMessage,
    ]


def test_long_term_memory_format_has_full_current_tools_and_light_history_tools() -> None:
    """验证情景提取器对当前和历史工具上下文沿用原有格式。"""
    memory = EpisodicMemory.__new__(EpisodicMemory)
    memory.chat_settings = SimpleNamespace(address="用户", name="AI")
    messages = [
        AssistantMessage(
            content=text_content(""),
            tool_calls=(ToolCall(id="c", name="read_file", args={"path": "a.py"}),),
        ),
        ToolMessage(content=text_content("result"), tool_name="read_file", tool_call_id="c"),
    ]

    current = memory._format_messages(messages)
    history = memory._format_messages(messages, mark_as_history=True)

    assert 'AI：调用工具 read_file，参数：{"path":"a.py"}' in current
    assert "工具结果：result" in current
    assert history == "AI：调用了工具 read_file"

def test_diary_format_skips_system_injected_tool_images() -> None:
    """验证日记格式化跳过内部工具图片。"""
    formatted = DiaryMemory._format_messages([
        HumanMessage(content=text_content("请看一下屏幕")),
        ToolMessage(content=text_content("截屏成功"), tool_name="screenshot", tool_call_id="call-shot"),
        ToolMessage(tool_name="screenshot", tool_call_id="image-screenshot", content=(ImageContent("data:image/png;base64,QUFB", media_kind="screenshot"),)),
        ToolMessage(tool_name="draw", tool_call_id="image-mcp", content=(ImageContent("data:image/png;base64,QkJC", media_kind="mcp"),)),
        ToolMessage(tool_name="draw", tool_call_id="image-tool", content=(ImageContent("data:image/png;base64,Q0ND", media_kind="tool"),)),
    ])
    assert "用户：请看一下屏幕" in formatted
    assert "工具结果：截屏成功" in formatted
    assert "[系统消息]工具" not in formatted

def test_diary_generation_restores_previous_diaries_and_conversation_context() -> None:
    """验证生成日记时恢复旧日记和更早对话前情。"""
    class ChatStore:
        """提供当前用例所需的 ChatStore 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证生成日记时恢复旧日记和更早对话前情。
        """
        def __init__(self) -> None:
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证生成日记时恢复旧日记和更早对话前情。
            """
            self.before_calls: list[dict] = []
        async def get_last_chat_date(self, *_args, **_kwargs):
            """提供用例指定的最近聊天日，并记录排除日期和读取边界。

            该定义仅用于外层用例：验证生成日记时恢复旧日记和更早对话前情。

            Args:
                _args (dict[str, Any]): 当前工具调用或被包装函数的参数。
                _kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            return date(2026, 9, 1)
        async def get_messages_by_date(self, *_args, **_kwargs):
            """提供目标日期的测试对话，并记录历史读取边界。

            该定义仅用于外层用例：验证生成日记时恢复旧日记和更早对话前情。

            Args:
                _args (dict[str, Any]): 当前工具调用或被包装函数的参数。
                _kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            return [HumanMessage(content=text_content("今天继续昨天的设计"))]
        async def get_messages_before_date(
            self, session_id, before_date, limit=10, *, max_history_id=None
        ):
            """提供仅作为前情使用的较早对话。

            该定义仅用于外层用例：验证生成日记时恢复旧日记和更早对话前情。

            Args:
                session_id (Any): 当前操作所属的会话编号。
                before_date (Any): 只读取该有效日期之前的记录。
                limit (Any): 最多返回的记录数量。
                max_history_id (Any): 本次提取开始前捕获的协议日志最大编号；排除之后到达的消息。
            """
            self.before_calls.append({
                "session_id": session_id,
                "before_date": before_date,
                "limit": limit,
                "max_history_id": max_history_id,
            })
            return [
                HumanMessage(content=text_content("昨天先确定两个字段")),
                AssistantMessage(content=text_content("已经确定 active_tasks 和 summary")),
            ]
    class DiaryStore:
        """提供当前用例所需的 DiaryStore 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证生成日记时恢复旧日记和更早对话前情。
        """
        def __init__(self) -> None:
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证生成日记时恢复旧日记和更早对话前情。
            """
            self.added: list[tuple[str, date, str]] = []
        async def exists(self, *_args):
            """返回用例指定的日记是否存在。

            该定义仅用于外层用例：验证生成日记时恢复旧日记和更早对话前情。

            Args:
                _args (dict[str, Any]): 当前工具调用或被包装函数的参数。
            """
            return False
        async def get_recent_before_date(self, session_id, before_date, n=2):
            """提供目标日期之前的测试日记。

            该定义仅用于外层用例：验证生成日记时恢复旧日记和更早对话前情。

            Args:
                session_id (Any): 当前操作所属的会话编号。
                before_date (Any): 只读取该有效日期之前的记录。
                n (Any): 最多返回的日记条数。
            """
            assert session_id == "session"
            assert before_date == date(2026, 9, 1)
            assert n == 2
            return [
                (date(2026, 8, 31), "昨天讨论了上下文摘要。"),
                (date(2026, 8, 30), "前天开始审核上下文系统。"),
            ]
        async def add(self, session_id, effective_date, content):
            """记录本次日记或长期记忆保存内容，供外层检查。

            该定义仅用于外层用例：验证生成日记时恢复旧日记和更早对话前情。

            Args:
                session_id (Any): 当前操作所属的会话编号。
                effective_date (Any): 按日记规则划分的有效日期，格式为 YYYY-MM-DD。
                content (Any): 当前处理的正文。
            """
            self.added.append((session_id, effective_date, content))
            return 1
    class LLM:
        """提供当前用例所需的 LLM 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证生成日记时恢复旧日记和更早对话前情。
        """
        def __init__(self) -> None:
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证生成日记时恢复旧日记和更早对话前情。
            """
            self.calls: list[list[dict]] = []
        async def ainvoke(self, messages):
            """记录日记模型收到的提示词并返回脚本正文。

            该定义仅用于外层用例：验证生成日记时恢复旧日记和更早对话前情。

            Args:
                messages (list[dict]): 按对话顺序排列的消息集合。
            """
            messages = capture_input(messages)
            self.calls.append(messages)
            return SimpleNamespace(text="生成的日记")
    async def scenario():
        """由外层测试调用，完成“生成日记时恢复旧日记和更早对话前情”的异步步骤。"""
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
    """验证缺失前情在日记提示词中明确说明。"""
    memory = DiaryMemory.__new__(DiaryMemory)
    memory.chat_settings = SimpleNamespace(
        name="Ayaya",
        feature="温柔",
        character="助手",
        address="用户",
        characteristic="忠实记录共同经历。",
    )
    prompt = memory._build_diary_prompt(
        [HumanMessage(content=text_content("今天第一次聊天"))],
        date(2026, 9, 1),
        previous_diaries=[],
        previous_messages=[],
    )
    assert "暂无以前的日记" in prompt
    assert "此前暂无对话记录" in prompt
    assert "今天第一次聊天" in prompt
    assert "None" not in prompt

def test_long_term_memory_is_scheduled_after_next_activation_input_commit(monkeypatch) -> None:
    """到期长期记忆只在下一激活输入提交成功后启动，响应提交不启动。

    Args:
        monkeypatch (pytest.MonkeyPatch): 替换后台调度和记忆准备依赖的工具。
    """
    scheduled: list[str] = []
    class MemoryManager:
        """提供激活前记忆准备所需的固定协议截止位置。"""
        async def get_protocol_history_cutoff(self):
            """返回用于检验前后钩子传递的历史截止位置。

            Returns:
                int: 测试中固定的截止消息编号。
            """
            return 42
    def capture(coro, *, logger, task_name):
        """记录后台任务准备成功，但不执行外部提取。

        Args:
            coro (Coroutine): 插件准备的真实后台协程。
            logger (logging.Logger): 插件日志对象。
            task_name (str): 派生任务名称。
        """
        scheduled.append(task_name)
        coro.close()

    monkeypatch.setattr(memory_plugin_module, "create_background_task", capture)

    async def scenario():
        """依次触发响应和激活提交事件，比较到期计数的消费时点。

        Returns:
            tuple[AgentState, list[str]]: 最终状态和已启动任务的名称。
        """
        plugin = MemoryPlugin(enable_diary=False)
        monkeypatch.setattr(plugin, "_mm", lambda state: MemoryManager())
        state = AgentState.create_new("session")
        state.long_term_memory_turn_counter = 10
        response_context = {}
        await plugin.execute(HookContext.create(
            PluginHook.BEFORE_RESPONSE_COMMIT, state, data=response_context
        ))
        await plugin.execute(HookContext.create(
            PluginHook.AFTER_RESPONSE_COMMIT, state, data=response_context
        ))
        assert state.long_term_memory_turn_counter == 10
        assert scheduled == []
        input_context = {}
        await plugin.execute(HookContext.create(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data=input_context
        ))
        assert state.long_term_memory_turn_counter == 0
        assert scheduled == []
        assert input_context["memory"] == {
            "manager": input_context["memory"]["manager"],
            "session_id": "session",
            "protocol_history_cutoff": 42,
            "check_diary": False,
            "extract_long_term_memory": True,
            "long_term_memory_turns": 10,
        }
        await plugin.execute(HookContext.create(
            PluginHook.AFTER_ACTIVATION_INPUT_COMMIT, state, data=input_context
        ))
        return state, scheduled
    state, tasks = asyncio.run(scenario())
    assert state.long_term_memory_turn_counter == 0
    assert tasks == ["memory.persist_long_term"]

def test_diary_and_long_term_memory_are_scheduled_independently(monkeypatch) -> None:
    """一次新激活可同时准备日记和长期记忆，两类后台任务独立启动。

    Args:
        monkeypatch (pytest.MonkeyPatch): 替换后台调度和记忆准备依赖的工具。
    """
    scheduled: list[str] = []
    class MemoryManager:
        """提供固定历史范围和不实际运行的日记入口。"""
        async def get_protocol_history_cutoff(self):
            """返回两类派生任务共用的截止编号。

            Returns:
                int: 测试中固定的截止消息编号。
            """
            return 84
        async def check_diary(self, *, max_history_id):
            """提供后台日记协程，本测试只检查它是否被安排。

            Args:
                max_history_id (int): 激活前固定的历史截止编号。

            Raises:
                AssertionError: 调度记录测试意外执行了后台协程。
            """
            raise AssertionError("background coroutine should not run in this test")
    def capture(coro, *, logger, task_name):
        """记录两类任务各自的调度，不执行派生处理。

        Args:
            coro (Coroutine): 插件准备的真实后台协程。
            logger (logging.Logger): 插件日志对象。
            task_name (str): 日记或长期记忆任务名称。
        """
        scheduled.append(task_name)
        coro.close()
    monkeypatch.setattr(memory_plugin_module, "create_background_task", capture)
    async def scenario():
        """用同一激活上下文执行准备和提交成功事件。

        Returns:
            tuple[AgentState, dict]: 消费批次后的状态和任务准备数据。
        """
        plugin = MemoryPlugin()
        monkeypatch.setattr(plugin, "_mm", lambda state: MemoryManager())
        state = AgentState.create_new("session")
        state.long_term_memory_turn_counter = 12
        commit_context = {}
        await plugin.execute(HookContext.create(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data=commit_context
        ))
        await plugin.execute(HookContext.create(
            PluginHook.AFTER_ACTIVATION_INPUT_COMMIT, state, data=commit_context
        ))
        return state, commit_context
    state, commit_context = asyncio.run(scenario())
    assert state.long_term_memory_turn_counter == 0
    assert commit_context["memory"]["long_term_memory_turns"] == 12
    assert scheduled == ["memory.check_diary", "memory.persist_long_term"]

def test_long_term_memory_uses_frozen_protocol_cutoff(monkeypatch) -> None:
    """验证长期记忆提取使用捕获的协议日志边界。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    calls: list[dict] = []
    class Store:
        """提供当前用例所需的 Store 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证长期记忆提取使用捕获的协议日志边界。
        """
        async def get_latest_message_id(self, session_id):
            """提供用例指定的协议日志最大编号。

            该定义仅用于外层用例：验证长期记忆提取使用捕获的协议日志边界。

            Args:
                session_id (Any): 当前操作所属的会话编号。
            """
            raise AssertionError("must use the cutoff frozen before the new user input")
        async def get_recent_completed_turns(self, session_id, **kwargs):
            """按用例提供前情和本批完整轮次，并记录日志边界。

            该定义仅用于外层用例：验证长期记忆提取使用捕获的协议日志边界。

            Args:
                session_id (Any): 当前操作所属的会话编号。
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            calls.append({"session_id": session_id, **kwargs})
            current = []
            for index in range(12):
                current.extend([
                    HumanMessage(content=text_content(f"human-{index}")),
                    AssistantMessage(content=text_content(f"assistant-{index}")),
                ])
            return [], current
    class MemoryManager:
        """提供当前用例所需的 MemoryManager 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证长期记忆提取使用捕获的协议日志边界。
        """
        chat_history_store = Store()
        async def add(self, messages, history, **kwargs):
            """记录本次日记或长期记忆保存内容，供外层检查。

            该定义仅用于外层用例：验证长期记忆提取使用捕获的协议日志边界。

            Args:
                messages (list[dict]): 按对话顺序排列的消息集合。
                history (Any): 只作为前情使用的旧消息。
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            calls.append({
                "message_count": len(messages),
                "history_count": len(history),
                **kwargs,
            })
    monkeypatch.setattr(
        memory_plugin_module,
        "get_memory_manager",
        lambda session_id: MemoryManager(),
    )
    async def scenario():
        """由外层测试调用，完成“长期记忆提取使用捕获的协议日志边界”的异步步骤。"""
        plugin = MemoryPlugin()
        await plugin._persist_from_protocol(
            "session",
            max_history_id=42,
            batch_turns=12,
        )
    asyncio.run(scenario())
    assert calls[0] == {
        "session_id": "session",
        "current_turns": 12,
        "history_turns": 5,
        "max_history_id": 42,
    }
    assert calls[1] == {
        "message_count": 24,
        "history_count": 0,
        "enable_episodic": True,
        "enable_semantic": True,
    }

def test_disabled_long_term_memory_resets_reached_interval_on_next_input() -> None:
    """长期记忆关闭时，新激活消费已到期计数而不准备提取任务。"""
    async def scenario():
        """触发关闭所有记忆类别的激活准备事件。

        Returns:
            tuple[AgentState, dict]: 清零后的状态与空任务准备数据。
        """
        plugin = MemoryPlugin(
            enable_diary=False,
            enable_episodic=False,
            enable_semantic=False,
        )
        state = AgentState.create_new("session")
        state.long_term_memory_turn_counter = 10
        commit_context = {}
        await plugin.execute(HookContext.create(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data=commit_context
        ))
        return state, commit_context
    state, commit_context = asyncio.run(scenario())
    assert state.long_term_memory_turn_counter == 0
    assert commit_context == {}

def test_disabled_long_term_memory_resets_counter_when_diary_preparation_fails(
    monkeypatch,
) -> None:
    """长期记忆关闭时，日记准备失败也不阻止新激活清零到期计数。

    Args:
        monkeypatch (pytest.MonkeyPatch): 注入日记准备失败的工具。
    """
    async def scenario():
        """在长期记忆关闭的激活准备中注入记忆查询失败。

        Returns:
            tuple[AgentState, dict]: 已消费计数的状态和未准备成功的任务数据。
        """
        plugin = MemoryPlugin(
            enable_diary=True,
            enable_episodic=False,
            enable_semantic=False,
        )
        def fail(_state):
            """模拟日记依赖不可用。

            Args:
                _state (AgentState): 本次新激活的状态。

            Raises:
                RuntimeError: 测试指定的记忆管理器不可用错误。
            """
            raise RuntimeError("diary manager unavailable")
        monkeypatch.setattr(plugin, "_mm", fail)
        state = AgentState.create_new("session")
        state.long_term_memory_turn_counter = 10
        commit_context = {}
        await plugin.execute(HookContext.create(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data=commit_context
        ))
        return state, commit_context
    state, commit_context = asyncio.run(scenario())
    assert state.long_term_memory_turn_counter == 0
    assert commit_context == {}

def test_memory_preparation_failure_keeps_pending_turn_counter(monkeypatch) -> None:
    """长期记忆准备失败时保留计数，让下一次新激活重新准备批次。

    Args:
        monkeypatch (pytest.MonkeyPatch): 注入记忆准备失败的工具。
    """
    async def scenario():
        """触发已到期批次的准备失败，观察计数和任务数据。

        Returns:
            tuple[AgentState, dict]: 保留到期计数的状态和空任务数据。
        """
        plugin = MemoryPlugin(enable_diary=False)
        def fail(_state):
            """模拟长期记忆准备依赖不可用。

            Args:
                _state (AgentState): 本次新激活的状态。

            Raises:
                RuntimeError: 测试指定的记忆管理器不可用错误。
            """
            raise RuntimeError("memory manager unavailable")
        monkeypatch.setattr(plugin, "_mm", fail)
        state = AgentState.create_new("session")
        state.long_term_memory_turn_counter = 10
        commit_context = {}
        await plugin.execute(HookContext.create(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data=commit_context
        ))
        return state, commit_context
    state, commit_context = asyncio.run(scenario())
    assert state.long_term_memory_turn_counter == 10
    assert commit_context == {}

def test_memory_plugin_does_not_own_completed_turn_counting() -> None:
    """验证记忆插件不自行增加已完成轮次计数。"""
    async def scenario():
        """由外层测试调用，完成“记忆插件不自行增加已完成轮次计数”的异步步骤。"""
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
    """验证日记使用独立有效日期唯一表。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    store = DiarySqliteStore(str(tmp_path / "memory.sqlite3"))
    assert store.TABLE_NAME == "diaries"


def test_memory_prompts_only_add_the_tool_evidence_constraint() -> None:
    """验证记忆提示词只追加工具事实约束。"""
    assert TOOL_MEMORY_CONSTRAINT in MEM0_CUSTOM_INSTRUCTIONS
    memory = EpisodicMemory.__new__(EpisodicMemory)
    memory.chat_settings = SimpleNamespace(
        name="AI", feature="温柔", character="助手", address="用户", characteristic="忠实"
    )
    assert TOOL_MEMORY_CONSTRAINT in memory._build_extraction_prompt("当前", "前情")
