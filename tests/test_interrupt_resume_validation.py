from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agent.core.event_router import AgentEvent, EventType
from app.agent.state import AgentState
from app.routes.control_screen import ControlScreenResponseRequest
from app.routes.control_screen import router as control_screen_router
from app.routes.screenshot import ScreenshotResponseRequest
from app.routes.screenshot import router as screenshot_router
from app.schemas.tool import ToolAuthorizationResponse, ToolContinuationResponse
from app.dependencies import get_agent_service
from app.services.agent_service import AgentService


class _StateManager:
    """返回测试预设中断状态的最小 StateManager 替身。"""

    state: AgentState

    def __init__(self, _session_id: str) -> None:
        pass

    async def load(self) -> AgentState:
        return self.state

    async def close(self) -> None:
        pass

    async def save(self, state: AgentState, *, checkpoint_type: str) -> int:
        self.state = state
        _StateManager.state = state
        return 1


class _Agent:
    """记录恢复参数的最小 Agent 替身。"""

    def __init__(self) -> None:
        self.resume_calls: list[dict] = []
        self.run_calls: list[str] = []
        self.closed = False
        self.continuation_valid = True

    async def resume(self, data: dict) -> AsyncIterator[AgentEvent]:
        self.resume_calls.append(data)
        yield AgentEvent(EventType.TEXT_CHUNK, "继续")

    async def run(self, message: str) -> AsyncIterator[AgentEvent]:
        self.run_calls.append(message)
        yield AgentEvent(EventType.TEXT_CHUNK, "已按引导继续")

    async def validate_pending_continuation(self, _interrupt: dict) -> bool:
        return self.continuation_valid

    async def close(self) -> None:
        self.closed = True


def _state(interrupt_type: str | None, request_id: str = "request-current") -> AgentState:
    state = AgentState.create_new("session-a")
    if interrupt_type is not None:
        interrupt = {
            "type": interrupt_type,
            "request_id": request_id,
            "message": "请确认",
        }
        if interrupt_type == "continuation_request":
            interrupt["resume_state"] = {"continuation_claimed": True}
        state.set_interrupt(interrupt)
    return state


def _service(monkeypatch, state: AgentState) -> tuple[AgentService, _Agent, list[str]]:
    from app.services import agent_service as agent_service_module

    _StateManager.state = state
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
@pytest.mark.parametrize(
    ("interrupt_type", "resume"),
    [
        (
            "mcp_tool_approval_request",
            lambda service: service.resume_after_screenshot(
                "session-a",
                request_id="request-current",
                approved=True,
                screenshot_data="image",
            ),
        ),
        (
            "screenshot_request",
            lambda service: service.resume_after_control_screen(
                "session-a",
                request_id="request-current",
                approved=True,
            ),
        ),
        (
            "control_screen_execute_request",
            lambda service: service.resume_after_mcp_tool(
                "session-a",
                request_id="request-current",
                approved=True,
            ),
        ),
    ],
)
async def test_resume_endpoint_rejects_other_interrupt_types(
    monkeypatch,
    interrupt_type: str,
    resume: Callable[[AgentService], AsyncIterator[AgentEvent]],
) -> None:
    service, _agent, constructed = _service(monkeypatch, _state(interrupt_type))

    with pytest.raises(ValueError, match="类型与响应接口不匹配"):
        await _drain(resume(service))

    assert constructed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interrupt_type", "resume"),
    [
        (
            "screenshot_request",
            lambda service: service.resume_after_screenshot(
                "session-a", request_id="request-stale", approved=False
            ),
        ),
        (
            "control_screen_capture_request",
            lambda service: service.resume_after_control_screen(
                "session-a", request_id="request-stale"
            ),
        ),
        (
            "mcp_tool_approval_request",
            lambda service: service.resume_after_mcp_tool(
                "session-a", request_id="request-stale", approved=False
            ),
        ),
    ],
)
async def test_resume_endpoint_rejects_stale_request_id_before_agent_creation(
    monkeypatch,
    interrupt_type: str,
    resume: Callable[[AgentService], AsyncIterator[AgentEvent]],
) -> None:
    service, _agent, constructed = _service(monkeypatch, _state(interrupt_type))

    with pytest.raises(ValueError, match="确认请求已过期"):
        await _drain(resume(service))

    assert constructed == []


@pytest.mark.asyncio
async def test_resume_endpoint_rejects_missing_interrupt_before_agent_creation(
    monkeypatch,
) -> None:
    service, _agent, constructed = _service(monkeypatch, _state(None))

    with pytest.raises(ValueError, match="没有待处理的确认请求"):
        await _drain(service.resume_after_screenshot(
            "session-a",
            request_id="request-current",
            approved=False,
        ))

    assert constructed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interrupt_type", "resume"),
    [
        (
            "continuation_request",
            lambda service: service.resume_after_tool_authorization(
                "session-a",
                request_id="request-current",
                decision={"kind": "approve"},
            ),
        ),
        (
            "authorization_request",
            lambda service: service.resume_after_tool_continuation(
                "session-a",
                request_id="request-current",
                response={"executed": True},
            ),
        ),
    ],
)
async def test_generic_resume_interfaces_do_not_consume_each_others_interrupts(
    monkeypatch,
    interrupt_type: str,
    resume: Callable[[AgentService], AsyncIterator[AgentEvent]],
) -> None:
    service, _agent, constructed = _service(monkeypatch, _state(interrupt_type))

    with pytest.raises(ValueError, match="类型与响应接口不匹配"):
        await _drain(resume(service))

    assert constructed == []


@pytest.mark.asyncio
async def test_generic_authorization_and_continuation_resume_exact_payloads(monkeypatch) -> None:
    authorization_service, authorization_agent, _ = _service(
        monkeypatch,
        _state("authorization_request"),
    )
    await _drain(authorization_service.resume_after_tool_authorization(
        "session-a",
        request_id="request-current",
        decision={"kind": "reject"},
    ))
    assert authorization_agent.resume_calls == [{"kind": "reject"}]

    continuation_service, continuation_agent, _ = _service(
        monkeypatch,
        _state("continuation_request"),
    )
    await _drain(continuation_service.resume_after_tool_continuation(
        "session-a",
        request_id="request-current",
        response={"executed": True},
    ))
    assert continuation_agent.resume_calls == [{"executed": True}]


@pytest.mark.asyncio
async def test_continuation_claim_rechecks_before_frontend_effect_and_is_required(monkeypatch) -> None:
    state = _state("continuation_request")
    state.interrupt_data["resume_state"] = {}
    service, agent, constructed = _service(monkeypatch, state)

    with pytest.raises(ValueError, match="尚未通过执行前复核"):
        await _drain(service.resume_after_tool_continuation(
            "session-a",
            request_id="request-current",
            response={"executed": True},
        ))

    await service.claim_tool_continuation(
        "session-a",
        request_id="request-current",
    )
    assert _StateManager.state.interrupt_data["resume_state"]["continuation_claimed"] is True
    assert agent.closed is True
    assert constructed == ["agent"]

    with pytest.raises(ValueError, match="已被认领"):
        await service.claim_tool_continuation(
            "session-a",
            request_id="request-current",
        )
    assert constructed == ["agent"]


@pytest.mark.asyncio
async def test_steer_starts_new_user_turn_without_resuming_old_tool(monkeypatch) -> None:
    service, agent, constructed = _service(monkeypatch, _state("authorization_request"))

    events = await _drain(service.resume_after_tool_authorization(
        "session-a",
        request_id="request-current",
        decision={"kind": "steer", "message": "改为只读取摘要"},
    ))

    assert [event.data for event in events] == ["已按引导继续"]
    assert agent.run_calls == ["改为只读取摘要"]
    assert agent.resume_calls == []
    assert agent.closed is True
    assert constructed == ["agent"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interrupt_type", "resume", "expected_data"),
    [
        (
            "screenshot_request",
            lambda service: service.resume_after_screenshot(
                "session-a",
                request_id="request-current",
                approved=False,
            ),
            {"approved": False},
        ),
        (
            "control_screen_capture_request",
            lambda service: service.resume_after_control_screen(
                "session-a",
                request_id="request-current",
                screenshot_data="image",
                width=10,
                height=20,
            ),
            {"screenshot_data": "image", "width": 10, "height": 20},
        ),
        (
            "control_screen_execute_request",
            lambda service: service.resume_after_control_screen(
                "session-a",
                request_id="request-current",
                approved=True,
                executed=True,
            ),
            {"approved": True, "executed": True},
        ),
        (
            "mcp_tool_approval_request",
            lambda service: service.resume_after_mcp_tool(
                "session-a",
                request_id="request-current",
                approved=True,
            ),
            {"approved": True},
        ),
    ],
)
async def test_matching_interrupt_resumes_once(
    monkeypatch,
    interrupt_type: str,
    resume: Callable[[AgentService], AsyncIterator[AgentEvent]],
    expected_data: dict,
) -> None:
    service, agent, constructed = _service(monkeypatch, _state(interrupt_type))

    events = await _drain(resume(service))

    assert [event.data for event in events] == ["继续"]
    assert agent.resume_calls == [expected_data]
    assert agent.closed is True
    assert constructed == ["agent"]


def test_old_resume_routes_require_non_empty_request_id() -> None:
    with pytest.raises(ValidationError):
        ScreenshotResponseRequest.model_validate({"session_id": "session-a", "approved": False})
    with pytest.raises(ValidationError):
        ScreenshotResponseRequest.model_validate({
            "session_id": "session-a",
            "request_id": "",
            "approved": False,
        })
    with pytest.raises(ValidationError):
        ControlScreenResponseRequest.model_validate({"session_id": "session-a"})
    with pytest.raises(ValidationError):
        ControlScreenResponseRequest.model_validate({
            "session_id": "session-a",
            "request_id": "",
            "approved": False,
        })

    capture_payload = ControlScreenResponseRequest.model_validate({
        "session_id": "session-a",
        "request_id": "request-current",
        "screenshot_data": "image",
        "width": 10,
        "height": 20,
    })
    assert capture_payload.approved is None


def test_generic_response_models_require_valid_decisions_and_request_ids() -> None:
    with pytest.raises(ValidationError):
        ToolAuthorizationResponse.model_validate({
            "session_id": "session-a",
            "request_id": "request-current",
            "kind": "steer",
            "message": "  ",
        })
    with pytest.raises(ValidationError):
        ToolContinuationResponse.model_validate({
            "session_id": "session-a",
            "request_id": "",
            "response": {},
        })


def test_control_screen_response_requires_execution_result_when_approved() -> None:
    with pytest.raises(ValidationError, match="executed"):
        ControlScreenResponseRequest.model_validate({
            "session_id": "session-a",
            "request_id": "request-current",
            "approved": True,
        })


def test_control_screen_response_requires_dimensions_with_screenshot() -> None:
    with pytest.raises(ValidationError, match="width"):
        ControlScreenResponseRequest.model_validate({
            "session_id": "session-a",
            "request_id": "request-current",
            "approved": True,
            "executed": True,
            "screenshot_data": "image",
        })


@pytest.mark.parametrize("path", ["/screenshot/respond", "/control-screen/respond"])
@pytest.mark.parametrize("request_id", [None, ""])
def test_old_resume_http_routes_reject_missing_or_empty_request_id(
    path: str,
    request_id: str | None,
) -> None:
    app = FastAPI()
    app.include_router(screenshot_router)
    app.include_router(control_screen_router)
    app.dependency_overrides[get_agent_service] = lambda: object()
    payload = {"session_id": "session-a", "approved": False}
    if request_id is not None:
        payload["request_id"] = request_id

    with TestClient(app) as client:
        response = client.post(path, json=payload)

    assert response.status_code == 422


class _RouteAgentService:
    """记录旧恢复路由转发参数的 AgentService 替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def resume_after_screenshot(
        self,
        session_id: str,
        *,
        request_id: str,
        **_kwargs,
    ) -> AsyncIterator[AgentEvent]:
        self.calls.append(("screenshot", session_id, request_id))
        yield AgentEvent(EventType.TEXT_CHUNK, "ok")

    async def resume_after_control_screen(
        self,
        session_id: str,
        *,
        request_id: str,
        **_kwargs,
    ) -> AsyncIterator[AgentEvent]:
        self.calls.append(("control", session_id, request_id))
        yield AgentEvent(EventType.TEXT_CHUNK, "ok")


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/screenshot/respond", "screenshot"),
        ("/control-screen/respond", "control"),
    ],
)
def test_old_resume_http_routes_forward_interrupt_request_id(
    path: str,
    kind: str,
) -> None:
    app = FastAPI()
    app.include_router(screenshot_router)
    app.include_router(control_screen_router)
    service = _RouteAgentService()
    app.dependency_overrides[get_agent_service] = lambda: service

    with TestClient(app) as client:
        response = client.post(path, json={
            "session_id": "session-a",
            "request_id": "interrupt-123",
            "approved": False,
        })

    assert response.status_code == 200
    assert service.calls == [(kind, "session-a", "interrupt-123")]
