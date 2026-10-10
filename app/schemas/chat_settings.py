from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.agent.context import ContextStrategyConfig
from app.schemas.mcp import MCPModelSettings
from app.schemas.tool_permissions import TOOL_PERMISSIONS_SCHEMA_VERSION, ToolPolicy


MEMORY_DEFAULT_CONFIG = {
    "enable_diary": True,
    "enable_episodic": True,
    "enable_semantic": True,
    "long_term_memory_every_human_messages": 10,
}

class AgentPluginSettings(BaseModel):
    """保存会话中一个插件的启用标记与专属配置。"""
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


def _merge_plugin_config(defaults: dict[str, Any], value: dict[str, Any] | None) -> dict[str, Any]:
    """在默认配置副本上覆盖已保存值，避免修改全局默认设置。

    Args:
        defaults (dict[str, Any]): 该插件的默认配置副本来源。
        value (dict[str, Any] | None): 当前需要校验或转换的值。

    Returns:
        dict[str, Any]: 合并后的独立配置字典。
    """
    merged = dict(defaults)
    if value:
        merged.update(value)
    return merged


def _validate_plugin_config(name: str, config: dict[str, Any]) -> dict[str, Any]:
    """使用插件声明的配置模型规范化设置，未声明模型时保持原配置。

    Args:
        name (str): 当前对象的名称。
        config (dict[str, Any]): 调用方提供的配置。

    Returns:
        dict[str, Any]: 经过配置模型转换的字典，或原字典。
    """
    from app.agent.plugins.registry import PluginRegistry

    plugin_class = PluginRegistry.get(name)
    config_model = getattr(plugin_class, "config_model", None) if plugin_class else None
    if config_model is None:
        return config
    return config_model(**config).model_dump()


class ChatSettings(BaseModel):
    """承载会话的模型、角色、上下文策略和权限，构造时同步规范化插件配置。"""
    session_id: str
    model_name: str
    openai_api_key: str
    openai_base_url: str
    temperature: float
    system_prompt: str
    tool_permissions: dict[str, ToolPolicy] = Field(default_factory=dict)
    tool_permissions_schema_version: int = TOOL_PERMISSIONS_SCHEMA_VERSION
    context_strategy: ContextStrategyConfig
    agent_plugins: dict[str, AgentPluginSettings] | None = None
    skills: list[str] | None = None
    mcp: MCPModelSettings = Field(default_factory=MCPModelSettings)

    name: str | None = None
    feature: str | None = None
    character: str | None = None
    address: str | None = None
    characteristic: str | None = None
    constraint: str | None = None

    @model_validator(mode="after")
    def normalize_agent_plugins(self) -> "ChatSettings":
        """Pydantic 字段校验后补齐固有记忆插件，并按各插件配置模型校验设置。

        Returns:
            ChatSettings: 已规范化插件设置的当前 ChatSettings。
        """
        plugins = dict(self.agent_plugins or {})

        memory = plugins.get("memory")
        if memory is None:
            plugins["memory"] = AgentPluginSettings(
                enabled=True,
                config=_validate_plugin_config("memory", dict(MEMORY_DEFAULT_CONFIG)),
            )
        else:
            plugins["memory"] = AgentPluginSettings(
                enabled=True,
                config=_validate_plugin_config(
                    "memory",
                    _merge_plugin_config(MEMORY_DEFAULT_CONFIG, memory.config),
                ),
            )

        for name, settings in list(plugins.items()):
            if name == "memory":
                continue
            plugins[name] = AgentPluginSettings(
                enabled=settings.enabled,
                config=_validate_plugin_config(name, settings.config),
            )

        self.agent_plugins = plugins
        return self

    def __hash__(self):
        """按模型、权限、上下文和插件配置生成哈希，反映影响 Agent 装配的设置变化。

        Returns:
            int: 当前配置字段组合的哈希值。
        """
        return hash((
            self.session_id,
            self.model_name,
            self.openai_api_key,
            self.openai_base_url,
            self.temperature,
            self.system_prompt,
            tuple(sorted((name, value.value) for name, value in self.tool_permissions.items())),
            tuple(sorted(self.context_strategy.model_dump().items())),
            tuple(
                (name, settings.enabled, tuple(sorted(settings.config.items())))
                for name, settings in sorted(self.agent_plugins.items())
            ),
            tuple(self.skills or []),
            tuple(
                (
                    server_id,
                    policy.enabled,
                    policy.identity_fingerprint,
                )
                for server_id, policy in sorted(self.mcp.servers.items())
            ),
        ))
