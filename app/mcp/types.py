from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from app.schemas.mcp import MCPConnectionConfig


@dataclass(frozen=True)
class PluginMCPKey:
    """按会话、插件和服务区分连接，允许不同插件使用同名服务。"""

    session_id: str
    plugin_name: str
    server_id: str

    def __str__(self) -> str:
        """返回日志中可识别归属的名称。

        Returns:
            str: 包含会话、插件和服务的名称。
        """
        return f"{self.session_id}/{self.plugin_name}/{self.server_id}"


@dataclass(frozen=True)
class MCPToolDescriptor:
    """描述 MCP Server 暴露的单个工具契约。

    Attributes:
        name: MCP Server 中的原始工具名称。
        description: 提供给模型的工具说明。
        input_schema: 工具参数的 JSON Schema。
        annotations: MCP 协议附带的工具注解。
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)


PluginMCPBinder = Callable[[PluginMCPKey, MCPConnectionConfig, list[MCPToolDescriptor]], Awaitable[None]]
