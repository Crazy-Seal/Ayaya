import asyncio
from dataclasses import FrozenInstanceError

import pytest

from app.agent.context import BasePlugin, HookContext, PluginHook
from app.agent.agent import Agent, AgentConfig
from app.agent.context_strategy import ContextStrategyConfig
from app.agent.core.event_router import EventType
from app.agent.core.plugin_manager import PluginManager
from app.agent.models.llm_client import StreamChunk
from app.agent.state import AgentState


def test_hook_context_keeps_bindings_fixed_but_allows_in_place_changes() -> None:
    state = AgentState.create_new("plugin-contract")
    data = {}
    context = HookContext.create(PluginHook.BEFORE_LLM, state, data)

    context.agent_state.add_user_message("原地修改")
    context.data["prepared"] = True
    context.metadata["visited"] = True

    assert context.agent_state is state
    assert state.messages[-1]["content"] == "原地修改"
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
    name = "replacing"

    @property
    def hooks(self) -> list[PluginHook]:
        return list(PluginHook)

    async def execute(self, context: HookContext) -> None:
        setattr(context, "agent_state", context.agent_state.model_copy(deep=True))


class _MutatingPlugin(BasePlugin):
    def __init__(self, name: str) -> None:
        self.name = name
        self.seen_states: list[AgentState] = []

    @property
    def hooks(self) -> list[PluginHook]:
        return list(PluginHook)

    async def execute(self, context: HookContext) -> None:
        self.seen_states.append(context.agent_state)
        context.agent_state.extra[self.name] = True
        if isinstance(context.data, dict):
            context.data[self.name] = True


@pytest.mark.parametrize("hook", list(PluginHook))
def test_hooks_continue_on_original_state_after_rejected_replacement(hook, caplog) -> None:
    async def scenario() -> None:
        manager = PluginManager()
        state = AgentState.create_new("plugin-contract")
        data = {}
        first = _MutatingPlugin("first")
        last = _MutatingPlugin("last")
        for plugin in (first, _ReplacingPlugin(), last):
            await manager.register(plugin)
        try:
            result = await manager.run_hooks(hook, state, data)
            assert first.seen_states[0] is state
            assert last.seen_states[0] is state
            assert state.extra == {"first": True, "last": True}
            assert data == {"first": True, "last": True}
            assert result is None
        finally:
            await manager.close()

    asyncio.run(scenario())
    assert "replacing" in caplog.text
    assert "agent_state" in caplog.text


def test_agent_commits_response_and_in_place_changes_after_rejected_replacement(tmp_path) -> None:
    class ScriptedLLM:
        async def astream(self, messages, tools=None):
            yield StreamChunk(content="已保存的回复")

        async def close(self) -> None:
            pass

    async def scenario() -> None:
        agent = Agent(
            AgentConfig(
                session_id="plugin-contract", model_name="test", api_key="test",
                context_strategy=ContextStrategyConfig(),
            ),
            db_path=str(tmp_path / "checkpoints.sqlite3"),
            llm_client=ScriptedLLM(),
        )
        plugin = _MutatingPlugin("updated")
        await agent.register_plugin(_ReplacingPlugin())
        await agent.register_plugin(plugin)
        try:
            events = [event async for event in agent.run("开始")]
            saved = await agent.state_manager.load()
            assert events[-1].type == EventType.DONE
            assert [event.data for event in events if event.type == EventType.TEXT_CHUNK] == [
                "已保存的回复"
            ]
            assert [(message["role"], message["content"]) for message in saved.messages] == [
                ("user", "开始"),
                ("assistant", "已保存的回复"),
            ]
            assert saved.extra["updated"] is True
            assert saved.long_term_memory_turn_counter == 1
            assert all(state is plugin.seen_states[0] for state in plugin.seen_states)
        finally:
            await agent.close()

    asyncio.run(scenario())
