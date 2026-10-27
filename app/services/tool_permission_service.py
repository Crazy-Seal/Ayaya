"""为设置页查询工具目录、校验权限修改并保存会话配置。"""

from app.agent.tools.authorization.catalog import UnifiedToolCatalog
from app.agent.tools.authorization.models import ToolIdentity
from app.crud.chat_settings_dao import ChatSettingsDao
from app.schemas.tool import ToolPolicyView
from app.schemas.tool_permissions import ToolPolicy
from app.schemas.chat_settings import AgentPluginSettings
from app.services.settings_mutation import SettingsMutationCoordinator


class ToolPermissionService:
    """处理设置页的工具权限查询和修改，执行时的权限判断由 ToolAuthorization 负责。

    Attributes:
        _chat_settings_dao (ChatSettingsDao): 读取当前会话配置并保存合并后的权限。
        _catalog (UnifiedToolCatalog): 提供本地及 MCP 工具名称、来源和可用状态。
        _mutation_coordinator (SettingsMutationCoordinator): 与会话设置、MCP 服务共用的写入锁。
    """

    def __init__(
        self,
        chat_settings_dao: ChatSettingsDao,
        catalog: UnifiedToolCatalog,
        mutation_coordinator: SettingsMutationCoordinator,
    ) -> None:
        """保存配置存储、工具目录和共用写入锁，收到设置页请求后再查询或保存。

        Args:
            chat_settings_dao (ChatSettingsDao): 当前应用共享的会话配置存储。
            catalog (UnifiedToolCatalog): 合并本地与 MCP 工具信息的目录。
            mutation_coordinator (SettingsMutationCoordinator): 防止权限修改覆盖同时发生的其他配置变更。
        """
        self._chat_settings_dao = chat_settings_dao
        self._catalog = catalog
        self._mutation_coordinator = mutation_coordinator

    def list_policies(self, session_id: str) -> list[ToolPolicyView]:
        """读取会话配置并合并工具目录，返回设置页所需的权限和不可用原因。

        Args:
            session_id (str): 当前操作所属的会话编号。

        Returns:
            list[ToolPolicyView]: 本地及 MCP 工具的权限展示记录。
        """
        settings = self._chat_settings_dao.get_chat_settings(session_id)
        return [
            ToolPolicyView(**entry.__dict__, policy_key=entry.policy_key)
            for entry in self._catalog.list_entries(settings)
        ]

    async def set_policy(
        self,
        session_id: str,
        identity: ToolIdentity,
        policy: ToolPolicy,
    ) -> list[ToolPolicyView]:
        """修改一个工具的会话权限，复用批量入口的校验、加锁和保存流程。

        Args:
            session_id (str): 用户正在修改工具权限的会话编号。
            identity (ToolIdentity): 按本地或 MCP 来源区分的工具标识。
            policy (ToolPolicy): 本次保存的 allow、ask 或 deny 权限。

        Returns:
            list[ToolPolicyView]: 保存后的完整工具目录，供设置页更新显示。
        """
        return await self.set_policies(session_id, {identity.stable_key: policy})

    async def set_policies(
        self,
        session_id: str,
        policies: dict[str, ToolPolicy],
    ) -> list[ToolPolicyView]:
        """校验整批工具权限并保存，再返回最新目录。

        配置读取和保存都在共用写入锁内完成。保存后的目录查询在锁外执行，
        查询失败不撤销已经保存的配置。

        Args:
            session_id (str): 当前操作所属的会话编号。
            policies (dict[str, ToolPolicy]): 按 local:名称 或 mcp:服务编号:名称 保存的工具权限。

        Returns:
            list[ToolPolicyView]: 保存后的权限展示记录。

        Raises:
            ValueError: 提交了不在工具目录中的标识，整批权限不保存。
            KeyError: 指定会话没有配置。
        """
        # 1. 先取得与会话设置、MCP 修改共用的锁，再读取配置，避免覆盖刚保存的变更。
        async with self._mutation_coordinator.lock:
            settings = self._chat_settings_dao.get_chat_settings(session_id)
            known = {entry.stable_key for entry in self._catalog.list_entries(settings)}
            unknown = sorted(set(policies) - known)
            if unknown:
                raise ValueError(f"包含未知工具权限: {', '.join(unknown)}")
            # 2. 所有工具标识有效后才合并本次权限，保留模型、人设及未修改的权限字段。
            merged = dict(settings.tool_permissions)
            plugins = {name: item.model_copy(deep=True) for name, item in settings.agent_plugins.items()}
            entries = {entry.stable_key: entry for entry in self._catalog.list_entries(settings)}
            for key, policy in policies.items():
                entry = entries[key]
                if entry.plugin_name:
                    item = plugins.setdefault(entry.plugin_name, AgentPluginSettings(enabled=False))
                    identity = ToolIdentity(source=entry.source, name=entry.name, server_id=entry.server_id,
                                            plugin_name=entry.plugin_name)
                    item.tool_permissions[identity.policy_key] = policy
                else:
                    merged[key] = policy
            self._chat_settings_dao.update_chat_settings(
                session_id, settings.model_copy(update={"tool_permissions": merged, "agent_plugins": plugins}),
            )
        # 3. 返回最新目录供设置页显示；即使查询失败，已保存配置仍然有效。
        return self.list_policies(session_id)
