"""统一工具授权页使用的本地与 MCP 目录 Adapter。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.agent.tools.registry import ToolRegistry
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.schemas.chat_settings import ChatSettings
from app.schemas.tool_permissions import ToolPolicy
from app.services.mcp_connection_manager import MCPConnectionManager

from .models import ToolIdentity


@dataclass(frozen=True)
class ToolCatalogEntry:
    """权限页所需的来源无关工具目录项。"""

    stable_key: str
    source: Literal["local", "mcp"]
    name: str
    display_name: str
    description: str
    policy: ToolPolicy
    server_id: str | None = None
    server_name: str | None = None
    available: bool = True
    unavailable_reason: str | None = None


class LocalToolCatalogAdapter:
    """把本地 ToolRegistry 投影为统一目录。"""

    def list_entries(self, settings: ChatSettings) -> list[ToolCatalogEntry]:
        result: list[ToolCatalogEntry] = []
        for name in ToolRegistry.list_tools():
            tool_class = ToolRegistry.get(name)
            if tool_class is None:
                continue
            identity = ToolIdentity.local(name)
            result.append(ToolCatalogEntry(
                stable_key=identity.stable_key,
                source="local",
                name=name,
                display_name=getattr(tool_class, "display_name", None) or name,
                description=(getattr(tool_class, "description", "") or "").strip(),
                policy=settings.tool_permissions.get(
                    identity.stable_key, ToolPolicy.ASK
                ),
            ))
        return result


class MCPToolCatalogAdapter:
    """把连接目录和会话绑定投影为统一目录。"""

    def __init__(
        self,
        settings_dao: MCPSettingsDao,
        connection_manager: MCPConnectionManager,
    ) -> None:
        self._settings_dao = settings_dao
        self._connection_manager = connection_manager

    def list_entries(self, settings: ChatSettings) -> list[ToolCatalogEntry]:
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
            prefix = f"mcp:{server_id}:"
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
                    source="mcp",
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
        return result


class UnifiedToolCatalog:
    """合并所有来源，同时隐藏每种目录的发现细节。"""

    def __init__(
        self,
        local: LocalToolCatalogAdapter,
        mcp: MCPToolCatalogAdapter,
    ) -> None:
        self._local = local
        self._mcp = mcp

    def list_entries(self, settings: ChatSettings) -> list[ToolCatalogEntry]:
        return [
            *self._local.list_entries(settings),
            *self._mcp.list_entries(settings),
        ]
