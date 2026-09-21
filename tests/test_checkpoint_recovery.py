import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from app.agent.agent import Agent, AgentConfig, INTERRUPTED_TOOL_RESULT
from app.agent.context import ContextStrategyConfig, ModelInputCoordinator
from app.agent.core.event_router import EventType
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.plugins.base import BasePlugin, HookContext, PluginHook
from app.agent.plugins.memory import MemoryPlugin
from app.agent.state.manager import CheckpointType, StateManager
from app.agent.state.messages import (
    AssistantMessage,
    HumanMessage,
    MCP_TOOL_IMAGE_MESSAGE_NAME,
    TOOL_IMAGE_MESSAGE_NAME,
    ToolMediaMessage,
    ToolCall,
    ToolMessage,
)
from app.agent.models.llm_client import StreamChunk
from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolContext, ToolResult
from app.schemas.chat import AgentInput
from app.services.agent_service import AgentService
import app.agent.plugins.memory as memory_plugin_module


class ScriptedLLM:
    """按调用顺序回放流式结果或异常。"""

    def __init__(self, scripts: list[list[StreamChunk] | BaseException]):
        self.scripts = list(scripts)
        self.closed = False

    async def astream(self, messages, tools=None):
        script = self.scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        for chunk in script:
            yield chunk

    async def close(self) -> None:
        self.closed = True


class StepTool(BaseTool):
    name = "step"
    description = "执行测试步骤"
    parameters_schema = {"type": "object", "properties": {"label": {"type": "string"}}}

    def __init__(self, crash_on: str | None = None):
        self.crash_on = crash_on
        self.executed: list[str] = []

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
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
        label = args.get("label", "")
        return ToolResult.success(
            f"完成 {label}",
            image_urls=["data:image/png;base64,iVBORw0KGgpwYXlsb2Fk"],
        )


class ErrorTool(BaseTool):
    name = "error_step"
    description = "返回工具错误"
    parameters_schema = {"type": "object", "properties": {}}

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        raise RuntimeError("测试工具失败")


class ResumableScreenshotTool(BaseTool):
    name = "screenshot"
    description = "测试截屏中断"
    parameters_schema = {"type": "object", "properties": {}}
    is_resumable = True

    def __init__(self, resumed_calls: list[bool]):
        self.resumed_calls = resumed_calls

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        if context.resume_data is None:
            return ToolResult.needs_input("authorization_request", "允许截屏？")
        self.resumed_calls.append(context.resume_data.get("kind") == "approve")
        return ToolResult.success("截屏恢复完成")


class PhasedInterruptTool(BaseTool):
    name = "phased_interrupt"
    description = "按指定阶段数暂停，验证恢复路径"
    parameters_schema = {"type": "object", "properties": {"stages": {"type": "integer"}}}
    is_resumable = True

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        phase = (context.resume_state or {}).get("phase", 0)
        if phase < args["stages"]:
            return ToolResult.needs_input(
                "authorization_request", "允许继续？", resume_state={"phase": phase + 1},
            )
        return ToolResult.success("阶段完成")


class InterruptRecordingPlugin(BasePlugin):
    name = "interrupt_recording"

    def __init__(self) -> None:
        self.invocations = 0
        self.interrupt_ids: list[str] = []

    @property
    def hooks(self) -> list[PluginHook]:
        return [PluginHook.ON_INVOKE, PluginHook.ON_INTERRUPT]

    async def execute(self, context: HookContext) -> None:
        if context.hook == PluginHook.ON_INVOKE:
            self.invocations += 1
            return
        interrupt = context.data["interrupt"]
        assert context.agent_state.interrupt_data["request_id"] == interrupt.request_id
        assert not context.agent_state.pending_tool_calls
        self.interrupt_ids.append(interrupt.request_id)
        context.agent_state.extra["interrupt_ids"] = list(self.interrupt_ids)


class FailingInterruptPlugin(BasePlugin):
    name = "failing_interrupt"
    priority = 90

    @property
    def hooks(self) -> list[PluginHook]:
        return [PluginHook.ON_INTERRUPT]

    async def execute(self, context: HookContext) -> None:
        context.agent_state.extra["before_plugin_error"] = True
        raise RuntimeError("测试中断插件失败")


@pytest.mark.parametrize("path", ["normal", "same_tool", "remaining_tool"])
@pytest.mark.parametrize("plugin_fails", [False, True])
def test_all_interrupt_paths_save_hook_updates_before_emitting(
    tmp_path: Path, path: str, plugin_fails: bool, caplog,
) -> None:
    async def scenario() -> None:
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
    async def scenario() -> None:
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
    return [event async for event in stream]


async def checkpoint_types(manager: StateManager) -> list[str]:
    db = await manager._get_db()
    async with db.execute(
        """
        SELECT checkpoint_type FROM checkpoints
        WHERE session_id = ? ORDER BY id
        """,
        (manager.session_id,),
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]


def tool_call(call_id: str, name: str = "step", label: str = "") -> StreamChunk:
    args = {"label": label} if label else {}
    return StreamChunk(tool_call=ToolCall(id=call_id, name=name, args=args))


class CommitOrderPlugin(BasePlugin):
    name = "commit_order"

    def __init__(self, order: list[str]):
        self.order = order

    @property
    def hooks(self) -> list[PluginHook]:
        return [
            PluginHook.BEFORE_RESPONSE_COMMIT,
            PluginHook.AFTER_RESPONSE_COMMIT,
        ]

    async def execute(self, context: HookContext) -> None:
        if context.hook == PluginHook.BEFORE_RESPONSE_COMMIT:
            self.order.append("before_commit")
            context.data["prepared"] = True
            context.agent_state.long_term_memory_turn_counter += 1
        elif context.hook == PluginHook.AFTER_RESPONSE_COMMIT:
            assert context.data == {"prepared": True}
            self.order.append("after_commit")


class UserInputCommitOrderPlugin(BasePlugin):
    name = "user_input_commit_order"

    def __init__(self, order: list[str]):
        self.order = order

    @property
    def hooks(self) -> list[PluginHook]:
        return [
            PluginHook.BEFORE_USER_INPUT_COMMIT,
            PluginHook.AFTER_USER_INPUT_COMMIT,
        ]

    async def execute(self, context: HookContext) -> None:
        if context.hook == PluginHook.BEFORE_USER_INPUT_COMMIT:
            assert not any(
                isinstance(message, HumanMessage) and message.text == "本轮新输入"
                for message in context.agent_state.messages
            )
            self.order.append("before_user_input_commit")
            context.data["prepared_previous_turn"] = True
        elif context.hook == PluginHook.AFTER_USER_INPUT_COMMIT:
            assert context.data == {"prepared_previous_turn": True}
            assert any(
                isinstance(message, HumanMessage) and message.text == "本轮新输入"
                for message in context.agent_state.messages
            )
            self.order.append("after_user_input_commit")


def test_response_commit_hooks_wrap_completed_checkpoint(tmp_path: Path) -> None:
    async def scenario() -> list[str]:
        order: list[str] = []
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [[StreamChunk(content="完成")]],
            StepTool(),
        )
        await agent.plugin_manager.register(CommitOrderPlugin(order))
        original_save = agent.state_manager.save

        async def tracked_save(state, *, checkpoint_type):
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


def test_user_input_commit_hooks_wrap_intermediate_checkpoint(tmp_path: Path) -> None:
    async def scenario() -> list[str]:
        order: list[str] = []
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [[StreamChunk(content="完成")]],
            StepTool(),
        )
        await agent.plugin_manager.register(UserInputCommitOrderPlugin(order))
        original_save = agent.state_manager.save

        async def tracked_save(state, *, checkpoint_type):
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
        "before_user_input_commit",
        "save_intermediate",
        "after_user_input_commit",
        "save_completed",
    ]


def test_intermediate_checkpoint_failure_skips_after_user_input_hook(
    tmp_path: Path,
) -> None:
    async def scenario() -> list[str]:
        order: list[str] = []
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [[StreamChunk(content="不应执行")]],
            StepTool(),
        )
        await agent.plugin_manager.register(UserInputCommitOrderPlugin(order))

        async def failing_save(state, *, checkpoint_type):
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
        "before_user_input_commit",
        "save_intermediate_failed",
    ]


def test_failed_user_input_commit_keeps_pending_long_term_memory_batch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scheduled: list[str] = []

    class MemoryManager:
        async def get_protocol_history_cutoff(self):
            return 2

        async def get_context(self, **kwargs):
            return ""

    def capture(coro, *, logger, task_name):
        scheduled.append(task_name)
        coro.close()

    monkeypatch.setattr(memory_plugin_module, "create_background_task", capture)

    async def register_memory(agent: Agent) -> None:
        plugin = MemoryPlugin(enable_diary=False)
        monkeypatch.setattr(plugin, "_mm", lambda state: MemoryManager())
        await agent.plugin_manager.register(plugin)

    async def scenario() -> tuple[int, int, list[str]]:
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
    async def scenario() -> tuple[list[str], int]:
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
    async def scenario() -> None:
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
            assert [item.content for item in tool_messages] == ["完成 A", "完成 B", "完成 C"]
            assert state.pending_tool_calls == ()
            assert state.long_term_memory_turn_counter == 1
            assert await checkpoint_types(agent.state_manager) == ["intermediate"]
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_new_message_after_llm_failure_counts_each_committed_human_input(tmp_path: Path) -> None:
    async def scenario() -> None:
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
    async def scenario() -> None:
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
    async def scenario() -> None:
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
            assert [item.content for item in tool_messages] == ["完成 A"]
            assert state.pending_tool_calls == ()
            assert await checkpoint_types(agent.state_manager) == ["intermediate"]
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_tool_error_result_is_checkpointed(tmp_path: Path) -> None:
    async def scenario() -> None:
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [[tool_call("call-error", name="error_step")], RuntimeError("LLM 崩溃")],
            ErrorTool(),
        )
        try:
            await drain(agent.run("执行失败工具"))
            state = await agent.state_manager.load()
            tool_messages = [item for item in state.messages if isinstance(item, ToolMessage)]
            assert tool_messages[-1].content == "错误: 测试工具失败"
            assert state.pending_tool_calls == ()
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_partial_tool_batch_is_marked_interrupted_without_retry(tmp_path: Path) -> None:
    async def scenario() -> None:
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
                item.tool_call_id: item.content
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
    class PipelineHarness:
        def __init__(self):
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
    """最后一个工具的 checkpoint 必须同时提交整批延迟图片。"""

    async def scenario() -> list[AgentState]:
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
    assert first_result.deferred_tool_media_count == 1
    assert isinstance(first_result.messages[-1], ToolMessage)

    completed_batch = snapshots[-1]
    assert completed_batch.pending_tool_calls == ()
    assert completed_batch.deferred_tool_media_count == 0
    tool_messages = [
        item for item in completed_batch.messages if isinstance(item, ToolMessage)
    ]
    image_messages = [
        item
        for item in completed_batch.messages
        if isinstance(item, ToolMediaMessage)
    ]
    assert [item.associated_message_id for item in image_messages] == [
        item.message_id for item in tool_messages
    ]


def test_resumed_last_tool_checkpoint_flushes_deferred_images(tmp_path: Path) -> None:
    """审批恢复的最后一个工具必须与已有延迟图片一起完整提交。"""

    async def scenario() -> AgentState:
        agent = await make_agent(
            tmp_path / "checkpoints.sqlite3",
            [],
            ImageStepTool(),
        )
        state = AgentState.create_new("checkpoint-test")
        call_a = ToolCall(id="call-a", name="image_step", args={"label": "A"})
        call_b = ToolCall(id="call-b", name="image_step", args={"label": "B"})
        state.record_assistant_step("", (call_a, call_b))
        state.record_tool_result(
            call_a,
            "完成 A",
            image_urls=("data:image/png;base64,iVBORw0KGgpwYXlsb2Fk",),
            image_message_name=MCP_TOOL_IMAGE_MESSAGE_NAME,
        )
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
    assert completed_batch.deferred_tool_media_count == 0
    tool_messages = [
        item for item in completed_batch.messages if isinstance(item, ToolMessage)
    ]
    image_messages = [
        item
        for item in completed_batch.messages
        if isinstance(item, ToolMediaMessage)
    ]
    assert [item.associated_message_id for item in image_messages] == [
        item.message_id for item in tool_messages
    ]


def test_interrupt_recovers_across_fresh_agents_and_repeated_response_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "checkpoints.sqlite3"
        from app.services import agent_service as agent_service_module

        monkeypatch.setattr(
            agent_service_module,
            "StateManager",
            lambda session_id: StateManager(session_id, db_path=str(db_path)),
        )
        resumed_calls: list[bool] = []
        all_agents = [
            await make_agent(db_path, [[tool_call("shot-1", name="screenshot")]], ResumableScreenshotTool(resumed_calls)),
            await make_agent(db_path, [[tool_call("shot-2", name="screenshot")]], ResumableScreenshotTool(resumed_calls)),
            await make_agent(db_path, [[StreamChunk(content="恢复完成")]], ResumableScreenshotTool(resumed_calls)),
            await make_agent(db_path, [], ResumableScreenshotTool(resumed_calls)),
        ]
        agents = list(all_agents)

        class Settings:
            model_name = "test"

        service = AgentService(
            chat_history_dao=None,
            chat_settings_loader=lambda session_id: Settings(),
            agent_factory=lambda settings: agents.pop(0),
        )

        first_stream = service.stream_chat(AgentInput(message="截屏"), "checkpoint-test")
        assert (await anext(first_stream)).type == EventType.TOOL_CALL
        assert (await anext(first_stream)).type == EventType.INTERRUPT

        # 生成器仍停在 interrupt yield 处时，另一连接已经能读取中断 checkpoint。
        manager = StateManager("checkpoint-test", db_path=str(db_path))
        try:
            first_interrupt = await manager.load()
            assert first_interrupt.is_interrupted()
            first_request_id = first_interrupt.interrupt_data["request_id"]
            assert first_interrupt.long_term_memory_turn_counter == 1
            assert await checkpoint_types(manager) == ["intermediate"]
        finally:
            await manager.close()
        await first_stream.aclose()
        assert all_agents[0].llm_client.closed is True

        # 即使创建新的 Agent，请求也能从 checkpoint 恢复，并再次产生持久化中断。
        second_events = await drain(service.resume_after_tool_authorization(
            "checkpoint-test",
            request_id=first_request_id,
            decision={"kind": "approve"},
        ))
        assert second_events[-1].type == EventType.INTERRUPT
        assert all_agents[1].llm_client.closed is True

        manager = StateManager("checkpoint-test", db_path=str(db_path))
        try:
            second_interrupt = await manager.load()
            second_request_id = second_interrupt.interrupt_data["request_id"]
            assert second_request_id != first_request_id
        finally:
            await manager.close()

        third_events = await drain(service.resume_after_tool_authorization(
            "checkpoint-test",
            request_id=second_request_id,
            decision={"kind": "reject"},
        ))
        assert any(event.type == EventType.TEXT_CHUNK for event in third_events)
        assert resumed_calls == [True, False]
        assert all_agents[2].llm_client.closed is True

        manager = StateManager("checkpoint-test", db_path=str(db_path))
        try:
            resumed_state = await manager.load()
            assert resumed_state.long_term_memory_turn_counter == 1
        finally:
            await manager.close()

        # 中断已消费，重复响应不得再次执行工具。
        with pytest.raises(ValueError, match="没有待处理的确认请求"):
            await drain(service.resume_after_tool_authorization(
                "checkpoint-test",
                request_id=second_request_id,
                decision={"kind": "reject"},
            ))
        assert resumed_calls == [True, False]
        assert agents == [all_agents[3]]
        assert all_agents[3].llm_client.closed is False
        await all_agents[3].close()

    asyncio.run(scenario())


def test_chat_resolves_unresolved_interrupt_before_new_message(tmp_path: Path) -> None:
    async def scenario() -> None:
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
                and "已拒绝" in message.content
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
    async def scenario() -> None:
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
                message.tool_call_id: message.content
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
    async def scenario() -> tuple[AgentState, list[bool]]:
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
        if isinstance(message, ToolMediaMessage)
        and message.associated_message_id
    )

    assert set(tool_positions) == {"image-1", "shot-2", "step-3"}
    assert media_position > max(tool_positions.values())
    assert resumed_calls == []


def test_successful_turn_replaces_intermediate_with_completed(tmp_path: Path) -> None:
    async def scenario() -> None:
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
