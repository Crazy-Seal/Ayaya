from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_mcp_service
from app.schemas.mcp import MCPServerConfig
from app.schemas.result import Result
from app.services.mcp_connection_manager import MCPConnectionError, MCPServerDisabledError
from app.services.mcp_service import MCPService


router = APIRouter(prefix="/mcp", tags=["mcp"])


def _result(data) -> Result:
    """构造统一的成功响应。

    Args:
        data: 写入响应的数据。

    Returns:
        状态码为 200 的统一响应对象。
    """
    return Result(data=data, msg="success", code=200)


@router.get("/servers", response_model=Result)
def list_servers(service: MCPService = Depends(get_mcp_service)) -> Result:
    """列出全部 MCP Server。

    Args:
        service: 由依赖注入提供的 MCP 应用服务。

    Returns:
        包含 Server 视图列表的统一响应。
    """
    return _result(service.list_servers())


@router.post("/servers/test", response_model=Result)
async def test_server(
    server: MCPServerConfig,
    service: MCPService = Depends(get_mcp_service),
) -> Result:
    """测试 MCP Server 配置而不保存。

    Args:
        server: 待测试的 Server 配置。
        service: 由依赖注入提供的 MCP 应用服务。

    Returns:
        包含连接诊断结果的统一响应。

    Raises:
        HTTPException: Server 连接测试失败。
    """
    try:
        return _result(await service.test_server(server))
    except MCPConnectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/servers", response_model=Result)
async def create_server(
    server: MCPServerConfig,
    service: MCPService = Depends(get_mcp_service),
) -> Result:
    """创建 MCP Server。

    Args:
        server: 待创建的 Server 配置。
        service: 由依赖注入提供的 MCP 应用服务。

    Returns:
        包含新 Server 视图的统一响应。

    Raises:
        HTTPException: Server ID 已存在。
    """
    try:
        return _result(await service.create_server(server))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/servers/{server_id}", response_model=Result)
async def update_server(
    server_id: str,
    server: MCPServerConfig,
    service: MCPService = Depends(get_mcp_service),
) -> Result:
    """更新指定 MCP Server。

    Args:
        server_id: 路径中的 MCP Server ID。
        server: 更新后的完整 Server 配置。
        service: 由依赖注入提供的 MCP 应用服务。

    Returns:
        包含更新后 Server 视图的统一响应。

    Raises:
        HTTPException: Server 不存在或请求尝试修改 ID。
    """
    try:
        return _result(await service.update_server(server_id, server))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/servers/{server_id}", response_model=Result)
async def delete_server(
    server_id: str,
    service: MCPService = Depends(get_mcp_service),
) -> Result:
    """删除 MCP Server 并清理模型引用。

    Args:
        server_id: 待删除的 MCP Server ID。
        service: 由依赖注入提供的 MCP 应用服务。

    Returns:
        包含受影响会话 ID 的统一响应。

    Raises:
        HTTPException: 指定 Server 不存在。
    """
    try:
        affected = await service.delete_server(server_id)
        return _result({"affected_sessions": affected})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/servers/{server_id}/reconnect", response_model=Result)
async def reconnect_server(
    server_id: str,
    service: MCPService = Depends(get_mcp_service),
) -> Result:
    """重新连接指定 MCP Server。

    Args:
        server_id: 待重连的 MCP Server ID。
        service: 由依赖注入提供的 MCP 应用服务。

    Returns:
        包含重连后 Server 视图的统一响应。

    Raises:
        HTTPException: Server 不存在或重连失败。
    """
    try:
        return _result(await service.reconnect(server_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MCPServerDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MCPConnectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/servers/{server_id}/tools", response_model=Result)
def list_server_tools(
    server_id: str,
    service: MCPService = Depends(get_mcp_service),
) -> Result:
    """列出指定 MCP Server 的工具目录。

    Args:
        server_id: MCP Server ID。
        service: 由依赖注入提供的 MCP 应用服务。

    Returns:
        包含工具目录的统一响应。

    Raises:
        HTTPException: 指定 Server 不存在。
    """
    try:
        return _result(service.list_tools(server_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
