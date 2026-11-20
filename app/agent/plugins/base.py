"""插件钩子契约与基础实现。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from pydantic import BaseModel

from app.agent.plugins.runtime import PluginRuntime
from app.agent.state.model import AgentState
from app.agent.tools.core.base import BaseTool
from app.schemas.mcp import MCPConnectionConfig
from app.repl.repl_api import ReplCapability
from app.crud.sqlite_factory import SqliteFactory
from app.schemas.chat_settings import ChatSettings


@dataclass(frozen=True)
class PluginContext:
    """在插件装配时提供当前执行的配置快照和存储依赖。

    Attributes:
        sqlite_factory (SqliteFactory): 宿主提供的数据库位置和连接配置，连接由实际使用者打开并关闭。
        chat_settings (ChatSettings | None): 当前 Agent 使用的独立会话配置快照；独立执行可省略。
    """

    sqlite_factory: SqliteFactory
    chat_settings: ChatSettings | None = None


class PluginHook(str, Enum):
    """定义插件可订阅的执行阶段，激活输入钩子只属于新一轮输入提交。

    Args:
        value (str): 对应执行阶段的内部字符串标识。

    Attributes:
        BEFORE_ACTIVATION_INPUT_COMMIT (PluginHook): Agent 在新一轮真人或内部通知
            输入追加到内存后、持久提交前触发，用于准备随输入保存的插件状态和后续任务材料；
            原轮次恢复和中途通知不触发。
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
    agent_state: AgentState
    data: Any
    metadata: dict = field(default_factory=dict)
    runtime: PluginRuntime | None = None

    @classmethod
    def create(
        cls,
        hook: PluginHook,
        state: AgentState,
        data: Any = None,
        metadata: dict | None = None,
        runtime: PluginRuntime | None = None,
    ) -> "HookContext":
        """创建钩子上下文。

        Args:
            hook (PluginHook): 当前插件登记或执行的时机。
            state (AgentState): 当前会话的内存状态；保存由调用方负责。
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
    """声明插件能力，框架以 Plugin(context=context, **config) 创建实例。"""

    name: str
    description: str = ""
    config_model: type[BaseModel] | None = None
    version: str = "1.0.0"
    priority: int = 100
    tools: tuple[type[BaseTool], ...] = ()
    mcp_servers: tuple[MCPConnectionConfig, ...] = ()

    def __init__(self, *, context: PluginContext) -> None:
        """接收宿主装配依赖，插件可覆盖构造函数读取自己的配置参数。

        Args:
            context (PluginContext): 当前执行的配置快照及数据库连接工厂。
        """

    def create_tools(self) -> tuple[BaseTool, ...]:
        """为当前插件创建工具，依赖共享插件资源的工具由所属插件装配。

        Returns:
            tuple[BaseTool, ...]: 已绑定依赖、准备交给工具管理器的实例。
        """
        return tuple(tool_class() for tool_class in self.tools)

    @classmethod
    def resolve_mcp_servers(cls, config: dict[str, Any]) -> tuple[MCPConnectionConfig, ...]:
        """根据会话插件参数返回连接声明，默认使用类中列出的服务。

        Args:
            config (dict[str, Any]): 已通过插件配置模型校验的会话参数。

        Returns:
            tuple[MCPConnectionConfig, ...]: 随插件启用的服务连接参数。
        """
        return cls.mcp_servers

    @classmethod
    def resolve_repl_capabilities(cls, config: dict[str, Any]) -> tuple[ReplCapability, ...]:
        """声明插件启用时向原生 Python REPL 预装的能力。

        Args:
            config (dict[str, Any]): 已校验的会话插件配置副本。

        Returns:
            tuple[ReplCapability, ...]: 由 runner 导入并调用 create 的能力声明。
        """
        return ()

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


__all__ = ["BasePlugin", "PluginContext", "HookContext", "PluginHook"]
