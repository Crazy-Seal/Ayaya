"""工具的稳定来源身份，供配置、授权与子 Agent 白名单共同使用。"""

from dataclasses import dataclass

from app.schemas.tool_types import LOCAL_TOOL_KEY_PREFIX, MCP_TOOL_KEY_PREFIX, PLUGIN_TOOL_KEY_PREFIX, ToolSource


@dataclass(frozen=True)
class ToolIdentity:
    """用来源、工具名和可选服务编号区分工具身份，避免本地与 MCP 同名工具权限混用。"""

    source: ToolSource
    name: str
    server_id: str | None = None
    plugin_name: str | None = None

    @classmethod
    def local(cls, name: str, *, plugin_name: str | None = None) -> "ToolIdentity":
        """为本地工具建立与 MCP 工具区分的身份。

        Args:
            name (str): 当前对象的名称。
            plugin_name (str | None): 提供本地工具的插件；为空时属于独立工具。

        Returns:
            ToolIdentity: source 为 local 的工具身份。
        """
        return cls(source=ToolSource.LOCAL, name=name, plugin_name=plugin_name)

    @classmethod
    def mcp(cls, server_id: str, name: str, *, plugin_name: str | None = None) -> "ToolIdentity":
        """为指定 MCP 服务的工具建立独立身份，避免同名工具权限混用。

        Args:
            server_id (str): MCP 服务的稳定编号。
            name (str): 当前对象的名称。
            plugin_name (str | None): 提供 MCP 服务的插件；为空时属于手动服务。

        Returns:
            ToolIdentity: 包含 server_id 的 MCP 工具身份。
        """
        return cls(source=ToolSource.MCP, server_id=server_id, name=name, plugin_name=plugin_name)

    @property
    def stable_key(self) -> str:
        """生成持久权限字典和审批共同使用的工具标识。

        Returns:
            str: local:名称 或 mcp:服务编号:名称。

        Raises:
            ValueError: MCP 工具身份缺少服务编号。
        """
        key = self.policy_key
        return f"{PLUGIN_TOOL_KEY_PREFIX}{self.plugin_name}:{key}" if self.plugin_name else key

    @property
    def policy_key(self) -> str:
        """返回所属配置内部的权限键，插件归属由外层记录确定。

        Returns:
            str: local:工具 或 mcp:服务:工具。

        Raises:
            ValueError: MCP 工具缺少服务编号。
        """
        if self.source == ToolSource.LOCAL:
            return f"{LOCAL_TOOL_KEY_PREFIX}{self.name}"
        if not self.server_id:
            raise ValueError("MCP 工具身份缺少 server_id")
        return f"{MCP_TOOL_KEY_PREFIX}{self.server_id}:{self.name}"
