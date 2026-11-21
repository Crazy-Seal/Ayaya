from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class MCPServerStatus(StrEnum):
    """表示本应用管理的 MCP 连接状态。

    Attributes:
        DISABLED (MCPServerStatus): 全局配置已禁用。
        DISCONNECTED (MCPServerStatus): 尚未连接或已经断开。
        CONNECTING (MCPServerStatus): 正在连接服务。
        AVAILABLE (MCPServerStatus): 连接和工具目录可用。
        ERROR (MCPServerStatus): 连接或调用出现错误。
    """

    DISABLED = "disabled"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AVAILABLE = "available"
    ERROR = "error"


class MCPTransport(StrEnum):
    """区分 MCP 服务的连接方式。

    Attributes:
        STDIO (MCPTransport): 通过标准输入输出连接。
        STREAMABLE_HTTP (MCPTransport): 通过流式 HTTP 连接。
    """

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


SERVER_ID_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"


class MCPServerPolicy(BaseModel):
    """描述单个模型对一个 MCP Server 的使用权限。"""

    enabled: bool = False
    identity_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class MCPModelSettings(BaseModel):
    """保存单个模型的 MCP Server 权限集合。"""

    servers: dict[str, MCPServerPolicy] = Field(default_factory=dict)

    @field_validator("servers")
    @classmethod
    def validate_server_ids(cls, value: dict[str, MCPServerPolicy]) -> dict[str, MCPServerPolicy]:
        """校验模型权限中引用的 MCP Server ID。

        Args:
            value (dict[str, MCPServerPolicy]): Server ID 到模型级权限的映射。

        Returns:
            dict[str, MCPServerPolicy]: 校验通过的原映射。

        Raises:
            ValueError: 任一 Server ID 不符合命名规则。
        """
        import re

        if any(re.fullmatch(SERVER_ID_PATTERN, server_id) is None for server_id in value):
            raise ValueError("MCP server_id 只能包含字母、数字、下划线和连字符，最长 32 位")
        return value


class MCPConnectionConfigBase(BaseModel):
    """定义 MCP 连接参数；服务启停由所属插件或独立服务设置决定。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=SERVER_ID_PATTERN)
    name: str = Field(min_length=1, max_length=100)
    connect_timeout_seconds: float = Field(default=15, gt=0, le=300)
    call_timeout_seconds: float = Field(default=60, gt=0, le=3600)


class StdioMCPConnectionConfig(MCPConnectionConfigBase):
    """定义插件通过本地标准输入输出建立连接所需的参数。"""

    transport: Literal[MCPTransport.STDIO] = MCPTransport.STDIO
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        """禁止 MCP 子进程覆盖桌面后端运行控制变量。

        Args:
            value (dict[str, str]): 用户为该 Server 显式配置的环境变量。

        Returns:
            dict[str, str]: 校验通过的环境变量映射。

        Raises:
            ValueError: 配置中包含禁止传递的运行控制变量。
        """
        forbidden = {"AYAYA_API_TOKEN", "AYAYA_PARENT_PID"}
        configured = {key.upper() for key in value}
        invalid = sorted(configured & forbidden)
        if invalid:
            raise ValueError(
                f"MCP Server env 不允许配置应用运行控制变量: {', '.join(invalid)}"
            )
        return value


class StreamableHttpMCPConnectionConfig(MCPConnectionConfigBase):
    """定义插件通过 Streamable HTTP 建立连接所需的参数。"""

    transport: Literal[MCPTransport.STREAMABLE_HTTP] = MCPTransport.STREAMABLE_HTTP
    url: str
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """校验并规范化 Streamable HTTP Endpoint。

        Args:
            value (str): 用户提供的 Endpoint URL。

        Returns:
            str: 去除首尾空白后的 URL。

        Raises:
            ValueError: URL 非 HTTP(S) 地址或包含用户凭据。
        """
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MCP URL 必须是有效的 HTTP 或 HTTPS 地址")
        if parsed.username or parsed.password:
            raise ValueError("MCP URL 不允许包含用户信息")
        return value.strip()


class StdioMCPServerConfig(StdioMCPConnectionConfig):
    """保存手动添加的本地 MCP 服务参数及独立开关。"""

    enabled: bool = False


class StreamableHttpMCPServerConfig(StreamableHttpMCPConnectionConfig):
    """保存手动添加的 HTTP MCP 服务参数及独立开关。"""

    enabled: bool = False


MCPConnectionConfig = Annotated[
    StdioMCPConnectionConfig | StreamableHttpMCPConnectionConfig,
    Field(discriminator="transport"),
]


MCPServerConfig = Annotated[
    StdioMCPServerConfig | StreamableHttpMCPServerConfig,
    Field(discriminator="transport"),
]

MCPManagedConfig = MCPServerConfig | MCPConnectionConfig


class MCPServersConfig(BaseModel):
    """表示本地配置中的全部 MCP Server。"""

    servers: list[MCPServerConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "MCPServersConfig":
        """校验 MCP Server ID 在配置中唯一。

        Returns:
            MCPServersConfig: 校验通过的当前配置对象。

        Raises:
            ValueError: 配置中存在重复的 Server ID。
        """
        ids = [server.id for server in self.servers]
        if len(ids) != len(set(ids)):
            raise ValueError("MCP server_id 不能重复")
        return self


class MCPRuntimeStatus(BaseModel):
    """向设置页交付一个 MCP 连接的当前运行状态。

    Attributes:
        status (MCPServerStatus): 连接所处的状态。
        tool_count (int): 当前连接提供的工具数量。
        last_error (str | None): 最近一次连接错误。
        instructions (str | None): 服务提供的使用说明。
    """

    status: MCPServerStatus
    tool_count: int
    last_error: str | None
    instructions: str | None


class MCPServerView(BaseModel):
    """向 MCP 设置页交付服务配置及其使用情况。

    Attributes:
        config (MCPServerConfig): 保存后的服务配置。
        runtime (MCPRuntimeStatus): 当前连接状态。
        affected_model_count (int): 引用此服务的会话数量。
    """

    config: MCPServerConfig
    runtime: MCPRuntimeStatus
    affected_model_count: int


class MCPToolInfo(BaseModel):
    """向工具浏览页交付 MCP 服务公开的工具契约。

    Attributes:
        name (str): 服务中的工具名称。
        description (str): 工具功能说明。
        input_schema (dict[str, JsonValue]): 服务声明的参数 JSON Schema。
        annotations (dict[str, JsonValue]): 服务提供的工具注解。
    """

    name: str
    description: str
    input_schema: dict[str, JsonValue]
    annotations: dict[str, JsonValue]


class MCPTestResult(BaseModel):
    """向连接测试接口交付服务状态、说明及发现的工具。

    Attributes:
        status (MCPServerStatus): 本次测试取得的连接状态。
        instructions (str | None): 服务提供的使用说明。
        tools (list[MCPToolInfo]): 测试期间发现的工具。
    """

    status: MCPServerStatus
    instructions: str | None
    tools: list[MCPToolInfo]


class MCPDeleteResult(BaseModel):
    """删除 MCP 服务后交付被清理引用的会话列表。

    Attributes:
        affected_sessions (list[str]): 配置引用受到影响的会话编号。
    """

    affected_sessions: list[str]
