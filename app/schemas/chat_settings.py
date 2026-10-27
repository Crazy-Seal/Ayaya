from typing import Any
import json

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
    tool_permissions: dict[str, ToolPolicy] = Field(default_factory=dict)
    mcp_identities: dict[str, str] = Field(default_factory=dict)


class ChatSettings(BaseModel):
    """承载会话设置的数据结构，动态插件校验由应用层执行。"""
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
        """补齐旧会话的默认记忆记录，保留用户开关和插件专属数据。

        Returns:
            ChatSettings: 已规范化插件设置的当前 ChatSettings。
        """
        plugins = dict(self.agent_plugins or {})

        # 1. 旧会话沿用记忆默认开启；已有记录中的显式禁用会继续保留。
        plugins.setdefault("memory", AgentPluginSettings(config=dict(MEMORY_DEFAULT_CONFIG)))
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
            json.dumps({name: settings.model_dump(mode="json") for name, settings in self.agent_plugins.items()},
                       sort_keys=True, ensure_ascii=False, separators=(",", ":")),
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
