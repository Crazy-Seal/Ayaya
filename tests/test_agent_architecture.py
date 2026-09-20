import ast
import asyncio
import gc
import inspect
from pathlib import Path
from types import SimpleNamespace
import weakref

from app.agent.agent import Agent, AgentConfig
from app.agent.context import (
    ContextStrategyConfig,
    ModelInputCoordinator,
    build_token_accounting_key,
)
from app.agent.core.event_router import AgentEvent, EventRouter, EventType
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.core.plugin_manager import PluginManager
from app.agent.core.tool_manager import ToolManager
from app.agent.models.llm_client import StreamChunk, TokenUsage
from app.agent.plugins.base import BasePlugin, HookContext, PluginHook
from app.agent.state import AgentState
from app.agent.state.messages import ToolCall
from app.agent.tools.base import BaseTool, ToolContext, ToolResult


class _ScriptedLLM:
    def __init__(self, scripts: list[list[StreamChunk]] | None = None) -> None:
        self.scripts = list(scripts or [[StreamChunk(content="完成")]])
        self.closed = False

    async def astream(self, messages, tools=None):
        for chunk in self.scripts.pop(0):
            yield chunk

    async def close(self) -> None:
        self.closed = True


class _NoopPlugin(BasePlugin):
    name = "noop"

    @property
    def hooks(self) -> list[PluginHook]:
        return []

    async def execute(self, context: HookContext) -> None:
        pass


class _RecordingPlugin(BasePlugin):
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[PluginHook] = []

    @property
    def hooks(self) -> list[PluginHook]:
        return [PluginHook.BEFORE_LLM]

    async def execute(self, context: HookContext) -> None:
        self.calls.append(context.hook)


class _DynamicTool(BaseTool):
    name = "dynamic_tool"
    description = "验证 Pipeline 构造后的动态工具注册"
    parameters_schema = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.executed = False

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        self.executed = True
        return ToolResult.success("动态工具完成")


def _config() -> AgentConfig:
    return AgentConfig(
        session_id="architecture-test",
        model_name="test",
        api_key="test",
        context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        system_prompt="system",
    )


def _pipeline(
    llm: _ScriptedLLM,
    *,
    accounting_key: str = "architecture-test",
) -> ExecutionPipeline:
    config = _config()
    router = EventRouter()
    return ExecutionPipeline(
        prompt_source=config,
        llm_client=llm,
        tool_manager=ToolManager(),
        plugin_manager=PluginManager(),
        model_input=ModelInputCoordinator(
            config.context_strategy,
            llm,
            accounting_key=accounting_key,
        ),
        emit_event=router.emit,
    )


def test_pipeline_uses_explicit_dependencies_without_agent_back_reference() -> None:
    async def scenario() -> list:
        pipeline = _pipeline(_ScriptedLLM())
        state = AgentState.create_new("architecture-test")
        state.add_user_message("开始")
        return [event async for event in pipeline.execute(state)]

    events = asyncio.run(scenario())

    assert all(event.type != EventType.DONE for event in events)
    assert "agent" not in vars(_pipeline(_ScriptedLLM()))


def test_pipeline_records_usage_only_after_a_successful_stream() -> None:
    async def scenario(scripts: list[list[StreamChunk]]) -> AgentState:
        pipeline = _pipeline(_ScriptedLLM(scripts))
        state = AgentState.create_new("architecture-test")
        state.add_user_message("开始")
        _ = [event async for event in pipeline.execute(state)]
        return state

    successful = asyncio.run(scenario([[
        StreamChunk(content="完成"),
        StreamChunk(usage=TokenUsage(prompt_tokens=123, completion_tokens=7, total_tokens=130)),
    ]]))

    assert successful.token_baseline is not None
    assert successful.token_baseline.actual_prompt_tokens == 123


def test_pipeline_does_not_record_usage_from_a_failed_stream() -> None:
    class _FailedAfterUsageLLM(_ScriptedLLM):
        async def astream(self, messages, tools=None):
            yield StreamChunk(
                usage=TokenUsage(prompt_tokens=123, completion_tokens=7, total_tokens=130)
            )
            raise RuntimeError("connection reset")

    async def scenario() -> AgentState:
        pipeline = _pipeline(_FailedAfterUsageLLM())
        state = AgentState.create_new("architecture-test")
        state.add_user_message("开始")
        _ = [event async for event in pipeline.execute(state)]
        return state

    failed = asyncio.run(scenario())

    assert failed.token_baseline is None


def test_pipeline_does_not_record_usage_from_an_empty_response() -> None:
    async def scenario() -> tuple[AgentState, list]:
        pipeline = _pipeline(_ScriptedLLM([[
            StreamChunk(
                usage=TokenUsage(prompt_tokens=123, completion_tokens=0, total_tokens=123)
            ),
        ]]))
        state = AgentState.create_new("architecture-test")
        state.add_user_message("开始")
        events = [event async for event in pipeline.execute(state)]
        return state, events

    state, events = asyncio.run(scenario())

    assert state.token_baseline is None
    assert any(event.type == EventType.ERROR for event in events)


def test_pipeline_uses_explicit_accounting_key() -> None:
    llm = _ScriptedLLM()
    llm.config = SimpleNamespace(
        model="actual-model",
        base_url="https://actual.example/v1/",
    )
    accounting_key = build_token_accounting_key(
        protocol="openai_chat_completions",
        model=llm.config.model,
        endpoint=llm.config.base_url,
    )

    pipeline = _pipeline(llm, accounting_key=accounting_key)

    assert pipeline._model_input.accounting_key == accounting_key


def test_retained_pipeline_does_not_keep_agent_alive() -> None:
    async def scenario():
        llm = _ScriptedLLM()
        agent = Agent(_config(), llm_client=llm)
        retained_pipeline = agent.pipeline
        reference = weakref.ref(agent)
        await agent.close()
        del agent
        gc.collect()
        return reference, retained_pipeline, llm

    reference, retained_pipeline, llm = asyncio.run(scenario())

    assert retained_pipeline is not None
    assert reference() is None
    assert llm.closed is True


def test_plugin_registration_does_not_require_or_retain_agent() -> None:
    async def scenario() -> PluginManager:
        manager = PluginManager()
        await manager.register(_NoopPlugin())
        return manager

    manager = asyncio.run(scenario())

    assert manager.list_plugins() == ["noop"]
    assert "_agent" not in vars(manager)


def test_runtime_registrations_remain_visible_to_constructed_pipeline(tmp_path) -> None:
    async def scenario():
        llm = _ScriptedLLM([
            [StreamChunk(tool_call=ToolCall(id="dynamic-1", name="dynamic_tool", args={}))],
            [StreamChunk(content="执行完成")],
        ])
        agent = Agent(
            _config(),
            db_path=str(tmp_path / "architecture.sqlite3"),
            llm_client=llm,
        )
        tool = _DynamicTool()
        plugin = _RecordingPlugin()
        agent.register_tool(tool)
        await agent.register_plugin(plugin)
        try:
            events = [event async for event in agent.run("开始")]
            return events, tool, plugin
        finally:
            await agent.close()

    events, tool, plugin = asyncio.run(scenario())

    assert tool.executed is True
    assert plugin.calls == [PluginHook.BEFORE_LLM, PluginHook.BEFORE_LLM]
    assert events[-1].type == EventType.DONE


def test_agent_run_uses_preloaded_state_without_loading_again(tmp_path) -> None:
    async def scenario() -> tuple[list, AgentState]:
        agent = Agent(
            _config(),
            db_path=str(tmp_path / "architecture.sqlite3"),
            llm_client=_ScriptedLLM(),
        )
        state = AgentState.create_new("architecture-test")

        async def reject_duplicate_load() -> AgentState:
            raise AssertionError("预加载状态不应再次从 checkpoint 读取")

        agent.state_manager.load = reject_duplicate_load  # type: ignore[method-assign]
        try:
            events = [
                event
                async for event in agent.run("开始", initial_state=state)
            ]
            return events, state
        finally:
            await agent.close()

    events, state = asyncio.run(scenario())

    assert events[-1].type == EventType.DONE
    assert state.messages[0].text == "开始"


def test_agent_resume_uses_preloaded_state_without_loading_again(tmp_path) -> None:
    async def scenario() -> tuple[list, AgentState]:
        agent = Agent(
            _config(),
            db_path=str(tmp_path / "architecture.sqlite3"),
            llm_client=_ScriptedLLM(),
        )
        state = AgentState.create_new("architecture-test")
        state.set_interrupt({"type": "test", "request_id": "request-1"})

        async def reject_duplicate_load() -> AgentState:
            raise AssertionError("预加载状态不应再次从 checkpoint 读取")

        async def resume_without_tools(
            resumed_state: AgentState,
            _resume_data: dict,
            checkpoint=None,
            runtime_context=None,
        ):
            resumed_state.clear_interrupt()
            yield AgentEvent(EventType.TEXT_CHUNK, "继续")

        agent.state_manager.load = reject_duplicate_load  # type: ignore[method-assign]
        agent.pipeline.resume_tools = resume_without_tools  # type: ignore[method-assign]
        try:
            events = [
                event
                async for event in agent.resume(
                    {"approved": True},
                    initial_state=state,
                )
            ]
            return events, state
        finally:
            await agent.close()

    events, state = asyncio.run(scenario())

    assert [event.type for event in events] == [EventType.TEXT_CHUNK, EventType.DONE]
    assert state.interrupt_data is None


def test_agent_runtime_import_graph_is_acyclic() -> None:
    root = Path(__file__).parents[1] / "app" / "agent"
    modules = {
        ".".join(path.relative_to(root.parent.parent).with_suffix("").parts): path
        for path in root.rglob("*.py")
    }
    graph: dict[str, set[str]] = {module: set() for module in modules}

    def visit(node: ast.AST, imports: list[str]) -> None:
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            for child in node.orelse:
                visit(child, imports)
            return
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
        for child in ast.iter_child_nodes(node):
            visit(child, imports)

    for module, path in modules.items():
        imports: list[str] = []
        visit(ast.parse(path.read_text(encoding="utf-8-sig")), imports)
        for imported in imports:
            graph[module].update(
                target
                for target in modules
                if imported == target or imported.startswith(f"{target}.")
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def assert_acyclic(module: str, trail: tuple[str, ...]) -> None:
        if module in visiting:
            cycle = " -> ".join((*trail, module))
            raise AssertionError(f"检测到运行期循环导入: {cycle}")
        if module in visited:
            return
        visiting.add(module)
        for dependency in graph[module]:
            assert_acyclic(dependency, (*trail, module))
        visiting.remove(module)
        visited.add(module)

    for module in graph:
        assert_acyclic(module, ())


def test_state_and_context_are_deep_packages_with_a_narrow_pipeline_seam() -> None:
    agent_root = Path(__file__).parents[1] / "app" / "agent"
    expected_modules = {
        "state/model.py",
        "state/messages.py",
        "state/time.py",
        "state/manager.py",
        "context/config.py",
        "context/runtime.py",
        "context/coordinator.py",
        "context/ports.py",
        "context/_budget.py",
        "context/_projection.py",
        "context/_compaction.py",
        "core/response_filter.py",
        "utils/domain/weighted_text.py",
    }
    retired_modules = {
        "context.py",
        "context_strategy.py",
        "message.py",
        "message_names.py",
        "message_time.py",
        "model_input.py",
        "state.py",
        "tool_result_normalizer.py",
        "tool_schema_compat.py",
        "core/state_manager.py",
    }

    assert not {
        path for path in expected_modules if not (agent_root / path).is_file()
    }
    assert not {
        path for path in retired_modules if (agent_root / path).exists()
    }

    parameters = inspect.signature(ExecutionPipeline.__init__).parameters
    assert "model_input" in parameters
    assert {
        "context_strategy",
        "accounting_key",
        "diary_coverage",
        "chat_settings",
    }.isdisjoint(parameters)

    for relative_path in ("context/_compaction.py", "memory/manager.py"):
        source = (agent_root / relative_path).read_text(encoding="utf-8")
        assert "app.agent.tools.result_normalizer" not in source
