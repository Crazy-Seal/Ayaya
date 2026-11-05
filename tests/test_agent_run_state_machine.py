"""通过运行控制接口验证父子共用的阶段与轮次保护。"""

import pytest

from app.agent.runtime.agent_run_state_machine import AgentRunStateMachine, InvalidStateTransitionError
from app.agent.runtime.turn_control import TurnControl
from app.schemas.runtime_types import InterruptKind, TurnInterruptReason, TurnPhase


def test_committing_rejects_stop_and_illegal_tool_execution() -> None:
    """最终回答开始提交后，停止和下一次工具执行都不能改变该提交。"""
    machine = AgentRunStateMachine()
    control = TurnControl("first", run_state=machine)
    control.start_llm()
    control.begin_commit()
    assert not control.request(TurnInterruptReason.STOP)
    with pytest.raises(InvalidStateTransitionError):
        control.start_tool()
    assert machine.phase == TurnPhase.COMMITTING


def test_old_control_cannot_change_next_run() -> None:
    """引导开启下一轮后，旧轮控制不再接受停止或推进阶段。"""
    machine = AgentRunStateMachine()
    old = TurnControl("first", run_state=machine)
    machine.begin_finishing("first")
    new = TurnControl("second", run_state=machine)
    assert not old.request(TurnInterruptReason.STOP)
    with pytest.raises(InvalidStateTransitionError):
        old.start_llm()
    new.start_llm()
    assert machine.run_id == "second"
    assert machine.phase == TurnPhase.LLM


def test_approval_resume_keeps_identity_and_checks_request() -> None:
    """已保存审批只能由匹配的请求恢复，继续使用原轮次身份。"""
    machine = AgentRunStateMachine()
    control = TurnControl("first", run_state=machine)
    control.start_tool()
    machine.interrupt_saved("first", InterruptKind.AUTHORIZATION_REQUEST, "approval")
    assert not machine.prepare_resume("first", "stale")
    assert machine.phase == TurnPhase.INTERRUPTED
    assert machine.prepare_resume("first", "approval")
    control.start_tool()
    assert machine.run_id == "first"
