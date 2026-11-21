from dataclasses import dataclass

from app.schemas.tool_types import LOCAL_TOOL_KEY_PREFIX, MCP_TOOL_KEY_PREFIX
from app.agent.tools.core.registry import ToolRegistry
from app.crud.chat_settings_dao import ChatSettingsDao
from app.errors import ResourceNotFoundError
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.mcp.identity import server_identity_fingerprint
from app.schemas.chat_settings import ChatSettings
from app.schemas.mcp import MCPModelSettings, MCPServerPolicy
from app.schemas.tool_permissions import ToolPolicy
from app.mcp.mcp_connection_manager import MCPConnectionManager
from app.services.settings_mutation import SettingsMutationCoordinator
from app.services.plugin_service import PluginService, PreparedPlugin, PluginConfigurationError
from app.repl.contracts import PreparedReplCapability


@dataclass(frozen=True)
class PreparedChatSettings:
    """汇总本次会话准备结果，供 Agent 复用参数和 MCP 声明。

    Attributes:
        settings (ChatSettings): 参数已整理、工具权限已补齐的会话设置副本。
        plugins (dict[str, PreparedPlugin]): 按插件名称保存本次准备的参数和连接声明。
        repl_capabilities (tuple[PreparedReplCapability, ...]): 启用插件提供的能力，暴露名唯一。
    """

    settings: ChatSettings
    plugins: dict[str, PreparedPlugin]
    repl_capabilities: tuple[PreparedReplCapability, ...]


class ChatSettingsService:
    """统一整理会话参数和工具权限，协调配置保存和 MCP 清理。"""

    def __init__(
        self,
        chat_settings_dao: ChatSettingsDao,
        mcp_settings_dao: MCPSettingsDao | None = None,
        connection_manager: MCPConnectionManager | None = None,
        mutation_coordinator: SettingsMutationCoordinator | None = None,
        plugin_service: PluginService | None = None,
    ):
        """初始化模型配置服务。

        Args:
            chat_settings_dao (ChatSettingsDao): 模型配置数据访问对象。
            mcp_settings_dao (MCPSettingsDao | None): 可选的 MCP Server 配置数据访问对象。
            connection_manager (MCPConnectionManager | None): 可选的 MCP 连接管理器。
            mutation_coordinator (SettingsMutationCoordinator | None): 可选的跨配置写入协调器。
            plugin_service (PluginService | None): 共用的插件参数校验和声明处理模块。
        """
        self.chat_settings_dao = chat_settings_dao
        self.mcp_settings_dao = mcp_settings_dao
        self.connection_manager = connection_manager
        self.mutation_coordinator = mutation_coordinator or SettingsMutationCoordinator()
        self.plugin_service = plugin_service or PluginService()

    def normalize(self, chat_settings: ChatSettings, *, validate_enabled: bool = True) -> ChatSettings:
        """校验插件参数，统一补齐独立工具和插件本地工具的会话权限。

        Args:
            chat_settings (ChatSettings): 读取、保存或创建 Agent 时使用的会话配置。
            validate_enabled (bool): 保存和执行前严格校验；读取设置时允许保留待修正参数。

        Returns:
            ChatSettings: 整理后的独立副本，缺失权限设为 ask，已有权限保持原值。

        Raises:
            PluginConfigurationError: 严格校验时插件参数或 MCP 声明不符合要求。
        """
        return self.prepare(chat_settings, validate_enabled=validate_enabled).settings

    def prepare(self, chat_settings: ChatSettings, *, validate_enabled: bool = True) -> PreparedChatSettings:
        """汇总插件准备结果并补齐工具权限，让调用方复用本次解析的连接声明。

        Args:
            chat_settings (ChatSettings): 读取、保存或创建 Agent 时使用的会话配置。
            validate_enabled (bool): 保存和执行前严格校验；读取时保留待修正参数。

        Returns:
            PreparedChatSettings: 整理后的设置及各插件准备结果。

        Raises:
            PluginConfigurationError: 插件参数或 MCP 声明无效，错误包含对应的配置字段路径。
        """
        result = chat_settings.model_copy(deep=True)
        plugins = {}
        capabilities: list[PreparedReplCapability] = []
        export_names: set[str] = set()
        # 1. 每个插件只准备一次，直接使用插件准备层报告的参数字段错误。
        for name, item in result.agent_plugins.items():
            try:
                prepared = self.plugin_service.prepare_plugin(name, item, validate_enabled=validate_enabled)
                for capability in prepared.repl_capabilities:
                    export = capability.declaration.export_name
                    if export in export_names:
                        raise ValueError(f"REPL 能力暴露名重复：{export}")
                    export_names.add(export)
                    capabilities.append(capability)
            except PluginConfigurationError:
                raise
            except ValueError as exc:
                raise PluginConfigurationError([{"loc": ["agent_plugins", name, "config"],
                                                 "msg": str(exc), "type": "value_error"}]) from exc
            item.config = prepared.config
            plugins[name] = prepared
        # 2. 独立工具的权限保存在会话顶层，只为当前目录中缺失的工具新增 ask。
        for name in ToolRegistry.list_tools():
            result.tool_permissions.setdefault(f"{LOCAL_TOOL_KEY_PREFIX}{name}", ToolPolicy.ASK)
        # 3. 插件本地工具使用各自的权限记录，禁用插件也可提前配置工具权限。
        for name, tools in self.plugin_service.local_tools().items():
            item = result.agent_plugins.get(name)
            if item is None:
                continue
            for tool in tools:
                item.tool_permissions.setdefault(f"{LOCAL_TOOL_KEY_PREFIX}{tool.name}", ToolPolicy.ASK)
        return PreparedChatSettings(result, plugins, tuple(sorted(capabilities, key=lambda item: item.declaration.export_name)))

    async def add_chat_settings(self, chat_settings: ChatSettings) -> ChatSettings:
        """新增模型配置并正规化 MCP 权限，保存成功后让记忆装配使用新配置。

        Args:
            chat_settings (ChatSettings): 待新增的模型配置。

        Returns:
            ChatSettings: 已保存的模型配置。
        """
        async with self.mutation_coordinator.lock:
            normalized = self._sanitize_plugins(self.normalize(chat_settings), None)
            saved = self.chat_settings_dao.add_chat_settings(self._sanitize_mcp(normalized, None))
            return saved

    async def delete_chat_settings(self, session_id: str) -> None:
        """删除配置并使缓存失效，文件替换成功后由 Scheduler 异步释放资源。

        Args:
            session_id (str): 待删除配置的父身份。
        """
        # 1. 配置写锁在外、执行受理短锁在内；文件替换失败时仍保留原身份。
        async with self.mutation_coordinator.lock:
            async with self.mutation_coordinator.admission_lock:
                self.chat_settings_dao.delete_chat_settings(session_id)

    def get_chat_settings_by_session(self, session_id: str) -> ChatSettings:
        """读取会话设置并在内存副本中整理参数与工具权限，保留待修正参数。

        Args:
            session_id (str): 当前操作所属的会话编号。

        Returns:
            ChatSettings: 指定会话的配置。

        Raises:
            ResourceNotFoundError: 指定会话不存在。
        """
        return self.normalize(self.chat_settings_dao.get_chat_settings(session_id), validate_enabled=False)

    async def update_chat_settings(self, chat_settings: ChatSettings) -> ChatSettings:
        """更新模型配置并安全合并 MCP 权限，保存成功后使旧记忆配置失效。

        Args:
            chat_settings (ChatSettings): 更新后的完整模型配置。

        Returns:
            ChatSettings: 已保存的模型配置。
        """
        async with self.mutation_coordinator.lock:
            try:
                existing = self.chat_settings_dao.get_chat_settings(chat_settings.session_id)
            except ResourceNotFoundError:
                existing = None
            normalized = self._sanitize_plugins(self.normalize(chat_settings), existing)
            sanitized = self._sanitize_mcp(normalized, existing)
            saved = self.chat_settings_dao.update_chat_settings(chat_settings.session_id, sanitized)
            if existing is not None and self.connection_manager is not None:
                for name, previous in existing.agent_plugins.items():
                    current = saved.agent_plugins.get(name)
                    if current is None or not current.enabled or current.config != previous.config:
                        await self.connection_manager.disconnect_plugin(saved.session_id, name)
            return saved

    def _sanitize_plugins(self, incoming: ChatSettings, existing: ChatSettings | None) -> ChatSettings:
        """保留框架建立的 MCP 身份，参数变化时清除旧授权绑定。

        Args:
            incoming (ChatSettings): 已校验的待保存设置。
            existing (ChatSettings | None): 原会话设置。

        Returns:
            ChatSettings: 不采纳客户端身份写入的配置。
        """
        for name, plugin in incoming.agent_plugins.items():
            previous = existing.agent_plugins.get(name) if existing else None
            plugin.mcp_identities = dict(previous.mcp_identities) if previous else {}
            if previous is None or previous.config != plugin.config:
                plugin.mcp_identities.clear()
                plugin.tool_permissions = {key: (ToolPolicy.ASK if key.startswith(MCP_TOOL_KEY_PREFIX) else policy)
                                           for key, policy in plugin.tool_permissions.items()}
        return incoming

    def _sanitize_mcp(
        self,
        chat_settings: ChatSettings,
        existing: ChatSettings | None,
    ) -> ChatSettings:
        """依据服务身份和可用工具目录正规化 MCP 权限。

        Args:
            chat_settings (ChatSettings): 前端提交的模型配置。
            existing (ChatSettings | None): 当前已保存的模型配置；新建时为 ``None``。

        Returns:
            ChatSettings: 已移除无效引用并安全降级新权限的配置副本。
        """
        if self.mcp_settings_dao is None:
            return chat_settings
        configs = {server.id: server for server in self.mcp_settings_dao.load().servers}
        normalized: dict[str, MCPServerPolicy] = {}
        permissions = {
            key: value
            for key, value in chat_settings.tool_permissions.items()
            if not key.startswith(MCP_TOOL_KEY_PREFIX)
        }
        # 1. 保存设置时按当前服务目录过滤已删除服务，并重新核对权限绑定的真实身份。
        for server_id, incoming in chat_settings.mcp.servers.items():
            config = configs.get(server_id)
            if config is None:
                continue
            identity = server_identity_fingerprint(config)
            previous = existing.mcp.servers.get(server_id) if existing else None
            previous_is_bound = bool(previous and previous.identity_fingerprint == identity)
            incoming_is_bound = incoming.identity_fingerprint == identity
            available_identity = (
                self.connection_manager.get_available_identity(server_id)
                if self.connection_manager is not None
                else None
            )
            available_tools = (
                [tool.name for tool in self.connection_manager.get_tools(server_id)]
                if available_identity == identity and self.connection_manager is not None
                else None
            )

            # 2. 新旧配置都绑定当前身份时才保留已有工具权限，新出现工具仍默认要求审批。
            if previous_is_bound and incoming_is_bound:
                tool_names = (
                    available_tools
                    if available_tools is not None
                    else list(dict.fromkeys([
                        *self._mcp_tool_names(existing, server_id),
                        *self._mcp_tool_names(chat_settings, server_id),
                    ]))
                )
                normalized[server_id] = MCPServerPolicy(
                    enabled=incoming.enabled,
                    identity_fingerprint=identity,
                )
                for name in tool_names:
                    key = f"{MCP_TOOL_KEY_PREFIX}{server_id}:{name}"
                    permissions[key] = (
                        chat_settings.tool_permissions.get(key, ToolPolicy.ASK)
                        if key in existing.tool_permissions
                        else ToolPolicy.ASK
                    )
                continue

            # 3. 身份不一致时重建工具列表并全部降为 ask，旧 allow 不能自动授权变化后的服务。
            if available_tools is not None:
                tool_names = available_tools
                fingerprint = identity
            else:
                tool_names = self._mcp_tool_names(chat_settings, server_id)
                fingerprint = previous.identity_fingerprint if previous else None
            normalized[server_id] = MCPServerPolicy(
                enabled=incoming.enabled,
                identity_fingerprint=fingerprint,
            )
            permissions.update({
                f"{MCP_TOOL_KEY_PREFIX}{server_id}:{name}": ToolPolicy.ASK
                for name in tool_names
            })

        return chat_settings.model_copy(
            update={
                "mcp": MCPModelSettings(servers=normalized),
                "tool_permissions": permissions,
            }
        )

    @staticmethod
    def _mcp_tool_names(settings: ChatSettings | None, server_id: str) -> list[str]:
        """从权限键中取出指定 MCP 服务已保存的工具名称，供配置重绑定使用。

        Args:
            settings (ChatSettings | None): 当前会话保存的模型和工具权限设置。
            server_id (str): MCP 服务的稳定编号。

        Returns:
            list[str]: 该服务的工具名称列表，没有设置时为空。
        """
        if settings is None:
            return []
        prefix = f"{MCP_TOOL_KEY_PREFIX}{server_id}:"
        return [
            key[len(prefix):]
            for key in settings.tool_permissions
            if key.startswith(prefix)
        ]
