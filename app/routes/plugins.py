from fastapi import APIRouter, Depends, HTTPException

from app.schemas.result import Result
from app.dependencies import get_plugin_service, get_mcp_service
from app.services.plugin_service import PluginService
from app.services.mcp_service import MCPService

router = APIRouter(tags=["plugins"])


@router.get("/plugins", response_model=Result)
def list_plugins(service: PluginService = Depends(get_plugin_service)) -> Result:
    """返回应用服务提供的插件目录和发现错误。

    Args:
        service (PluginService): 组合插件元数据与发现结果的应用服务。

    Returns:
        Result: 插件设置页使用的统一响应。
    """
    return Result(data=service.list_plugins(), msg="success", code=200)


@router.get("/plugins/mcp-status/{session_id}", response_model=Result)
def plugin_mcp_status(session_id: str, service: MCPService = Depends(get_mcp_service)) -> Result:
    """返回应用服务组合的插件 MCP 状态，将会话不存在转换为 HTTP 404。

    Args:
        session_id (str): 设置页正在编辑的会话。
        service (MCPService): 组合插件服务声明与运行状态的应用服务。

    Returns:
        Result: 按插件分组的服务名称与运行状态，不包含连接凭据。

    Raises:
        HTTPException: 指定会话不存在。
    """
    try:
        result = service.get_plugin_mcp_status(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Result(data={"plugins": result}, msg="success", code=200)
