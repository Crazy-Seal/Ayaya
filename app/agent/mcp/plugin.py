"""MCP 集成插件 - 连接 MCP Server，把其工具注册进 Agent。

支持 stdio / sse 两种传输。会话用 AsyncExitStack 持有，on_unregister 时关闭，避免泄漏子进程。
mcp SDK 延迟导入，未安装时给出清晰告警而不影响框架其余部分。
"""

import logging
from contextlib import AsyncExitStack
from typing import Any

from app.agent.context import BasePlugin, PluginHook
from app.agent.mcp.adapter import MCPToolAdapter

logger = logging.getLogger(__name__)


class MCPPlugin(BasePlugin):
    """把一个 MCP Server 的工具集成进 Agent（工具提供者，不订阅钩子）。"""

    name = "mcp"
    version = "1.0.0"
    priority = 50

    def __init__(self, server_config: dict):
        """
        Args:
            server_config:
                - name: 服务器名称
                - transport: "stdio" | "sse"
                - command, args, env: stdio 方式参数
                - url: sse 方式地址
        """
        self.server_config = server_config
        self._exit_stack: AsyncExitStack | None = None
        self.session: Any = None
        self.tools: list[MCPToolAdapter] = []

    @property
    def hooks(self) -> list[PluginHook]:
        return []

    async def on_register(self, agent: Any) -> None:
        server_name = self.server_config.get("name", "unknown")
        try:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client, StdioServerParameters
            from mcp.client.sse import sse_client
        except ImportError as e:
            logger.error("未安装 mcp 依赖，无法启用 MCP（%s）: %s", server_name, e)
            return

        transport = (self.server_config.get("transport") or "stdio").lower()
        self._exit_stack = AsyncExitStack()
        try:
            if transport == "stdio":
                params = StdioServerParameters(
                    command=self.server_config["command"],
                    args=self.server_config.get("args", []),
                    env=self.server_config.get("env"),
                )
                read, write = await self._exit_stack.enter_async_context(stdio_client(params))
            elif transport == "sse":
                read, write = await self._exit_stack.enter_async_context(
                    sse_client(self.server_config["url"])
                )
            else:
                logger.error("未知 MCP transport: %s", transport)
                await self._cleanup()
                return

            session = await self._exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.session = session

            tools_result = await session.list_tools()
            for tool in tools_result.tools:
                adapter = MCPToolAdapter(tool, session)
                self.tools.append(adapter)
                agent.tool_manager.register(adapter)
            logger.info("MCP '%s' 注册 %d 个工具", server_name, len(self.tools))
        except Exception as e:
            logger.error("MCP '%s' 连接失败: %s", server_name, e)
            await self._cleanup()

    async def on_unregister(self) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
        self.tools.clear()
        self.session = None
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception as e:
                logger.warning("关闭 MCP 连接失败: %s", e)
            self._exit_stack = None

    async def execute(self, context: Any) -> Any:
        return context
