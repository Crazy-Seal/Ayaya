from pathlib import Path

import yaml

from app.schemas.chat_settings import ChatSettings
from app.runtime import get_chat_settings_file
from app.schemas.tool_permissions import TOOL_PERMISSIONS_SCHEMA_VERSION


_LEGACY_APPROVAL_TOOLS = frozenset({
    "screenshot",
    "control_screen",
    "control_screen_with_vlm",
})


def _known_local_tool_names() -> list[str]:
    from app.agent.tools.registry import ToolRegistry

    return ToolRegistry.list_tools()


def _migrate_model_tool_permissions(item: dict) -> bool:
    """把单个模型的旧工具配置迁移到统一 stable-key 映射。"""
    changed = False
    version = item.get("tool_permissions_schema_version")
    permissions = dict(item.get("tool_permissions") or {})
    legacy_tools = {
        "apply_patch" if name in {"write_file", "edit_file", "delete_file"} else name
        for name in (item.get("tools_list") or [])
    }

    if version != TOOL_PERMISSIONS_SCHEMA_VERSION:
        for name in _known_local_tool_names():
            key = f"local:{name}"
            if key not in permissions:
                if name not in legacy_tools:
                    permissions[key] = "deny"
                elif name in _LEGACY_APPROVAL_TOOLS:
                    permissions[key] = "ask"
                else:
                    permissions[key] = "allow"
        for server_id, server in (((item.get("mcp") or {}).get("servers")) or {}).items():
            if not isinstance(server, dict):
                continue
            for name, policy in (server.get("tools") or {}).items():
                permissions.setdefault(f"mcp:{server_id}:{name}", policy)
        changed = True
    else:
        # 迁移完成后新发现的本地工具安全默认为 ASK。
        for name in _known_local_tool_names():
            key = f"local:{name}"
            if key not in permissions:
                permissions[key] = "ask"
                changed = True

    if item.pop("tools_list", None) is not None:
        changed = True
    for server in (((item.get("mcp") or {}).get("servers")) or {}).values():
        if isinstance(server, dict) and server.pop("tools", None) is not None:
            changed = True
    if item.get("tool_permissions") != permissions:
        item["tool_permissions"] = permissions
        changed = True
    if item.get("tool_permissions_schema_version") != TOOL_PERMISSIONS_SCHEMA_VERSION:
        item["tool_permissions_schema_version"] = TOOL_PERMISSIONS_SCHEMA_VERSION
        changed = True
    return changed


class ChatSettingsDao:
    def __init__(self, config_file: Path | None = None):
        self.config_file = config_file or get_chat_settings_file()
        self._cache: dict[str, ChatSettings] = {}

    def _load_chat_settings_file(self) -> dict:
        if not self.config_file.exists():
            raise RuntimeError(f"Config file not found: {self.config_file}")

        with self.config_file.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file) or {"chat_models": []}
        migrated = False
        for item in data.get("chat_models", []):
            migrated = _migrate_model_tool_permissions(item) or migrated
        if migrated:
            self._save_chat_settings_file(data)
        return data

    def _save_chat_settings_file(self, data: dict) -> None:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.config_file.with_suffix(f"{self.config_file.suffix}.tmp")
        with temp_file.open("w", encoding="utf-8") as file:
            yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False)
        temp_file.replace(self.config_file)

    def _clear_caches(self) -> None:
        """清除所有相关缓存"""
        self._cache.clear()
        # 清除 MemoryManager 工厂缓存
        from app.agent.memory import get_memory_manager
        get_memory_manager.cache_clear()

    @staticmethod
    def _to_chat_settings(item: dict) -> ChatSettings:
        return ChatSettings(
            session_id=item["session_id"],
            model_name=item["model_name"],
            openai_api_key=item["openai_api_key"],
            openai_base_url=item["openai_base_url"],
            temperature=item["temperature"],
            system_prompt=item["system_prompt"],
            tools_list=item.get("tools_list") or [],
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
            server_id: 待移除引用的 MCP Server ID。

        Returns:
            权限配置受到修改的会话 ID 列表。
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
            server_id: MCP Server ID。
            identity_fingerprint: 当前 Server 身份指纹。
            tool_names: 当前 Server 暴露的工具名称。

        Returns:
            权限配置受到同步的会话 ID 列表。
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
            server_id: 身份发生变化的 MCP Server ID。

        Returns:
            工具权限被降级为 ``ask`` 的会话 ID 列表。
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
            server_id: MCP Server ID。

        Returns:
            包含该 Server 权限配置的模型数量。
        """
        data = self._load_chat_settings_file()
        return sum(
            server_id in (((item.get("mcp") or {}).get("servers")) or {})
            for item in data["chat_models"]
        )
