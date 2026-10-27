"""组合插件声明与会话设置，供设置页和运行装配共用。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from pathlib import Path

from pydantic import ValidationError

from app.agent.plugins.registry import PluginRegistry
from app.agent.plugins.base import BasePlugin
from app.schemas.chat_settings import AgentPluginSettings
from app.schemas.mcp import MCPConnectionConfig
from app.schemas.tool_permissions import ToolPolicy
from app.schemas.tool_types import MCP_TOOL_KEY_PREFIX
from app.crud.chat_settings_dao import ChatSettingsDao
from app.mcp.types import PluginMCPKey, MCPToolDescriptor
from app.mcp.identity import server_identity_fingerprint
from app.services.settings_mutation import SettingsMutationCoordinator
from app.agent.tools.core.base import BaseTool


class PluginConfigurationError(ValueError):
    """保存插件字段校验错误，HTTP 层据此定位前端输入。"""

    def __init__(self, errors: list[dict[str, Any]], *, message: str = "插件参数校验失败") -> None:
        """记录已经带插件字段路径的错误。

        Args:
            errors (list[dict[str, Any]]): 可交给前端的字段路径和错误说明。
            message (str): 状态查询和日志使用的错误说明，参数校验时保留原始详情。
        """
        super().__init__(message)
        self.errors = errors


@dataclass(frozen=True)
class PreparedPlugin:
    """保存单次准备中已校验的插件参数和连接声明，供应用直接装配。

    Attributes:
        name (str): 插件的稳定名称。
        config (dict[str, Any]): 已整理的参数；宽松读取时可保留待修正输入。
        mcp_servers (tuple[MCPConnectionConfig, ...]): 启用插件严格校验后生成的连接声明。
    """

    name: str
    config: dict[str, Any]
    mcp_servers: tuple[MCPConnectionConfig, ...]


class PluginService:
    """管理插件声明投影和参数校验，保持目录查询与装配一致。"""

    def __init__(self, settings_dao: ChatSettingsDao | None = None,
                 coordinator: SettingsMutationCoordinator | None = None) -> None:
        """保存需要读写会话时使用的依赖，纯声明查询可不提供存储。

        Args:
            settings_dao (ChatSettingsDao | None): 当前应用的会话配置存储。
            coordinator (SettingsMutationCoordinator | None): 配置写入共用锁。
        """
        self.settings_dao = settings_dao
        self.coordinator = coordinator or SettingsMutationCoordinator()

    def local_tools(self) -> dict[str, tuple[type[BaseTool], ...]]:
        """向工具目录提供已发现插件的声明，保持工具模块与插件发现解耦。

        Returns:
            dict[str, tuple[type[BaseTool], ...]]: 插件稳定名称及其本地工具类。
        """
        return {name: plugin.tools for name in PluginRegistry.list_plugins()
                if (plugin := PluginRegistry.get(name)) is not None}

    async def discover(self, root: Path) -> list[str]:
        """启动时发现独立插件，成功后清理已移除插件的配置。

        Args:
            root (Path): 外部插件目录。

        Returns:
            list[str]: 导入及声明错误；有错误时保留已有设置。
        """
        errors = PluginRegistry.discover_external(root)
        for name in PluginRegistry.list_plugins():
            if PluginRegistry.get(name) is None:
                errors.append(f"插件 {name} 的入口或声明加载失败")
        PluginRegistry.discovery_errors = errors
        if errors or self.settings_dao is None:
            return errors
        # 1. 只有完整目录可信时才按名称清理，不把代码导入异常当成卸载。
        async with self.coordinator.lock:
            for settings in self.settings_dao.list_chat_settings():
                kept = {name: item for name, item in settings.agent_plugins.items() if PluginRegistry.has(name)}
                normalized = settings.model_copy(deep=True, update={"agent_plugins": deepcopy(kept)})
                for name, item in normalized.agent_plugins.items():
                    item.config = self.prepare_plugin(name, item, validate_enabled=False).config
                if normalized != settings:
                    self.settings_dao.update_chat_settings(settings.session_id, normalized)
        return errors

    def connection_config(self, key: PluginMCPKey) -> MCPConnectionConfig:
        """供连接模块按完整归属查询最新声明，不让连接模块读取插件目录。

        Args:
            key (PluginMCPKey): 会话、插件和服务身份。

        Returns:
            MCPConnectionConfig: 最新参数生成的连接配置。

        Raises:
            KeyError: 插件已禁用或服务已经移除。
        """
        settings = self.settings_dao.get_chat_settings(key.session_id)
        item = settings.agent_plugins.get(key.plugin_name)
        if item is None or not item.enabled:
            raise KeyError(f"插件服务不存在或未启用: {key}")
        for server in self.prepare_plugin(key.plugin_name, item).mcp_servers:
            if server.id == key.server_id:
                return server
        raise KeyError(f"插件服务不存在或未启用: {key}")

    async def bind_mcp(self, key: PluginMCPKey, server: MCPConnectionConfig,
                       tools: list[MCPToolDescriptor]) -> None:
        """成功连接后保存身份和默认权限，参数变化时旧 allow 不继承。

        Args:
            key (PluginMCPKey): 成功连接的插件服务。
            server (MCPConnectionConfig): 实际建立连接的参数。
            tools (list[MCPToolDescriptor]): 服务返回的有效工具目录。
        """
        async with self.coordinator.lock:
            settings = self.settings_dao.get_chat_settings(key.session_id).model_copy(deep=True)
            plugin = settings.agent_plugins.get(key.plugin_name)
            if plugin is None or not plugin.enabled:
                return
            # 1. 建连期间参数可能被修改，旧连接不能替新参数建立身份绑定。
            if self.connection_config(key) != server:
                return
            fingerprint = server_identity_fingerprint(server)
            changed = plugin.mcp_identities.get(server.id) != fingerprint
            prefix = f"{MCP_TOOL_KEY_PREFIX}{server.id}:"
            if changed:
                for name in plugin.tool_permissions:
                    if name.startswith(prefix):
                        plugin.tool_permissions[name] = ToolPolicy.ASK
            for tool in tools:
                policy_key = prefix + tool.name
                if changed or policy_key not in plugin.tool_permissions:
                    plugin.tool_permissions[policy_key] = ToolPolicy.ASK
            plugin.mcp_identities[server.id] = fingerprint
            self.settings_dao.update_chat_settings(settings.session_id, settings)

    def list_plugins(self) -> dict[str, Any]:
        """组合插件说明、配置字段和发现错误，供插件设置页展示完整目录。

        Returns:
            dict[str, Any]: plugins 为可用插件的声明信息，errors 为发现或导入失败的说明。
        """
        result = []
        for name in PluginRegistry.list_plugins():
            plugin = PluginRegistry.get(name)
            if plugin is None:
                continue
            model = plugin.config_model
            defaults = {}
            if model is not None:
                for key, field in model.model_fields.items():
                    if not field.is_required() and not field.default_factory_takes_validated_data:
                        defaults[key] = field.get_default(call_default_factory=True)
            result.append({"name": plugin.name, "description": plugin.description,
                           "default_config": defaults,
                           "config_schema": model.model_json_schema() if model else {"type": "object", "properties": {}}})
        return {"plugins": result, "errors": list(PluginRegistry.discovery_errors)}

    def prepare_plugin(self, plugin_name: str, item: AgentPluginSettings,
                       *, validate_enabled: bool = True) -> PreparedPlugin:
        """为一个插件校验一次参数，再用校验结果生成本次连接声明。

        Args:
            plugin_name (str): 插件的稳定名称。
            item (AgentPluginSettings): 当前会话中该插件的开关和参数。
            validate_enabled (bool): 保存及执行前要求有效；读取时保留待修正参数。

        Returns:
            PreparedPlugin: 独立参数副本与已解析的连接声明。

        Raises:
            PluginConfigurationError: 参数无效；禁用插件仅缺少必填项时允许保存。
            ValueError: 启用插件生成的 MCP 声明不符合要求。
        """
        plugin = PluginRegistry.get(plugin_name)
        config = deepcopy(item.config)
        if plugin is None:
            return PreparedPlugin(plugin_name, config, ())
        # 1. 校验并填入默认值；读取设置时保留无效输入，方便用户继续修正。
        if plugin.config_model is not None:
            try:
                config = plugin.config_model.model_validate(config).model_dump(mode="json")
            except ValidationError as exc:
                if validate_enabled:
                    errors = [error for error in exc.errors() if item.enabled or error["type"] not in {"missing", "default_factory_not_called"}]
                    if errors:
                        raise PluginConfigurationError([
                            {"loc": ["agent_plugins", plugin_name, "config", *error["loc"]],
                             "msg": error["msg"], "type": error["type"]}
                            for error in errors
                        ], message=str(exc)) from exc
        # 2. 只在严格准备已启用插件时解析 MCP，解析函数直接使用本次校验结果。
        servers = self._resolve_mcp_servers(plugin, config) if item.enabled and validate_enabled else ()
        return PreparedPlugin(plugin_name, config, servers)

    def _resolve_mcp_servers(self, plugin: type[BasePlugin], config: dict[str, Any]) -> tuple[MCPConnectionConfig, ...]:
        """用已经校验的参数计算并检查 MCP 声明，保持插件参数副本独立。

        Args:
            plugin (type[BasePlugin]): 当前插件的声明类。
            config (dict[str, Any]): 已通过该插件配置模型校验的参数。

        Returns:
            tuple[MCPConnectionConfig, ...]: 参数校验通过的连接声明。

        Raises:
            ValueError: 连接声明包含重复服务 ID 或独立启停状态。
        """
        # 1. 解析器可以整理本次参数；使用独立副本，保持保存的配置及其嵌套内容不变。
        servers = tuple(plugin.resolve_mcp_servers(deepcopy(config)))
        from app.schemas.mcp import StdioMCPConnectionConfig, StreamableHttpMCPConnectionConfig
        if any(type(server) not in (StdioMCPConnectionConfig, StreamableHttpMCPConnectionConfig) for server in servers):
            raise ValueError(f"插件 {plugin.name} 必须声明不含 enabled 的 MCP 连接参数")
        ids = [server.id for server in servers]
        if len(ids) != len(set(ids)):
            raise ValueError(f"插件 {plugin.name} 的 MCP 声明重复或带有独立 enabled 开关")
        return servers
