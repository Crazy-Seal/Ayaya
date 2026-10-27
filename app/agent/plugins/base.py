"""插件钩子契约与基础实现。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from pydantic import BaseModel

from app.agent.plugins.runtime import PluginRuntime
from app.agent.tools.core.base import BaseTool
from app.schemas.mcp import MCPConnectionConfig


class PluginHook(str, Enum):
    """定义插件可订阅的执行阶段，激活输入钩子只属于新一轮输入提交。

    Args:
        value (str): 对应执行阶段的内部字符串标识。

    Attributes:
        BEFORE_ACTIVATION_INPUT_COMMIT (PluginHook): Agent 在新一轮真人或内部通知
            输入追加前触发，用于准备上一批派生任务；原轮次恢复和中途通知不触发。
        ON_INVOKE (PluginHook): 新一轮输入、协议历史和 Inbox 交付保存成功后触发，
            接收前置钩子准备的数据及 runtime_context；审批恢复沿用原轮次进度。
    """

    BEFORE_ACTIVATION_INPUT_COMMIT = "before_activation_input_commit"
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
    runtime: PluginRuntime | None = None

    @classmethod
    def create(
        cls,
        hook: PluginHook,
        state: Any,
        data: Any = None,
        metadata: dict | None = None,
        runtime: PluginRuntime | None = None,
    ) -> "HookContext":
        """创建钩子上下文。

        Args:
            hook (PluginHook): 当前插件登记或执行的时机。
            state (Any): 当前会话的内存 AgentState；保存由调用方负责。
            data (Any): 需要解析或保存的结构化数据。
            metadata (dict | None): 本次插件执行的附加调用信息。
            runtime (PluginRuntime | None): 框架绑定插件、会话、来源轮次及任务编号的调用对象。
        """
        return cls(
            hook=hook,
            agent_state=state,
            data=data,
            metadata=metadata if metadata is not None else {},
            runtime=runtime,
        )


class BasePlugin(ABC):
    """插件基类。"""

    name: str
    description: str = ""
    config_model: type[BaseModel] | None = None
    version: str = "1.0.0"
    priority: int = 100
    tools: tuple[type[BaseTool], ...] = ()
    mcp_servers: tuple[MCPConnectionConfig, ...] = ()

    @classmethod
    def resolve_mcp_servers(cls, config: dict[str, Any]) -> tuple[MCPConnectionConfig, ...]:
        """根据会话插件参数返回连接声明，默认使用类中列出的服务。

        Args:
            config (dict[str, Any]): 已通过插件配置模型校验的会话参数。

        Returns:
            tuple[MCPConnectionConfig, ...]: 随插件启用的服务连接参数。
        """
        return cls.mcp_servers

    @property
    @abstractmethod
    def hooks(self) -> list[PluginHook]:
        """订阅的钩子列表。"""
        pass

    @abstractmethod
    async def execute(self, context: HookContext) -> None:
        """原地修改状态或钩子数据，不返回替代上下文。

        Args:
            context (HookContext): 执行管道提供的工具运行上下文。
        """
        pass

    async def on_register(self) -> None:
        """注册时的初始化。"""
        pass

    async def on_unregister(self) -> None:
        """注销时的清理。"""
        pass


__all__ = ["BasePlugin", "HookContext", "PluginHook"]
