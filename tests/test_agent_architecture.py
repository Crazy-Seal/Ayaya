from tests.model_capture import capture_input
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
    """提供按用例脚本输出的模型响应，验证流式处理、摘要或失败恢复，不调用真实模型。"""
    def __init__(self, scripts: list[list[StreamChunk]] | None = None) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        Args:
            scripts (list[list[StreamChunk]] | None): 按模型调用顺序准备的响应或片段列表。
        """
        self.scripts = list(scripts or [[StreamChunk(content="完成")]])
        self.closed = False

    async def astream(self, messages, tools=None):
        """模型管道调用时按测试脚本顺序产出片段或异常，供外层检查消息和用量处理。

        Args:
            messages (list[dict]): 按对话顺序排列的消息集合。
            tools (list[dict] | None): 当前向模型公开的工具定义。
        """
        messages = capture_input(messages)
        for chunk in self.scripts.pop(0):
            yield chunk

    async def close(self) -> None:
        """接收测试对象的关闭请求，不释放任何生产资源。"""
        self.closed = True


class _NoopPlugin(BasePlugin):
    """提供用例控制的插件回调，验证执行时机和状态修改约束。"""
    name = "noop"

    @property
    def hooks(self) -> list[PluginHook]:
        """向插件管理器提供当前用例需要监听的执行时机。

        Returns:
            list[PluginHook]: 当前用例预设的回调时机或工具目录。
        """
        return []

    async def execute(self, context: HookContext) -> None:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            context (HookContext): 执行管道提供的工具运行上下文。
        """
        pass


class _RecordingPlugin(BasePlugin):
    """提供用例控制的插件回调，验证执行时机和状态修改约束。"""
    name = "recording"

    def __init__(self) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。"""
        self.calls: list[PluginHook] = []

    @property
    def hooks(self) -> list[PluginHook]:
        """向插件管理器提供当前用例需要监听的执行时机。

        Returns:
            list[PluginHook]: 当前用例预设的回调时机或工具目录。
        """
        return [PluginHook.BEFORE_LLM]

    async def execute(self, context: HookContext) -> None:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            context (HookContext): 执行管道提供的工具运行上下文。
        """
        self.calls.append(context.hook)


class _DynamicTool(BaseTool):
    """提供用例指定的工具响应、中断或异常，验证执行管道的结果保存与恢复。"""
    name = "dynamic_tool"
    description = "验证 Pipeline 构造后的动态工具注册"
    parameters_schema = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。"""
        self.executed = False

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            args (dict): 当前工具调用或被包装函数的参数。
            context (ToolContext): 执行管道提供的工具运行上下文。

        Returns:
            ToolResult: 供外层用例断言使用的预设结果。
        """
        self.executed = True
        return ToolResult.success("动态工具完成")


def _config() -> AgentConfig:
    """构造当前架构测试使用的最小 Agent 配置。

    Returns:
        AgentConfig: 供外层用例断言使用的预设结果。
    """
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
    """装配显式测试依赖的执行管道，不调用真实模型。

    Args:
        llm (_ScriptedLLM): 当前用例提供的脚本模型客户端。
        accounting_key (str): 用例指定的模型计量标识。

    Returns:
        ExecutionPipeline: 供外层用例断言使用的预设结果。
    """
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
    """验证管道使用显式依赖且不反向引用 Agent。"""
    async def scenario() -> list:
        """由外层测试调用，完成“管道使用显式依赖且不反向引用 Agent”的异步步骤。

        Returns:
            list: 供外层用例断言使用的预设结果。
        """
        pipeline = _pipeline(_ScriptedLLM())
        state = AgentState.create_new("architecture-test")
        state.add_user_message("开始")
        return [event async for event in pipeline.execute(state)]

    events = asyncio.run(scenario())

    assert all(event.type != EventType.DONE for event in events)
    assert "agent" not in vars(_pipeline(_ScriptedLLM()))


def test_pipeline_records_usage_only_after_a_successful_stream() -> None:
    """验证只有模型流成功完成才记录真实用量。"""
    async def scenario(scripts: list[list[StreamChunk]]) -> AgentState:
        """由外层测试调用，完成“只有模型流成功完成才记录真实用量”的异步步骤。

        Args:
            scripts (list[list[StreamChunk]]): 按模型调用顺序准备的响应或片段列表。

        Returns:
            AgentState: 供外层用例断言使用的预设结果。
        """
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
    """验证失败模型流不更新用量基线。"""
    class _FailedAfterUsageLLM(_ScriptedLLM):
        """提供按用例脚本输出的模型响应，验证流式处理、摘要或失败恢复，不调用真实模型。

        该定义仅用于外层用例：验证失败模型流不更新用量基线。
        """
        async def astream(self, messages, tools=None):
            """模型管道调用时按测试脚本顺序产出片段或异常，供外层检查消息和用量处理。

            该定义仅用于外层用例：验证失败模型流不更新用量基线。

            Args:
                messages (list[dict]): 按对话顺序排列的消息集合。
                tools (list[dict] | None): 当前向模型公开的工具定义。
            """
            messages = capture_input(messages)
            yield StreamChunk(
                usage=TokenUsage(prompt_tokens=123, completion_tokens=7, total_tokens=130)
            )
            raise RuntimeError("connection reset")

    async def scenario() -> AgentState:
        """由外层测试调用，完成“失败模型流不更新用量基线”的异步步骤。

        Returns:
            AgentState: 供外层用例断言使用的预设结果。
        """
        pipeline = _pipeline(_FailedAfterUsageLLM())
        state = AgentState.create_new("architecture-test")
        state.add_user_message("开始")
        _ = [event async for event in pipeline.execute(state)]
        return state

    failed = asyncio.run(scenario())

    assert failed.token_baseline is None


def test_pipeline_does_not_record_usage_from_an_empty_response() -> None:
    """验证空模型回复不更新用量基线。"""
    async def scenario() -> tuple[AgentState, list]:
        """由外层测试调用，完成“空模型回复不更新用量基线”的异步步骤。

        Returns:
            tuple[AgentState, list]: 供外层用例断言使用的预设结果。
        """
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
    """验证管道使用显式模型计量标识。"""
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
    """验证保留管道引用不会阻止 Agent 被回收。"""
    async def scenario():
        """由外层测试调用，完成“保留管道引用不会阻止 Agent 被回收”的异步步骤。"""
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
    """验证注册插件不要求或保留 Agent 引用。"""
    async def scenario() -> PluginManager:
        """由外层测试调用，完成“注册插件不要求或保留 Agent 引用”的异步步骤。

        Returns:
            PluginManager: 供外层用例断言使用的预设结果。
        """
        manager = PluginManager()
        await manager.register(_NoopPlugin())
        return manager

    manager = asyncio.run(scenario())

    assert manager.list_plugins() == ["noop"]
    assert "_agent" not in vars(manager)


def test_runtime_registrations_remain_visible_to_constructed_pipeline(tmp_path) -> None:
    """验证管道构造后仍能看到后来注册的工具和插件。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario():
        """由外层测试调用，完成“管道构造后仍能看到后来注册的工具和插件”的异步步骤。"""
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
    """验证Agent 执行复用已加载状态。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> tuple[list, AgentState]:
        """由外层测试调用，完成“Agent 执行复用已加载状态”的异步步骤。

        Returns:
            tuple[list, AgentState]: 供外层用例断言使用的预设结果。
        """
        agent = Agent(
            _config(),
            db_path=str(tmp_path / "architecture.sqlite3"),
            llm_client=_ScriptedLLM(),
        )
        state = AgentState.create_new("architecture-test")

        async def reject_duplicate_load() -> AgentState:
            """如果 Agent 再次加载已提供的状态就主动失败，防止隐藏重复数据库读取。

            该定义仅用于外层用例：验证Agent 执行复用已加载状态。
            """
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
    """验证Agent 恢复复用已加载状态。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> tuple[list, AgentState]:
        """由外层测试调用，完成“Agent 恢复复用已加载状态”的异步步骤。

        Returns:
            tuple[list, AgentState]: 供外层用例断言使用的预设结果。
        """
        agent = Agent(
            _config(),
            db_path=str(tmp_path / "architecture.sqlite3"),
            llm_client=_ScriptedLLM(),
        )
        state = AgentState.create_new("architecture-test")
        state.set_interrupt({"type": "test", "request_id": "request-1"})

        async def reject_duplicate_load() -> AgentState:
            """如果 Agent 再次加载已提供的状态就主动失败，防止隐藏重复数据库读取。

            该定义仅用于外层用例：验证Agent 恢复复用已加载状态。
            """
            raise AssertionError("预加载状态不应再次从 checkpoint 读取")

        async def resume_without_tools(
            resumed_state: AgentState,
            _resume_data: dict,
            checkpoint=None,
            runtime_context=None,
        ):
            """记录恢复时提供的状态，用于确认 Agent 复用已经加载的恢复材料。

            该定义仅用于外层用例：验证Agent 恢复复用已加载状态。

            Args:
                resumed_state (AgentState): 从中断恢复后传入的会话状态。
                _resume_data (dict): 本次模拟审批或桌面回报的数据。
                checkpoint (Any): 测试注入的状态保存回调。
                runtime_context (Any): 当前模型轮次的临时上下文。
            """
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
    """验证Agent 运行模块的导入关系不存在循环。"""
    root = Path(__file__).parents[1] / "app" / "agent"
    modules = {
        ".".join(path.relative_to(root.parent.parent).with_suffix("").parts): path
        for path in root.rglob("*.py")
    }
    graph: dict[str, set[str]] = {module: set() for module in modules}

    def visit(node: ast.AST, imports: list[str]) -> None:
        """检查源文件中的导入节点，收集 Agent 模块依赖。

        该定义仅用于外层用例：验证Agent 运行模块的导入关系不存在循环。

        Args:
            node (ast.AST): 当前递归处理的 JSON Schema 节点。
            imports (list[str]): 待检查模块的导入关系。
        """
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
        """递归检查当前模块导入路径，发现重复路径即报告循环。

        该定义仅用于外层用例：验证Agent 运行模块的导入关系不存在循环。

        Args:
            module (str): 已导入且可能包含插件类的模块。
            trail (tuple[str, ...]): 当前检查中的模块路径，用于发现循环导入。
        """
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
    """验证状态和上下文包仅通过必要接口与管道连接。"""
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
    assert not {
        path for path in expected_modules if not (agent_root / path).is_file()
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
