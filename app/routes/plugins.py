from fastapi import APIRouter, Depends
from pydantic import TypeAdapter

from app.schemas.result import Result
from app.schemas.plugins import PluginListResponse, PluginMCPStatusResponse
from app.dependencies import get_plugin_service, get_mcp_service
from app.services.plugin_service import PluginService
from app.services.mcp_service import MCPService

router = APIRouter(tags=["plugins"])


@router.get("/plugins")
def list_plugins(service: PluginService = Depends(get_plugin_service)) -> Result[PluginListResponse]:
    """返回应用服务提供的插件目录和发现错误。

    Args:
        service (PluginService): 组合插件元数据与发现结果的应用服务。

    Returns:
        Result[PluginListResponse]: 插件设置页使用的目录和发现错误。
    """
    metadata = TypeAdapter(dict[str, object]).dump_python(service.list_plugins(), mode="json", by_alias=True)
    data = PluginListResponse.model_validate(metadata)
    return Result[PluginListResponse](data=data, msg="success", code=200)


@router.get("/plugins/mcp-status/{session_id}", responses={404: {"description": "会话不存在"}})
def plugin_mcp_status(
    session_id: str, service: MCPService = Depends(get_mcp_service),
) -> Result[PluginMCPStatusResponse]:
    """返回设置页正在编辑的会话所启用的插件 MCP 状态。

    Args:
        session_id (str): 设置页正在编辑的会话。
        service (MCPService): 组合插件服务声明与运行状态的应用服务。

    Returns:
        Result[PluginMCPStatusResponse]: 按插件分组的服务名称与运行状态。

    Raises:
        ResourceNotFoundError: 指定会话不存在。
    """
    result = service.get_plugin_mcp_status(session_id)
    data = PluginMCPStatusResponse.model_validate({"plugins": result})
    return Result[PluginMCPStatusResponse](data=data, msg="success", code=200)
