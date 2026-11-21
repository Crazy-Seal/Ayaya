from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_mcp_service
from app.schemas.mcp import MCPDeleteResult, MCPServerConfig, MCPServerView, MCPTestResult, MCPToolInfo
from app.schemas.result import Result
from app.mcp.mcp_connection_manager import MCPConnectionError, MCPServerDisabledError
from app.services.mcp_service import MCPService


router = APIRouter(prefix="/mcp", tags=["mcp"])
T = TypeVar("T")


def _result(data: T) -> Result[T]:
    """构造统一的成功响应。

    Args:
        data (T): 已构造为当前接口具体类型的响应数据。

    Returns:
        Result[T]: 状态码为 200 的统一响应对象。
    """
    return Result[T](data=data, msg="success", code=200)


@router.get("/servers")
def list_servers(service: MCPService = Depends(get_mcp_service)) -> Result[list[MCPServerView]]:
    """列出全部 MCP Server。

    Args:
        service (MCPService): 由依赖注入提供的 MCP 应用服务。

    Returns:
        Result[list[MCPServerView]]: 包含 Server 视图列表的统一响应。
    """
    return _result([MCPServerView.model_validate(item) for item in service.list_servers()])


@router.post("/servers/test")
async def test_server(
    server: MCPServerConfig,
    service: MCPService = Depends(get_mcp_service),
) -> Result[MCPTestResult]:
    """测试 MCP Server 配置而不保存。

    Args:
        server (MCPServerConfig): 待测试的 Server 配置。
        service (MCPService): 由依赖注入提供的 MCP 应用服务。

    Returns:
        Result[MCPTestResult]: 包含连接诊断结果的统一响应。

    Raises:
        HTTPException: Server 连接测试失败。
    """
    try:
        return _result(MCPTestResult.model_validate(await service.test_server(server)))
    except MCPConnectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/servers")
async def create_server(
    server: MCPServerConfig,
    service: MCPService = Depends(get_mcp_service),
) -> Result[MCPServerView]:
    """创建 MCP Server。

    Args:
        server (MCPServerConfig): 待创建的 Server 配置。
        service (MCPService): 由依赖注入提供的 MCP 应用服务。

    Returns:
        Result[MCPServerView]: 包含新 Server 视图的统一响应。

    Raises:
        HTTPException: Server ID 已存在。
    """
    try:
        data = await service.create_server(server)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _result(MCPServerView.model_validate(data))


@router.put("/servers/{server_id}")
async def update_server(
    server_id: str,
    server: MCPServerConfig,
    service: MCPService = Depends(get_mcp_service),
) -> Result[MCPServerView]:
    """更新指定 MCP Server。

    Args:
        server_id (str): 路径中的 MCP Server ID。
        server (MCPServerConfig): 更新后的完整 Server 配置。
        service (MCPService): 由依赖注入提供的 MCP 应用服务。

    Returns:
        Result[MCPServerView]: 包含更新后 Server 视图的统一响应。

    Raises:
        HTTPException: Server 不存在或请求尝试修改 ID。
    """
    try:
        data = await service.update_server(server_id, server)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _result(MCPServerView.model_validate(data))


@router.delete("/servers/{server_id}")
async def delete_server(
    server_id: str,
    service: MCPService = Depends(get_mcp_service),
) -> Result[MCPDeleteResult]:
    """删除 MCP Server 并清理模型引用。

    Args:
        server_id (str): 待删除的 MCP Server ID。
        service (MCPService): 由依赖注入提供的 MCP 应用服务。

    Returns:
        Result[MCPDeleteResult]: 包含受影响会话 ID 的统一响应。

    Raises:
        HTTPException: 指定 Server 不存在。
    """
    try:
        affected = await service.delete_server(server_id)
        return _result(MCPDeleteResult(affected_sessions=affected))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/servers/{server_id}/reconnect")
async def reconnect_server(
    server_id: str,
    service: MCPService = Depends(get_mcp_service),
) -> Result[MCPServerView]:
    """重新连接指定 MCP Server。

    Args:
        server_id (str): 待重连的 MCP Server ID。
        service (MCPService): 由依赖注入提供的 MCP 应用服务。

    Returns:
        Result[MCPServerView]: 包含重连后 Server 视图的统一响应。

    Raises:
        HTTPException: Server 不存在或重连失败。
    """
    try:
        return _result(MCPServerView.model_validate(await service.reconnect(server_id)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MCPServerDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MCPConnectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/servers/{server_id}/tools")
def list_server_tools(
    server_id: str,
    service: MCPService = Depends(get_mcp_service),
) -> Result[list[MCPToolInfo]]:
    """列出指定 MCP Server 的工具目录。

    Args:
        server_id (str): MCP Server ID。
        service (MCPService): 由依赖注入提供的 MCP 应用服务。

    Returns:
        Result[list[MCPToolInfo]]: 包含工具目录的统一响应。

    Raises:
        HTTPException: 指定 Server 不存在。
    """
    try:
        return _result([MCPToolInfo.model_validate(item) for item in service.list_tools(server_id)])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
