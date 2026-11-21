"""将明确的业务失败转换为供桌面端读取的 HTTP 错误。"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agent.state import MessageContractError
from app.errors import InvalidOperationError, ResourceConflictError, ResourceNotFoundError
from app.mcp.mcp_connection_manager import MCPConnectionError, MCPServerDisabledError
from app.services.plugin_service import PluginConfigurationError


_ERROR_STATUS: dict[type[Exception], int] = {
    ResourceNotFoundError: 404,
    ResourceConflictError: 409,
    InvalidOperationError: 400,
    PluginConfigurationError: 422,
    MCPServerDisabledError: 409,
    MCPConnectionError: 400,
    MessageContractError: 500,
}


async def _error_response(_request: Request, exc: Exception) -> JSONResponse:
    """保留错误说明和插件字段列表，返回对应的失败状态。

    Args:
        _request (Request): 触发错误的 HTTP 请求。
        exc (Exception): 已由应用登记处理的业务或消息契约异常。

    Returns:
        JSONResponse: 包含 detail 的错误响应；插件错误提供字段列表。
    """
    status_code = next(status for error_type, status in _ERROR_STATUS.items() if isinstance(exc, error_type))
    detail: object
    if isinstance(exc, PluginConfigurationError):
        detail = exc.errors
    elif isinstance(exc, MessageContractError):
        detail = f"数据库与当前消息契约不兼容：{exc}"
    else:
        detail = str(exc)
    return JSONResponse(status_code=status_code, content={"detail": detail})


def install_http_error_handlers(application: FastAPI) -> None:
    """为 HTTP 接口登记明确异常的响应转换。

    Args:
        application (FastAPI): 挂载设置、查询和管理接口的应用。
    """
    for error_type in _ERROR_STATUS:
        application.add_exception_handler(error_type, _error_response)
