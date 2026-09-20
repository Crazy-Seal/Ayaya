import asyncio
from datetime import date

from app.agent.context import ContextStrategyConfig
from app.agent.memory import manager as memory_manager_module
from app.agent.memory.config import MemoryConfig
from app.agent.memory.manager import MemoryManager
from app.agent.memory.memories import episodic as episodic_module
from app.agent.state.messages import HumanMessage
from app.schemas.chat_settings import AgentPluginSettings, ChatSettings


def _chat_settings(*, enable_diary=True, enable_episodic=True, enable_semantic=True):
    return ChatSettings(
        session_id="test-session",
        model_name="test-model",
        openai_api_key="test-key",
        openai_base_url="http://127.0.0.1:1/v1",
        temperature=0.1,
        system_prompt="test",
        tools_list=[],
        context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        agent_plugins={
            "memory": AgentPluginSettings(
                enabled=True,
                config={
                    "enable_diary": enable_diary,
                    "enable_episodic": enable_episodic,
                    "enable_semantic": enable_semantic,
                },
            )
        },
        skills=[],
    )


def _capture_episodic_llm_config(monkeypatch, config: MemoryConfig):
    captured = {}

    class FakeStore:
        def __init__(self, *args, **kwargs):
            pass

    class FakeLLMClient:
        def __init__(self, llm_config):
            captured["config"] = llm_config

    monkeypatch.setattr(episodic_module, "EpisodicChromaStore", FakeStore)
    monkeypatch.setattr(episodic_module, "EpisodicSqliteStore", FakeStore)
    monkeypatch.setattr(episodic_module, "LLMClient", FakeLLMClient)
    episodic_module.EpisodicMemory("test-session", config, _chat_settings())
    return captured["config"]


def test_memory_config_reads_episodic_extraction_environment(monkeypatch) -> None:
    monkeypatch.setenv("EPISODIC_EXTRACTION_MODEL", "gpt-test")
    monkeypatch.setenv("EPISODIC_EXTRACTION_BASE_URL", "https://memory.example/v1")
    monkeypatch.setenv("EPISODIC_EXTRACTION_API_KEY", "memory-key")

    config = MemoryConfig.from_env()

    assert config.episodic_extraction_model == "gpt-test"
    assert config.episodic_extraction_base_url == "https://memory.example/v1"
    assert config.episodic_extraction_api_key == "memory-key"


def test_episodic_memory_uses_independent_extraction_config(monkeypatch) -> None:
    llm_config = _capture_episodic_llm_config(
        monkeypatch,
        MemoryConfig(
            episodic_extraction_model="gpt-test",
            episodic_extraction_base_url="https://memory.example/v1",
            episodic_extraction_api_key="memory-key",
        ),
    )

    assert llm_config.model == "gpt-test"
    assert llm_config.base_url == "https://memory.example/v1"
    assert llm_config.api_key == "memory-key"


def test_episodic_memory_uses_only_explicit_model_without_chat_fallback(monkeypatch) -> None:
    llm_config = _capture_episodic_llm_config(
        monkeypatch,
        MemoryConfig(episodic_extraction_model="gpt-test"),
    )

    assert llm_config.model == "gpt-test"
    assert llm_config.base_url == ""
    assert llm_config.api_key == ""


def test_episodic_memory_does_not_fall_back_to_chat_model_config(monkeypatch) -> None:
    llm_config = _capture_episodic_llm_config(monkeypatch, MemoryConfig())

    assert llm_config.model == ""
    assert llm_config.base_url == ""
    assert llm_config.api_key == ""


def test_memory_manager_skips_disabled_memory_subsystems(monkeypatch) -> None:
    constructed: list[str] = []

    class FakeChatHistoryStore:
        def __init__(self, *args, **kwargs):
            constructed.append("chat_history")

    class DisabledSubsystem:
        def __init__(self, *args, **kwargs):
            raise AssertionError("disabled memory subsystem should not initialize")

    monkeypatch.setattr(memory_manager_module, "ChatHistoryStore", FakeChatHistoryStore)
    monkeypatch.setattr(memory_manager_module, "DiaryMemory", DisabledSubsystem)
    monkeypatch.setattr(memory_manager_module, "EpisodicMemory", DisabledSubsystem)
    monkeypatch.setattr(memory_manager_module, "Mem0SemanticMemory", DisabledSubsystem)

    manager = MemoryManager(
        "test-session",
        MemoryConfig(),
        _chat_settings(enable_diary=False, enable_episodic=False, enable_semantic=False),
    )

    assert constructed == []
    assert manager.diary_memory is None
    assert manager.episodic_memory is None
    assert manager.semantic_memory is None


def test_memory_manager_uses_mem0_even_with_unrelated_backend_env(monkeypatch) -> None:
    constructed: list[tuple[str, str]] = []

    class FakeChatHistoryStore:
        def __init__(self, *args, **kwargs):
            pass

    class DisabledSubsystem:
        def __init__(self, *args, **kwargs):
            raise AssertionError("disabled memory subsystem should not initialize")

    class FakeMem0SemanticMemory:
        def __init__(self, session_id, config, chat_settings):
            constructed.append((session_id, chat_settings.session_id))

    monkeypatch.setenv("SEMANTIC_BACKEND", "native")
    monkeypatch.setattr(memory_manager_module, "ChatHistoryStore", FakeChatHistoryStore)
    monkeypatch.setattr(memory_manager_module, "DiaryMemory", DisabledSubsystem)
    monkeypatch.setattr(memory_manager_module, "EpisodicMemory", DisabledSubsystem)
    monkeypatch.setattr(memory_manager_module, "Mem0SemanticMemory", FakeMem0SemanticMemory)

    manager = MemoryManager(
        "test-session",
        MemoryConfig.from_env(),
        _chat_settings(enable_diary=False, enable_episodic=False, enable_semantic=True),
    )

    assert constructed == [("test-session", "test-session")]
    assert isinstance(manager.semantic_memory, FakeMem0SemanticMemory)


def test_check_diary_is_noop_when_diary_disabled() -> None:
    async def scenario() -> None:
        manager = MemoryManager(
            "test-session",
            MemoryConfig(),
            _chat_settings(enable_diary=False, enable_episodic=False, enable_semantic=False),
        )
        await manager.check_diary()

    asyncio.run(scenario())


def test_memory_manager_check_diary_uses_protocol_history_cutoff(monkeypatch) -> None:
    checks: list[tuple[date, int | None]] = []

    class FakeChatHistoryStore:
        def __init__(self, *args, **kwargs):
            pass

    class FakeDiaryMemory:
        def __init__(self, *args, **kwargs):
            pass

        async def check_and_generate(
            self,
            effective_date: date,
            *,
            max_history_id: int | None = None,
        ) -> None:
            checks.append((effective_date, max_history_id))

    monkeypatch.setattr(memory_manager_module, "ChatHistoryStore", FakeChatHistoryStore)
    monkeypatch.setattr(memory_manager_module, "DiaryMemory", FakeDiaryMemory)

    manager = MemoryManager(
        "test-session",
        MemoryConfig(),
        _chat_settings(enable_diary=True, enable_episodic=False, enable_semantic=False),
    )
    asyncio.run(manager.check_diary(max_history_id=17))

    assert checks and checks[0][1] == 17


def test_disabled_memory_subsystems_are_not_accessed(monkeypatch) -> None:
    class FakeChatHistoryStore:
        def __init__(self, *args, **kwargs):
            pass

    class DisabledSubsystem:
        def __init__(self, *args, **kwargs):
            raise AssertionError("disabled memory subsystem should not initialize")

    monkeypatch.setattr(memory_manager_module, "ChatHistoryStore", FakeChatHistoryStore)
    monkeypatch.setattr(memory_manager_module, "DiaryMemory", DisabledSubsystem)
    monkeypatch.setattr(memory_manager_module, "EpisodicMemory", DisabledSubsystem)
    monkeypatch.setattr(memory_manager_module, "Mem0SemanticMemory", DisabledSubsystem)

    async def scenario() -> tuple[dict[str, int], str, str, str]:
        manager = MemoryManager(
            "test-session",
            MemoryConfig(),
            _chat_settings(enable_diary=False, enable_episodic=False, enable_semantic=False),
        )
        add_result = await manager.add(
            [HumanMessage(text="近期对话")],
            enable_episodic=False,
            enable_semantic=False,
        )
        context = await manager.get_context("查询")
        search_result = await manager.search("查询", "all")
        diary_result = await manager.search_diary(date(2024, 1, 1), date(2024, 1, 2))
        return add_result, context, search_result, diary_result

    add_result, context, search_result, diary_result = asyncio.run(scenario())

    assert add_result == {"episodic": 0, "semantic": 0}
    assert context == ""
    assert search_result == "未找到相关记忆"
    assert diary_result == "日记记忆未启用"
