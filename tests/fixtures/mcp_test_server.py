"""真实 MCP SDK 集成测试服务，不读写项目运行时数据。"""

from __future__ import annotations

import os
import sys

from mcp.server.fastmcp import FastMCP


transport = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
server = FastMCP(
    "Ayaya integration test",
    instructions="integration-test-only",
    host="127.0.0.1",
    port=port,
    stateless_http=True,
    json_response=True,
)


@server.tool()
def echo(value: str) -> dict[str, str]:
    """返回集成测试输入文本，用于验证真实 MCP 请求和响应链路。

    Args:
        value (str): 当前需要校验或转换的值。
    """
    return {"value": value}


@server.tool()
def read_environment(name: str) -> str:
    """返回测试允许读取的环境字段，用于检查 MCP 子进程环境隔离。

    Args:
        name (str): 当前对象的名称。
    """
    return os.environ.get(name, "")


if transport == "http":
    import uvicorn

    app = server.streamable_http_app()

    class RequireIntegrationHeader:
        """校验 HTTP 集成测试请求头，避免测试误连接不属于本次用例的服务。"""
        def __init__(self, inner):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            Args:
                inner (Any): 需要增加集成测试请求头校验的 ASGI 应用。
            """
            self.inner = inner

        async def __call__(self, scope, receive, send):
            """处理集成测试 ASGI 请求，只放行包含测试请求头的调用。

            Args:
                scope (Any): 当前 ASGI 请求的协议与请求头信息。
                receive (Any): ASGI 提供的请求数据读取函数。
                send (Any): ASGI 提供的响应发送函数。
            """
            if scope["type"] == "http":
                headers = {key.lower(): value for key, value in scope.get("headers", [])}
                if headers.get(b"x-integration-test") != b"plain-local-value":
                    await send({"type": "http.response.start", "status": 403, "headers": []})
                    await send({"type": "http.response.body", "body": b"missing test header"})
                    return
            await self.inner(scope, receive, send)

    uvicorn.run(RequireIntegrationHeader(app), host="127.0.0.1", port=port, log_level="error")
else:
    server.run(transport="stdio")
