from datetime import datetime, timezone
from types import SimpleNamespace

from app.agent.context._projection import build_model_window, project_messages_with_time
from app.agent.memory.manager import MemoryManager
from app.agent.memory.memories.semantic_mem0 import Mem0SemanticMemory
from app.agent.state import AgentState
from app.agent.state.messages import (
    AssistantMessage,
    AvailableMedia,
    HumanMessage,
    ToolCall,
    ToolMediaMessage,
    ToolMessage,
    message_from_storage_format,
)


FIXED_TIME = datetime(2026, 7, 12, 3, 4, 5, tzinfo=timezone.utc)


def test_message_round_trip_preserves_created_at() -> None:
    message = HumanMessage(text="原始输入", created_at=FIXED_TIME)
    payload = message.to_storage_format()

    restored = message_from_storage_format(payload)

    assert isinstance(restored, HumanMessage)
    assert restored.text == "原始输入"
    assert restored.created_at == FIXED_TIME


def test_time_projection_covers_dialogue_and_screenshot_without_mutation() -> None:
    messages = (
        HumanMessage(text="你好", created_at=FIXED_TIME),
        AssistantMessage(text="你好呀", created_at=FIXED_TIME),
        ToolMediaMessage(
            tool_name="screenshot",
            media_kind="screenshot",
            text="截图",
            payload=AvailableMedia(urls=("data:image/png;base64,eA==",)),
            created_at=FIXED_TIME,
        ),
        ToolMessage(
            content="结果",
            tool_name="read_file",
            tool_call_id="call",
            created_at=FIXED_TIME,
        ),
        AssistantMessage(
            text="",
            tool_calls=(ToolCall(id="c1", name="read_file", args={}),),
            created_at=FIXED_TIME,
        ),
    )

    projected = project_messages_with_time(messages)

    assert "发送时间：2026-07-12 11:04:05 +0800 星期日" in projected[0]["content"]
    assert "发送时间：2026-07-12 11:04:05 +0800 星期日" in projected[1]["content"]
    assert "发送时间" not in str(projected[2]["content"])
    assert projected[3]["content"] == "结果"
    assert projected[4]["content"] == ""
    assert messages[0].text == "你好"


def test_context_strategy_projects_time_and_removes_internal_metadata() -> None:
    state = AgentState.create_new("time-test")
    state.append_input(HumanMessage(text="你好", created_at=FIXED_TIME))
    result = build_model_window(state)

    assert "发送时间" in result[0]["content"]
    assert "_created_at" not in result[0]
    assert state.messages[0].text == "你好"


def test_episodic_and_semantic_inputs_include_structured_message_time() -> None:
    message = HumanMessage(text="我准备学习 Python", created_at=FIXED_TIME)

    manager = object.__new__(MemoryManager)
    manager.chat_settings = SimpleNamespace(address="主人", name="Ayaya")
    episodic_text = manager._format_messages([message])

    semantic = object.__new__(Mem0SemanticMemory)
    semantic_messages = semantic._convert_messages([message])

    assert "[发送时间：2026-07-12 11:04:05 +0800 星期日]" in episodic_text
    assert "[发送时间：2026-07-12 11:04:05 +0800 星期日]" in semantic_messages[0]["content"]
