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

LEGACY_FILE_MUTATION_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})


class AgentPluginSettings(BaseModel):
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


def _merge_plugin_config(defaults: dict[str, Any], value: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(defaults)
    if value:
        merged.update(value)
    return merged


def _validate_plugin_config(name: str, config: dict[str, Any]) -> dict[str, Any]:
    from app.agent.plugins.registry import PluginRegistry

    plugin_class = PluginRegistry.get(name)
    config_model = getattr(plugin_class, "config_model", None) if plugin_class else None
    if config_model is None:
        return config
    return config_model(**config).model_dump()


class ChatSettings(BaseModel):
    session_id: str
    model_name: str
    openai_api_key: str
    openai_base_url: str
    temperature: float
    system_prompt: str
    # 兼容旧请求一个迁移周期；持久化时只写 tool_permissions。
    tools_list: list[str] = Field(default_factory=list, exclude=True)
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
    def normalize_tools_list(self) -> "ChatSettings":
        normalized_tools: list[str] = []
        for tool_name in self.tools_list:
            current = "apply_patch" if tool_name in LEGACY_FILE_MUTATION_TOOLS else tool_name
            if current not in normalized_tools:
                normalized_tools.append(current)
        self.tools_list = normalized_tools
        if normalized_tools and not any(key.startswith("local:") for key in self.tool_permissions):
            approval_tools = {"screenshot", "control_screen", "control_screen_with_vlm"}
            self.tool_permissions.update({
                f"local:{name}": ToolPolicy.ASK if name in approval_tools else ToolPolicy.ALLOW
                for name in normalized_tools
            })
        for server_id, server in self.mcp.servers.items():
            for name, policy in server.tools.items():
                self.tool_permissions.setdefault(f"mcp:{server_id}:{name}", ToolPolicy(policy))
        return self

    @model_validator(mode="after")
    def normalize_agent_plugins(self) -> "ChatSettings":
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
