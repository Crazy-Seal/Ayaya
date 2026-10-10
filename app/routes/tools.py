from fastapi import APIRouter, Depends, HTTPException

from app.schemas.result import Result
from app.dependencies import get_tool_permission_service
from app.schemas.tool import (
    ToolInfo,
    ToolPermissionsUpdate,
)
from app.services.tool_permission_service import ToolPermissionService

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
