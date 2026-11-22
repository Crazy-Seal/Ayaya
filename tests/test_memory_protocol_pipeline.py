from tests.memory_support import bind_memory_manager, operation_for
from tests.memory_support import memory_count, set_memory_count
from tests.memory_support import make_memory_plugin
from app.crud.sqlite_factory import SqliteFactory
from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator
from tests.model_capture import result_parts
from tests.runtime_support import configured_settings
from tests.model_capture import capture_input
from app.agent.state.messages import text_content
import asyncio
import pytest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.agent.plugins.base import HookContext, PluginHook
from app.agent.state.manager import SessionStateManager
from app.agent.plugins.memory.memories.diary import DiaryGeneration, DiaryMemory
from app.agent.plugins.memory.memories.episodic import EpisodicMemory
from app.agent.plugins.memory.memories.semantic_mem0 import Mem0SemanticMemory
from app.agent.plugins.memory.store.chat_history_store import ChatHistoryStore
from app.agent.state.messages import (
    AssistantMessage,
    HumanMessage,
    PluginNotificationMessage,
    RuntimeInputMessage,
    SubagentMessage,
    TaskResultMessage,
    ToolCall,
    ImageContent,
    ToolMessage,
)
from app.agent.plugins.memory.memory import MemoryPlugin
from app.agent.state.time import render_timed_text
from app.schemas.runtime_types import TaskResultStatus
from app.agent.state import AgentState
import app.agent.plugins.memory.memory as memory_plugin_module


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
        manager = SessionStateManager("session", sqlite_factory=SqliteFactory(str(db_path)))
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
            store = ChatHistoryStore(sqlite_factory=SqliteFactory(db_path))
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
        manager = SessionStateManager("session", sqlite_factory=SqliteFactory(str(db_path)))
        try:
            state = AgentState.create_new("session")
            state.add_user_message("请读取文件")
            call = ToolCall(id="call-1", name="read_file", args={})
            state.record_assistant_step("", (call,))
            state.record_tool_result(call, text_content("file-content"))
            state.add_assistant_message("读取完成")
            await manager.save(state, checkpoint_type="completed")
            store = ChatHistoryStore(sqlite_factory=SqliteFactory(db_path))
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
    memory.chat_settings = SimpleNamespace(address="主人", name="Ayaya")
    messages = [
        AssistantMessage(
            content=text_content(""),
            tool_calls=(ToolCall(id="c", name="read_file", args={"path": "a.py"}),),
        ),
        ToolMessage(content=text_content("result"), tool_name="read_file", tool_call_id="c"),
    ]

    current = memory._format_messages(messages)
    history = memory._format_messages(messages, mark_as_history=True)

    assert 'Ayaya：调用工具 read_file，参数：{"path":"a.py"}' in current
    assert "工具结果：result" in current
    assert history == "Ayaya：调用了工具 read_file"

@pytest.mark.parametrize("mark_as_history", [False, True])
def test_episodic_preserves_text_with_multiple_tool_calls(mark_as_history: bool) -> None:
    """验证助手同条消息中的说明和多个工具调用按顺序保留。

    Args:
        mark_as_history (bool): 是否使用历史材料的工具参数省略规则。
    """
    memory = EpisodicMemory.__new__(EpisodicMemory)
    memory.chat_settings = SimpleNamespace(address="主人", name="Ayaya")
    message = AssistantMessage(
        content=text_content("我先看两个文件。\n然后比较内容。"), origin_channel="qq",
        created_at=datetime(2026, 11, 6, 2, tzinfo=timezone.utc),
        tool_calls=(
            ToolCall(id="a", name="read_file", args={"path": "a.py"}),
            ToolCall(id="b", name="read_file", args={"path": "b.py"}),
        ),
    )
    expected = [f"Ayaya（回复 qq）: {render_timed_text(message.text, message.created_at)}"]
    if mark_as_history:
        expected.extend(["Ayaya（回复 qq）：调用了工具 read_file"] * 2)
    else:
        expected.extend([
            'Ayaya（回复 qq）：调用工具 read_file，参数：{"path":"a.py"}',
            'Ayaya（回复 qq）：调用工具 read_file，参数：{"path":"b.py"}',
        ])

    assert memory._format_messages([message], mark_as_history=mark_as_history) == "\n".join(expected)


def test_diary_format_skips_system_injected_tool_images() -> None:
    """验证日记格式化跳过内部工具图片。"""
    memory = DiaryMemory.__new__(DiaryMemory)
    memory.chat_settings = SimpleNamespace(address="主人", name="Ayaya")
    messages = [
        HumanMessage(content=text_content("请看一下屏幕")),
        ToolMessage(content=text_content("观察成功"), tool_name="python", tool_call_id="call-shot"),
        ToolMessage(tool_name="python", tool_call_id="image-python", content=(ImageContent("data:image/png;base64,QUFB", media_kind="tool"),)),
        ToolMessage(tool_name="draw", tool_call_id="image-mcp", content=(ImageContent("data:image/png;base64,QkJC", media_kind="mcp"),)),
        ToolMessage(tool_name="draw", tool_call_id="image-tool", content=(ImageContent("data:image/png;base64,Q0ND", media_kind="tool"),)),
    ]
    formatted = memory._format_messages(messages)
    assert f"主人：{render_timed_text(messages[0].text, messages[0].created_at)}" in formatted
    assert f"工具结果：{render_timed_text(messages[1].text, messages[1].created_at)}" in formatted
    assert "[系统消息]工具" not in formatted


@pytest.mark.parametrize(
    ("name", "address", "ai_label", "user_label"),
    [(" Ayaya ", " 主人 ", "Ayaya", "主人"), (None, None, "AI助手", "用户"), (" ", " ", "AI助手", "用户")],
)
def test_diary_uses_configured_speakers_and_preserves_message_sources(
    name: str | None, address: str | None, ai_label: str, user_label: str,
) -> None:
    """验证发言、工具及通知带原消息时间，角色称呼和渠道来源保持可辨认。

    Args:
        name (str | None): 角色名称设置。
        address (str | None): 用户称呼设置。
        ai_label (str): 助手发言应使用的名称。
        user_label (str): 用户发言应使用的称呼。
    """
    memory = DiaryMemory.__new__(DiaryMemory)
    memory.chat_settings = SimpleNamespace(name=name, address=address)
    messages = [
        HumanMessage(content=text_content("第一行\nuser: 原文")),
        AssistantMessage(content=text_content("桌面回复")),
        HumanMessage(content=text_content("渠道消息"), origin_channel="qq"),
        AssistantMessage(
            content=text_content("渠道回复"), origin_channel="qq",
            tool_calls=(ToolCall(id="call", name="read_file", args={"path": "a.py"}),),
        ),
        ToolMessage(content=text_content("文件正文"), tool_name="read_file", tool_call_id="call"),
        PluginNotificationMessage(plugin_name="test", name="notice", text="提醒内容"),
    ]

    assert memory._format_messages(messages) == "\n".join([
        f"{user_label}：{render_timed_text(messages[0].text, messages[0].created_at)}",
        f"{ai_label}：{render_timed_text(messages[1].text, messages[1].created_at)}",
        f"{user_label}（来自 qq）：{render_timed_text(messages[2].text, messages[2].created_at)}",
        f"{ai_label}（回复 qq）：{render_timed_text(messages[3].text, messages[3].created_at)}",
        f"{ai_label}：" + render_timed_text('调用工具 read_file，参数：{"path":"a.py"}', messages[3].created_at),
        f"工具结果：{render_timed_text(messages[4].text, messages[4].created_at)}",
        "[插件通知：test/notice]：" + render_timed_text("提醒内容", messages[5].created_at),
    ])


@pytest.mark.parametrize(
    ("message", "label"),
    [
        (TaskResultMessage(task_id="t", task_name="研究资料", status=TaskResultStatus.COMPLETED,
                           result="第一篇资料\n第二篇资料"), "[后台任务完成：研究资料]"),
        (TaskResultMessage(task_id="t", task_name="研究资料", status=TaskResultStatus.FAILED,
                           result="读取失败"), "[后台任务失败：研究资料]"),
        (PluginNotificationMessage(plugin_name="reminder", name="due", text="该给团团喂食了。"),
         "[插件通知：reminder/due]"),
        (SubagentMessage(subagent_id="s", subagent_name="资料助手", result="养猫注意事项\n每天换水"),
         "[子 Agent 返回：资料助手]"),
    ],
)
def test_memory_notifications_put_source_before_time_and_preserve_body(
    message: RuntimeInputMessage, label: str,
) -> None:
    """验证三种记忆统一按通知来源、时间和正文排序，并保留多行正文。

    Args:
        message (RuntimeInputMessage): 不同来源和状态的通知消息。
        label (str): 与通知来源和状态对应的显示标签。
    """
    settings = SimpleNamespace(name="Ayaya", address="主人")
    diary = object.__new__(DiaryMemory)
    diary.chat_settings = settings
    episodic = object.__new__(EpisodicMemory)
    episodic.chat_settings = settings
    semantic = object.__new__(Mem0SemanticMemory)
    expected = f"{label}：{render_timed_text(message.text, message.created_at)}"

    assert diary._format_messages([message]) == expected
    assert episodic._format_messages([message]) == expected
    assert semantic._convert_messages([message])[0]["content"] == expected
    assert semantic._convert_messages([message], mark_as_history=True)[0]["content"] == f"[前情提要] {expected}"

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
        generation = await memory.prepare_generation(date(2026, 9, 2), max_history_id=42)
        try:
            await memory.generate(generation.target_date, generation.messages, max_history_id=generation.max_history_id)
        finally:
            generation.release()
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
    assert "昨天先确定两个字段" in prompt
    assert "今天继续昨天的设计" in prompt
    assert "没有与用户交互" not in prompt
    assert memory.store.added == [("session", date(2026, 9, 1), "生成的日记")]

def test_diary_prompt_preserves_current_messages_without_reference_context() -> None:
    """验证没有前情时仍保留当前对话，且不将 None 文本发送给模型。"""
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
    assert "今天第一次聊天" in prompt
    assert "None" not in prompt

def test_memory_jobs_start_only_after_input_commit_with_frozen_history(tmp_path, monkeypatch) -> None:
    """真实任务入口保留提交时序、固定历史范围和两类静默任务。"""
    from app.agent.plugins.runtime import PluginRuntime
    from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
    calls = []

    class MemoryManager:
        """只替代外部记忆工作，不替代 Supervisor 或任务持久化。"""
        chat_history_store = None

        async def get_protocol_history_cutoff(self):
            """返回准备阶段固定的历史范围。"""
            return 84

        async def prepare_diary(self, *, max_history_id):
            """提供需要实际生成的日期和固定历史范围。"""
            return DiaryGeneration("session", date(2026, 9, 1), [], max_history_id)

        async def generate_diary(self, generation):
            """记录实际任务使用的截止编号。"""
            calls.append(generation.max_history_id)
            return "日记生成成功"

    bind_memory_manager(monkeypatch, lambda self, session: MemoryManager())

    async def scenario():
        """通过钩子提交任务，再通过正式记录和等待接口验证。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        runtime = PluginRuntime("memory", "session", _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue)
        plugin = make_memory_plugin(sqlite_factory=SqliteFactory())
        state = AgentState.create_new("session")
        set_memory_count(state, 12)
        data = {}
        try:
            await plugin.execute(HookContext.create(PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data, runtime=runtime))
            assert await supervisor.list_tasks("session") == []
            assert memory_count(state) == 0
            assert data["memory"].protocol_history_cutoff == 84
            await plugin.execute(HookContext.create(PluginHook.ON_INVOKE, state, data, runtime=runtime))
            records = await supervisor.list_tasks("session")
            assert {record.name for record in records} == {"memory.check_diary", "memory.persist_long_term"}
            for record in records:
                done = await runtime.wait_task(record.task_id, timeout_ms=1000)
                assert done.status == "completed"
                assert not done.notify_agent
            assert calls == [84]
        finally:
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_long_term_memory_uses_frozen_protocol_cutoff(tmp_path) -> None:
    """验证长期记忆提取使用捕获的协议日志边界。

    Args:
        tmp_path (Path): 当前用例的隔离数据目录。
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
            return {"episodic": 0, "semantic": 0}

        episodic_memory = None
        semantic_memory = None
    async def scenario():
        """由外层测试调用，完成“长期记忆提取使用捕获的协议日志边界”的异步步骤。"""
        from app.agent.plugins.memory.tasks.memory_tasks import LongTermMemoryTask
        from app.agent.plugins.runtime import PluginRuntime
        from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
        from app.schemas.runtime_types import TaskStatus
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        runtime = PluginRuntime("memory", "session", _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue)
        try:
            task_id = await runtime.submit_task(LongTermMemoryTask(operation_for(MemoryManager()), "session", 42, 12, True, True),
                name="memory.persist_long_term", title="提取长期记忆", notify_agent=False)
            assert (await runtime.wait_task(task_id, timeout_ms=1000)).status == TaskStatus.COMPLETED
        finally:
            await supervisor.close()
            await supervisor.records.close()
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
        plugin = make_memory_plugin(
            enable_diary=False,
            enable_episodic=False,
            enable_semantic=False,
        sqlite_factory=SqliteFactory())
        state = AgentState.create_new("session")
        set_memory_count(state, 10)
        commit_context = {}
        await plugin.execute(HookContext.create(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data=commit_context
        ))
        return state, commit_context
    state, commit_context = asyncio.run(scenario())
    assert memory_count(state) == 0
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
        plugin = make_memory_plugin(
            enable_diary=True,
            enable_episodic=False,
            enable_semantic=False,
        sqlite_factory=SqliteFactory())
        def fail(_state):
            """模拟日记依赖不可用。

            Args:
                _state (AgentState): 本次新激活的状态。

            Raises:
                RuntimeError: 测试指定的记忆管理器不可用错误。
            """
            raise RuntimeError("diary manager unavailable")
        bind_memory_manager(monkeypatch, lambda self, session: fail(session))
        state = AgentState.create_new("session")
        set_memory_count(state, 10)
        commit_context = {}
        await plugin.execute(HookContext.create(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data=commit_context
        ))
        return state, commit_context
    state, commit_context = asyncio.run(scenario())
    assert memory_count(state) == 0
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
        plugin = make_memory_plugin(enable_diary=False, sqlite_factory=SqliteFactory())
        def fail(_state):
            """模拟长期记忆准备依赖不可用。

            Args:
                _state (AgentState): 本次新激活的状态。

            Raises:
                RuntimeError: 测试指定的记忆管理器不可用错误。
            """
            raise RuntimeError("memory manager unavailable")
        bind_memory_manager(monkeypatch, lambda self, session: fail(session))
        state = AgentState.create_new("session")
        set_memory_count(state, 10)
        commit_context = {}
        await plugin.execute(HookContext.create(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data=commit_context
        ))
        return state, commit_context
    state, commit_context = asyncio.run(scenario())
    assert memory_count(state) == 10
    assert commit_context == {}

def test_memory_plugin_does_not_own_completed_turn_counting() -> None:
    """验证记忆插件不自行增加已完成轮次计数。"""
    async def scenario():
        """由外层测试调用，完成“记忆插件不自行增加已完成轮次计数”的异步步骤。"""
        plugin = make_memory_plugin(
            enable_diary=False,
            enable_episodic=False,
            enable_semantic=False,
        sqlite_factory=SqliteFactory())
        state = AgentState.create_new("session")
        state.add_user_message("尚未完成的真人输入")
        before = memory_count(state)
        await plugin.execute(HookContext.create(
            PluginHook.BEFORE_RESPONSE_COMMIT, state, data={}
        ))
        return before, memory_count(state)

    assert asyncio.run(scenario()) == (0, 0)


def test_episodic_prompt_includes_the_tool_evidence_constraint() -> None:
    """验证情景记忆构造的模型输入包含工具事实约束。"""
    memory = EpisodicMemory.__new__(EpisodicMemory)
    memory.chat_settings = SimpleNamespace(
        name="AI", feature="温柔", character="助手", address="用户", characteristic="忠实"
    )
    assert TOOL_MEMORY_CONSTRAINT in memory._build_extraction_prompt("当前", "前情")
