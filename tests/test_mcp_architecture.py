from pathlib import Path

import pytest

from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.authorization.manager import ToolAuthorization
from app.mcp.identity import server_identity_fingerprint
from app.mcp.types import MCPToolDescriptor
from app.schemas.chat_settings import ChatSettings
from app.schemas.mcp import (
    MCPModelSettings,
    MCPServerPolicy,
    MCPServersConfig,
    StdioMCPServerConfig,
)
from app.services.mcp_connection_manager import MCPConnectionError
from app.schemas.tool_permissions import ToolPolicy


def _chat_settings(**overrides) -> ChatSettings:
    """构造当前 MCP 或记忆测试所需的会话配置。

    Args:
        overrides (Any): 覆盖当前测试配置或工具参数默认值的字段。

    Returns:
        ChatSettings: 供外层用例断言使用的预设结果。
    """
    values = {
        "session_id": "session-a",
        "model_name": "model",
        "openai_api_key": "key",
        "openai_base_url": "http://127.0.0.1:1/v1",
        "temperature": 0.1,
        "system_prompt": "prompt",
        "context_strategy": ContextStrategyConfig(compression_trigger_tokens_k=100),
    }
    values.update(overrides)
    return ChatSettings(**values)


class _ExistingTool(BaseTool):
    """提供用例指定的工具响应、中断或异常，验证执行管道的结果保存与恢复。"""
    name = "mcp__filesystem__read_file"
    description = "existing"
    parameters_schema = {"type": "object", "properties": {}}

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            args (dict): 当前工具调用或被包装函数的参数。
            context (ToolContext): 执行管道提供的工具运行上下文。

        Returns:
            ToolResult: 供外层用例断言使用的预设结果。
        """
        return ToolResult.success("ok")


class _Manager:
    """提供当前用例所需的 _Manager 测试替身，执行记录仅供外层断言使用。"""
    def __init__(self) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。"""
        self.configs = {
            "filesystem": StdioMCPServerConfig(
                id="filesystem", name="Filesystem", enabled=True, command="python"
            ),
            "broken": StdioMCPServerConfig(
                id="broken", name="Broken", enabled=True, command="python"
            ),
            "disabled": StdioMCPServerConfig(
                id="disabled", name="Disabled", enabled=False, command="python"
            ),
        }
        self.connected: list[str] = []

    def get_server_config(self, server_id: str):
        """读取测试目录中的 MCP 服务配置。

        Args:
            server_id (str): MCP 服务的稳定编号。
        """
        if server_id not in self.configs:
            raise MCPConnectionError(f"MCP Server '{server_id}' 不存在")
        return self.configs[server_id]

    def get_server_identity(self, server_id: str) -> str:
        """返回测试指定的服务身份摘要。

        Args:
            server_id (str): MCP 服务的稳定编号。

        Returns:
            str: 供外层用例断言使用的预设结果。
        """
        return server_identity_fingerprint(self.configs[server_id])

    async def connect(self, server_id: str) -> list[MCPToolDescriptor]:
        """记录服务连接请求，并提供用例指定的工具目录或失败。

        Args:
            server_id (str): MCP 服务的稳定编号。

        Returns:
            list[MCPToolDescriptor]: 供外层用例断言使用的预设结果。
        """
        self.connected.append(server_id)
        if server_id == "broken":
            raise MCPConnectionError("连接失败")
        return [
            MCPToolDescriptor("read_file", "读取", {"type": "object"}, None),
            MCPToolDescriptor("write_file", "写入", {"type": "object"}, None),
            MCPToolDescriptor("delete_file", "删除", {"type": "object"}, None),
        ]


@pytest.mark.asyncio
async def test_agent_assembles_tools_and_isolates_server_failures(tmp_path: Path) -> None:
    """验证Agent 装配 MCP 工具时隔离单个服务失败。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    manager = _Manager()
    settings = _chat_settings(
        tool_permissions={
            "mcp:filesystem:read_file": ToolPolicy.ALLOW,
            "mcp:filesystem:write_file": ToolPolicy.ASK,
            "mcp:filesystem:delete_file": ToolPolicy.DENY,
        },
        mcp=MCPModelSettings(
            servers={
                "filesystem": MCPServerPolicy(enabled=True),
                "broken": MCPServerPolicy(enabled=True),
                "disabled": MCPServerPolicy(enabled=True),
            }
        )
    )
    agent = Agent(
        AgentConfig(
            session_id=settings.session_id,
            model_name=settings.model_name,
            api_key=settings.openai_api_key,
            context_strategy=settings.context_strategy,
            mcp=settings.mcp,
        ),
        db_path=str(tmp_path / "state.sqlite3"),
        mcp_connection_manager=manager,
        tool_authorization=ToolAuthorization(lambda _session_id: settings),
    )
    agent.tool_manager.register(_ExistingTool())

    await agent.initialize()

    assert manager.connected == ["filesystem", "broken"]
    assert agent.tool_manager.get("mcp__filesystem__read_file").description == "existing"
    assert agent.tool_manager.has("mcp__filesystem__write_file")
    assert agent.tool_manager.has("mcp__filesystem__delete_file")
    exposed_names = {
        tool.name for tool in agent.tool_manager.get_tools()
    }
    assert "mcp__filesystem__write_file" in exposed_names
    assert "mcp__filesystem__delete_file" not in exposed_names
    assert agent.tool_manager.get("mcp__filesystem__write_file").is_resumable is False
    await agent.close()
