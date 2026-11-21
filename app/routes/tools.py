from fastapi import APIRouter, Depends

from app.schemas.result import Result
from app.dependencies import get_tool_permission_service
from app.schemas.tool import (
    ToolInfo,
    ToolPermissionsUpdate,
    ToolListResponse,
    ToolPermissionsResponse,
)
from app.services.tool_permission_service import ToolPermissionService

router = APIRouter(tags=["tools"])


@router.get("/tools")
def list_tools() -> Result[ToolListResponse]:
    """获取所有可用工具的名称和描述。

    Returns:
        Result[ToolListResponse]: 当前已注册工具的列表。
    """
    from app.agent.tools.core.registry import ToolRegistry

    tool_list: list[ToolInfo] = []
    for name in ToolRegistry.list_tools():
        tool_class = ToolRegistry.get(name)
        if tool_class is None:
            continue
        description = getattr(tool_class, "description", "") or ""
        tool_list.append(ToolInfo(name=tool_class.name, description=description.strip()))

    return Result[ToolListResponse](data=ToolListResponse(tools=tool_list), msg="success", code=200)


@router.get("/tool-permissions/{session_id}", responses={404: {"description": "会话不存在"}})
def list_tool_permissions(
    session_id: str,
    service: ToolPermissionService = Depends(get_tool_permission_service),
) -> Result[ToolPermissionsResponse]:
    """设置页读取本地和 MCP 工具目录、权限及不可用原因。

    Args:
        session_id (str): 当前操作所属的会话编号。
        service (ToolPermissionService): FastAPI 注入的工具权限应用服务。

    Returns:
        Result[ToolPermissionsResponse]: 包含 tools 列表的统一接口结果。
    """
    return Result[ToolPermissionsResponse](
        data=ToolPermissionsResponse(tools=service.list_policies(session_id)),
        msg="success",
        code=200,
    )


@router.put("/tool-permissions/{session_id}", responses={400: {"description": "提交了未知工具权限"}, 404: {"description": "会话不存在"}})
async def update_tool_permissions(
    session_id: str,
    payload: ToolPermissionsUpdate,
    service: ToolPermissionService = Depends(get_tool_permission_service),
) -> Result[ToolPermissionsResponse]:
    """设置页提交权限时校验并保存，然后返回最新工具目录。

    Args:
        session_id (str): 当前操作所属的会话编号。
        payload (ToolPermissionsUpdate): 已经通过请求模型校验的工具权限更新。
        service (ToolPermissionService): FastAPI 注入的工具权限应用服务。

    Returns:
        Result[ToolPermissionsResponse]: 包含保存后 tools 列表的统一接口结果。

    Raises:
        ResourceNotFoundError: 指定会话不存在。
        InvalidOperationError: 提交了未知工具权限。
    """
    tools = await service.set_policies(session_id, payload.tool_permissions)
    return Result[ToolPermissionsResponse](
        data=ToolPermissionsResponse(tools=tools),
        msg="success",
        code=200,
    )
