from tests.runtime_support import collect_agent_events
from app.crud.sqlite_factory import SqliteFactory
from tests.model_capture import capture_input
import asyncio
from dataclasses import FrozenInstanceError

import pytest

from app.agent.plugins.base import BasePlugin, HookContext, PluginHook
from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.core.events import EventType
from app.agent.core.plugin_manager import PluginManager
from app.agent.models.llm_client import StreamChunk
from app.agent.state import AgentState
from app.agent.state.messages import AssistantMessage, HumanMessage


def test_hook_context_keeps_bindings_fixed_but_allows_in_place_changes() -> None:
    """验证钩子上下文不允许替换绑定但允许修改原状态。"""
    state = AgentState.create_new("plugin-contract")
    data = {}
    context = HookContext.create(PluginHook.BEFORE_LLM, state, data)

    context.agent_state.add_user_message("原地修改")
    context.data["prepared"] = True
    context.metadata["visited"] = True

    assert context.agent_state is state
    assert state.messages[-1].text == "原地修改"
    assert data == {"prepared": True}
    assert context.metadata == {"visited": True}
    for field, replacement in (
        ("agent_state", state.model_copy(deep=True)),
        ("data", {}),
        ("metadata", {}),
        ("hook", PluginHook.AFTER_LLM),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(context, field, replacement)


class _ReplacingPlugin(BasePlugin):
    """提供用例控制的插件回调，验证执行时机和状态修改约束。"""
    name = "replacing"

    @property
    def hooks(self) -> list[PluginHook]:
        """向插件管理器提供当前用例需要监听的执行时机。

        Returns:
            list[PluginHook]: 当前用例预设的回调时机或工具目录。
        """
        return list(PluginHook)

    async def execute(self, context: HookContext) -> None:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            context (HookContext): 执行管道提供的工具运行上下文。
        """
        setattr(context, "agent_state", context.agent_state.model_copy(deep=True))


class _MutatingPlugin(BasePlugin):
    """提供用例控制的插件回调，验证执行时机和状态修改约束。"""
    def __init__(self, name: str) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        Args:
            name (str): 当前对象的名称。
        """
        self.name = name
        self.seen_states: list[AgentState] = []
        self.seen_contexts: list[HookContext] = []

    @property
    def hooks(self) -> list[PluginHook]:
        """向插件管理器提供当前用例需要监听的执行时机。

        Returns:
            list[PluginHook]: 当前用例预设的回调时机或工具目录。
        """
        return list(PluginHook)

    async def execute(self, context: HookContext) -> None:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            context (HookContext): 执行管道提供的工具运行上下文。
        """
        self.seen_states.append(context.agent_state)
        self.seen_contexts.append(context)
        context.agent_state.extra[self.name] = True
        if isinstance(context.data, dict):
            context.data[self.name] = True


def test_replacing_and_unregistering_plugins_updates_hook_order() -> None:
    """同步登记保留优先级，同名替换与注销会移除旧实例的所有钩子。"""
    async def scenario() -> None:
        """执行真实钩子，核对替换、注销和清空后的调用顺序。"""
        events: list[str] = []

        class RecordingPlugin(_MutatingPlugin):
            """在各个业务时机记录当前实例的调用。"""

            async def execute(self, context: HookContext) -> None:
                """将本次执行的插件名称写入记录。

                Args:
                    context (HookContext): 本次触发的业务钩子上下文。
                """
                events.append(self.name)

        manager = PluginManager()
        old = RecordingPlugin("replace")
        old.priority = 1
        first = RecordingPlugin("first")
        first.priority = 10
        replacement = RecordingPlugin("replace")
        replacement.priority = 20
        last = RecordingPlugin("last")
        last.priority = 30
        for plugin in (old, last, first, replacement):
            manager.register(plugin)
        assert manager.get_plugin("replace") is replacement
        state = AgentState.create_new("plugin-order")
        for hook in PluginHook:
            await manager.run_hooks(hook, state)
        assert events == ["first", "replace", "last"] * len(PluginHook)

        assert manager.unregister("replace") is True
        assert manager.unregister("replace") is False
        assert manager.get_plugin("replace") is None
        events.clear()
        for hook in PluginHook:
            await manager.run_hooks(hook, state)
        assert events == ["first", "last"] * len(PluginHook)

        manager.clear()
        events.clear()
        assert manager.list_plugins() == []
        for hook in PluginHook:
            await manager.run_hooks(hook, state)
        assert events == []

    asyncio.run(scenario())


@pytest.mark.parametrize("provide_metadata", [False, True])
def test_plugins_receive_separate_contexts_with_shared_hook_data(provide_metadata: bool) -> None:
    """每个插件取得独立上下文，同时保留同次钩子的状态和准备数据共享。

    Args:
        provide_metadata (bool): 是否由调用方传入需要共享的空 metadata 字典。
    """
    async def scenario() -> None:
        """通过真实插件分发验证上下文身份及公开字段的共享关系。"""
        manager = PluginManager()
        state = AgentState.create_new("separate-plugin-contexts")
        data = {}
        metadata = {} if provide_metadata else None
        first = _MutatingPlugin("first")
        last = _MutatingPlugin("last")
        for plugin in (first, last):
            manager.register(plugin)
        try:
            # 1. 分发同次钩子，两个插件仍按顺序原地补充同一份准备数据。
            await manager.run_hooks(PluginHook.BEFORE_LLM, state, data, metadata)
            first_context = first.seen_contexts[0]
            last_context = last.seen_contexts[0]
            assert first_context is not last_context
            assert first_context.agent_state is last_context.agent_state is state
            assert first_context.data is last_context.data is data
            assert data == {"first": True, "last": True}
            # 2. 各插件继续共享本次附加信息，调用方提供空字典时也保留其原对象。
            assert first_context.metadata is last_context.metadata
            first_context.metadata["prepared"] = True
            assert last_context.metadata == {"prepared": True}
            if provide_metadata:
                assert first_context.metadata is metadata
                assert metadata == {"prepared": True}
        finally:
            manager.clear()

    asyncio.run(scenario())


@pytest.mark.parametrize("hook", list(PluginHook))
def test_hooks_continue_on_original_state_after_rejected_replacement(hook, caplog) -> None:
    """验证拒绝状态替换后后续钩子仍使用原状态。

    Args:
        hook (Any): 当前触发的插件执行时机。
        caplog (pytest.LogCaptureFixture): Pytest 收集的日志，供检查级别、诊断和敏感内容是否泄漏。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“拒绝状态替换后后续钩子仍使用原状态”的异步步骤。"""
        manager = PluginManager()
        state = AgentState.create_new("plugin-contract")
        data = {}
        first = _MutatingPlugin("first")
        last = _MutatingPlugin("last")
        for plugin in (first, _ReplacingPlugin(), last):
            manager.register(plugin)
        try:
            result = await manager.run_hooks(hook, state, data)
            assert first.seen_states[0] is state
            assert last.seen_states[0] is state
            assert state.extra == {"first": True, "last": True}
            assert data == {"first": True, "last": True}
            assert result is None
        finally:
            manager.clear()

    asyncio.run(scenario())
    assert "replacing" in caplog.text
    assert "agent_state" in caplog.text


def test_agent_commits_response_and_in_place_changes_after_rejected_replacement(tmp_path) -> None:
    """验证拒绝替换后 Agent 仍提交回复和允许的原地修改。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    class ScriptedLLM:
        """提供按用例脚本输出的模型响应，验证流式处理、摘要或失败恢复，不调用真实模型。

        该定义仅用于外层用例：验证拒绝替换后 Agent 仍提交回复和允许的原地修改。
        """
        async def astream(self, messages, tools=None):
            """模型管道调用时按测试脚本顺序产出片段或异常，供外层检查消息和用量处理。

            该定义仅用于外层用例：验证拒绝替换后 Agent 仍提交回复和允许的原地修改。

            Args:
                messages (list[dict]): 按对话顺序排列的消息集合。
                tools (list[dict] | None): 当前向模型公开的工具定义。
            """
            messages = capture_input(messages)
            yield StreamChunk(content="已保存的回复")

        async def close(self) -> None:
            """接收测试对象的关闭请求，不释放任何生产资源。

            该定义仅用于外层用例：验证拒绝替换后 Agent 仍提交回复和允许的原地修改。
            """
            pass

    async def scenario() -> None:
        """由外层测试调用，完成“拒绝替换后 Agent 仍提交回复和允许的原地修改”的异步步骤。"""
        agent = Agent(
            AgentConfig(
                session_id="plugin-contract", model_name="test", api_key="test",
                context_strategy=ContextStrategyConfig(),
            ),
            sqlite_factory=SqliteFactory(str(tmp_path / "checkpoints.sqlite3")),
            llm_client=ScriptedLLM(),
        )
        plugin = _MutatingPlugin("updated")
        agent.register_plugin(_ReplacingPlugin())
        agent.register_plugin(plugin)
        try:
            events = [event for event in await collect_agent_events(agent.run, '开始')]
            saved = await agent.session_state_manager.load()
            assert events[-1].type == EventType.DONE
            assert [event.data for event in events if event.type == EventType.TEXT_CHUNK] == [
                "已保存的回复"
            ]
            assert isinstance(saved.messages[0], HumanMessage)
            assert isinstance(saved.messages[1], AssistantMessage)
            assert [message.text for message in saved.messages] == ["开始", "已保存的回复"]
            assert saved.extra["updated"] is True
            assert saved.long_term_memory_turn_counter == 1
            assert all(state is plugin.seen_states[0] for state in plugin.seen_states)
        finally:
            await agent.close()

    asyncio.run(scenario())
