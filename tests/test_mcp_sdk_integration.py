from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from app.schemas.mcp import MCPServersConfig, StdioMCPServerConfig, StreamableHttpMCPServerConfig
from app.services.mcp_connection_manager import MCPConnectionManager


SERVER_SCRIPT = Path(__file__).parent / "fixtures" / "mcp_test_server.py"


@pytest.mark.asyncio
async def test_real_stdio_uses_minimal_environment_and_explicit_overrides(monkeypatch) -> None:
    """验证真实 stdio 连接使用最小环境及显式覆盖值。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    monkeypatch.setenv("AYAYA_MCP_INHERITED", "from-parent")
    config = MCPServersConfig(servers=[StdioMCPServerConfig(
        id="real_stdio",
        name="Real stdio",
        enabled=True,
        command=sys.executable,
        args=[str(SERVER_SCRIPT), "stdio"],
        env={"AYAYA_MCP_OVERRIDE": "from-config"},
    )])
    manager = MCPConnectionManager(lambda: config)
    try:
        tools = await manager.connect("real_stdio")
        assert {tool.name for tool in tools} == {"echo", "read_environment"}

        inherited = await manager.call_tool(
            "real_stdio", "read_environment", {"name": "AYAYA_MCP_INHERITED"}
        )
        overridden = await manager.call_tool(
            "real_stdio", "read_environment", {"name": "AYAYA_MCP_OVERRIDE"}
        )
        assert inherited.content[0].text == ""
        assert overridden.content[0].text == "from-config"
    finally:
        await manager.close()


def _free_port() -> int:
    """从操作系统取得可供集成测试使用的空闲本机端口。

    Returns:
        int: 供外层用例断言使用的预设结果。
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int) -> None:
    """轮询测试服务端口，等待监听成功后再开始连接。

    Args:
        port (int): 临时集成测试服务监听的本机端口。
    """
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError("MCP HTTP 测试服务未按时启动")


@pytest.mark.asyncio
async def test_real_streamable_http_initialize_list_call_and_reconnect() -> None:
    """验证真实 HTTP 连接完成初始化、工具查询、调用和重连。"""
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT), "http", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
    )
    config = MCPServersConfig(servers=[StreamableHttpMCPServerConfig(
        id="real_http",
        name="Real HTTP",
        enabled=True,
        url=f"http://127.0.0.1:{port}/mcp",
        headers={"X-Integration-Test": "plain-local-value"},
    )])
    manager = MCPConnectionManager(lambda: config)
    try:
        _wait_for_port(port)
        tools = await manager.connect("real_http")
        assert {tool.name for tool in tools} == {"echo", "read_environment"}
        result = await manager.call_tool("real_http", "echo", {"value": "first"})
        assert result.structuredContent == {"value": "first"}

        await manager.reconnect("real_http")
        second = await manager.call_tool("real_http", "echo", {"value": "second"})
        assert second.structuredContent == {"value": "second"}
    finally:
        await manager.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
