from app.crud.sqlite_factory import SqliteFactory
from app.agent.context import ContextStrategyConfig
from app.schemas.chat_settings import AgentPluginSettings, ChatSettings
from app.agent.runtime import agent_factory
from types import SimpleNamespace


def _settings(agent_plugins=None, context_strategy=None):
    """构造当前用例需要的最小会话设置。

    Args:
        agent_plugins (Any): 用例指定的插件开关和配置。
        context_strategy (Any): 用例指定的上下文压缩配置。
    """
    return ChatSettings(
        session_id="test-session",
        model_name="test-model",
        openai_api_key="test-key",
        openai_base_url="http://127.0.0.1:1/v1",
        temperature=0.1,
        system_prompt="test",
        agent_plugins=agent_plugins,
        context_strategy=context_strategy or ContextStrategyConfig(compression_trigger_tokens_k=100),
        name="Ayaya",
        feature="温柔",
        character="助手",
        address="用户",
        characteristic="忠实记录共同经历。",
    )


def test_memory_plugin_owns_image_processing(monkeypatch, repl_service) -> None:
    """验证图片处理由记忆插件负责。

    Args:
        repl_service (ReplService): 测试 fixture 提供的懒启动原生 Python 资源。
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    captured = {}
    monkeypatch.setattr(
        agent_factory,
        "Agent",
        lambda config, sqlite_factory, **kwargs: captured.update(kwargs) or SimpleNamespace(config=config, register_tool=lambda _: None),
    )
    settings = _settings()

    config = agent_factory.build_agent(settings, sqlite_factory=SqliteFactory(), repl_service=repl_service).config

    assert config.context_strategy.compression_trigger_tokens_k == 100
    assert captured["chat_settings"] is settings
    assert config.plugins == [{"name": "memory", "config": {
        "enable_diary": True,
        "enable_episodic": True,
        "enable_semantic": True,
        "long_term_memory_every_human_messages": 10,
    }}]


def test_agent_factory_uses_top_level_context_strategy(monkeypatch, repl_service) -> None:
    """验证Agent 工厂使用会话顶层上下文策略。

    Args:
        repl_service (ReplService): 测试 fixture 提供的懒启动原生 Python 资源。
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    monkeypatch.setattr(agent_factory, "Agent", lambda config, sqlite_factory, **kwargs: SimpleNamespace(config=config, register_tool=lambda _: None))

    settings = _settings(
        context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        agent_plugins={
            "memory": AgentPluginSettings(
                enabled=True,
                config={
                    "enable_diary": False,
                    "enable_episodic": True,
                    "enable_semantic": False,
                    "long_term_memory_every_human_messages": 7,
                },
            ),
        }
    )

    config = agent_factory.build_agent(settings, sqlite_factory=SqliteFactory(), repl_service=repl_service).config

    assert config.context_strategy.compression_trigger_tokens_k == 100
    assert config.plugins[0]["config"] == {
        "enable_diary": False,
        "enable_episodic": True,
        "enable_semantic": False,
        "long_term_memory_every_human_messages": 7,
    }


def test_agent_factory_loads_enabled_memory_when_all_memory_types_are_disabled(monkeypatch, repl_service) -> None:
    """验证插件开启但全部记忆类别关闭时，仍按插件总开关装配。

    Args:
        repl_service (ReplService): 测试 fixture 提供的懒启动原生 Python 资源。
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    monkeypatch.setattr(agent_factory, "Agent", lambda config, sqlite_factory, **kwargs: SimpleNamespace(config=config, register_tool=lambda _: None))

    settings = _settings(
        agent_plugins={
            "memory": AgentPluginSettings(
                enabled=True,
                config={
                    "enable_diary": False,
                    "enable_episodic": False,
                    "enable_semantic": False,
                },
            )
        }
    )

    from app.crud.chat_settings_dao import ChatSettingsDao
    from app.services.chat_settings_service import ChatSettingsService
    config = agent_factory.build_agent(ChatSettingsService(ChatSettingsDao()).normalize(settings), sqlite_factory=SqliteFactory(), repl_service=repl_service).config

    assert config.plugins[0]["config"] == {
        "enable_diary": False,
        "enable_episodic": False,
        "enable_semantic": False,
        "long_term_memory_every_human_messages": 10,
    }
