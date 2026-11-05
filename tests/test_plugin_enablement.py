"""验证保存的插件开关能控制真实 Agent 的插件装配和钩子执行。"""

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.agent.plugins.base import BasePlugin, HookContext, PluginHook
from app.agent.plugins.registry import PluginRegistry
from app.agent.runtime import agent_factory
from app.agent.state import AgentState
from app.crud.chat_settings_dao import ChatSettingsDao
from app.schemas.chat_settings import ChatSettings


@pytest.mark.parametrize("enabled", [False, True])
def test_saved_plugin_switch_controls_registration_and_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    """普通插件的配置经过保存和读取后，只有开启的插件会注册并执行钩子。

    Args:
        tmp_path (Path): Pytest 创建的隔离目录。
        monkeypatch (pytest.MonkeyPatch): 注入测试插件和记忆依赖。
        enabled (bool): 本轮保存的插件总开关。
    """
    events: list[str] = []

    class SampleConfig(BaseModel):
        """声明测试插件的参数，确认配置模型默认值与用户参数都能传入实例。"""

        label: str = "默认参数"
        interval: int = 3

    class SamplePlugin(BasePlugin):
        """记录真实插件装配、执行和注销过程，不访问外部程序。"""

        name = "sample_enablement"
        config_model = SampleConfig

        def __init__(self, **config) -> None:
            """保存框架传入的插件配置。

            Args:
                **config (dict): 经 SampleConfig 校验的插件参数。
            """
            self.config = SampleConfig(**config)

        @property
        def hooks(self) -> list[PluginHook]:
            """订阅每次主模型请求前的回调。

            Returns:
                list[PluginHook]: 本用例验证的模型请求前钩子。
            """
            return [PluginHook.BEFORE_LLM]

        async def on_register(self) -> None:
            """登记装配事件，确认禁用插件没有产生初始化行为。"""
            events.append("register")

        async def execute(self, context: HookContext) -> None:
            """将真实装配参数写入临时背景，供外层确认钩子确实执行。

            Args:
                context (HookContext): PluginManager 组装的真实钩子上下文。
            """
            events.append(self.config.label)
            context.agent_state.set_extra("sample.interval", self.config.interval)

        async def on_unregister(self) -> None:
            """记录 Agent 关闭时调用的插件清理。"""
            events.append("unregister")

    monkeypatch.setitem(PluginRegistry._plugins, SamplePlugin.name, SamplePlugin)
    config_file = tmp_path / "chat_settings.yaml"
    config_file.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(config_file)
    dao.add_chat_settings(ChatSettings(
        session_id="plugin-switch", model_name="test", openai_api_key="test",
        openai_base_url="http://127.0.0.1:1/v1", temperature=0.1,
        system_prompt="测试", context_strategy={},
        agent_plugins={
            "memory": {"enabled": False},
            SamplePlugin.name: {"enabled": enabled, "config": {"label": "用户参数"}},
        },
    ))
    reloaded = ChatSettingsDao(config_file).get_chat_settings("plugin-switch")
    assert reloaded.agent_plugins["memory"].enabled is False
    assert reloaded.agent_plugins[SamplePlugin.name].enabled is enabled
    # 1. DAO 只还原保存内容，默认参数由工厂共用的应用校验入口补齐。
    assert reloaded.agent_plugins[SamplePlugin.name].config == {"label": "用户参数"}

    async def scenario() -> None:
        """使用生产工厂创建 Agent，观察真实注册和钩子分发。"""
        agent = agent_factory.build_agent(reloaded)
        state = AgentState.create_new(reloaded.session_id)
        try:
            await agent.initialize()
            assert agent.get_plugins() == ([SamplePlugin.name] if enabled else [])
            await agent.plugin_manager.run_hooks(PluginHook.BEFORE_LLM, state)
            assert state.get_extra("sample.interval") == (3 if enabled else None)
        finally:
            await agent.close()

    asyncio.run(scenario())
    assert events == (["register", "用户参数", "unregister"] if enabled else [])
