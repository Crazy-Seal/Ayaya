import ast
import asyncio
import gc
from pathlib import Path
import weakref

from app.agent.agent import Agent, AgentConfig
from app.agent.context import (
    BasePlugin,
    BaseTool,
    HookContext,
    PluginHook,
    ToolContext,
    ToolResult,
)
from app.agent.context_strategy import ContextStrategyConfig, ContextStrategyManager
from app.agent.core.event_router import EventRouter, EventType
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.core.plugin_manager import PluginManager
from app.agent.core.tool_manager import ToolManager
from app.agent.models.llm_client import StreamChunk
from app.agent.message import ToolCall
from app.agent.state import AgentState


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

    async def execute(self, context: HookContext) -> HookContext:
        return context


class _RecordingPlugin(BasePlugin):
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[PluginHook] = []

    @property
    def hooks(self) -> list[PluginHook]:
        return [PluginHook.BEFORE_LLM]

    async def execute(self, context: HookContext) -> HookContext:
        self.calls.append(context.hook)
        return context


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


def _pipeline(llm: _ScriptedLLM) -> ExecutionPipeline:
    config = _config()
    router = EventRouter()
    return ExecutionPipeline(
        prompt_source=config,
        llm_client=llm,
        tool_manager=ToolManager(),
        plugin_manager=PluginManager(),
        context_strategy=ContextStrategyManager(config.context_strategy),
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
