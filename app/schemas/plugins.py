"""插件设置接口的目录和连接状态响应。"""

from pydantic import BaseModel, JsonValue

from app.schemas.mcp import MCPRuntimeStatus


class PluginInfo(BaseModel):
    """向插件设置页交付可安装配置的插件元数据。

    Attributes:
        name (str): 插件名称。
        description (str): 插件功能说明。
        default_config (dict[str, JsonValue]): 插件声明的默认配置。
        config_schema (dict[str, JsonValue]): 设置编辑器使用的 JSON Schema。
    """

    name: str
    description: str
    default_config: dict[str, JsonValue]
    config_schema: dict[str, JsonValue]


class PluginListResponse(BaseModel):
    """交付插件发现结果及发现期间的错误。

    Attributes:
        plugins (list[PluginInfo]): 可供设置的插件。
        errors (list[str]): 插件发现错误。
    """

    plugins: list[PluginInfo]
    errors: list[str]


class PluginMCPStatus(BaseModel):
    """交付插件声明的单个 MCP 服务状态。

    Attributes:
        id (str): 插件内的服务编号。
        name (str): 设置页显示的服务名称。
        runtime (MCPRuntimeStatus): 当前连接状态。
    """

    id: str
    name: str
    runtime: MCPRuntimeStatus


class PluginMCPStatusResponse(BaseModel):
    """按插件名称交付当前会话的 MCP 连接状态。

    Attributes:
        plugins (dict[str, list[PluginMCPStatus]]): 每个插件的服务列表。
    """

    plugins: dict[str, list[PluginMCPStatus]]
