import asyncio

from app.agent.state import AgentState
from app.agent.state.manager import StateManager
from app.agent.state.messages import ToolCall
from app.services.desktop_execution_broker import DesktopExecutionBroker, DesktopExecutionRequest


def test_desktop_execution_claim_is_single_use_and_result_returns_to_runtime(tmp_path) -> None:
    """验证屏幕请求只能领取一次且结果交回后端运行时。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：屏幕请求只能领取一次且结果交回后端运行时。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        manager = StateManager("s", db_path)
        state = AgentState.create_new("s")
        state.add_user_message("请截图", origin_channel="qq")
        call = ToolCall(id="c", name="system_screenshot", args={})
        state.record_assistant_step("", (call,), origin_channel="qq")
        state.clear_pending_tool_calls()
        state.set_interrupt({
            "type": "continuation_request", "request_id": "r",
            "tool_call_id": "c", "tool_name": "system_screenshot",
            "tool_args": {}, "resume_state": {"continuation": "capture_screen"},
            "data": {"kind": "capture_screen"}, "origin_channel": "qq",
        })
        await manager.save(state, checkpoint_type="intermediate")
        await manager.close()
        returned: list[tuple[str, str, dict]] = []

        class FakeAgent:
            """提供隔离测试替身，用于验证：屏幕请求只能领取一次且结果交回后端运行时。"""
            async def validate_pending_continuation(self, _interrupt):
                """执行该回归场景中的局部测试步骤。

                Args:
                    _interrupt (对象): 测试保留的待复核中断快照。
                """
                return True

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        broker = DesktopExecutionBroker(
            state_manager_factory=lambda session: StateManager(session, db_path),
            agent_factory=lambda _session: FakeAgent(),
            on_result=lambda session, request, result: returned.append((session, request, result)),
        )
        stream = broker.subscribe()
        await broker.enqueue(DesktopExecutionRequest("s", "r", "capture_screen", {}))
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert (await asyncio.wait_for(anext(stream), 1)).request_id == "r"
        assert await broker.claim("s", "r")
        assert not await broker.claim("s", "r")
        assert await broker.submit_result("s", "r", {"screenshot_data": "data:image/png;base64,abc"})
        assert not await broker.submit_result("s", "r", {})
        assert returned == [("s", "r", {"screenshot_data": "data:image/png;base64,abc"})]
        await stream.aclose()

    asyncio.run(scenario())


def test_revoked_screen_grant_fails_without_executing_and_unblocks_run(tmp_path) -> None:
    """验证撤销授权后不执行屏幕动作并解除运行等待。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：撤销授权后不执行屏幕动作并解除运行等待。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        state = AgentState.create_new("s")
        call = ToolCall(id="c", name="control_screen", args={})
        state.record_assistant_step("", (call,))
        state.clear_pending_tool_calls()
        state.set_interrupt({
            "type": "continuation_request", "request_id": "r",
            "tool_call_id": "c", "tool_name": "control_screen",
            "resume_state": {"continuation": "perform_screen_action"},
            "data": {"kind": "perform_screen_action"},
        })
        manager = StateManager("s", db_path)
        await manager.save(state, checkpoint_type="intermediate")
        await manager.close()
        resumed: list[dict] = []

        class FakeAgent:
            """提供隔离测试替身，用于验证：撤销授权后不执行屏幕动作并解除运行等待。"""
            async def validate_pending_continuation(self, _interrupt):
                """执行该回归场景中的局部测试步骤。

                Args:
                    _interrupt (对象): 测试保留的待复核中断快照。
                """
                return False

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        broker = DesktopExecutionBroker(
            state_manager_factory=lambda session: StateManager(session, db_path),
            agent_factory=lambda _session: FakeAgent(),
            on_result=lambda _session, _request, result: resumed.append(result),
        )
        await broker.enqueue(DesktopExecutionRequest("s", "r", "perform_screen_action", {}))
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert not await broker.claim("s", "r")
        assert resumed == [{"executed": False, "error": "工具授权已失效"}]
        assert not await broker.claim("s", "r")
        manager = StateManager("s", db_path)
        try:
            interrupted = (await manager.load()).interrupt_data
            assert interrupted["desktop_result"] == resumed[0]
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_slow_desktop_executor_disconnects_and_reconnects_from_pending_requests() -> None:
    """验证慢执行器重连可以恢复仍待领取的请求。"""
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：慢执行器重连可以恢复仍待领取的请求。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        broker = DesktopExecutionBroker(
            state_manager_factory=lambda _session: None,
            agent_factory=lambda _session: None,
            on_result=lambda _session, _request, _result: None,
        )
        stream = broker.subscribe()
        first = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await broker.enqueue(DesktopExecutionRequest("s", "r0", "capture_screen", {}))
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert (await first).request_id == "r0"
        for index in range(1, 66):
            await broker.enqueue(DesktopExecutionRequest(
                "s", f"r{index}", "capture_screen", {},
            ))
        try:
            await anext(stream)
            raise AssertionError("慢执行器应被断开")
        except StopAsyncIteration:
            pass
        recovered = broker.subscribe()
        assert (await asyncio.wait_for(anext(recovered), 1)).request_id == "r0"
        await recovered.aclose()

    asyncio.run(scenario())
