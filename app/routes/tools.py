from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.schemas.result import Result
from app.dependencies import get_tool_permission_service
from app.dependencies import get_agent_service
from app.schemas.tool import (
    ToolAuthorizationResponse,
    ToolContinuationClaim,
    ToolContinuationResponse,
    ToolInfo,
    ToolPermissionsUpdate,
)
from app.services.tool_permission_service import ToolPermissionService
from app.services.agent_service import AgentService
from app.utils.sse_formatter import SSEFormatter

router = APIRouter(tags=["tools"])


@router.get("/tools", response_model=Result)
def list_tools() -> Result:
    """获取所有可用工具的名称和描述"""
    from app.agent.tools.registry import ToolRegistry

    tool_list: list[ToolInfo] = []
    for name in ToolRegistry.list_tools():
        tool_class = ToolRegistry.get(name)
        if tool_class is None:
            continue
        description = getattr(tool_class, "description", "") or ""
        tool_list.append(ToolInfo(name=tool_class.name, description=description.strip()))

    return Result(data={"tools": [t.model_dump() for t in tool_list]}, msg="success", code=200)


@router.get("/tool-permissions/{session_id}", response_model=Result)
def list_tool_permissions(
    session_id: str,
    service: ToolPermissionService = Depends(get_tool_permission_service),
) -> Result:
    return Result(
        data={"tools": [item.model_dump(mode="json") for item in service.list_policies(session_id)]},
        msg="success",
        code=200,
    )


@router.put("/tool-permissions/{session_id}", response_model=Result)
async def update_tool_permissions(
    session_id: str,
    payload: ToolPermissionsUpdate,
    service: ToolPermissionService = Depends(get_tool_permission_service),
) -> Result:
    try:
        tools = await service.set_policies(session_id, payload.tool_permissions)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Result(
        data={"tools": [item.model_dump(mode="json") for item in tools]},
        msg="success",
        code=200,
    )


def _resume_stream(stream) -> StreamingResponse:
    async def event_stream():
        formatter = SSEFormatter()
        try:
            async for event in stream:
                formatted = formatter.format(event)
                if formatted:
                    yield formatted
            yield formatter.done()
        except Exception as exc:
            yield formatter.error(str(exc))

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/tool-authorization/respond")
def respond_to_tool_authorization(
    payload: ToolAuthorizationResponse,
    agent_service: AgentService = Depends(get_agent_service),
) -> StreamingResponse:
    return _resume_stream(agent_service.resume_after_tool_authorization(
        payload.session_id,
        request_id=payload.request_id,
        decision={"kind": payload.kind, "message": payload.message},
    ))


@router.post("/tool-continuation/claim", response_model=Result)
async def claim_tool_continuation(
    payload: ToolContinuationClaim,
    agent_service: AgentService = Depends(get_agent_service),
) -> Result:
    try:
        await agent_service.claim_tool_continuation(
            payload.session_id,
            request_id=payload.request_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Result(data={"claimed": True}, msg="success", code=200)


@router.post("/tool-continuation/respond")
def respond_to_tool_continuation(
    payload: ToolContinuationResponse,
    agent_service: AgentService = Depends(get_agent_service),
) -> StreamingResponse:
    return _resume_stream(agent_service.resume_after_tool_continuation(
        payload.session_id,
        request_id=payload.request_id,
        response=payload.response,
    ))
