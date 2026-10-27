from pathlib import Path

import pytest
from pydantic import ValidationError

from app.crud.chat_settings_dao import ChatSettingsDao
from app.agent.context import ContextStrategyConfig
from app.schemas.chat_settings import AgentPluginSettings, ChatSettings


def test_skills_survive_round_trip(tmp_path: Path) -> None:
    """验证技能设置经 YAML 保存读取仍完整。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    config_file = tmp_path / "chat_settings.yaml"
    config_file.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(config_file=config_file)
    settings = ChatSettings(
        session_id="test-session",
        model_name="test-model",
        openai_api_key="test-key",
        openai_base_url="http://127.0.0.1:1/v1",
        temperature=0.1,
        system_prompt="test",
        context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        skills=["sample"],
    )

    dao.add_chat_settings(settings)
    reloaded = ChatSettingsDao(config_file=config_file).get_chat_settings("test-session")

    assert reloaded.skills == ["sample"]
    assert reloaded.agent_plugins["memory"].enabled is True


def test_agent_plugins_round_trip_with_long_term_memory_interval(tmp_path: Path) -> None:
    """验证插件设置保留长期记忆提取间隔。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    config_file = tmp_path / "chat_settings.yaml"
    config_file.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(config_file=config_file)
    settings = ChatSettings(
        session_id="test-session",
        model_name="test-model",
        openai_api_key="test-key",
        openai_base_url="http://127.0.0.1:1/v1",
        temperature=0.1,
        system_prompt="test",
        context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        agent_plugins={
            "memory": AgentPluginSettings(
                enabled=True,
                config={
                    "enable_diary": False,
                    "enable_episodic": True,
                    "enable_semantic": True,
                    "long_term_memory_every_human_messages": 12,
                },
            )
        },
        skills=[],
    )

    dao.add_chat_settings(settings)
    reloaded = ChatSettingsDao(config_file=config_file).get_chat_settings("test-session")
    raw_yaml = config_file.read_text(encoding="utf-8")

    assert reloaded.agent_plugins["memory"].config["long_term_memory_every_human_messages"] == 12
    assert reloaded.context_strategy.compression_trigger_tokens_k == 100
    assert "long_term_memory_every_human_messages: 12" in raw_yaml
    assert "memory" + "_plugins" not in raw_yaml


def test_memory_plugin_keeps_explicit_disabled_state() -> None:
    """验证记忆插件保留用户设置的禁用状态和各记忆类别配置。"""
    settings = ChatSettings(
        session_id="test-session",
        model_name="test-model",
        openai_api_key="test-key",
        openai_base_url="http://127.0.0.1:1/v1",
        temperature=0.1,
        system_prompt="test",
        context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        agent_plugins={
            "memory": AgentPluginSettings(
                enabled=False,
                config={
                    "enable_diary": False,
                    "enable_episodic": False,
                    "enable_semantic": False,
                },
            )
        },
    )

    memory = settings.agent_plugins["memory"]
    assert memory.enabled is False
    assert memory.config["enable_diary"] is False
    assert memory.config["enable_episodic"] is False
    assert memory.config["enable_semantic"] is False


def test_agent_plugin_config_is_validated_before_persistence(tmp_path: Path) -> None:
    """验证插件配置在持久保存前经过校验。

    Args:
        tmp_path (Path): Pytest 提供的隔离配置目录。
    """
    from app.services.plugin_service import PluginConfigurationError
    from app.services.chat_settings_service import ChatSettingsService
    path = tmp_path / "settings.yaml"
    path.write_text("chat_models: []\n", encoding="utf-8")
    with pytest.raises(PluginConfigurationError):
        ChatSettingsService(ChatSettingsDao(path)).normalize(ChatSettings(
            session_id="test-session",
            model_name="test-model",
            openai_api_key="test-key",
            openai_base_url="http://127.0.0.1:1/v1",
            temperature=0.1,
            system_prompt="test",
            context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
            agent_plugins={
                "memory": AgentPluginSettings(
                    enabled=True,
                    config={"long_term_memory_every_human_messages": 0},
                ),
            },
        ))


def test_context_strategy_is_required_and_validated() -> None:
    """验证上下文策略必须存在并满足约束。"""
    with pytest.raises(ValidationError):
        ChatSettings(
            session_id="test-session",
            model_name="test-model",
            openai_api_key="test-key",
            openai_base_url="http://127.0.0.1:1/v1",
            temperature=0.1,
            system_prompt="test",
        )

    with pytest.raises(ValidationError):
        ContextStrategyConfig(compression_trigger_tokens_k=9)

    with pytest.raises(ValidationError):
        ContextStrategyConfig(max_screenshots_in_context=-1)

    with pytest.raises(ValidationError):
        ContextStrategyConfig(screenshot_ttl_human_messages=-1)


def test_context_strategy_defaults_to_100k_and_two_screenshots(tmp_path: Path) -> None:
    """验证默认压缩阈值为十万 token 且保留两张截图。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    config_file = tmp_path / "chat_settings.yaml"
    config_file.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(config_file=config_file)
    settings = ChatSettings(
        session_id="test-session",
        model_name="test-model",
        openai_api_key="test-key",
        openai_base_url="http://127.0.0.1:1/v1",
        temperature=0.1,
        system_prompt="test",
        context_strategy=ContextStrategyConfig(),
    )

    dao.add_chat_settings(settings)
    reloaded = ChatSettingsDao(config_file=config_file).get_chat_settings("test-session")

    assert reloaded.context_strategy.compression_trigger_tokens_k == 100
    assert reloaded.context_strategy.max_screenshots_in_context == 2
    assert reloaded.context_strategy.screenshot_ttl_human_messages == 2
    assert "compression_trigger_tokens_k: 100" in config_file.read_text(encoding="utf-8")


def test_legacy_context_strategy_without_screenshot_fields_uses_defaults(
    tmp_path: Path,
) -> None:
    """验证旧上下文配置缺少截图字段时补默认值。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    config_file = tmp_path / "chat_settings.yaml"
    config_file.write_text(
        """chat_models:
- session_id: legacy-session
  model_name: test-model
  openai_api_key: test-key
  openai_base_url: http://127.0.0.1:1/v1
  temperature: 0.1
  system_prompt: test
  tool_permissions: {}
  tool_permissions_schema_version: 1
  context_strategy:
    compression_trigger_tokens_k: 100
  mcp:
    servers: {}
""",
        encoding="utf-8",
    )

    reloaded = ChatSettingsDao(config_file=config_file).get_chat_settings(
        "legacy-session"
    )

    assert reloaded.context_strategy.max_screenshots_in_context == 2
    assert reloaded.context_strategy.screenshot_ttl_human_messages == 2
