from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agent.core.event_router import AgentEvent, EventType
from app.agent.state import AgentState
from app.dependencies import get_agent_service
from app.routes.tools import router as tools_router
from app.schemas.tool import ToolAuthorizationResponse, ToolContinuationClaim, ToolContinuationResponse
from app.services.agent_service import AgentService


class _StateManager:
    """返回测试预设中断状态的最小 StateManager 替身。"""

    state: AgentState
    load_calls = 0

    def __init__(self, _session_id: str) -> None:
        pass

    async def load(self) -> AgentState:
        type(self).load_calls += 1
        return self.state

    async def close(self) -> None:
        pass

    async def save(self, state: AgentState, *, checkpoint_type: str) -> int:
        type(self).state = state
        return 1


class _Agent:
    """记录统一授权和 continuation 的恢复参数。"""

    def __init__(self) -> None:
        self.resume_calls: list[tuple[dict, AgentState | None]] = []
        self.run_calls: list[tuple[str, AgentState | None]] = []
        self.closed = False
        self.continuation_valid = True

    async def resume(
        self, data: dict, *, initial_state: AgentState | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self.resume_calls.append((data, initial_state))
        yield AgentEvent(EventType.TEXT_CHUNK, "继续")

    async def run(
        self, message: str, *, initial_state: AgentState | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self.run_calls.append((message, initial_state))
        yield AgentEvent(EventType.TEXT_CHUNK, "已按引导继续")

    async def validate_pending_continuation(self, _interrupt: dict) -> bool:
        return self.continuation_valid

    async def close(self) -> None:
        self.closed = True


def _state(interrupt_type: str | None, request_id: str = "request-current") -> AgentState:
    state = AgentState.create_new("session-a")
    if interrupt_type is not None:
        interrupt = {"type": interrupt_type, "request_id": request_id, "message": "请确认"}
        if interrupt_type == "continuation_request":
            interrupt["resume_state"] = {"continuation_claimed": True}
        state.set_interrupt(interrupt)
    return state


def _service(monkeypatch, state: AgentState) -> tuple[AgentService, _Agent, list[str]]:
    from app.services import agent_service as agent_service_module

    _StateManager.state = state
    _StateManager.load_calls = 0
    monkeypatch.setattr(agent_service_module, "StateManager", _StateManager)
    agent = _Agent()
    constructed: list[str] = []

    def factory(_settings) -> _Agent:
        constructed.append("agent")
        return agent

    service = AgentService(
        chat_history_dao=None,
        chat_settings_loader=lambda _session_id: object(),
        agent_factory=factory,
    )
    return service, agent, constructed


async def _drain(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in stream]


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_type", ["authorization_request", "continuation_request"])
async def test_resume_rejects_stale_id_before_agent_creation(monkeypatch, interrupt_type: str) -> None:
    service, _agent, constructed = _service(monkeypatch, _state(interrupt_type))
    if interrupt_type == "authorization_request":
        resume = service.resume_after_tool_authorization(
            "session-a", request_id="stale", decision={"kind": "approve"},
        )
    else:
        resume = service.resume_after_tool_continuation(
            "session-a", request_id="stale", response={"executed": True},
        )

    with pytest.raises(ValueError, match="确认请求已过期"):
        await _drain(resume)
    assert constructed == []


@pytest.mark.asyncio
async def test_resume_rejects_missing_interrupt_before_agent_creation(monkeypatch) -> None:
    service, _agent, constructed = _service(monkeypatch, _state(None))
    with pytest.raises(ValueError, match="没有待处理的确认请求"):
        await _drain(service.resume_after_tool_authorization(
            "session-a", request_id="request-current", decision={"kind": "reject"},
        ))
    assert constructed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_type", ["authorization_request", "continuation_request"])
async def test_unified_resume_endpoints_reject_wrong_interrupt_type(
    monkeypatch, interrupt_type: str,
) -> None:
    service, _agent, constructed = _service(monkeypatch, _state(interrupt_type))
    if interrupt_type == "authorization_request":
        resume = service.resume_after_tool_continuation(
            "session-a", request_id="request-current", response={"executed": True},
        )
    else:
        resume = service.resume_after_tool_authorization(
            "session-a", request_id="request-current", decision={"kind": "approve"},
        )
    with pytest.raises(ValueError, match="类型与响应接口不匹配"):
        await _drain(resume)
    assert constructed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_authorization_resumes_with_exact_decision(monkeypatch, decision: str) -> None:
    service, agent, constructed = _service(monkeypatch, _state("authorization_request"))
    events = await _drain(service.resume_after_tool_authorization(
        "session-a", request_id="request-current", decision={"kind": decision},
    ))
    assert [event.data for event in events] == ["继续"]
    assert agent.resume_calls == [({"kind": decision}, _StateManager.state)]
    assert agent.closed is True
    assert constructed == ["agent"]


@pytest.mark.asyncio
async def test_continuation_claim_rechecks_once_before_effect_and_resumes(monkeypatch) -> None:
    state = _state("continuation_request")
    state.interrupt_data["resume_state"] = {}
    service, agent, constructed = _service(monkeypatch, state)

    with pytest.raises(ValueError, match="尚未通过执行前复核"):
        await _drain(service.resume_after_tool_continuation(
            "session-a", request_id="request-current", response={"executed": True},
        ))
    await service.claim_tool_continuation("session-a", request_id="request-current")
    assert _StateManager.state.interrupt_data["resume_state"]["continuation_claimed"] is True
    with pytest.raises(ValueError, match="已被认领"):
        await service.claim_tool_continuation("session-a", request_id="request-current")

    events = await _drain(service.resume_after_tool_continuation(
        "session-a", request_id="request-current", response={"executed": True},
    ))
    assert [event.data for event in events] == ["继续"]
    assert agent.resume_calls == [({"executed": True}, _StateManager.state)]
    assert constructed == ["agent", "agent"]


@pytest.mark.asyncio
async def test_continuation_claim_rejects_revoked_grant(monkeypatch) -> None:
    state = _state("continuation_request")
    state.interrupt_data["resume_state"] = {}
    service, agent, _ = _service(monkeypatch, state)
    agent.continuation_valid = False
    with pytest.raises(ValueError, match="授权已失效"):
        await service.claim_tool_continuation("session-a", request_id="request-current")
    assert state.interrupt_data["resume_state"] == {}


@pytest.mark.asyncio
async def test_steer_starts_new_user_turn_without_resuming_old_tool(monkeypatch) -> None:
    service, agent, constructed = _service(monkeypatch, _state("authorization_request"))
    events = await _drain(service.resume_after_tool_authorization(
        "session-a", request_id="request-current", decision={"kind": "steer", "message": "改为只读取摘要"},
    ))
    assert [event.data for event in events] == ["已按引导继续"]
    assert agent.run_calls == [("改为只读取摘要", _StateManager.state)]
    assert agent.resume_calls == []
    assert constructed == ["agent"]


def test_unified_response_models_require_valid_decisions_and_request_ids() -> None:
    for model, payload in [
        (ToolAuthorizationResponse, {"session_id": "session-a", "request_id": "", "kind": "approve"}),
        (ToolAuthorizationResponse, {"session_id": "session-a", "request_id": "id", "kind": "steer", "message": " "}),
        (ToolContinuationResponse, {"session_id": "session-a", "request_id": "", "response": {}}),
        (ToolContinuationClaim, {"session_id": "session-a", "request_id": ""}),
    ]:
        with pytest.raises(ValidationError):
            model.model_validate(payload)


class _RouteAgentService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict]] = []

    async def resume_after_tool_authorization(
        self, session_id: str, *, request_id: str, decision: dict,
    ) -> AsyncIterator[AgentEvent]:
        self.calls.append(("authorization", session_id, request_id, decision))
        yield AgentEvent(EventType.TEXT_CHUNK, "继续")

    async def resume_after_tool_continuation(
        self, session_id: str, *, request_id: str, response: dict,
    ) -> AsyncIterator[AgentEvent]:
        self.calls.append(("continuation", session_id, request_id, response))
        yield AgentEvent(EventType.TEXT_CHUNK, "继续")


@pytest.mark.parametrize("path", ["/tool-authorization/respond", "/tool-continuation/respond"])
def test_unified_http_routes_reject_missing_request_id(path: str) -> None:
    app = FastAPI()
    app.include_router(tools_router)
    app.dependency_overrides[get_agent_service] = lambda: _RouteAgentService()
    with TestClient(app) as client:
        response = client.post(path, json={"session_id": "session-a", "kind": "reject", "response": {}})
    assert response.status_code == 422


@pytest.mark.parametrize("path,kind,payload", [
    ("/tool-authorization/respond", "authorization", {"kind": "reject"}),
    ("/tool-continuation/respond", "continuation", {"response": {"executed": False}}),
])
def test_unified_http_routes_forward_request_and_payload(path: str, kind: str, payload: dict) -> None:
    app = FastAPI()
    app.include_router(tools_router)
    service = _RouteAgentService()
    app.dependency_overrides[get_agent_service] = lambda: service
    with TestClient(app) as client:
        response = client.post(path, json={
            "session_id": "session-a", "request_id": "interrupt-123", **payload,
        })
    assert response.status_code == 200
    forwarded = payload["response"] if kind == "continuation" else {"kind": "reject", "message": None}
    assert service.calls == [(kind, "session-a", "interrupt-123", forwarded)]
