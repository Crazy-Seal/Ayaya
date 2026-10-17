"""验证审批状态归属及 Scheduler 保存成功后才发布决定的顺序。"""

import asyncio
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest

from app.agent.core.event_router import AgentEvent, EventType
from app.agent.runtime.session_inbox import SessionInbox
from app.agent.state import AgentState
from app.agent.state.manager import StateManager
from app.agent.state.messages import ToolCall
from app.schemas.runtime_types import SessionPhase
from app.orchestration.agent_runtime_scheduler import AgentRuntimeScheduler
from app.interaction.interaction_contract import ChannelCommand
from app.interaction.interaction_event_hub import InteractionEventHub


def approval_state() -> AgentState:
    """创建带恢复材料和剩余工具的合法审批 checkpoint 状态。

    Returns:
        AgentState: 当前等待审批、同批还有一个未执行调用的状态。
    """
    state = AgentState.create_new("s")
    current = ToolCall(id="call", name="read_file", args={"path": "a.py"})
    remaining = ToolCall(id="next", name="read_file", args={"path": "b.py"})
    state.record_assistant_step("", (current, remaining))
    state.clear_pending_tool_calls()
    state.set_interrupt({
        "type": "authorization_request", "request_id": "approval", "message": "允许读取？",
        "tool_call_id": current.id, "tool_name": current.name, "tool_args": current.args,
        "origin_channel": "desktop", "data": {"display": "文件读取"},
        "resume_state": {
            "authorization_request": {"snapshot": "original"},
            "tool_resume_state": {"phase": "prepared"}, "prepared_action": {"path": "a.py"},
            "hooks_started": True,
        },
        "extension": {"preserve": True},
    }, (remaining,))
    return state


@pytest.mark.parametrize("kind", ["approve", "reject", "steer"])
def test_first_decision_preserves_resume_material_and_rejects_later_changes(kind: str) -> None:
    """验证首次决定保留全部恢复字段，重复决定和系统取消不能覆盖。

    Args:
        kind (str): 当前测试的真人审批决定类别。
    """
    state = approval_state()
    original = deepcopy(state.interrupt_data)
    pending = state.pending_actions
    decision = {"kind": kind, "message": "新指令"} if kind == "steer" else {"kind": kind}
    # 1. 返回审批快照不能让调用方原地改变状态中的工具恢复材料。
    snapshot = state.get_pending_authorization("approval")
    assert snapshot is not None
    snapshot["resume_state"]["tool_resume_state"]["phase"] = "changed"
    assert state.interrupt_data == original
    # 2. 记录决定只改变决定字段，命令对象后续变化不能修改已记录事实。
    assert state.record_authorization_decision("approval", decision)
    expected = {**original, "runtime_decision": deepcopy(decision)}
    decision["kind"] = "reject" if kind != "reject" else "approve"
    assert state.interrupt_data == expected and state.pending_actions == pending
    assert not state.record_authorization_decision("approval", {"kind": "approve"})
    assert not state.cancel_pending_interrupt()
    assert state.get_pending_authorization("approval") is None
    # 3. 持久化格式继续使用原字典，重开 checkpoint 后字段及剩余调用仍相同。
    restored = AgentState.from_checkpoint(state.to_checkpoint())
    assert restored.interrupt_data == expected and restored.pending_actions == pending


def test_stale_wrong_type_and_invalid_decisions_do_not_modify_state() -> None:
    """验证旧编号、其他中断类型和无效决定均不能修改当前审批。"""
    state = approval_state()
    before = state.to_checkpoint()
    assert state.get_pending_authorization("old") is None
    assert not state.record_authorization_decision("old", {"kind": "approve"})
    with pytest.raises(ValueError, match="无效的审批决定"):
        state.record_authorization_decision("approval", {"kind": "invalid"})
    assert state.to_checkpoint() == before
    state.set_interrupt({**state.interrupt_data, "type": "custom_wait"}, state.pending_actions)
    before = state.to_checkpoint()
    assert not state.record_authorization_decision("approval", {"kind": "approve"})
    assert state.to_checkpoint() == before


def test_system_cancellation_preserves_resume_material_and_first_decision() -> None:
    """验证系统取消只增加决定，保留恢复材料，且无法被后来批准覆盖。"""
    state = approval_state()
    original = deepcopy(state.interrupt_data)
    pending = state.pending_actions
    assert state.cancel_pending_interrupt()
    expected = {**original, "runtime_decision": {"kind": "cancelled"}}
    assert state.interrupt_data == expected and state.pending_actions == pending
    assert not state.cancel_pending_interrupt()
    assert not state.record_authorization_decision("approval", {"kind": "approve"})
    assert AgentState.from_checkpoint(state.to_checkpoint()).interrupt_data == expected
    state.clear_interrupt()
    assert not state.cancel_pending_interrupt()


class WaitingAgent:
    """阻塞后续回答，只替换与本测试无关的 Agent 执行，不触发模型或工具。

    Attributes:
        waiting (asyncio.Event): 等待测试清理取消 worker 的屏障。
    """
    def __init__(self) -> None:
        """准备阻塞回答的异步屏障。"""
        self.waiting = asyncio.Event()

    async def run(self, message: str, **kwargs):
        """让新指令停在回答前，供测试检查 Scheduler 已提交的状态。

        Args:
            message (str): 已领取的新指令正文。
            **kwargs (dict): Scheduler 的输入、控制和回调参数。

        Yields:
            AgentEvent: 屏障结束时的完成事件，测试正常清理会先取消等待。
        """
        await self.waiting.wait()
        yield AgentEvent(EventType.DONE, None)

    async def close(self) -> None:
        """替身不持有外部资源，worker 取消后无需额外清理。"""
        pass


def make_scheduler(path: str) -> AgentRuntimeScheduler:
    """装配真实 Scheduler、Inbox、状态存储及事件流，只阻塞后续回答。

    Args:
        path (str): 测试专用 SQLite 路径。

    Returns:
        AgentRuntimeScheduler: 使用真实持久化与事件发布的调度器。
    """
    def state_factory(session: str) -> StateManager:
        """为 Scheduler 的读取与保存创建隔离连接。

        Args:
            session (str): 需要访问的会话。

        Returns:
            StateManager: 测试数据库访问对象。
        """
        return StateManager(session, path)

    def inbox_factory(session: str) -> SessionInbox:
        """为命令提交和 worker 创建真实隔离输入队列。

        Args:
            session (str): 需要访问的会话。

        Returns:
            SessionInbox: 测试数据库输入队列。
        """
        return SessionInbox(session, path)

    def agent_factory(session: str) -> WaitingAgent:
        """在输入已保存后阻塞后续回答，不参与审批保存规则。

        Args:
            session (str): 即将回答的会话。

        Returns:
            WaitingAgent: 可随 worker 一起取消的执行替身。
        """
        return WaitingAgent()

    return AgentRuntimeScheduler(
        InteractionEventHub(), agent_factory=agent_factory,
        inbox_factory=inbox_factory, state_manager_factory=state_factory,
    )


@pytest.mark.parametrize("operation", ["approve", "cancel"])
def test_decision_save_failure_publishes_nothing_and_retry_uses_persisted_state(
    tmp_path: Path, operation: str,
) -> None:
    """验证批准及新消息取消在保存失败时不发布事件，后续重试仍能处理旧审批。

    Args:
        tmp_path (Path): 测试专用 SQLite 目录。
        operation (str): 真人批准或新消息触发的系统取消。
    """
    async def scenario() -> None:
        """使用真实触发器拒绝状态保存，通过公开命令和事件流核对失败与重试。"""
        path = str(tmp_path / "state.sqlite3")
        scheduler = make_scheduler(path)
        manager = StateManager("s", path)
        subscription = None
        next_event = None
        try:
            await scheduler.start()
            state = approval_state()
            await manager.save(state, checkpoint_type="intermediate")
            scheduler._phases["s"] = SessionPhase.PENDING_INTERRUPT
            subscription = scheduler.events.subscribe("s", "desktop")
            await anext(subscription)
            next_event = asyncio.create_task(anext(subscription))
            # 1. 审批已经持久等待，拒绝接下来的 checkpoint 插入以模拟保存失败。
            with closing(sqlite3.connect(path)) as inspection:
                inspection.execute(
                    "CREATE TRIGGER reject_decision BEFORE INSERT ON checkpoints "
                    "WHEN NEW.session_id = 's' BEGIN SELECT RAISE(ABORT, 'reject decision'); END",
                )
                inspection.commit()
            command = ChannelCommand("s", "desktop", "resolve_authorization", {"kind": "approve"},
                                     request_id="approval") if operation == "approve" else ChannelCommand(
                "s", "desktop", "human_turn", {"message": "取消旧审批并开始新指令"},
            )
            with pytest.raises(sqlite3.IntegrityError, match="reject decision"):
                await scheduler.submit(command)
            # 2. 已加载对象中的决定未提交，数据库仍未决，事件接收方仍在等待。
            assert (await manager.load()).get_pending_authorization("approval") is not None
            await asyncio.sleep(0)
            assert not next_event.done()
            with closing(sqlite3.connect(path)) as inspection:
                inspection.execute("DROP TRIGGER reject_decision")
                inspection.commit()
            # 3. 重试读取持久旧状态；保存成功之后才发布决定，恢复信息仍保持原值。
            assert (await scheduler.submit(command)).accepted
            event = await asyncio.wait_for(next_event, 1)
            assert event.kind == "authorization_resolved"
            assert event.payload["outcome"] == ("approve" if operation == "approve" else "cancelled")
            restored = await manager.load()
            assert restored.interrupt_data["resume_state"] == state.interrupt_data["resume_state"]
            assert restored.pending_actions == state.pending_actions
        finally:
            if next_event is not None and not next_event.done():
                next_event.cancel()
                await asyncio.gather(next_event, return_exceptions=True)
            if subscription is not None:
                await subscription.aclose()
            await manager.close()
            await scheduler.close()

    asyncio.run(scenario())


def test_steer_inbox_failure_preserves_approval_and_retry_saves_input_first(tmp_path: Path) -> None:
    """验证引导先保存新消息，Inbox 失败不处理旧审批，重试保持保存顺序。

    Args:
        tmp_path (Path): 测试专用 SQLite 目录。
    """
    async def scenario() -> None:
        """用两个触发器分别注入 Inbox 失败和检查新消息先于决定持久保存。"""
        path = str(tmp_path / "state.sqlite3")
        scheduler = make_scheduler(path)
        manager = StateManager("s", path)
        try:
            await scheduler.start()
            await manager.save(approval_state(), checkpoint_type="intermediate")
            command = ChannelCommand(
                "s", "desktop", "resolve_authorization", {"kind": "steer", "message": "新指令"},
                request_id="approval",
            )
            # 1. Inbox 拒绝新输入时，旧审批仍未决，也不留下待执行新输入。
            with closing(sqlite3.connect(path)) as inspection:
                inspection.execute(
                    "CREATE TRIGGER reject_steer BEFORE INSERT ON session_inbox "
                    "BEGIN SELECT RAISE(ABORT, 'reject steer'); END",
                )
                inspection.commit()
            with pytest.raises(sqlite3.IntegrityError, match="reject steer"):
                await scheduler.submit(command)
            assert (await manager.load()).get_pending_authorization("approval") is not None
            with closing(sqlite3.connect(path)) as inspection:
                assert inspection.execute("SELECT COUNT(*) FROM session_inbox").fetchone()[0] == 0
                inspection.execute("DROP TRIGGER reject_steer")
                # 2. 真实保存顺序必须先有 Inbox 新消息，否则拒绝带引导决定的 checkpoint。
                inspection.execute(
                    "CREATE TRIGGER guard_steer BEFORE INSERT ON checkpoints "
                    "WHEN json_extract(NEW.state_json, '$.interrupt_data.runtime_decision.kind') = 'steer' "
                    "AND NOT EXISTS (SELECT 1 FROM session_inbox WHERE session_id = 's' "
                    "AND json_extract(payload_json, '$.message') = '新指令') "
                    "BEGIN SELECT RAISE(ABORT, 'input must commit first'); END",
                )
                inspection.commit()
            assert (await scheduler.submit(command)).accepted
            restored = await manager.load()
            assert restored.interrupt_data["runtime_decision"] == command.payload
            with closing(sqlite3.connect(path)) as inspection:
                assert inspection.execute("SELECT COUNT(*) FROM session_inbox").fetchone()[0] == 1
            # 3. 旧请求随后过期，不能再次保存同一引导消息。
            assert not (await scheduler.submit(command)).accepted
            with closing(sqlite3.connect(path)) as inspection:
                assert inspection.execute("SELECT COUNT(*) FROM session_inbox").fetchone()[0] == 1
        finally:
            await manager.close()
            await scheduler.close()

    asyncio.run(scenario())
