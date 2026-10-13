from app.agent.models.openai_message_names import MCP_TOOL_IMAGE_MESSAGE_NAME, TOOL_IMAGE_MESSAGE_NAME
from tests.model_capture import result_parts
from tests.model_capture import capture_input
import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from app.agent.agent import Agent, AgentConfig, INTERRUPTED_TOOL_RESULT
from app.agent.runtime.turn_control import TurnControl
from app.agent.context import ContextStrategyConfig, ModelInputCoordinator
from app.agent.core.event_router import EventType
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.plugins.base import BasePlugin, HookContext, PluginHook
from app.agent.plugins.memory import MemoryPlugin
from app.agent.state.manager import CheckpointType, StateManager
from app.agent.state.messages import (AssistantMessage, HumanMessage, ImageContent, ToolMessage, ToolCall, ToolMessage)
from app.agent.models.llm_client import StreamChunk
from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolContext, ToolResult
import app.agent.plugins.memory as memory_plugin_module


class ScriptedLLM:
    """按调用顺序回放流式结果或异常。"""

    def __init__(self, scripts: list[list[StreamChunk] | BaseException]):
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        该定义仅用于外层用例：按调用顺序回放流式结果或异常。

        Args:
            scripts (list[list[StreamChunk] | BaseException]): 按模型调用顺序准备的响应或片段列表。
        """
        self.scripts = list(scripts)
        self.closed = False

    async def astream(self, messages, tools=None):
        """模型管道调用时按测试脚本顺序产出片段或异常，供外层检查消息和用量处理。

        该定义仅用于外层用例：按调用顺序回放流式结果或异常。

        Args:
            messages (list[dict]): 按对话顺序排列的消息集合。
            tools (list[dict] | None): 当前向模型公开的工具定义。
        """
        messages = capture_input(messages)
        script = self.scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        for chunk in script:
            yield chunk

    async def close(self) -> None:
        """接收测试对象的关闭请求，不释放任何生产资源。

        该定义仅用于外层用例：按调用顺序回放流式结果或异常。
        """
        self.closed = True


class StepTool(BaseTool):
    """提供用例指定的工具响应、中断或异常，验证执行管道的结果保存与恢复。"""
    name = "step"
    description = "执行测试步骤"
    parameters_schema = {"type": "object", "properties": {"label": {"type": "string"}}}

    def __init__(self, crash_on: str | None = None):
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        Args:
            crash_on (str | None): 需要主动制造失败的执行步骤。
        """
        self.crash_on = crash_on
        self.executed: list[str] = []

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            args (dict): 当前工具调用或被包装函数的参数。
            context (ToolContext): 执行管道提供的工具运行上下文。

        Returns:
            ToolResult: 供外层用例断言使用的预设结果。
        """
        label = args.get("label", "")
        self.executed.append(label)
        if label == self.crash_on:
            raise asyncio.CancelledError()
        return ToolResult.success(f"完成 {label}")


class ImageStepTool(BaseTool):
    """返回图片的测试工具。"""

    name = "image_step"
    description = "执行返回图片的测试步骤"
    parameters_schema = {"type": "object", "properties": {"label": {"type": "string"}}}
    is_resumable = True

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        该定义仅用于外层用例：返回图片的测试工具。

        Args:
            args (dict): 当前工具调用或被包装函数的参数。
            context (ToolContext): 执行管道提供的工具运行上下文。

        Returns:
            ToolResult: 供外层用例断言使用的预设结果。
        """
        label = args.get("label", "")
        return ToolResult.success(
            f"完成 {label}",
            image_urls=["data:image/png;base64,iVBORw0KGgpwYXlsb2Fk"],
        )


class ErrorTool(BaseTool):
    """提供用例指定的工具响应、中断或异常，验证执行管道的结果保存与恢复。"""
    name = "error_step"
    description = "返回工具错误"
    parameters_schema = {"type": "object", "properties": {}}

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            args (dict): 当前工具调用或被包装函数的参数。
            context (ToolContext): 执行管道提供的工具运行上下文。
        """
        raise RuntimeError("测试工具失败")


class ResumableScreenshotTool(BaseTool):
    """提供用例指定的工具响应、中断或异常，验证执行管道的结果保存与恢复。"""
    name = "screenshot"
    description = "测试截屏中断"
    parameters_schema = {"type": "object", "properties": {}}
    is_resumable = True

    def __init__(self, resumed_calls: list[bool]):
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        Args:
            resumed_calls (list[bool]): 恢复后预期继续执行的工具调用记录。
        """
        self.resumed_calls = resumed_calls

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            args (dict): 当前工具调用或被包装函数的参数。
            context (ToolContext): 执行管道提供的工具运行上下文。

        Returns:
            ToolResult: 供外层用例断言使用的预设结果。
        """
        if context.resume_data is None:
            return ToolResult.needs_input("authorization_request", "允许截屏？")
        self.resumed_calls.append(context.resume_data.get("kind") == "approve")
        return ToolResult.success("截屏恢复完成")


class PhasedInterruptTool(BaseTool):
    """提供用例指定的工具响应、中断或异常，验证执行管道的结果保存与恢复。"""
    name = "phased_interrupt"
    description = "按指定阶段数暂停，验证恢复路径"
    parameters_schema = {"type": "object", "properties": {"stages": {"type": "integer"}}}
    is_resumable = True

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            args (dict): 当前工具调用或被包装函数的参数。
            context (ToolContext): 执行管道提供的工具运行上下文。

        Returns:
            ToolResult: 供外层用例断言使用的预设结果。
        """
        phase = (context.resume_state or {}).get("phase", 0)
        if phase < args["stages"]:
            return ToolResult.needs_input(
                "authorization_request", "允许继续？", resume_state={"phase": phase + 1},
            )
        return ToolResult.success("阶段完成")


class InterruptRecordingPlugin(BasePlugin):
    """提供用例控制的插件回调，验证执行时机和状态修改约束。"""
    name = "interrupt_recording"

    def __init__(self) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。"""
        self.invocations = 0
        self.interrupt_ids: list[str] = []

    @property
    def hooks(self) -> list[PluginHook]:
        """向插件管理器提供当前用例需要监听的执行时机。

        Returns:
            list[PluginHook]: 当前用例预设的回调时机或工具目录。
        """
        return [PluginHook.ON_INVOKE, PluginHook.ON_INTERRUPT]

    async def execute(self, context: HookContext) -> None:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            context (HookContext): 执行管道提供的工具运行上下文。
        """
        if context.hook == PluginHook.ON_INVOKE:
            self.invocations += 1
            return
        interrupt = context.data["interrupt"]
        assert context.agent_state.interrupt_data["request_id"] == interrupt.request_id
        assert not context.agent_state.pending_tool_calls
        self.interrupt_ids.append(interrupt.request_id)
        context.agent_state.extra["interrupt_ids"] = list(self.interrupt_ids)


class FailingInterruptPlugin(BasePlugin):
    """提供用例控制的插件回调，验证执行时机和状态修改约束。"""
    name = "failing_interrupt"
    priority = 90

    @property
    def hooks(self) -> list[PluginHook]:
        """向插件管理器提供当前用例需要监听的执行时机。

        Returns:
            list[PluginHook]: 当前用例预设的回调时机或工具目录。
        """
        return [PluginHook.ON_INTERRUPT]

    async def execute(self, context: HookContext) -> None:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            context (HookContext): 执行管道提供的工具运行上下文。
        """
        context.agent_state.extra["before_plugin_error"] = True
        raise RuntimeError("测试中断插件失败")


@pytest.mark.parametrize("path", ["normal", "same_tool", "remaining_tool"])
@pytest.mark.parametrize("plugin_fails", [False, True])
def test_all_interrupt_paths_save_hook_updates_before_emitting(
    tmp_path: Path, path: str, plugin_fails: bool, caplog,
) -> None:
    """验证各中断路径先保存插件修改再发布中断事件。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
        path (str): 要读取或写入的文件路径。
        plugin_fails (bool): 是否让测试插件主动抛出异常。
        caplog (pytest.LogCaptureFixture): Pytest 收集的日志，供检查级别、诊断和敏感内容是否泄漏。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“各中断路径先保存插件修改再发布中断事件”的异步步骤。"""
        calls = [StreamChunk(tool_call=ToolCall(
            id="first", name="phased_interrupt", args={"stages": 2 if path == "same_tool" else 1},
        )), StreamChunk(tool_call=ToolCall(
            id="second", name="phased_interrupt", args={"stages": 1},
        ))]
        db_path = tmp_path / "interrupts.sqlite3"
        agent = await make_agent(db_path, [calls], PhasedInterruptTool())
        plugin = InterruptRecordingPlugin()
        if plugin_fails:
            await agent.register_plugin(FailingInterruptPlugin())
        await agent.register_plugin(plugin)
        reader = StateManager("checkpoint-test", db_path=str(db_path))

        async def observe(stream, expected_call: str, expected_count: int) -> None:
            """记录本次模型输入用量观察，供用例检查只有成功响应才更新基线。

            该定义仅用于外层用例：验证各中断路径先保存插件修改再发布中断事件。

            Args:
                stream (Any): 需要消费的 Agent 或模型事件流。
                expected_call (str): 参数化用例期望发生的调用。
                expected_count (int): 参数化用例期望记录的次数。
            """
            events = []
            try:
                async for event in stream:
                    events.append(event)
                    if event.type == EventType.INTERRUPT:
                        saved = await reader.load()
                        request_id = event.data["request_id"]
                        assert len(plugin.interrupt_ids) == expected_count
                        assert saved.extra["interrupt_ids"] == plugin.interrupt_ids
                        if plugin_fails:
                            assert saved.extra["before_plugin_error"] is True
                        assert plugin.interrupt_ids[-1] == request_id
                        assert saved.interrupt_data["tool_call_id"] == expected_call
                        assert saved.interrupt_data["request_id"] == request_id
                        assert saved.interrupt_data["resume_state"]["phase"] == (
                            2 if path == "same_tool" and expected_count == 2 else 1
                        )
                        assert [item.id for item in saved.pending_actions] == (
                            ["second"] if expected_call == "first" else []
                        )
                        assert not saved.pending_tool_calls
                        assert saved.long_term_memory_turn_counter == 1
            finally:
                await stream.aclose()
            assert events[-1].type == EventType.INTERRUPT
            assert sum(event.type == EventType.INTERRUPT for event in events) == 1
            assert all(event.type != EventType.DONE for event in events)

        try:
            await observe(agent.run("开始"), "first", 1)
            if path != "normal":
                await observe(
                    agent.resume({"approved": True}),
                    "first" if path == "same_tool" else "second", 2,
                )
            assert plugin.invocations == 1
            assert len(set(plugin.interrupt_ids)) == len(plugin.interrupt_ids)
        finally:
            await reader.close()
            await agent.close()

    asyncio.run(scenario())
    if plugin_fails:
        assert "failing_interrupt" in caplog.text
        assert "测试中断插件失败" in caplog.text


@pytest.mark.parametrize("path", ["normal", "same_tool", "remaining_tool"])
@pytest.mark.parametrize("storage", ["failure", "none"])
def test_interrupt_paths_respect_checkpoint_failure_and_optional_storage(
    tmp_path: Path, path: str, storage: str,
) -> None:
    """验证中断路径正确处理 checkpoint 保存失败和无存储情况。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
        path (str): 要读取或写入的文件路径。
        storage (str): 本用例是否启用 checkpoint 保存。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“中断路径正确处理 checkpoint 保存失败和无存储情况”的异步步骤。"""
        calls = [StreamChunk(tool_call=ToolCall(
            id="first", name="phased_interrupt", args={"stages": 2 if path == "same_tool" else 1},
        )), StreamChunk(tool_call=ToolCall(
            id="second", name="phased_interrupt", args={"stages": 1},
        ))]
        agent = await make_agent(tmp_path / "unused.sqlite3", [calls], PhasedInterruptTool())
        plugin = InterruptRecordingPlugin()
        await agent.register_plugin(plugin)
        state = AgentState.create_new("checkpoint-test")
        state.add_user_message("开始")
        fail = path == "normal"
        failed_saves = []

        async def checkpoint(current: AgentState, *, checkpoint_type: CheckpointType) -> int:
            """记录当前状态保存类型和执行顺序，供用例确认先保存再调用模型或发布事件。

            该定义仅用于外层用例：验证中断路径正确处理 checkpoint 保存失败和无存储情况。

            Args:
                current (AgentState): 回调收到的当前 AgentState。
                checkpoint_type (CheckpointType): 本次保存中间执行进度或完整轮次的类型。

            Returns:
                int: 供外层用例断言使用的预设结果。
            """
            assert checkpoint_type == "intermediate"
            if current.is_interrupted() and fail:
                # 失败发生在钩子之后，但事件尚未发出。
                assert current.extra["interrupt_ids"] == plugin.interrupt_ids
                failed_saves.append(current.interrupt_data["request_id"])
                raise RuntimeError("中断保存失败")
            return 1

        callback = checkpoint if storage == "failure" else None
        stream = agent.pipeline.execute(state, checkpoint=callback)
        events = []
        try:
            if path != "normal":
                initial = await drain(stream)
                assert initial[-1].type == EventType.INTERRUPT
                fail = True
                stream = agent.pipeline.resume_tools(state, {"approved": True}, checkpoint=callback)
            if storage == "failure":
                with pytest.raises(RuntimeError, match="中断保存失败"):
                    async for event in stream:
                        events.append(event)
                assert failed_saves == [plugin.interrupt_ids[-1]]
                assert all(event.type not in {EventType.INTERRUPT, EventType.DONE} for event in events)
            else:
                events = await drain(stream)
                assert events[-1].type == EventType.INTERRUPT
                assert state.extra["interrupt_ids"] == plugin.interrupt_ids
            assert len(plugin.interrupt_ids) == (1 if path == "normal" else 2)
            assert plugin.invocations == 1
        finally:
            await stream.aclose()
            await agent.close()

    asyncio.run(scenario())


async def make_agent(
    db_path: Path,
    scripts: list[list[StreamChunk] | BaseException],
    tool: BaseTool,
    session_id: str = "checkpoint-test",
) -> Agent:
    """将脚本模型、工具和临时存储组装成当前用例的 Agent。

    Args:
        db_path (Path): 当前环境使用的 SQLite 数据库路径。
        scripts (list[list[StreamChunk] | BaseException]): 按模型调用顺序准备的响应或片段列表。
        tool (BaseTool): 需要注册或执行的工具对象。
        session_id (str): 当前操作所属的会话编号。

    Returns:
        Agent: 供外层用例断言使用的预设结果。
    """
    llm_client = ScriptedLLM(scripts)
    agent = Agent(
        AgentConfig(
            session_id=session_id,
            model_name="test",
            api_key="test",
            context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        ),
        db_path=str(db_path),
        llm_client=llm_client,
    )
    agent.tool_manager.register(tool)
    return agent


async def drain(stream) -> list:
    """完整消费 Agent 事件流并返回事件记录。

    Args:
        stream (Any): 需要消费的 Agent 或模型事件流。

    Returns:
        list: 供外层用例断言使用的预设结果。
    """
    return [event async for event in stream]


async def checkpoint_types(manager: StateManager) -> list[str]:
    """读取临时数据库中的 checkpoint 类型，检查保存顺序。

    Args:
        manager (StateManager): 本用例的临时数据库状态管理器。

    Returns:
        list[str]: 供外层用例断言使用的预设结果。
    """
    db = await manager._get_db()
    async with db.execute(
        """
        SELECT checkpoint_type FROM checkpoints
        WHERE session_id = ? ORDER BY id
        """,
        (manager.session_id,),
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]


def test_stop_during_llm_saves_visible_partial_and_completed_checkpoint(tmp_path: Path) -> None:
    """验证模型输出中停止保留可见片段和完成态 checkpoint。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：模型输出中停止保留可见片段和完成态 checkpoint。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        first_chunk = asyncio.Event()

        class WaitingLLM:
            """提供隔离测试替身，用于验证：模型输出中停止保留可见片段和完成态 checkpoint。"""
            async def astream(self, messages, tools=None):
                """按场景产生模型分片或工具调用。

                Args:
                    messages (消息序列): 需要转换或交付的领域消息集合。
                    tools (list | None): 当前模型可用的工具 schema。

                Yields:
                    LLMChunk: 当前测试场景的预设分片或事件。
                """
                messages = capture_input(messages)
                yield StreamChunk(content="已经展示的回答")
                first_chunk.set()
                await asyncio.Event().wait()

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        agent = Agent(
            AgentConfig(
                session_id="stop-test", model_name="test", api_key="test",
                context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
            ),
            db_path=str(tmp_path / "state.sqlite3"), llm_client=WaitingLLM(),
        )
        control = TurnControl("run-1")
        events = []

        async def run() -> None:
            """按场景屏障产生 Agent 事件并提交测试输入。"""
            async for event in agent.run("说句话", turn_control=control):
                events.append(event)

        task = asyncio.create_task(run())
        try:
            await asyncio.wait_for(first_chunk.wait(), 1)
            control.request("stop")
            await asyncio.wait_for(task, 1)
            state = await agent.state_manager.load()
            assert state.messages[-1].text == "已经展示的回答……（被用户打断）"
            assert agent.state_manager.last_loaded_checkpoint_type == "completed"
            assert events[-1].type == EventType.DONE
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_stop_flushes_incomplete_time_prefix_before_saving_partial(tmp_path: Path) -> None:
    """验证停止时先冲刷时间前缀缓冲再保存片段。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：停止时先冲刷时间前缀缓冲再保存片段。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        first_chunk = asyncio.Event()

        class WaitingLLM:
            """提供隔离测试替身，用于验证：停止时先冲刷时间前缀缓冲再保存片段。"""
            async def astream(self, messages, tools=None):
                """按场景产生模型分片或工具调用。

                Args:
                    messages (消息序列): 需要转换或交付的领域消息集合。
                    tools (list | None): 当前模型可用的工具 schema。

                Yields:
                    LLMChunk: 当前测试场景的预设分片或事件。
                """
                messages = capture_input(messages)
                yield StreamChunk(content="[发送时间：")
                first_chunk.set()
                await asyncio.Event().wait()

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        agent = Agent(
            AgentConfig(
                session_id="filter-stop", model_name="test", api_key="test",
                context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
            ),
            db_path=str(tmp_path / "state.sqlite3"), llm_client=WaitingLLM(),
        )
        control = TurnControl("run-1")
        events = []

        async def run() -> None:
            """按场景屏障产生 Agent 事件并提交测试输入。"""
            async for event in agent.run("说句话", turn_control=control):
                events.append(event)

        task = asyncio.create_task(run())
        try:
            await asyncio.wait_for(first_chunk.wait(), 1)
            control.request("stop")
            await asyncio.wait_for(task, 1)
            assert [event.data for event in events if event.type == EventType.TEXT_CHUNK] == [
                "[发送时间：", "……（被用户打断）",
            ]
            state = await agent.state_manager.load()
            assert state.messages[-1].text == "[发送时间：……（被用户打断）"
        finally:
            await agent.close()

    asyncio.run(scenario())


def tool_call(call_id: str, name: str = "step", label: str = "") -> StreamChunk:
    """构造当前测试使用的模型工具调用。

    Args:
        call_id (str): 本次测试的工具调用编号。
        name (str): 当前对象的名称。
        label (str): 记录到执行顺序列表中的步骤名称。

    Returns:
        StreamChunk: 供外层用例断言使用的预设结果。
    """
    args = {"label": label} if label else {}
    return StreamChunk(tool_call=ToolCall(id=call_id, name=name, args=args))


class CommitOrderPlugin(BasePlugin):
    """提供用例控制的插件回调，验证执行时机和状态修改约束。"""
    name = "commit_order"

    def __init__(self, order: list[str]):
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        Args:
            order (list[str]): 由测试共享的执行顺序记录列表。
        """
        self.order = order

    @property
    def hooks(self) -> list[PluginHook]:
        """向插件管理器提供当前用例需要监听的执行时机。

        Returns:
            list[PluginHook]: 当前用例预设的回调时机或工具目录。
        """
        return [
            PluginHook.BEFORE_RESPONSE_COMMIT,
            PluginHook.AFTER_RESPONSE_COMMIT,
        ]

    async def execute(self, context: HookContext) -> None:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            context (HookContext): 执行管道提供的工具运行上下文。
        """
        if context.hook == PluginHook.BEFORE_RESPONSE_COMMIT:
            self.order.append("before_commit")
            context.data["prepared"] = True
            context.agent_state.long_term_memory_turn_counter += 1
        elif context.hook == PluginHook.AFTER_RESPONSE_COMMIT:
            assert context.data == {"prepared": True}
            self.order.append("after_commit")


class ActivationInputCommitOrderPlugin(BasePlugin):
    """观察新激活输入保存前后的插件事件和共享提交数据。

    Attributes:
        order (list[str]): 钩子与 checkpoint 保存顺序的共同记录。
    """
    name = "activation_input_commit_order"

    def __init__(self, order: list[str]):
        """绑定需要观察的激活提交顺序。

        Args:
            order (list[str]): 测试共用的执行顺序记录。
        """
        self.order = order

    @property
    def hooks(self) -> list[PluginHook]:
        """订阅新激活的输入提交前后事件。

        Returns:
            list[PluginHook]: 输入保存之前和成功之后的两个钩子。
        """
        return [
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT,
            PluginHook.AFTER_ACTIVATION_INPUT_COMMIT,
        ]

    async def execute(self, context: HookContext) -> None:
        """核对输入所在阶段，记录前后钩子并检查提交数据共用。

        Args:
            context (HookContext): Agent 分发的激活提交上下文。
        """
        # 1. 前置事件应看不到本次新输入，准备的数据交给后置事件继续检查。
        if context.hook == PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT:
            assert not any(
                isinstance(message, HumanMessage) and message.text == "本轮新输入"
                for message in context.agent_state.messages
            )
            self.order.append("before_activation_input_commit")
            context.data["prepared_previous_turn"] = True
        # 2. 后置事件应看到已保存的新输入，并接到同一个前置准备结果。
        elif context.hook == PluginHook.AFTER_ACTIVATION_INPUT_COMMIT:
            assert context.data == {"prepared_previous_turn": True}
            assert any(
                isinstance(message, HumanMessage) and message.text == "本轮新输入"
                for message in context.agent_state.messages
            )
            self.order.append("after_activation_input_commit")


def test_response_commit_hooks_wrap_completed_checkpoint(tmp_path: Path) -> None:
    """验证最终回复提交钩子包围完成 checkpoint 保存。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> list[str]:
        """由外层测试调用，完成“最终回复提交钩子包围完成 checkpoint 保存”的异步步骤。

        Returns:
            list[str]: 供外层用例断言使用的预设结果。
        """
        order: list[str] = []
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [[StreamChunk(content="完成")]],
            StepTool(),
        )
        await agent.plugin_manager.register(CommitOrderPlugin(order))
        original_save = agent.state_manager.save

        async def tracked_save(state, *, checkpoint_type):
            """记录状态保存时机后调用临时数据库保存，供检查与插件和模型的先后顺序。

            该定义仅用于外层用例：验证最终回复提交钩子包围完成 checkpoint 保存。

            Args:
                state (Any): 当前会话的内存 AgentState；保存由调用方负责。
                checkpoint_type (Any): 本次保存中间执行进度或完整轮次的类型。
            """
            checkpoint_id = await original_save(state, checkpoint_type=checkpoint_type)
            order.append(f"save_{checkpoint_type}")
            return checkpoint_id

        agent.state_manager.save = tracked_save
        try:
            async for event in agent.run("开始"):
                if event.type == EventType.DONE:
                    order.append("done")
            return order
        finally:
            await agent.close()

    assert asyncio.run(scenario()) == [
        "save_intermediate",
        "before_commit",
        "save_completed",
        "after_commit",
        "done",
    ]


def test_activation_input_commit_hooks_wrap_intermediate_checkpoint(tmp_path: Path) -> None:
    """新激活前后钩子包围输入 checkpoint，不等待最终回复提交。

    Args:
        tmp_path (Path): Pytest 提供的临时存储目录。
    """
    async def scenario() -> list[str]:
        """记录真实真人激活中的钩子与两次 checkpoint 保存顺序。

        Returns:
            list[str]: 激活钩子、中间保存和最终保存的顺序。
        """
        order: list[str] = []
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [[StreamChunk(content="完成")]],
            StepTool(),
        )
        await agent.plugin_manager.register(ActivationInputCommitOrderPlugin(order))
        original_save = agent.state_manager.save

        async def tracked_save(state, *, checkpoint_type):
            """真实保存成功后记录 checkpoint 类别，不改变事务结果。

            Args:
                state (AgentState): 当前激活或回复状态。
                checkpoint_type (CheckpointType): 输入中间状态或完成状态。

            Returns:
                int: 实际保存成功的 checkpoint 编号。
            """
            checkpoint_id = await original_save(state, checkpoint_type=checkpoint_type)
            order.append(f"save_{checkpoint_type}")
            return checkpoint_id

        agent.state_manager.save = tracked_save
        try:
            await drain(agent.run("本轮新输入"))
            return order
        finally:
            await agent.close()

    assert asyncio.run(scenario()) == [
        "before_activation_input_commit",
        "save_intermediate",
        "after_activation_input_commit",
        "save_completed",
    ]


def test_intermediate_checkpoint_failure_skips_after_activation_input_hook(
    tmp_path: Path,
) -> None:
    """激活输入保存失败时只执行前置钩子，不通知输入已提交。

    Args:
        tmp_path (Path): Pytest 提供的临时存储目录。
    """
    async def scenario() -> list[str]:
        """拒绝中间 checkpoint，观察后置钩子是否保持未触发。

        Returns:
            list[str]: 保存失败前实际发生的事件。
        """
        order: list[str] = []
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [[StreamChunk(content="不应执行")]],
            StepTool(),
        )
        await agent.plugin_manager.register(ActivationInputCommitOrderPlugin(order))

        async def failing_save(state, *, checkpoint_type):
            """模拟输入事务失败，让激活入口停止后续处理。

            Args:
                state (AgentState): 尝试提交的新激活状态。
                checkpoint_type (CheckpointType): 应为 intermediate 的提交类别。

            Raises:
                RuntimeError: 模拟中间 checkpoint 保存失败。
            """
            assert checkpoint_type == "intermediate"
            order.append("save_intermediate_failed")
            raise RuntimeError("模拟 intermediate 保存失败")

        agent.state_manager.save = failing_save
        try:
            with pytest.raises(RuntimeError, match="intermediate 保存失败"):
                await drain(agent.run("本轮新输入"))
            return order
        finally:
            await agent.close()

    assert asyncio.run(scenario()) == [
        "before_activation_input_commit",
        "save_intermediate_failed",
    ]


def test_failed_user_input_commit_keeps_pending_long_term_memory_batch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """验证真人输入提交失败时保留待提取长期记忆批次。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    scheduled: list[str] = []

    class MemoryManager:
        """提供当前用例所需的 MemoryManager 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证真人输入提交失败时保留待提取长期记忆批次。
        """
        async def get_protocol_history_cutoff(self):
            """返回用例预设的协议日志边界，供检查提取不会读取后来消息。

            该定义仅用于外层用例：验证真人输入提交失败时保留待提取长期记忆批次。
            """
            return 2

        async def get_context(self, **kwargs):
            """返回用例指定的记忆背景，避免访问真实记忆存储。

            该定义仅用于外层用例：验证真人输入提交失败时保留待提取长期记忆批次。

            Args:
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            return ""

    def capture(coro, *, logger, task_name):
        """记录测试创建的后台任务，供用例等待它结束。

        该定义仅用于外层用例：验证真人输入提交失败时保留待提取长期记忆批次。

        Args:
            coro (Any): 需要启动并观察的异步过程。
            logger (Any): 记录当前工具调用的日志对象。
            task_name (Any): 用于辨认测试后台任务的名称。
        """
        scheduled.append(task_name)
        coro.close()

    monkeypatch.setattr(memory_plugin_module, "create_background_task", capture)

    async def register_memory(agent: Agent) -> None:
        """给测试 Agent 注册用例指定的记忆插件。

        该定义仅用于外层用例：验证真人输入提交失败时保留待提取长期记忆批次。

        Args:
            agent (Agent): 本用例创建的 Agent 对象。
        """
        plugin = MemoryPlugin(enable_diary=False)
        monkeypatch.setattr(plugin, "_mm", lambda state: MemoryManager())
        await agent.plugin_manager.register(plugin)

    async def scenario() -> tuple[int, int, list[str]]:
        """由外层测试调用，完成“真人输入提交失败时保留待提取长期记忆批次”的异步步骤。

        Returns:
            tuple[int, int, list[str]]: 供外层用例断言使用的预设结果。
        """
        db_path = tmp_path / "checkpoints.sqlite3"
        seed = StateManager("checkpoint-test", db_path=str(db_path))
        try:
            state = AgentState.create_new("checkpoint-test")
            state.add_user_message("已完成的输入")
            state.add_assistant_message("已完成的回复")
            state.long_term_memory_turn_counter = 10
            await seed.save(state, checkpoint_type="completed")
        finally:
            await seed.close()

        failed_agent = await make_agent(
            db_path,
            [[StreamChunk(content="不应执行")]],
            StepTool(),
        )
        await register_memory(failed_agent)

        async def failing_save(state, *, checkpoint_type):
            """在 checkpoint 保存阶段主动抛错，验证后续模型或提交后钩子不会执行。

            该定义仅用于外层用例：验证真人输入提交失败时保留待提取长期记忆批次。

            Args:
                state (Any): 当前会话的内存 AgentState；保存由调用方负责。
                checkpoint_type (Any): 本次保存中间执行进度或完整轮次的类型。
            """
            assert checkpoint_type == "intermediate"
            raise RuntimeError("模拟 intermediate 保存失败")

        failed_agent.state_manager.save = failing_save
        try:
            with pytest.raises(RuntimeError, match="intermediate 保存失败"):
                await drain(failed_agent.run("第一次重试"))
        finally:
            await failed_agent.close()

        persisted = StateManager("checkpoint-test", db_path=str(db_path))
        try:
            counter_after_failure = (
                await persisted.load()
            ).long_term_memory_turn_counter
        finally:
            await persisted.close()

        retry_agent = await make_agent(
            db_path,
            [[StreamChunk(content="完成")]],
            StepTool(),
        )
        await register_memory(retry_agent)
        try:
            events = await drain(retry_agent.run("第二次重试"))
            assert events[-1].type == EventType.DONE
            counter_after_retry = (
                await retry_agent.state_manager.load()
            ).long_term_memory_turn_counter
        finally:
            await retry_agent.close()

        return counter_after_failure, counter_after_retry, scheduled

    assert asyncio.run(scenario()) == (10, 1, ["memory.persist_long_term"])


def test_completed_checkpoint_failure_skips_after_commit_hook(tmp_path: Path) -> None:
    """验证完成 checkpoint 保存失败不执行提交后钩子。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> tuple[list[str], int]:
        """由外层测试调用，完成“完成 checkpoint 保存失败不执行提交后钩子”的异步步骤。

        Returns:
            tuple[list[str], int]: 供外层用例断言使用的预设结果。
        """
        order: list[str] = []
        db_path = tmp_path / "checkpoints.sqlite3"
        agent = await make_agent(
            db_path,
            [[StreamChunk(content="完成")]],
            StepTool(),
        )
        await agent.plugin_manager.register(CommitOrderPlugin(order))
        original_save = agent.state_manager.save

        async def failing_save(state, *, checkpoint_type):
            """在 checkpoint 保存阶段主动抛错，验证后续模型或提交后钩子不会执行。

            该定义仅用于外层用例：验证完成 checkpoint 保存失败不执行提交后钩子。

            Args:
                state (Any): 当前会话的内存 AgentState；保存由调用方负责。
                checkpoint_type (Any): 本次保存中间执行进度或完整轮次的类型。
            """
            if checkpoint_type == "completed":
                order.append("save_completed_failed")
                raise RuntimeError("模拟 completed 保存失败")
            return await original_save(state, checkpoint_type=checkpoint_type)

        agent.state_manager.save = failing_save
        try:
            with pytest.raises(RuntimeError, match="completed 保存失败"):
                await drain(agent.run("开始"))
        finally:
            await agent.close()

        manager = StateManager("checkpoint-test", db_path=str(db_path))
        try:
            persisted = await manager.load()
            return order, persisted.long_term_memory_turn_counter
        finally:
            await manager.close()

    order, persisted_counter = asyncio.run(scenario())
    assert order == ["before_commit", "save_completed_failed"]
    assert persisted_counter == 1


def test_completed_tool_results_survive_later_llm_failure(tmp_path: Path) -> None:
    """验证工具结果完成后不因后续模型失败丢失。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“工具结果完成后不因后续模型失败丢失”的异步步骤。"""
        db_path = tmp_path / "checkpoints.sqlite3"
        tool = StepTool()
        agent = await make_agent(
            db_path,
            [
                [tool_call("call-a", label="A")],
                [tool_call("call-b", label="B")],
                [tool_call("call-c", label="C")],
                RuntimeError("LLM 崩溃"),
            ],
            tool,
        )
        try:
            events = await drain(agent.run("执行三个步骤"))
            assert events[-1].type == EventType.ERROR

            state = await agent.state_manager.load()
            tool_messages = [item for item in state.messages if isinstance(item, ToolMessage)]
            assert [item.text for item in tool_messages] == ["完成 A", "完成 B", "完成 C"]
            assert state.pending_tool_calls == ()
            assert state.long_term_memory_turn_counter == 1
            assert await checkpoint_types(agent.state_manager) == ["intermediate"]
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_new_message_after_llm_failure_counts_each_committed_human_input(tmp_path: Path) -> None:
    """验证模型失败后的新输入逐条增加已提交真人计数。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“模型失败后的新输入逐条增加已提交真人计数”的异步步骤。"""
        db_path = tmp_path / "checkpoints.sqlite3"
        first_agent = await make_agent(
            db_path,
            [
                [tool_call("call-a", label="A")],
                RuntimeError("stream disconnected"),
            ],
            StepTool(),
        )
        try:
            events = await drain(first_agent.run("first human message"))
            assert events[-1].type == EventType.ERROR
            failed_state = await first_agent.state_manager.load()
            assert failed_state.long_term_memory_turn_counter == 1
        finally:
            await first_agent.close()

        second_agent = await make_agent(
            db_path,
            [[StreamChunk(content="continued")]],
            StepTool(),
        )
        try:
            events = await drain(second_agent.run("please continue"))
            assert events[-1].type == EventType.DONE
            completed_state = await second_agent.state_manager.load()
            assert completed_state.long_term_memory_turn_counter == 2
        finally:
            await second_agent.close()

    asyncio.run(scenario())


def test_initial_empty_response_keeps_user_intermediate_checkpoint(tmp_path: Path) -> None:
    """验证首次模型空回复保留真人输入中间 checkpoint。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“首次模型空回复保留真人输入中间 checkpoint”的异步步骤。"""
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [[StreamChunk(finish_reason="stop")]],
            StepTool(),
        )
        try:
            events = await drain(agent.run("在吗"))
            assert events[-1].type == EventType.ERROR
            assert "finish_reason=stop" in events[-1].data
            assert await checkpoint_types(agent.state_manager) == ["intermediate"]
            state = await agent.state_manager.load()
            assert isinstance(state.messages[-1], HumanMessage)
            assert "在吗" in state.messages[-1].text
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_empty_response_after_tool_keeps_intermediate_checkpoint(tmp_path: Path) -> None:
    """验证工具执行后的空回复保留中间 checkpoint。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“工具执行后的空回复保留中间 checkpoint”的异步步骤。"""
        tool = StepTool()
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [
                [tool_call("call-a", label="A")],
                [StreamChunk(finish_reason="stop")],
            ],
            tool,
        )
        try:
            events = await drain(agent.run("执行一步"))
            assert events[-1].type == EventType.ERROR
            assert "finish_reason=stop" in events[-1].data

            state = await agent.state_manager.load()
            tool_messages = [item for item in state.messages if isinstance(item, ToolMessage)]
            assert [item.text for item in tool_messages] == ["完成 A"]
            assert state.pending_tool_calls == ()
            assert await checkpoint_types(agent.state_manager) == ["intermediate"]
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_tool_error_result_is_checkpointed(tmp_path: Path) -> None:
    """验证工具错误结果也保存到 checkpoint。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“工具错误结果也保存到 checkpoint”的异步步骤。"""
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [[tool_call("call-error", name="error_step")], RuntimeError("LLM 崩溃")],
            ErrorTool(),
        )
        try:
            await drain(agent.run("执行失败工具"))
            state = await agent.state_manager.load()
            tool_messages = [item for item in state.messages if isinstance(item, ToolMessage)]
            assert tool_messages[-1].text == "错误: 测试工具失败"
            assert state.pending_tool_calls == ()
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_partial_tool_batch_is_marked_interrupted_without_retry(tmp_path: Path) -> None:
    """验证部分工具批次中断后不自动重放。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“部分工具批次中断后不自动重放”的异步步骤。"""
        db_path = tmp_path / "checkpoints.sqlite3"
        crashing_tool = StepTool(crash_on="B")
        first_agent = await make_agent(
            db_path,
            [[
                tool_call("call-a", label="A"),
                tool_call("call-b", label="B"),
                tool_call("call-c", label="C"),
            ]],
            crashing_tool,
        )
        try:
            with pytest.raises(asyncio.CancelledError):
                await drain(first_agent.run("执行批量工具"))
        finally:
            await first_agent.close()

        recovery_tool = StepTool()
        second_agent = await make_agent(
            db_path,
            [[StreamChunk(content="已继续处理新消息")]],
            recovery_tool,
        )
        try:
            await drain(second_agent.run("新的用户消息"))
            state = await second_agent.state_manager.load()
            by_id = {
                item.tool_call_id: item.text
                for item in state.messages
                if isinstance(item, ToolMessage)
            }
            assert by_id["call-a"] == "完成 A"
            assert by_id["call-b"] == INTERRUPTED_TOOL_RESULT
            assert by_id["call-c"] == INTERRUPTED_TOOL_RESULT
            assert recovery_tool.executed == []
            assert state.pending_tool_calls == ()
            assert await checkpoint_types(second_agent.state_manager) == [
                "completed",
                "completed",
            ]
        finally:
            await second_agent.close()

    asyncio.run(scenario())


def test_checkpoint_failure_keeps_tool_marked_pending() -> None:
    """验证checkpoint 保存失败时工具仍标记未完成。"""
    class PipelineHarness:
        """提供当前用例所需的 PipelineHarness 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证checkpoint 保存失败时工具仍标记未完成。
        """
        def __init__(self):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证checkpoint 保存失败时工具仍标记未完成。
            """
            from app.agent.core.event_router import EventRouter
            from app.agent.core.plugin_manager import PluginManager
            from app.agent.core.tool_manager import ToolManager

            self.config = AgentConfig(
                session_id="test",
                model_name="test",
                api_key="test",
                context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
            )
            self.plugin_manager = PluginManager()
            self.tool_manager = ToolManager()
            self.tool_manager.register(StepTool())
            self.event_router = EventRouter()
            self.llm_client = ScriptedLLM([[tool_call("call-a", label="A")]])

    async def scenario() -> None:
        """由外层测试调用，完成“checkpoint 保存失败时工具仍标记未完成”的异步步骤。"""
        agent = PipelineHarness()
        pipeline = ExecutionPipeline(
            prompt_source=agent.config,
            llm_client=agent.llm_client,
            tool_manager=agent.tool_manager,
            plugin_manager=agent.plugin_manager,
            model_input=ModelInputCoordinator(
                agent.config.context_strategy,
                agent.llm_client,
                accounting_key="test",
            ),
            emit_event=agent.event_router.emit,
        )
        state = AgentState.create_new("test")
        state.add_user_message("执行工具")
        snapshots: list[dict] = []

        async def checkpoint(
            current: AgentState,
            *,
            checkpoint_type: str,
        ) -> int:
            """记录当前状态保存类型和执行顺序，供用例确认先保存再调用模型或发布事件。

            该定义仅用于外层用例：验证checkpoint 保存失败时工具仍标记未完成。

            Args:
                current (AgentState): 回调收到的当前 AgentState。
                checkpoint_type (str): 本次保存中间执行进度或完整轮次的类型。

            Returns:
                int: 供外层用例断言使用的预设结果。
            """
            assert checkpoint_type == "intermediate"
            if snapshots:
                raise RuntimeError("模拟 checkpoint 写入失败")
            snapshots.append(deepcopy(current.to_checkpoint()))
            return 1

        with pytest.raises(RuntimeError, match="checkpoint 写入失败"):
            await drain(pipeline.execute(state, checkpoint=checkpoint))

        persisted = AgentState.from_checkpoint(snapshots[0])
        assert [item.id for item in persisted.pending_tool_calls] == ["call-a"]
        assert not any(isinstance(item, ToolMessage) for item in persisted.messages)

    asyncio.run(scenario())


def test_last_tool_checkpoint_flushes_all_batch_images(tmp_path: Path) -> None:
    """最后一个工具的 checkpoint 必须同时提交整批延迟图片。

    Args:
        tmp_path (Path): Pytest 为本用例创建的临时目录，不访问生产存储。
    """

    async def scenario() -> list[AgentState]:
        """由外层测试调用，完成“一个工具的 checkpoint 必须同时提交整批延迟图片”的异步步骤。

        Returns:
            list[AgentState]: 供外层用例断言使用的预设结果。
        """
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [[
                tool_call("call-a", name="image_step", label="A"),
                tool_call("call-b", name="image_step", label="B"),
            ]],
            ImageStepTool(),
        )
        state = AgentState.create_new("checkpoint-test")
        state.add_user_message("执行两个图片工具")
        snapshots: list[AgentState] = []

        async def checkpoint(
            current: AgentState,
            *,
            checkpoint_type: CheckpointType,
        ) -> int:
            """记录当前状态保存类型和执行顺序，供用例确认先保存再调用模型或发布事件。

            该定义仅用于外层用例：最后一个工具的 checkpoint 必须同时提交整批延迟图片。

            Args:
                current (AgentState): 回调收到的当前 AgentState。
                checkpoint_type (CheckpointType): 本次保存中间执行进度或完整轮次的类型。

            Returns:
                int: 供外层用例断言使用的预设结果。
            """
            assert checkpoint_type == "intermediate"
            snapshot = AgentState.from_checkpoint(deepcopy(current.to_checkpoint()))
            snapshots.append(snapshot)
            if not snapshot.pending_tool_calls and any(
                isinstance(item, ToolMessage) for item in snapshot.messages
            ):
                raise asyncio.CancelledError()
            return len(snapshots)

        try:
            with pytest.raises(asyncio.CancelledError):
                await drain(agent.pipeline.execute(state, checkpoint=checkpoint))
        finally:
            await agent.close()
        return snapshots

    snapshots = asyncio.run(scenario())
    first_result = snapshots[-2]
    assert [item.id for item in first_result.pending_tool_calls] == ["call-b"]
    assert any(isinstance(part, ImageContent) for part in first_result.messages[-1].content)
    assert isinstance(first_result.messages[-1], ToolMessage)

    completed_batch = snapshots[-1]
    assert completed_batch.pending_tool_calls == ()
    assert "_deferred_tool_media" not in completed_batch.to_checkpoint()
    tool_messages = [
        item for item in completed_batch.messages if isinstance(item, ToolMessage)
    ]
    image_messages = [
        item
        for item in completed_batch.messages
        if isinstance(item, ToolMessage) and any(isinstance(part, ImageContent) for part in item.content)
    ]
    assert [item.message_id for item in image_messages] == [
        item.message_id for item in tool_messages
    ]


def test_resumed_last_tool_checkpoint_flushes_deferred_images(tmp_path: Path) -> None:
    """审批恢复的最后一个工具必须与已有延迟图片一起完整提交。

    Args:
        tmp_path (Path): Pytest 为本用例创建的临时目录，不访问生产存储。
    """

    async def scenario() -> AgentState:
        """由外层测试调用，完成“恢复的最后一个工具必须与已有延迟图片一起完整提交”的异步步骤。

        Returns:
            AgentState: 供外层用例断言使用的预设结果。
        """
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [],
            ImageStepTool(),
        )
        state = AgentState.create_new("checkpoint-test")
        call_a = ToolCall(id="call-a", name="image_step", args={"label": "A"})
        call_b = ToolCall(id="call-b", name="image_step", args={"label": "B"})
        state.record_assistant_step("", (call_a, call_b))
        state.record_tool_result(call_a, result_parts('完成 A', ('data:image/png;base64,iVBORw0KGgpwYXlsb2Fk',), 'mcp'))
        state.set_interrupt({
            "type": "authorization_request",
            "request_id": "request-b",
            "tool_name": "image_step",
            "tool_call_id": "call-b",
            "tool_args": {"label": "B"},
            "resume_state": {},
        })
        snapshots: list[AgentState] = []

        async def checkpoint(
            current: AgentState,
            *,
            checkpoint_type: CheckpointType,
        ) -> int:
            """记录当前状态保存类型和执行顺序，供用例确认先保存再调用模型或发布事件。

            该定义仅用于外层用例：审批恢复的最后一个工具必须与已有延迟图片一起完整提交。

            Args:
                current (AgentState): 回调收到的当前 AgentState。
                checkpoint_type (CheckpointType): 本次保存中间执行进度或完整轮次的类型。

            Returns:
                int: 供外层用例断言使用的预设结果。
            """
            assert checkpoint_type == "intermediate"
            snapshot = AgentState.from_checkpoint(deepcopy(current.to_checkpoint()))
            snapshots.append(snapshot)
            if not snapshot.pending_tool_calls:
                raise asyncio.CancelledError()
            return len(snapshots)

        try:
            with pytest.raises(asyncio.CancelledError):
                await drain(agent.pipeline.resume_tools(
                    state,
                    {"kind": "approve"},
                    checkpoint=checkpoint,
                ))
        finally:
            await agent.close()
        return snapshots[-1]

    completed_batch = asyncio.run(scenario())
    assert completed_batch.pending_tool_calls == ()
    assert "_deferred_tool_media" not in completed_batch.to_checkpoint()
    tool_messages = [
        item for item in completed_batch.messages if isinstance(item, ToolMessage)
    ]
    image_messages = [
        item
        for item in completed_batch.messages
        if isinstance(item, ToolMessage) and any(isinstance(part, ImageContent) for part in item.content)
    ]
    assert [item.message_id for item in image_messages] == [
        item.message_id for item in tool_messages
    ]


def test_chat_resolves_unresolved_interrupt_before_new_message(tmp_path: Path) -> None:
    """验证新消息处理前先解决遗留中断。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“新消息处理前先解决遗留中断”的异步步骤。"""
        db_path = tmp_path / "checkpoints.sqlite3"
        resumed_calls: list[bool] = []
        first_agent = await make_agent(
            db_path,
            [[tool_call("shot-1", name="screenshot")]],
            ResumableScreenshotTool(resumed_calls),
        )
        try:
            await drain(first_agent.run("请求截屏"))
        finally:
            await first_agent.close()

        second_agent = await make_agent(
            db_path,
            [[StreamChunk(content="开始新任务")]],
            ResumableScreenshotTool(resumed_calls),
        )
        try:
            events = await drain(second_agent.run("新消息"))
            assert events[-1].type == EventType.DONE
            after = await second_agent.state_manager.load()
            assert after.interrupt_data is None
            assert any(
                isinstance(message, ToolMessage)
                and "已拒绝" in message.text
                for message in after.messages
            )
            assert any(
                isinstance(message, HumanMessage)
                and "新消息" in message.text
                for message in after.messages
            )
        finally:
            await second_agent.close()

    asyncio.run(scenario())


def test_new_message_cancels_interrupted_tool_batch_before_starting_turn(
    tmp_path: Path,
) -> None:
    """验证新消息先取消中断批次再开始新轮次。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“新消息先取消中断批次再开始新轮次”的异步步骤。"""
        db_path = tmp_path / "checkpoints.sqlite3"
        resumed_calls: list[bool] = []
        first_agent = await make_agent(
            db_path,
            [[
                tool_call("shot-1", name="screenshot"),
                tool_call("step-2", label="B"),
            ]],
            ResumableScreenshotTool(resumed_calls),
        )
        try:
            await drain(first_agent.run("请求截屏"))
        finally:
            await first_agent.close()

        second_agent = await make_agent(
            db_path,
            [[StreamChunk(content="已切换到新任务")]],
            ResumableScreenshotTool(resumed_calls),
        )
        try:
            events = await drain(second_agent.run("改做别的事情"))
            assert any(event.type == EventType.TEXT_CHUNK for event in events)

            state = await second_agent.state_manager.load()
            tool_results = {
                message.tool_call_id: message.text
                for message in state.messages
                if isinstance(message, ToolMessage)
            }
            assert "已拒绝" in tool_results["shot-1"]
            assert "已取消" in tool_results["step-2"]
            assert state.interrupt_data is None
            assert state.pending_actions == ()
            assert isinstance(state.messages[-2], HumanMessage)
            assert "改做别的事情" in state.messages[-2].text
            assert resumed_calls == []
        finally:
            await second_agent.close()

    asyncio.run(scenario())


def test_cancelling_interrupted_batch_flushes_prior_images_after_all_tool_slots(
    tmp_path: Path,
) -> None:
    """验证取消批次时旧图片仍排在全部工具结果之后。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> tuple[AgentState, list[bool]]:
        """由外层测试调用，完成“取消批次时旧图片仍排在全部工具结果之后”的异步步骤。

        Returns:
            tuple[AgentState, list[bool]]: 供外层用例断言使用的预设结果。
        """
        db_path = tmp_path / "checkpoints.sqlite3"
        resumed_calls: list[bool] = []
        first_agent = await make_agent(
            db_path,
            [[
                tool_call("image-1", name="image_step", label="A"),
                tool_call("shot-2", name="screenshot"),
                tool_call("step-3", label="C"),
            ]],
            ImageStepTool(),
        )
        first_agent.tool_manager.register(ResumableScreenshotTool(resumed_calls))
        first_agent.tool_manager.register(StepTool())
        try:
            events = await drain(first_agent.run("先取图再请求截屏"))
            assert events[-1].type == EventType.INTERRUPT
        finally:
            await first_agent.close()

        second_agent = await make_agent(
            db_path,
            [[StreamChunk(content="已切换任务")]],
            ResumableScreenshotTool(resumed_calls),
        )
        try:
            events = await drain(second_agent.run("改做别的事情"))
            assert events[-1].type == EventType.DONE
            return await second_agent.state_manager.load(), resumed_calls
        finally:
            await second_agent.close()

    state, resumed_calls = asyncio.run(scenario())
    batch_start = next(
        index for index, message in enumerate(state.messages)
        if isinstance(message, AssistantMessage)
        and [call.id for call in message.tool_calls] == ["image-1", "shot-2", "step-3"]
    )
    batch_tail = state.messages[batch_start + 1:]
    tool_positions = {
        message.tool_call_id: index
        for index, message in enumerate(batch_tail)
        if isinstance(message, ToolMessage)
        and message.tool_call_id in {"image-1", "shot-2", "step-3"}
    }
    media_position = next(
        index for index, message in enumerate(batch_tail)
        if isinstance(message, ToolMessage) and any(isinstance(part, ImageContent) for part in message.content)
    )

    assert set(tool_positions) == {"image-1", "shot-2", "step-3"}
    assert media_position == tool_positions["image-1"]
    assert resumed_calls == []


def test_successful_turn_replaces_intermediate_with_completed(tmp_path: Path) -> None:
    """验证成功轮次用完成 checkpoint 替换中间 checkpoint。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    async def scenario() -> None:
        """由外层测试调用，完成“成功轮次用完成 checkpoint 替换中间 checkpoint”的异步步骤。"""
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [
                [tool_call("call-a", label="A")],
                [StreamChunk(content="全部完成")],
            ],
            StepTool(),
        )
        try:
            events = await drain(agent.run("执行一步"))
            assert events[-1].type == EventType.DONE
            assert await checkpoint_types(agent.state_manager) == ["completed"]
        finally:
            await agent.close()

    asyncio.run(scenario())
