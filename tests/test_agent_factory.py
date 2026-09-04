from app.agent.context_strategy import ContextStrategyConfig
from app.schemas.chat_settings import AgentPluginSettings, ChatSettings
from app.services import agent_factory


def _settings(agent_plugins=None, context_strategy=None):
    return ChatSettings(
        session_id="test-session",
        model_name="test-model",
        openai_api_key="test-key",
        openai_base_url="http://127.0.0.1:1/v1",
        temperature=0.1,
        system_prompt="test",
        tools_list=[],
        agent_plugins=agent_plugins,
        context_strategy=context_strategy or ContextStrategyConfig(compression_trigger_tokens_k=100),
        skills=[],
        name="Ayaya",
        feature="温柔",
        character="助手",
        address="用户",
        characteristic="忠实记录共同经历。",
    )


def test_memory_plugin_owns_image_processing(monkeypatch) -> None:
    manager = object()
    captured = {}
    monkeypatch.setattr(agent_factory, "get_memory_manager", lambda session_id: manager)
    monkeypatch.setattr(
        agent_factory,
        "Agent",
        lambda config, **kwargs: captured.update(kwargs) or config,
    )
    settings = _settings()

    config = agent_factory.build_agent(settings)

    assert config.context_strategy.compression_trigger_tokens_k == 100
    assert captured["chat_settings"] is settings
    assert config.plugins == [{"name": "memory", "config": {
        "enable_diary": True,
        "enable_episodic": True,
        "enable_semantic": True,
        "long_term_memory_every_human_messages": 10,
    }}]


def test_agent_factory_uses_top_level_context_strategy(monkeypatch) -> None:
    manager = object()
    monkeypatch.setattr(agent_factory, "get_memory_manager", lambda session_id: manager)
    monkeypatch.setattr(agent_factory, "Agent", lambda config, **kwargs: config)

    settings = _settings(
        context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        agent_plugins={
            "memory": AgentPluginSettings(
                enabled=False,
                config={
                    "enable_diary": False,
                    "enable_episodic": True,
                    "enable_semantic": False,
                    "long_term_memory_every_human_messages": 7,
                },
            ),
        }
    )

    config = agent_factory.build_agent(settings)

    assert config.context_strategy.compression_trigger_tokens_k == 100
    assert config.plugins[0]["config"] == {
        "enable_diary": False,
        "enable_episodic": True,
        "enable_semantic": False,
        "long_term_memory_every_human_messages": 7,
    }


def test_agent_factory_loads_memory_when_all_memory_types_are_disabled(monkeypatch) -> None:
    manager = object()
    monkeypatch.setattr(agent_factory, "get_memory_manager", lambda session_id: manager)
    monkeypatch.setattr(agent_factory, "Agent", lambda config, **kwargs: config)

    settings = _settings(
        agent_plugins={
            "memory": AgentPluginSettings(
                enabled=False,
                config={
                    "enable_diary": False,
                    "enable_episodic": False,
                    "enable_semantic": False,
                },
            )
        }
    )

    config = agent_factory.build_agent(settings)

    assert config.plugins[0]["config"] == {
        "enable_diary": False,
        "enable_episodic": False,
        "enable_semantic": False,
        "long_term_memory_every_human_messages": 10,
    }


def test_image_is_not_registered_as_standalone_plugin() -> None:
    from app.agent.plugins.registry import PluginRegistry

    assert not PluginRegistry.has("image")
