from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

from app.agent.context_strategy import ContextStrategyConfig, ContextStrategyManager
from app.agent.message import Message, messages_from_openai_format
from app.agent.message_time import project_messages_with_time
from app.agent.memory.manager import MemoryManager
from app.agent.memory.memories.semantic_mem0 import Mem0SemanticMemory
from app.agent.state import AgentState


FIXED_TIME = datetime(2026, 7, 12, 3, 4, 5, tzinfo=timezone.utc)


def test_message_round_trip_preserves_created_at() -> None:
    message = Message.user_message("原始输入")
    message.timestamp = FIXED_TIME
    payload = message.to_openai_format()
    payload["_created_at"] = FIXED_TIME.isoformat()

    restored = messages_from_openai_format([payload])[0]

    assert restored.content == "原始输入"
    assert restored.timestamp == FIXED_TIME


def test_time_projection_covers_dialogue_and_screenshot_without_mutation() -> None:
    messages = [
        {"role": "user", "content": "你好", "_created_at": FIXED_TIME.isoformat()},
        {"role": "assistant", "content": "你好呀", "_created_at": FIXED_TIME.isoformat()},
        {
            "role": "user",
            "name": "system_screenshot",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}],
            "_created_at": FIXED_TIME.isoformat(),
        },
        {"role": "tool", "content": "结果", "_created_at": FIXED_TIME.isoformat()},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1"}],
            "_created_at": FIXED_TIME.isoformat(),
        },
    ]
    original = deepcopy(messages)

    projected = project_messages_with_time(messages)

    assert "发送时间：2026-07-12 11:04:05 +0800 星期日" in projected[0]["content"]
    assert "发送时间：2026-07-12 11:04:05 +0800 星期日" in projected[1]["content"]
    assert projected[2]["content"][0]["type"] == "image_url"
    assert projected[3]["content"] == "结果"
    assert projected[4]["content"] == ""
    assert messages == original


def test_context_strategy_projects_time_and_removes_internal_metadata() -> None:
    state = AgentState.create_new("time-test")
    state.messages = [
        {"role": "user", "content": "你好", "_created_at": FIXED_TIME.isoformat()}
    ]
    manager = ContextStrategyManager(ContextStrategyConfig(compression_trigger_tokens_k=100))

    result = manager.build_model_window(state)

    assert "发送时间" in result[0]["content"]
    assert "_created_at" not in result[0]
    assert state.messages[0]["content"] == "你好"


def test_episodic_and_semantic_inputs_include_structured_message_time() -> None:
    message = Message.user_message("我准备学习 Python")
    message.timestamp = FIXED_TIME

    manager = object.__new__(MemoryManager)
    manager.chat_settings = SimpleNamespace(address="主人", name="Ayaya")
    episodic_text = manager._format_messages([message])

    semantic = object.__new__(Mem0SemanticMemory)
    semantic_messages = semantic._convert_messages([message])

    assert "[发送时间：2026-07-12 11:04:05 +0800 星期日]" in episodic_text
    assert "[发送时间：2026-07-12 11:04:05 +0800 星期日]" in semantic_messages[0]["content"]
