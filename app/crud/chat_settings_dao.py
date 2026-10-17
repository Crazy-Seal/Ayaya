from pathlib import Path

import yaml

from app.schemas.chat_settings import ChatSettings
from app.runtime import get_chat_settings_file
from app.schemas.tool_permissions import TOOL_PERMISSIONS_SCHEMA_VERSION


def _known_local_tool_names() -> list[str]:
    """读取已发现的本地工具名称，供配置加载时补齐新工具的默认审批权限。

    Returns:
        list[str]: 工具注册表公开的本地工具名称列表。
    """
    from app.agent.tools.core.registry import ToolRegistry

    return ToolRegistry.list_tools()


def _reconcile_local_tool_permissions(item: dict) -> bool:
    """补齐新工具权限，终端工具改名时保留旧许可和拒绝。

    Args:
        item (dict): 从会话配置文件读取的单项模型配置。

    Returns:
        bool: 本次补齐或改名规则是否改变了配置内容。
    """
    changed = False
    permissions = dict(item.get("tool_permissions") or {})
    # 1. 工具改名时保留既有许可，旧 deny 不能因新增交互工具而变为 ask。
    names = _known_local_tool_names()
    legacy = permissions.get("local:run_ps")
    if "exec_command" in names and "local:exec_command" not in permissions and legacy is not None:
        permissions["local:exec_command"] = legacy
        changed = True
    if "write_stdin" in names and "local:write_stdin" not in permissions and legacy == "deny":
        permissions["local:write_stdin"] = "deny"
        changed = True
    for name in names:
        key = f"local:{name}"
        if key not in permissions:
            permissions[key] = "ask"
            changed = True
    if item.get("tool_permissions") != permissions:
        item["tool_permissions"] = permissions
        changed = True
    if item.get("tool_permissions_schema_version") != TOOL_PERMISSIONS_SCHEMA_VERSION:
        item["tool_permissions_schema_version"] = TOOL_PERMISSIONS_SCHEMA_VERSION
        changed = True
    return changed


class ChatSettingsDao:
    """按会话读写模型设置 YAML，并管理本对象的设置缓存。"""
    def __init__(self, config_file: Path | None = None):
        """选择当前环境的设置文件，创建空缓存；此时不读取文件。

        Args:
            config_file (Path | None): 会话设置 YAML 路径；省略时根据当前运行环境选择。
        """
        self.config_file = config_file or get_chat_settings_file()
        self._cache: dict[str, ChatSettings] = {}

    def _load_chat_settings_file(self) -> dict:
        """读取 YAML，为新本地工具补上 ask 权限；发生补齐时将配置写回文件。

        Returns:
            dict: 已补齐权限的配置字典。

        Raises:
            RuntimeError: 设置文件不存在。
        """
        if not self.config_file.exists():
            raise RuntimeError(f"Config file not found: {self.config_file}")

        with self.config_file.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file) or {"chat_models": []}
        # 1. 加载时补齐新工具的默认审批权限，不覆盖用户已经保存的许可。
        migrated = False
        for item in data.get("chat_models", []):
            migrated = _reconcile_local_tool_permissions(item) or migrated
        if migrated:
            self._save_chat_settings_file(data)
        return data

    def _save_chat_settings_file(self, data: dict) -> None:
        """先将 YAML 写入同目录临时文件，再替换目标文件，避免直接覆盖时留下半份配置。

        Args:
            data (dict): 需要解析或保存的结构化数据。
        """
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        # 1. 临时文件写完才替换目标；写失败时原设置文件仍保留。
        temp_file = self.config_file.with_suffix(f"{self.config_file.suffix}.tmp")
        with temp_file.open("w", encoding="utf-8") as file:
            yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False)
        temp_file.replace(self.config_file)

    def _clear_caches(self) -> None:
        """设置文件保存成功后清除本对象设置缓存和记忆管理器工厂缓存，后续装配读取新配置。"""
        self._cache.clear()
        # 清除 MemoryManager 工厂缓存
        from app.agent.memory import get_memory_manager
        get_memory_manager.cache_clear()

    @staticmethod
    def _to_chat_settings(item: dict) -> ChatSettings:
        """把单项 YAML 配置转换为经过字段和插件配置校验的会话设置。

        Args:
            item (dict): 从会话配置文件读取的单项模型配置。

        Returns:
            ChatSettings: 规范化的 ChatSettings。
        """
        return ChatSettings(
            session_id=item["session_id"],
            model_name=item["model_name"],
            openai_api_key=item["openai_api_key"],
            openai_base_url=item["openai_base_url"],
            temperature=item["temperature"],
            system_prompt=item["system_prompt"],
            tool_permissions=item.get("tool_permissions") or {},
            tool_permissions_schema_version=item.get(
                "tool_permissions_schema_version", TOOL_PERMISSIONS_SCHEMA_VERSION
            ),
            context_strategy=item["context_strategy"],
            agent_plugins=item.get("agent_plugins") or {},
            skills=item.get("skills"),
            mcp=item.get("mcp") or {},
            # 提示词模板字段
            name=item.get("name"),
            feature=item.get("feature"),
            character=item.get("character"),
            address=item.get("address"),
            characteristic=item.get("characteristic"),
            constraint=item.get("constraint"),
        )

    def add_chat_settings(self, chat_settings: ChatSettings) -> ChatSettings:
        """确认会话编号未重复后保存新配置，文件保存成功后清除设置及记忆管理器缓存。

        Args:
            chat_settings (ChatSettings): 当前会话的模型、角色及插件设置。

        Returns:
            ChatSettings: 已保存的会话设置。

        Raises:
            ValueError: 会话编号已经存在。
        """
        data = self._load_chat_settings_file()
        chat_models = data["chat_models"]
        session_id = chat_settings.session_id

        if any(item["session_id"] == session_id for item in chat_models):
            raise ValueError(f"session_id already exists: {session_id}")

        chat_models.append(chat_settings.model_dump(mode="json"))
        self._save_chat_settings_file(data)
        self._clear_caches()
        return chat_settings

    def get_chat_settings(self, session_id: str) -> ChatSettings:
        """优先读取缓存；未缓存时从 YAML 查找并校验会话设置。

        Args:
            session_id (str): 当前操作所属的会话编号。

        Returns:
            ChatSettings: 当前会话设置，并缓存该结果。

        Raises:
            KeyError: 指定会话不存在。
        """
        if session_id in self._cache:
            return self._cache[session_id]

        data = self._load_chat_settings_file()
        for item in data["chat_models"]:
            if item["session_id"] == session_id:
                result = self._to_chat_settings(item)
                self._cache[session_id] = result
                return result

        raise KeyError(f"session_id not found: {session_id}")

    def delete_chat_settings(self, session_id: str) -> None:
        """删除指定会话配置，保存成功后清除关联缓存。

        Args:
            session_id (str): 当前操作所属的会话编号。

        Raises:
            KeyError: 指定会话不存在。
        """
        data = self._load_chat_settings_file()
        chat_models = data["chat_models"]

        for index, item in enumerate(chat_models):
            if item["session_id"] == session_id:
                del chat_models[index]
                self._save_chat_settings_file(data)
                self._clear_caches()
                return

        raise KeyError(f"session_id not found: {session_id}")

    def update_chat_settings(self, session_id: str, chat_settings: ChatSettings) -> ChatSettings:
        """替换已存在会话的整项设置，保存成功后清除关联缓存。

        Args:
            session_id (str): 当前操作所属的会话编号。
            chat_settings (ChatSettings): 当前会话的模型、角色及插件设置。

        Returns:
            ChatSettings: 已保存的新设置。

        Raises:
            KeyError: 指定会话不存在。
        """
        data = self._load_chat_settings_file()
        chat_models = data["chat_models"]

        for index, item in enumerate(chat_models):
            if item["session_id"] == session_id:
                chat_models[index] = chat_settings.model_dump(mode="json")
                self._save_chat_settings_file(data)
                self._clear_caches()
                return chat_settings

        raise KeyError(f"session_id not found: {session_id}")

    def remove_mcp_server_references(self, server_id: str) -> list[str]:
        """移除全部模型对指定 MCP Server 的授权。

        Args:
            server_id (str): 待移除引用的 MCP Server ID。

        Returns:
            list[str]: 权限配置受到修改的会话 ID 列表。
        """
        data = self._load_chat_settings_file()
        affected: list[str] = []
        for item in data["chat_models"]:
            servers = ((item.get("mcp") or {}).get("servers") or {})
            if server_id not in servers:
                continue
            del servers[server_id]
            item["mcp"] = {"servers": servers}
            prefix = f"mcp:{server_id}:"
            item["tool_permissions"] = {
                key: value
                for key, value in (item.get("tool_permissions") or {}).items()
                if not key.startswith(prefix)
            }
            affected.append(item["session_id"])
        if affected:
            self._save_chat_settings_file(data)
            self._clear_caches()
        return affected

    def reconcile_mcp_server_tools(
        self,
        server_id: str,
        identity_fingerprint: str,
        tool_names: list[str],
    ) -> list[str]:
        """按当前服务身份和工具目录安全重绑定全部模型权限。

        Args:
            server_id (str): MCP Server ID。
            identity_fingerprint (str): 当前 Server 身份指纹。
            tool_names (list[str]): 当前 Server 暴露的工具名称。

        Returns:
            list[str]: 权限配置受到同步的会话 ID 列表。
        """
        data = self._load_chat_settings_file()
        affected: list[str] = []
        for item in data["chat_models"]:
            servers = ((item.get("mcp") or {}).get("servers") or {})
            policy = servers.get(server_id)
            if not isinstance(policy, dict):
                continue
            same_identity = policy.get("identity_fingerprint") == identity_fingerprint
            prefix = f"mcp:{server_id}:"
            previous_permissions = item.get("tool_permissions") or {}
            item["tool_permissions"] = {
                key: value
                for key, value in previous_permissions.items()
                if not key.startswith(prefix)
            }
            item["tool_permissions"].update({
                f"{prefix}{name}": previous_permissions.get(f"{prefix}{name}", "ask")
                if same_identity else "ask"
                for name in tool_names
            })
            policy["identity_fingerprint"] = identity_fingerprint
            affected.append(item["session_id"])
        if affected:
            self._save_chat_settings_file(data)
            self._clear_caches()
        return affected

    def invalidate_mcp_server_identity(self, server_id: str) -> list[str]:
        """在服务身份变化但尚未验证时降级工具权限。

        Args:
            server_id (str): 身份发生变化的 MCP Server ID。

        Returns:
            list[str]: 工具权限被降级为 ``ask`` 的会话 ID 列表。
        """
        data = self._load_chat_settings_file()
        affected: list[str] = []
        for item in data["chat_models"]:
            policy = (((item.get("mcp") or {}).get("servers") or {}).get(server_id))
            if not isinstance(policy, dict):
                continue
            prefix = f"mcp:{server_id}:"
            permissions = item.get("tool_permissions") or {}
            item["tool_permissions"] = {
                key: "ask" if key.startswith(prefix) else value
                for key, value in permissions.items()
            }
            affected.append(item["session_id"])
        if affected:
            self._save_chat_settings_file(data)
            self._clear_caches()
        return affected

    def count_mcp_server_references(self, server_id: str) -> int:
        """统计引用指定 MCP Server 的模型数量。

        Args:
            server_id (str): MCP Server ID。

        Returns:
            int: 包含该 Server 权限配置的模型数量。
        """
        data = self._load_chat_settings_file()
        return sum(
            server_id in (((item.get("mcp") or {}).get("servers")) or {})
            for item in data["chat_models"]
        )
