"""插件钩子契约与基础实现。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PluginHook(str, Enum):
    """插件钩子点。"""

    BEFORE_USER_INPUT_COMMIT = "before_user_input_commit"
    AFTER_USER_INPUT_COMMIT = "after_user_input_commit"
    ON_INVOKE = "on_invoke"
    BEFORE_LLM = "before_llm"
    AFTER_LLM = "after_llm"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_RESPONSE_COMMIT = "before_response_commit"
    AFTER_RESPONSE_COMMIT = "after_response_commit"
    ON_INTERRUPT = "on_interrupt"
    ON_ERROR = "on_error"


@dataclass(frozen=True)
class HookContext:
    """钩子上下文：字段引用固定，状态及钩子数据只允许原地修改。"""

    hook: PluginHook
    agent_state: Any
    data: Any
    metadata: dict = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        hook: PluginHook,
        state: Any,
        data: Any = None,
        metadata: dict | None = None,
    ) -> "HookContext":
        """创建钩子上下文。"""
        return cls(
            hook=hook,
            agent_state=state,
            data=data,
            metadata=metadata or {},
        )


class BasePlugin(ABC):
    """插件基类。"""

    name: str
    description: str = ""
    inherent: bool = False
    config_model: Any = None
    version: str = "1.0.0"
    priority: int = 100

    @property
    @abstractmethod
    def hooks(self) -> list[PluginHook]:
        """订阅的钩子列表。"""
        pass

    @abstractmethod
    async def execute(self, context: HookContext) -> None:
        """原地修改状态或钩子数据，不返回替代上下文。"""
        pass

    async def on_register(self) -> None:
        """注册时的初始化。"""
        pass

    async def on_unregister(self) -> None:
        """注销时的清理。"""
        pass


__all__ = ["BasePlugin", "HookContext", "PluginHook"]
