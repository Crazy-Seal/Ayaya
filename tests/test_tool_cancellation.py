"""通过运行控制与实际 Agent 入口验证工具停止、引导和保存顺序。"""

import asyncio
from pathlib import Path

import pytest

from app.agent.runtime.turn_control import TurnInterrupted
from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.core.events import AgentEvent, EventType
from app.agent.models.contracts import StreamChunk
from app.agent.plugins.base import BasePlugin, HookContext, PluginHook
from app.agent.state.messages import ToolCall, ToolMessage
from app.agent.state.manager import RequiredSaveError
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.authorization.manager import ToolAuthorization
from app.crud.sqlite_factory import SqliteFactory
from app.schemas.chat_settings import ChatSettings
from app.schemas.tool_types import AuthorizationTiming
from app.schemas.runtime_types import AgentRunOutcomeKind, CheckpointType
from tests.runtime_support import make_execution_control
from tests.test_checkpoint_recovery import make_agent, ScriptedLLM


def test_stop_waits_for_tool_cleanup_before_ending_invocation() -> None:
    """用户停止取消工具，工具清理完成后才允许本轮开始收尾保存。"""
    async def scenario() -> None:
        """用清理屏障验证停止及重复唤醒均等待工具释放资源。"""
        machine, control = make_execution_control("run")
        entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

        async def tool() -> str:
            """等待停止并在 finally 中模拟异步资源释放。"""
            entered.set()
            try:
                await asyncio.Event().wait()
                return "完成"
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()

        invocation = asyncio.create_task(control.run_tool(tool))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert machine.request_stop("run").accepted
            control.wake_interrupt()
            await asyncio.wait_for(cleaning.wait(), 1)
            control.wake_interrupt()
            assert not invocation.done()
            release.set()
            with pytest.raises(TurnInterrupted, match="^stop$"):
                await asyncio.wait_for(invocation, 1)
            assert cleaned.is_set()
        finally:
            release.set()
            if not invocation.done():
                invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)

    asyncio.run(scenario())


def test_steer_waits_for_tool_result_and_later_stop_can_cancel() -> None:
    """引导等待工具边界，随后的用户停止仍可取消同一个工具。"""
    async def scenario() -> None:
        """让工具主动观察引导请求，确认它继续执行到停止到达。"""
        machine, control = make_execution_control("run")
        entered, inspect, inspected = (asyncio.Event() for _ in range(3))

        async def tool() -> str:
            """在引导之后回报仍在运行，然后等待后续停止。"""
            entered.set()
            await inspect.wait()
            inspected.set()
            await asyncio.Event().wait()
            return "完成"

        invocation = asyncio.create_task(control.run_tool(tool))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert machine.request_steer("input", "run").accepted
            control.wake_interrupt()
            inspect.set()
            await asyncio.wait_for(inspected.wait(), 1)
            assert not invocation.done()
            assert machine.request_stop("run").accepted
            control.wake_interrupt()
            with pytest.raises(TurnInterrupted, match="^stop$"):
                await asyncio.wait_for(invocation, 1)
        finally:
            invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("stop_accepted", [False, True])
def test_outer_cancellation_propagates_after_tool_cleanup(stop_accepted: bool) -> None:
    """外层执行关闭传播 CancelledError，并等待实际工具释放资源。

    Args:
        stop_accepted (bool): 关闭之前是否已经受理用户停止。
    """
    async def scenario() -> None:
        """取消等待工具的外层任务，验证实际工具先完成 finally。"""
        machine, control = make_execution_control("run")
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def tool() -> str:
            """让外层取消进入工具并记录资源释放。"""
            entered.set()
            try:
                await asyncio.Event().wait()
                return "完成"
            finally:
                cleaned.set()

        invocation = asyncio.create_task(control.run_tool(tool))
        await asyncio.wait_for(entered.wait(), 1)
        if stop_accepted:
            machine.request_stop("run")
        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(invocation, 1)
        assert cleaned.is_set()

    asyncio.run(scenario())


def test_completed_tool_result_survives_stop_before_waiter_resumes() -> None:
    """工具已经返回时收到停止，保留真实结果并在后续安全点结束轮次。"""
    async def scenario() -> None:
        """让工具任务完成回调发出停止，制造结果返回与控制唤醒的竞争。"""
        machine, control = make_execution_control("run")

        def stop() -> None:
            """在结果已产生、调用方恢复之前接受停止。"""
            machine.request_stop("run")
            control.wake_interrupt()

        async def tool() -> str:
            """登记完成回调并返回已经可靠取得的结果。"""
            asyncio.get_running_loop().call_soon(stop)
            return "真实结果"

        assert await control.run_tool(tool) == "真实结果"
        with pytest.raises(TurnInterrupted, match="^stop$"):
            control.check()

    asyncio.run(scenario())


class BarrierTool(BaseTool):
    """按标签立即返回结果或等待控制，用于验证真实 Agent 的工具执行顺序。"""

    name = "barrier"
    description = "执行测试动作"
    parameters_schema = {"type": "object", "properties": {"label": {"type": "string"}}}

    def __init__(self) -> None:
        """准备当前工具的执行记录及等待、清理屏障。"""
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.executed: list[str] = []

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """先记录实际执行，再按标签决定是否等待测试释放。

        Args:
            args (dict): 当前动作标签，waiting 表示等待取消或释放。
            context (ToolContext): 本轮由 Pipeline 提供的执行上下文。

        Returns:
            ToolResult: 当前标签的真实完成正文。
        """
        label = args["label"]
        self.executed.append(label)
        if label == "waiting":
            self.entered.set()
            try:
                await self.release.wait()
            finally:
                self.cleaned.set()
        return ToolResult.success(f"完成 {label}")


def test_agent_stop_preserves_results_and_closes_unfinished_calls(tmp_path: Path) -> None:
    """工具运行期间停止后，已完成结果保留，当前及后续调用以取消结果闭合。

    Args:
        tmp_path (Path): 本用例独占的会话数据库目录。
    """
    async def scenario() -> None:
        """通过真实 Agent 保存与事件入口核对结果、清理和后续工具执行。"""
        tool = BarrierTool()
        calls = tuple(ToolCall(label, tool.name, {"label": label}) for label in ("first", "waiting", "later"))
        agent = await make_agent(tmp_path / "state.sqlite3", [[StreamChunk(tool_call=call) for call in calls]], tool)
        machine, control = make_execution_control("run")
        events: list[AgentEvent] = []

        async def receive(event: AgentEvent) -> None:
            """在完成事件到达时查询真实已保存结果。

            Args:
                event (AgentEvent): Agent 发布的当前执行进展。
            """
            events.append(event)
            if event.type == EventType.DONE:
                saved = await agent.session_state_manager.load()
                assert not saved.pending_tool_calls
                assert tool.cleaned.is_set()

        invocation = asyncio.create_task(agent.run("执行", turn_control=control, emit=receive))
        try:
            await asyncio.wait_for(tool.entered.wait(), 3)
            machine.request_stop("run")
            control.wake_interrupt()
            done, _ = await asyncio.wait({invocation}, timeout=2)
            assert invocation in done, "用户停止应主动结束工具等待"
            outcome = invocation.result()
            assert outcome.kind == AgentRunOutcomeKind.STOPPED
            saved = await agent.session_state_manager.load()
            results = [message for message in saved.messages if isinstance(message, ToolMessage)]
            assert [(message.tool_call_id, message.text) for message in results] == [
                ("first", "完成 first"),
                ("waiting", "被用户取消，工具调用未完成"),
                ("later", "被用户取消，工具调用未完成"),
            ]
            assert tool.executed == ["first", "waiting"]
            assert [event.data["tool_call_id"] for event in events if event.type == EventType.TOOL_COMPLETED] == ["first"]
            assert events[-1].type == EventType.DONE
        finally:
            tool.release.set()
            invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)
            await agent.close()

    asyncio.run(scenario())


def test_agent_steer_saves_current_tool_before_closing_later_calls(tmp_path: Path) -> None:
    """普通工具收到引导后继续完成，真实结果保存成功后结束旧轮次。

    Args:
        tmp_path (Path): 本用例独占的会话数据库目录。
    """
    async def scenario() -> None:
        """释放当前工具，验证引导保留结果并阻止后续调用。"""
        tool = BarrierTool()
        calls = tuple(ToolCall(label, tool.name, {"label": label}) for label in ("waiting", "later"))
        agent = await make_agent(tmp_path / "state.sqlite3", [[StreamChunk(tool_call=call) for call in calls]], tool)
        machine, control = make_execution_control("run")

        async def receive(event: AgentEvent) -> None:
            """通过正式事件入口消费执行进展。

            Args:
                event (AgentEvent): Agent 发布的当前执行进展。
            """

        invocation = asyncio.create_task(agent.run("执行", turn_control=control, emit=receive))
        try:
            await asyncio.wait_for(tool.entered.wait(), 3)
            assert machine.request_steer("input", "run").accepted
            control.wake_interrupt()
            tool.release.set()
            assert (await asyncio.wait_for(invocation, 3)).kind == AgentRunOutcomeKind.STEERED
            saved = await agent.session_state_manager.load()
            results = [message for message in saved.messages if isinstance(message, ToolMessage)]
            assert [(message.tool_call_id, message.text) for message in results] == [
                ("waiting", "完成 waiting"), ("later", "用户打断，本次工具调用已取消。"),
            ]
            assert tool.executed == ["waiting"]
        finally:
            tool.release.set()
            invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)
            await agent.close()

    asyncio.run(scenario())


class BarrierHook(BasePlugin):
    """暂停指定工具钩子，验证用户停止等待钩子完成后再收尾。"""

    name = "barrier_hook"

    def __init__(self, hook: PluginHook) -> None:
        """保存需要等待的工具钩子及本次清理屏障。

        Args:
            hook (PluginHook): 本次需要暂停的工具执行前或完成钩子。
        """
        self.hook = hook
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False

    @property
    def hooks(self) -> list[PluginHook]:
        """订阅本次测试指定的工具执行前或完成钩子。

        Returns:
            list[PluginHook]: 本插件负责暂停的钩子。
        """
        return [self.hook]

    async def execute(self, context: HookContext) -> None:
        """在钩子中等待释放，模拟异步插件业务。

        Args:
            context (HookContext): 本次钩子共享的 Agent 状态及工具结果。
        """
        self.entered.set()
        await self.release.wait()
        self.finished = True
        context.agent_state.extra["tool_hook_finished"] = True


@pytest.mark.parametrize("hook", [PluginHook.BEFORE_TOOL, PluginHook.AFTER_TOOL])
def test_stop_during_tool_hook_waits_and_preserves_real_result(tmp_path: Path, hook: PluginHook) -> None:
    """工具钩子等待期间停止，执行前阻止启动，完成后保留真实结果及钩子更新。

    Args:
        tmp_path (Path): 本用例独占的会话数据库目录。
        hook (PluginHook): 停止发生在工具执行前还是完成后的钩子。
    """
    async def scenario() -> None:
        """通过真实插件执行与保存事件检查停止时的结果归属。"""
        tool, plugin = BarrierTool(), BarrierHook(hook)
        agent = await make_agent(tmp_path / "state.sqlite3", [[StreamChunk(tool_call=ToolCall(
            "first", tool.name, {"label": "first"},
        ))]], tool)
        agent.register_plugin(plugin)
        machine, control = make_execution_control("run")
        events: list[AgentEvent] = []

        async def receive(event: AgentEvent) -> None:
            """结果完成事件到达时，确认真实工具结果已可查询。

            Args:
                event (AgentEvent): Agent 发布的工具完成或轮次结束进展。
            """
            events.append(event)
            if event.type == EventType.TOOL_COMPLETED:
                saved = await agent.session_state_manager.load()
                assert saved.extra["tool_hook_finished"]
                assert any(isinstance(item, ToolMessage) and item.text == "完成 first" for item in saved.messages)

        invocation = asyncio.create_task(agent.run("执行", turn_control=control, emit=receive))
        try:
            await asyncio.wait_for(plugin.entered.wait(), 3)
            machine.request_stop("run")
            control.wake_interrupt()
            assert not invocation.done()
            plugin.release.set()
            assert (await asyncio.wait_for(invocation, 3)).kind == AgentRunOutcomeKind.STOPPED
            assert plugin.finished
            saved = await agent.session_state_manager.load()
            results = [item for item in saved.messages if isinstance(item, ToolMessage)]
            completed = hook == PluginHook.AFTER_TOOL
            assert [item.text for item in results] == (["完成 first"] if completed else ["被用户取消，工具调用未完成"])
            assert tool.executed == (["first"] if completed else [])
            assert sum(event.type == EventType.TOOL_COMPLETED for event in events) == int(completed)
        finally:
            plugin.release.set()
            invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)
            await agent.close()

    asyncio.run(scenario())


class PreparedBarrierTool(BarrierTool):
    """只读准备后等待统一授权，获准执行复用普通工具的等待屏障。"""

    authorization_timing = AuthorizationTiming.AFTER_PREPARE

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """准备一次具体动作，获准重入时才开始真正等待。

        Args:
            args (dict): 当前实际执行的动作标签。
            context (ToolContext): 本次已授权材料及恢复数据。

        Returns:
            ToolResult: 首次调用的准备材料或实际执行结果。
        """
        if context.resume_data is None:
            return ToolResult.prepared({"action": "test"}, resume_state={"prepared": True})
        return await super().execute(args, context)


@pytest.mark.parametrize("prepared,policy", [(False, "ask"), (True, "allow"), (True, "ask")])
def test_stop_cancels_authorized_and_resumed_tool(tmp_path: Path, prepared: bool, policy: str) -> None:
    """获准执行、准备后执行和审批恢复均能停止，取消结果沿同一保存链闭合。

    Args:
        tmp_path (Path): 本用例独占的会话数据库目录。
        prepared (bool): 是否需要先准备具体动作，再获准执行。
        policy (str): 本次工具权限是直接允许还是等待审批。
    """
    async def scenario() -> None:
        """实际保存审批决定并恢复原轮次，再通过工具屏障发起停止。"""
        tool = PreparedBarrierTool() if prepared else BarrierTool()
        settings = ChatSettings(session_id="s", model_name="test", openai_api_key="test",
            openai_base_url="http://127.0.0.1:1/v1", temperature=0, system_prompt="test",
            context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
            tool_permissions={"local:barrier": policy})
        calls = [StreamChunk(tool_call=ToolCall(label, tool.name, {"label": label})) for label in ("waiting", "later")]
        agent = Agent(AgentConfig("s", "test", "test", ContextStrategyConfig(compression_trigger_tokens_k=100)),
            SqliteFactory(tmp_path / "state.sqlite3"), llm_client=ScriptedLLM([calls]),
            tool_authorization=ToolAuthorization(lambda _session: settings))
        agent.register_tool(tool)
        machine, control = make_execution_control("run")
        events: list[AgentEvent] = []

        async def receive(event: AgentEvent) -> None:
            """观察正式审批和执行事件，供测试取得待审批请求编号。

            Args:
                event (AgentEvent): Agent 发布的审批等待或执行进展。
            """
            events.append(event)

        invocation = None
        try:
            if policy == "ask":
                outcome = await agent.run("执行", turn_control=control, emit=receive)
                assert outcome.kind == AgentRunOutcomeKind.INTERRUPTED
                request_id = events[-1].data["request_id"]
                await agent.session_state_manager.record_authorization_decision(request_id, {"kind": "approve"})
                assert machine.resume_approval("run", request_id).accepted
                invocation = asyncio.create_task(agent.resume(request_id, turn_control=control, emit=receive))
            else:
                invocation = asyncio.create_task(agent.run("执行", turn_control=control, emit=receive))
            await asyncio.wait_for(tool.entered.wait(), 3)
            machine.request_stop("run")
            control.wake_interrupt()
            done, _ = await asyncio.wait({invocation}, timeout=3)
            assert invocation in done
            assert invocation.result().kind == AgentRunOutcomeKind.STOPPED
            saved = await agent.session_state_manager.load()
            assert [(item.tool_call_id, item.text) for item in saved.messages if isinstance(item, ToolMessage)] == [
                ("waiting", "被用户取消，工具调用未完成"), ("later", "被用户取消，工具调用未完成"),
            ]
            assert tool.executed == ["waiting"]
            assert tool.cleaned.is_set()
            assert saved.interrupt_data is None
            assert not saved.pending_tool_calls
        finally:
            tool.release.set()
            if invocation is not None:
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)
            await agent.close()

    asyncio.run(scenario())


def test_failed_stop_save_reports_failure_without_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """工具已被取消但最终保存失败时，报告保存故障且不发布本轮完成。

    Args:
        tmp_path (Path): 本用例隔离的会话数据库。
        monkeypatch (pytest.MonkeyPatch): 在公共保存接口模拟最终持久化失败。
    """
    async def scenario() -> None:
        """保留前面的真实进度，只让工具停止后的最终保存失败。"""
        tool = BarrierTool()
        agent = await make_agent(tmp_path / "state.sqlite3", [[StreamChunk(tool_call=ToolCall(
            "waiting", tool.name, {"label": "waiting"},
        ))]], tool)
        save = agent.session_state_manager.save

        async def fail_completed(state, *, checkpoint_type, **kwargs):
            """前置保存使用真实 SQLite，结束保存返回指定故障。

            Args:
                state (AgentState): 本次要保存的协议状态。
                checkpoint_type (CheckpointType): 当前保存是进度还是轮次结束。
                kwargs (dict): 输入交付等原有保存选项。

            Returns:
                int: 进度保存成功后的快照编号。

            Raises:
                RequiredSaveError: 本轮停止结果无法保存。
            """
            if checkpoint_type == CheckpointType.COMPLETED:
                raise RequiredSaveError("停止结果保存失败")
            return await save(state, checkpoint_type=checkpoint_type, **kwargs)

        monkeypatch.setattr(agent.session_state_manager, "save", fail_completed)
        machine, control = make_execution_control("run")
        events: list[AgentEvent] = []

        async def receive(event: AgentEvent) -> None:
            """记录真实执行事件，检查最终保存失败后的对外结果。

            Args:
                event (AgentEvent): Agent 发布的当前执行进展。
            """
            events.append(event)

        invocation = asyncio.create_task(agent.run("执行", turn_control=control, emit=receive))
        try:
            await asyncio.wait_for(tool.entered.wait(), 3)
            machine.request_stop("run")
            control.wake_interrupt()
            done, _ = await asyncio.wait({invocation}, timeout=3)
            assert invocation in done
            outcome = invocation.result()
            assert outcome.kind == AgentRunOutcomeKind.SAVE_FAILED
            assert outcome.error == "停止结果保存失败"
            assert tool.cleaned.is_set()
            assert all(event.type != EventType.DONE for event in events)
            assert (await agent.session_state_manager.load()).pending_tool_calls
        finally:
            tool.release.set()
            invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)
            await agent.close()

    asyncio.run(scenario())
