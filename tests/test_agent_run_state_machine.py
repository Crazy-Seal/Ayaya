"""通过运行控制接口验证父子共用的阶段与轮次保护。"""

import pytest

from app.agent.runtime.agent_run_state_machine import AgentRunStateMachine, InvalidStateTransitionError
from app.agent.runtime.turn_control import TurnControl, TurnInterrupted
from app.schemas.runtime_types import ApprovalWait, CommandStatus, ExecutionPhase, TurnInterruptReason


def test_stop_overrides_pending_steer_and_keeps_run_identity() -> None:
    """当前工具等待引导边界时，用户停止取得优先级且仍作用于原轮次。"""
    machine = AgentRunStateMachine("context")
    machine.begin_run("first")
    assert machine.request_steer(expected_run_id="first").accepted
    assert machine.request_stop("expired").status == CommandStatus.STALE
    assert machine.reason == TurnInterruptReason.STEER
    assert machine.request_stop("first").accepted
    assert machine.reason == TurnInterruptReason.STOP
    assert machine.phase == ExecutionPhase.STOPPING
    assert machine.run_id == "first"
    assert machine.request_steer(expected_run_id="first").status == CommandStatus.TOO_LATE
    assert machine.reason == TurnInterruptReason.STOP


def test_steer_during_pending_steer_is_too_late_and_stop_can_take_over() -> None:
    """执行已在结束当前回答时，后续引导保持原控制原因，停止仍可接管。"""
    machine = AgentRunStateMachine("context")
    machine.begin_run("first")
    assert machine.request_steer().accepted
    assert machine.request_steer().status == CommandStatus.TOO_LATE
    assert machine.phase == ExecutionPhase.STOPPING
    assert machine.reason == TurnInterruptReason.STEER
    assert machine.request_stop().accepted
    assert machine.request_stop().accepted
    assert machine.reason == TurnInterruptReason.STOP


def test_steer_checks_target_and_final_save_phase() -> None:
    """引导沿用显式目标轮次和最终保存保护，空闲时返回无活动执行。"""
    machine = AgentRunStateMachine("context")
    assert machine.request_steer().status == CommandStatus.STALE
    machine.begin_run("first")
    assert machine.request_steer(expected_run_id="expired").status == CommandStatus.STALE
    assert machine.phase == ExecutionPhase.RUNNING
    machine.begin_commit("first")
    assert machine.request_steer(expected_run_id="first").status == CommandStatus.TOO_LATE
    assert machine.phase == ExecutionPhase.COMMITTING


def test_committing_rejects_stop_and_illegal_tool_execution() -> None:
    """最终回答开始提交后，停止和下一次工具执行都不能改变该提交。"""
    machine = AgentRunStateMachine("context")
    machine.begin_run("first")
    control = TurnControl("first", machine)
    control.start_llm()
    control.begin_commit()
    assert machine.request_stop("first").status == CommandStatus.TOO_LATE
    with pytest.raises(InvalidStateTransitionError):
        control.start_tool()
    assert machine.phase == ExecutionPhase.COMMITTING


def test_old_control_cannot_change_next_run() -> None:
    """引导开启下一轮后，旧轮控制不再接受停止或推进阶段。"""
    machine = AgentRunStateMachine("context")
    machine.begin_run("first")
    old = TurnControl("first", machine)
    machine.begin_commit("first")
    machine.commit_succeeded("first")
    machine.begin_run("second")
    new = TurnControl("second", machine)
    assert machine.request_stop("first").status == CommandStatus.STALE
    with pytest.raises(TurnInterrupted):
        old.start_llm()
    new.start_llm()
    assert machine.run_id == "second"
    assert machine.phase == ExecutionPhase.RUNNING


def test_approval_resume_keeps_identity_and_checks_request() -> None:
    """已保存审批只能由匹配的请求恢复，继续使用原轮次身份。"""
    machine = AgentRunStateMachine("context")
    machine.begin_run("first")
    control = TurnControl("first", machine)
    control.start_tool()
    machine.wait_saved("first", ApprovalWait("approval"))
    assert machine.resume_approval("first", "stale").status == CommandStatus.STALE
    assert machine.phase == ExecutionPhase.WAITING
    assert machine.resume_approval("first", "approval").accepted
    control.start_tool()
    assert machine.run_id == "first"
