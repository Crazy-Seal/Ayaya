"""统一工具授权页使用的本地与 MCP 目录 Adapter。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from app.agent.tools.core.base import BaseTool
from app.mcp.types import PluginMCPKey

from app.schemas.tool_types import MCP_TOOL_KEY_PREFIX, ToolSource
from app.agent.tools.core.registry import ToolRegistry
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.schemas.chat_settings import ChatSettings
from app.schemas.tool_permissions import ToolPolicy
from app.mcp.mcp_connection_manager import MCPConnectionManager

from .models import ToolIdentity


@dataclass(frozen=True)
class ToolCatalogEntry:
    """权限页所需的来源无关工具目录项。"""

    stable_key: str
    source: ToolSource
    name: str
    display_name: str
    description: str
    policy: ToolPolicy
    server_id: str | None = None
    server_name: str | None = None
    available: bool = True
    unavailable_reason: str | None = None
    plugin_name: str | None = None

    @property
    def policy_key(self) -> str:
        """返回工具在所属插件或独立配置中的权限键。

        Returns:
            str: 不重复包含插件归属的权限键。
        """
        return ToolIdentity(self.source, self.name, self.server_id, self.plugin_name).policy_key


class LocalToolCatalogAdapter:
    """把本地 ToolRegistry 投影为统一目录。"""

    def __init__(self, plugin_tools_loader: Callable[[], dict[str, tuple[type[BaseTool], ...]]] | None = None) -> None:
        """接收应用提供的插件工具声明，工具模块不反向发现插件。

        Args:
            plugin_tools_loader (Callable | None): 返回插件名和工具类声明的函数。
        """
        self._plugin_tools_loader = plugin_tools_loader

    def list_entries(self, settings: ChatSettings) -> list[ToolCatalogEntry]:
        """读取本地工具注册信息及会话权限，新工具没有保存权限时默认要求审批。

        Args:
            settings (ChatSettings): 当前会话保存的模型和工具权限设置。

        Returns:
            list[ToolCatalogEntry]: 供设置页显示的本地工具目录。
        """
        result: list[ToolCatalogEntry] = []
        for name in ToolRegistry.list_tools():
            tool_class = ToolRegistry.get(name)
            if tool_class is None:
                continue
            identity = ToolIdentity.local(name)
            result.append(ToolCatalogEntry(
                stable_key=identity.stable_key,
                source=ToolSource.LOCAL,
                name=name,
                display_name=getattr(tool_class, "display_name", None) or name,
                description=(getattr(tool_class, "description", "") or "").strip(),
                policy=settings.tool_permissions.get(
                    identity.stable_key, ToolPolicy.ASK
                ),
            ))
        for plugin_name, tools in (self._plugin_tools_loader() if self._plugin_tools_loader else {}).items():
            item = settings.agent_plugins.get(plugin_name)
            enabled = bool(item and item.enabled)
            for tool in tools:
                identity = ToolIdentity.local(tool.name, plugin_name=plugin_name)
                result.append(ToolCatalogEntry(
                    stable_key=identity.stable_key, source=ToolSource.LOCAL, name=tool.name,
                    display_name=getattr(tool, "display_name", tool.name), description=tool.description,
                    policy=item.tool_permissions.get(identity.policy_key, ToolPolicy.ASK) if item else ToolPolicy.ASK,
                    plugin_name=plugin_name, available=enabled,
                    unavailable_reason=None if enabled else "插件未启用",
                ))
        return result


class MCPToolCatalogAdapter:
    """把连接目录和会话绑定投影为统一目录。"""

    def __init__(
        self,
        settings_dao: MCPSettingsDao,
        connection_manager: MCPConnectionManager,
        plugin_servers_loader: Callable | None = None,
    ) -> None:
        """保存服务配置与共享连接依赖，列表查询时再判断服务身份和工具可用性。

        Args:
            settings_dao (MCPSettingsDao): 读取 MCP 服务配置的数据访问对象。
            connection_manager (MCPConnectionManager): 进程共用的 MCP 连接及工具目录管理器。
            plugin_servers_loader (Callable | None): 应用注入的会话插件连接声明查询。
        """
        self._settings_dao = settings_dao
        self._connection_manager = connection_manager
        self._plugin_servers_loader = plugin_servers_loader

    def list_entries(self, settings: ChatSettings) -> list[ToolCatalogEntry]:
        """合并已连接工具与保存过的权限名称，服务禁用、失联或身份变化时明确标记不可用。

        Args:
            settings (ChatSettings): 当前会话保存的模型和工具权限设置。

        Returns:
            list[ToolCatalogEntry]: 含权限和不可用原因的 MCP 工具目录。
        """
        result: list[ToolCatalogEntry] = []
        configs = {server.id: server for server in self._settings_dao.load().servers}
        for server_id, server_policy in settings.mcp.servers.items():
            config = configs.get(server_id)
            if config is None:
                continue
            available = bool(
                config.enabled
                and server_policy.enabled
                and self._connection_manager.get_available_identity(server_id)
                == server_policy.identity_fingerprint
            )
            descriptors = (
                self._connection_manager.get_tools(server_id) if available else []
            )
            names = {descriptor.name for descriptor in descriptors}
            prefix = f"{MCP_TOOL_KEY_PREFIX}{server_id}:"
            names.update(
                key[len(prefix):]
                for key in settings.tool_permissions
                if key.startswith(prefix)
            )
            by_name = {descriptor.name: descriptor for descriptor in descriptors}
            for name in sorted(names):
                descriptor = by_name.get(name)
                identity = ToolIdentity.mcp(server_id, name)
                is_available = available and descriptor is not None
                result.append(ToolCatalogEntry(
                    stable_key=identity.stable_key,
                    source=ToolSource.MCP,
                    name=name,
                    display_name=name,
                    description=(descriptor.description if descriptor else "") or "",
                    policy=settings.tool_permissions.get(
                        identity.stable_key, ToolPolicy.ASK
                    ),
                    server_id=server_id,
                    server_name=config.name,
                    available=is_available,
                    unavailable_reason=(
                        None
                        if is_available
                        else "MCP 服务未启用、未连接或工具目录不可用"
                    ),
                ))
        if self._plugin_servers_loader is not None:
            result.extend(self._plugin_entries(settings))
        return result

    def _plugin_entries(self, settings: ChatSettings) -> list[ToolCatalogEntry]:
        """查询插件连接的工具目录，未连接时保留已保存的权限名称。

        Args:
            settings (ChatSettings): 当前会话设置。

        Returns:
            list[ToolCatalogEntry]: 带明确插件归属的 MCP 工具。
        """
        result = []
        for plugin_name, item in settings.agent_plugins.items():
            try:
                servers = {server.id: server for server in self._plugin_servers_loader(settings, plugin_name)}
            except ValueError:
                servers = {}
            server_ids = set(servers)
            server_ids.update(key.split(":", 2)[1] for key in item.tool_permissions if key.startswith(MCP_TOOL_KEY_PREFIX))
            for server_id in sorted(server_ids):
                key = PluginMCPKey(settings.session_id, plugin_name, server_id)
                available = bool(item.enabled and server_id in servers and
                                 self._connection_manager.get_available_identity(key) == item.mcp_identities.get(server_id)
                                 and item.mcp_identities.get(server_id))
                descriptors = self._connection_manager.get_tools(key) if available else []
                by_name = {descriptor.name: descriptor for descriptor in descriptors}
                prefix = f"{MCP_TOOL_KEY_PREFIX}{server_id}:"
                names = set(by_name) | {name[len(prefix):] for name in item.tool_permissions if name.startswith(prefix)}
                for name in sorted(names):
                    identity = ToolIdentity.mcp(server_id, name, plugin_name=plugin_name)
                    descriptor = by_name.get(name)
                    result.append(ToolCatalogEntry(
                        stable_key=identity.stable_key, source=ToolSource.MCP, name=name, display_name=name,
                        description=descriptor.description if descriptor else "",
                        policy=item.tool_permissions.get(identity.policy_key, ToolPolicy.ASK),
                        plugin_name=plugin_name, server_id=server_id,
                        server_name=servers[server_id].name if server_id in servers else server_id,
                        available=available and descriptor is not None,
                        unavailable_reason=None if available and descriptor else "插件服务未连接或工具目录不可用",
                    ))
        return result


class UnifiedToolCatalog:
    """合并所有来源，同时隐藏每种目录的发现细节。"""

    def __init__(
        self,
        local: LocalToolCatalogAdapter,
        mcp: MCPToolCatalogAdapter,
    ) -> None:
        """保存本地和 MCP 目录适配器，后续查询共用同一会话设置。

        Args:
            local (LocalToolCatalogAdapter): 本地工具目录查询对象。
            mcp (MCPToolCatalogAdapter): MCP 工具目录查询对象。
        """
        self._local = local
        self._mcp = mcp

    def list_entries(self, settings: ChatSettings) -> list[ToolCatalogEntry]:
        """合并当前会话的本地和 MCP 工具目录，交给权限服务转换为响应。

        Args:
            settings (ChatSettings): 当前会话保存的模型和工具权限设置。

        Returns:
            list[ToolCatalogEntry]: 本地工具在前、MCP 工具在后的目录列表。
        """
        return [
            *self._local.list_entries(settings),
            *self._mcp.list_entries(settings),
        ]
