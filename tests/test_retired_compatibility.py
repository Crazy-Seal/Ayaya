from pathlib import Path

from pydantic import ValidationError
import pytest

from app.agent.context_strategy import ContextStrategyConfig, ContextStrategyManager
from app.agent.message_time import RuntimeContext
from app.agent.plugins.memory import MemoryPluginConfig
from app.agent.state import AgentState


def test_retired_context_window_api_is_removed() -> None:
    assert set(ContextStrategyConfig.model_fields) == {"compression_trigger_tokens_k"}
    assert not hasattr(ContextStrategyManager, "compact_checkpoint")

    with pytest.raises(ValidationError):
        ContextStrategyConfig(max_context_tokens=128_000)


def test_retired_runtime_context_projection_api_is_removed() -> None:
    assert not hasattr(RuntimeContext, "started_at")
    assert not hasattr(RuntimeContext, "system_text")


def test_retired_memory_state_aliases_are_removed() -> None:
    assert not hasattr(AgentState, "summary_counter")
    assert not hasattr(AgentState, "increment_summary_counter")
    assert not hasattr(AgentState, "reset_summary_counter")


def test_retired_memory_plugin_setting_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryPluginConfig(summary_every_human_messages=10)


def test_runtime_source_has_no_retired_internal_names() -> None:
    root = Path(__file__).parents[1]
    retired = {
        "ImageTaskResult",
        "_image_description_tasks",
        "cancel_task",
        "clear_task",
        "generate_multiple_image_descriptions",
        "get_cache_key",
        "get_image_task",
        "has_image_content",
        "image_task_key",
        "memory_checkpoint_human_floor",
        "set_image_task",
        "summary_history_cutoff",
        "get_summary_history_cutoff",
        "check_summary",
        "recent_summaries_count",
        "summary_every_messages",
    }
    sources = "\n".join(
        path.read_text(encoding="utf-8-sig")
        for path in (root / "app").rglob("*.py")
    )
    for name in retired:
        assert name not in sources


def test_agent_does_not_own_diary_storage_details() -> None:
    root = Path(__file__).parents[1]
    source = (root / "app" / "agent" / "agent.py").read_text(encoding="utf-8-sig")
    assert "DiarySqliteStore" not in source
    assert "MemoryConfig" not in source
    assert "_has_diary_for_session" not in source
