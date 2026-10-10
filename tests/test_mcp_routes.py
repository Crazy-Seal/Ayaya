from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_mcp_service
from app.routes.mcp import router


class _Service:
    """提供当前用例所需的 _Service 测试替身，执行记录仅供外层断言使用。"""
    def list_servers(self):
        """返回用例准备的服务列表及状态。"""
        return [{"config": {"id": "demo"}, "runtime": {"status": "available"}}]

    async def test_server(self, server):
        """模拟服务连接测试并返回工具目录和连接状态，供检查路由响应。

        Args:
            server (Any): 用例提供的 MCP 服务配置。
        """
        return {"status": "available", "tools": [{"name": "echo"}], "id": server.id}

    async def create_server(self, server):
        """模拟创建服务并返回用例指定的配置。

        Args:
            server (Any): 用例提供的 MCP 服务配置。
        """
        return {"config": server.model_dump(mode="json"), "runtime": {"status": "disconnected"}}

    async def update_server(self, server_id, server):
        """模拟更新服务配置，供检查接口参数和返回包装。

        Args:
            server_id (Any): MCP 服务的稳定编号。
            server (Any): 用例提供的 MCP 服务配置。
        """
        assert server_id == server.id
        return {"config": server.model_dump(mode="json"), "runtime": {"status": "disconnected"}}

    async def delete_server(self, server_id):
        """模拟删除服务，供检查级联配置处理。

        Args:
            server_id (Any): MCP 服务的稳定编号。
        """
        return [f"session-for-{server_id}"]

    async def reconnect(self, server_id):
        """模拟服务重新连接，供检查路由是否调用正确服务。

        Args:
            server_id (Any): MCP 服务的稳定编号。
        """
        return {"config": {"id": server_id}, "runtime": {"status": "available"}}

    def list_tools(self, _server_id):
        """返回当前测试服务的工具目录。

        Args:
            _server_id (Any): MCP 服务的稳定编号。
        """
        return [{"name": "echo"}]


def _client() -> TestClient:
    """创建访问测试路由的 HTTP 客户端。

    Returns:
        TestClient: 供外层用例断言使用的预设结果。
    """
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_mcp_service] = lambda: _Service()
    return TestClient(app)


def test_mcp_server_routes_use_result_wrapper() -> None:
    """验证MCP 服务接口使用统一响应包装。"""
    with _client() as client:
        listed = client.get("/mcp/servers")
        tested = client.post(
            "/mcp/servers/test",
            json={
                "id": "demo",
                "name": "Demo",
                "transport": "stdio",
                "command": "python",
            },
        )
        created = client.post(
            "/mcp/servers",
            json={
                "id": "demo",
                "name": "Demo",
                "transport": "stdio",
                "command": "python",
            },
        )

    assert listed.json()["data"][0]["runtime"]["status"] == "available"
    assert tested.json()["data"]["tools"] == [{"name": "echo"}]
    assert created.json()["code"] == 200


def test_mcp_server_routes_update_reconnect_tools_and_delete() -> None:
    """验证MCP 接口支持更新、重连、工具查询和删除。"""
    with _client() as client:
        updated = client.put(
            "/mcp/servers/demo",
            json={
                "id": "demo",
                "name": "Demo 2",
                "transport": "streamable_http",
                "url": "http://127.0.0.1:9000/mcp",
            },
        )
        reconnected = client.post("/mcp/servers/demo/reconnect")
        tools = client.get("/mcp/servers/demo/tools")
        deleted = client.delete("/mcp/servers/demo")

    assert updated.json()["data"]["config"]["name"] == "Demo 2"
    assert reconnected.json()["data"]["runtime"]["status"] == "available"
    assert tools.json()["data"] == [{"name": "echo"}]
    assert deleted.json()["data"]["affected_sessions"] == ["session-for-demo"]
