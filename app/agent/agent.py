"""
Agent 核心类

门面类，协调各管理器，提供统一的接口。
"""
from app.crud.sqlite_factory import SqliteFactory

import asyncio
import sqlite3
import logging
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, TYPE_CHECKING, Awaitable, Callable, cast

from app.agent.models.contracts import OPENAI_CHAT_COMPLETIONS
from app.schemas.runtime_types import (
    CheckpointType, DESKTOP_CHANNEL, InboxKind,
)
from app.agent.context import (
    ContextStrategyConfig,
    ModelInputCoordinator,
    RuntimeContext,
    build_token_accounting_key,
)
from app.agent.core.tool_manager import ToolManager
from app.agent.core.plugin_manager import PluginManager
from app.agent.plugins.runtime import PluginRuntimeFactory
from app.agent.core.events import EventType, AgentEvent
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.models.llm_client import LLMClient, LLMConfig
from app.agent.plugins.base import BasePlugin, PluginContext, PluginHook
from app.agent.state import AgentState
from app.agent.state.manager import SessionStateManager
from app.agent.runtime.session_inbox import InboxEnvelope, InputNoLongerPending
from app.agent.runtime.turn_control import TurnControl, TurnInterrupted
from app.crud.sqlite_save import SqliteRollbackError
from app.agent.utils.infra.bounded_operation import OperationTimeoutError
from app.agent.state.manager import RequiredSaveError
from app.agent.utils.infra.bounded_operation import close_resources
from app.agent.state.messages import RuntimeInputMessage, AssistantMessage, HumanMessage
from app.schemas.tool_identity import ToolIdentity
from app.schemas.runtime_types import AgentRunOutcome, AgentRunOutcomeKind, TurnInterruptReason
from app.agent.tools.core.runtime import ToolRuntimeFactory
from app.agent.tools.core.base import BaseTool
from app.agent.tools.authorization.manager import ToolAuthorization
from app.schemas.mcp import MCPModelSettings, MCPConnectionConfig, MCPServerConfig
from app.mcp.types import PluginMCPBinder

if TYPE_CHECKING:
    from app.schemas.chat_settings import ChatSettings
    from app.mcp.mcp_connection_manager import MCPConnectionManager

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """保存一次 Agent 装配所需的会话、模型、提示词、上下文策略和工具/插件选择。"""
    session_id: str
    model_name: str
    api_key: str
    context_strategy: ContextStrategyConfig
    base_url: str = "https://api.openai.com/v1"
    api_format: str = OPENAI_CHAT_COMPLETIONS
    temperature: float = 0.7
    system_prompt: str = ""
    tools: list[str] = field(default_factory=list)
    plugins: list[str | dict[str, Any]] = field(default_factory=list)
    mcp: MCPModelSettings = field(default_factory=MCPModelSettings)
    plugin_mcp: dict[str, tuple[MCPConnectionConfig, ...]] = field(default_factory=dict)


class Agent:
    """管理单次 Agent 实例的模型、工具与插件，并拥有输入和最终响应的提交权。

    Attributes:
        config (AgentConfig): 当前 Agent 配置。
        mcp_connection_manager ('MCPConnectionManager | None'): 共享 MCP 连接管理器。
        tool_authorization (ToolAuthorization | None): 工具单次授权验证器。
        tool_manager (ToolManager): 本地及 MCP 工具注册管理器。
        plugin_manager (PluginManager): 当前插件的名称索引和业务钩子分发器。
        llm_client (LLMClient | None): 可替换的流式模型客户端。
        pipeline (ExecutionPipeline): 模型工具执行管道。
        _initialized (bool): 工具和插件是否已经完成初始化。
        session_state_manager (SessionStateManager): 当前会话的输入和状态业务对象。
        _owns_state_manager (bool): 独立创建时负责关闭；worker 注入的对象只借用。
    """

    def __init__(
        self,
        config: AgentConfig,
        sqlite_factory: SqliteFactory,
        mcp_connection_manager: "MCPConnectionManager | None" = None,
        *,
        llm_client: LLMClient | None = None,
        chat_settings: "ChatSettings | None" = None,
        tool_authorization: ToolAuthorization | None = None,
        tool_runtime_factory: ToolRuntimeFactory | None = None,
        session_state_manager: SessionStateManager | None = None,
        plugin_runtime_factory: PluginRuntimeFactory | None = None,
        bind_plugin_mcp: PluginMCPBinder | None = None,
        permission_session_id: str | None = None,
        tool_whitelist: frozenset[ToolIdentity] | None = None,
        enable_plugin_hooks: bool = True,
        environment_provider: Callable[[], Awaitable[str]] | None = None,
    ):
        """装配 Agent 执行依赖，将后台任务启动端口注入工具管道。

        Args:
            config (AgentConfig): 当前会话的 Agent 配置。
            mcp_connection_manager (MCPConnectionManager | None): 共享 MCP 连接管理器。
            llm_client (LLMClient | None): 可替换的流式模型客户端。
            chat_settings (ChatSettings | None): 会话模型和插件配置。
            tool_authorization (ToolAuthorization | None): 工具单次授权验证器。
            tool_runtime_factory (ToolRuntimeFactory | None): 按工具、轮次及调用编号创建运行时。
            session_state_manager (SessionStateManager | None): worker 借给本次执行的对象；省略时由 Agent 创建并关闭。
            plugin_runtime_factory (PluginRuntimeFactory | None): 按插件名称创建所属会话 PluginRuntime 的注入工厂。
            bind_plugin_mcp (PluginMCPBinder | None): 连接成功后由应用层保存身份与工具默认权限。
            permission_session_id (str | None): 子执行使用父会话的实时授权和共享连接身份。
            tool_whitelist (frozenset[ToolIdentity] | None): 子普通工具可见范围；空集合表示无普通工具。
            enable_plugin_hooks (bool): 是否执行已登记插件的自动业务钩子。
            environment_provider (Callable | None): 每次模型请求前异步读取当前子名单的入口。
            sqlite_factory (SqliteFactory): 应用装配提供的 SQLite 连接配置。
        """
        self.config = config
        self._sqlite_factory = sqlite_factory
        self.mcp_connection_manager = mcp_connection_manager
        self._bind_plugin_mcp = bind_plugin_mcp

        # 核心管理器
        self._plugin_settings = chat_settings.model_copy(deep=True) if chat_settings is not None else None
        self.tool_authorization = tool_authorization
        if self.tool_authorization is None and chat_settings is not None:
            self.tool_authorization = ToolAuthorization(lambda _session_id: chat_settings)
        self.tool_manager = ToolManager(
            session_id=permission_session_id or config.session_id,
            tool_whitelist=tool_whitelist,
            authorization=self.tool_authorization,
        )
        self._permission_session_id = permission_session_id or config.session_id
        self.plugin_manager = PluginManager(plugin_runtime_factory, enable_plugin_hooks=enable_plugin_hooks)
        self._owns_state_manager = session_state_manager is None
        self.session_state_manager = session_state_manager or SessionStateManager(config.session_id, sqlite_factory)
        # LLM 客户端
        self.llm_client = llm_client
        if self.llm_client is None:
            self.llm_client = LLMClient(LLMConfig(
                model=config.model_name,
                api_key=config.api_key,
                base_url=config.base_url,
                temperature=config.temperature,
                api_format=config.api_format,
            ))

        # 模型输入与执行管道
        model_input = ModelInputCoordinator(
            config.context_strategy,
            self.llm_client,
            chat_settings=chat_settings,
            accounting_key=build_token_accounting_key(
                protocol=config.api_format,
                model=config.model_name,
                endpoint=config.base_url,
            ),
        )
        self.pipeline = ExecutionPipeline(
            prompt_source=self.config,
            llm_client=self.llm_client,
            tool_manager=self.tool_manager,
            plugin_manager=self.plugin_manager,
            model_input=model_input,
            tool_authorization=self.tool_authorization,
            tool_runtime_factory=tool_runtime_factory,
            permission_session_id=self._permission_session_id,
            environment_provider=environment_provider,
        )

        # 初始化标志
        self._initialized = False


    async def initialize(self) -> None:
        """首次运行前装配工具和插件，完成后登记已初始化；重复调用复用现有装配结果。"""
        if self._initialized:
            return

        # 加载工具
        await self._setup_tools()

        # 加载插件
        self._setup_plugins()

        # 初始化 MCP
        await self._setup_mcp()

        self._initialized = True
        logger.info(f"Agent 初始化完成: {self.config.session_id}")

    async def _setup_tools(self) -> None:
        """加载普通工具，带应用依赖的委派及 Python 工具由工厂预装。"""
        # 从工具注册表加载
        from app.agent.tools.core.registry import ToolRegistry
        from app.schemas.subagent_types import SUBAGENT_PARENT_TOOL_NAMES
        from app.schemas.tool_types import REPL_TOOL_NAMES

        tool_names = (
            ToolRegistry.list_tools()
            if self.tool_authorization is not None
            else list(self.config.tools or [])
        )
        for tool_name in tool_names:
            if tool_name in SUBAGENT_PARENT_TOOL_NAMES | REPL_TOOL_NAMES or self.tool_manager.has(tool_name):
                continue
            tool_class = ToolRegistry.get(tool_name)
            if tool_class:
                tool = tool_class()
                self.tool_manager.register(tool)
                logger.info(f"加载工具: {tool_name}")
            else:
                logger.warning(f"工具 '{tool_name}' 不存在于注册表中")

    def _setup_plugins(self) -> None:
        """按会话配置创建插件并向管理器登记执行钩子。"""
        # 从插件注册表加载
        from app.agent.plugins.registry import PluginRegistry

        for plugin_config in self.config.plugins or []:
            if isinstance(plugin_config, str):
                plugin_name = plugin_config
                plugin_params = {}
            else:
                plugin_name = plugin_config.get("name")
                plugin_params = plugin_config.get("config", {})

            if not isinstance(plugin_name, str):
                raise ValueError("插件配置缺少名称")

            plugin_class = PluginRegistry.get(plugin_name)
            if plugin_class:
                try:
                    settings = self._plugin_settings.model_copy(deep=True) if self._plugin_settings is not None else None
                    context = PluginContext(sqlite_factory=self._sqlite_factory, chat_settings=settings)
                    plugin = plugin_class(context=context, **plugin_params)
                    self.plugin_manager.register(plugin)
                    from app.agent.plugins.tool_adapter import PluginToolAdapter
                    for tool in plugin.create_tools():
                        adapter = PluginToolAdapter(plugin_name, tool)
                        if self.tool_manager.has(adapter.name):
                            raise ValueError(f"插件工具公开名称冲突: {adapter.name}")
                        self.tool_manager.register(adapter)
                    logger.info(f"加载插件: {plugin_name}")
                except Exception:
                    logger.exception("插件 '%s' 初始化失败", plugin_name)
            else:
                logger.warning(f"插件 '{plugin_name}' 不存在于注册表中")

    async def _setup_mcp(self) -> None:
        """连接当前模型启用的 Server 并组装 MCP 模型工具空间。"""
        if not self.config.mcp.servers and not self.config.plugin_mcp:
            return
        if self.mcp_connection_manager is None or self.tool_authorization is None:
            logger.warning("MCP 配置已启用，但连接管理器或统一权限层未注入")
            return

        from app.agent.mcp.adapter import MCPToolAdapter
        from app.mcp.mcp_connection_manager import MCPConnectionError

        for server_id, server_policy in self.config.mcp.servers.items():
            try:
                server_config = cast(MCPServerConfig, self.mcp_connection_manager.get_server_config(server_id))
            except MCPConnectionError as exc:
                logger.error("MCP Server '%s' 配置不可用，已跳过: %s", server_id, exc)
                continue
            if not server_config.enabled or not server_policy.enabled:
                continue
            try:
                descriptors = await self.mcp_connection_manager.connect(server_id)
            except MCPConnectionError as exc:
                logger.error("MCP Server '%s' 连接失败，已跳过: %s", server_id, exc)
                continue
            for descriptor in descriptors:
                adapter = MCPToolAdapter(
                    session_id=self._permission_session_id,
                    server_id=server_id,
                    server_name=server_config.name,
                    descriptor=descriptor,
                    connection_manager=self.mcp_connection_manager,
                    authorization=self.tool_authorization,
                )
                if self.tool_manager.has(adapter.name):
                    logger.error("MCP 工具名称与现有工具冲突，已跳过: %s", adapter.name)
                    continue
                self.tool_manager.register(adapter)

        # 1. 插件连接按会话、插件、服务区分；声明即随插件启用，无独立服务开关。
        from app.mcp.types import PluginMCPKey
        for plugin_name, servers in self.config.plugin_mcp.items():
            if self.plugin_manager.get_plugin(plugin_name) is None:
                continue
            for server in servers:
                key = PluginMCPKey(self._permission_session_id, plugin_name, server.id)
                try:
                    descriptors = await self.mcp_connection_manager.connect(key)
                    if self._bind_plugin_mcp is not None:
                        await self._bind_plugin_mcp(key, server, descriptors)
                    for descriptor in descriptors:
                        adapter = MCPToolAdapter(
                            session_id=self._permission_session_id, server_id=server.id,
                            server_name=server.name, descriptor=descriptor,
                            connection_manager=self.mcp_connection_manager,
                            authorization=self.tool_authorization, plugin_name=plugin_name,
                        )
                        if self.tool_manager.has(adapter.name):
                            logger.error("插件 MCP 工具公开名称冲突，已跳过: %s", adapter.name)
                            continue
                        self.tool_manager.register(adapter)
                except MCPConnectionError as exc:
                    logger.error("插件 MCP '%s' 连接失败: %s", key, exc)

    async def _run(
        self,
        message: str,
        images: list[str] | None = None,
        *,
        origin_channel: str = DESKTOP_CHANNEL,
        inbox_input: InboxEnvelope | None = None,
        inbox_inputs: tuple[InboxEnvelope, ...] | None = None,
        turn_control: TurnControl | None = None,
        receive_task_notifications: bool = False,
        emit: Callable[[AgentEvent], Awaitable[None]],
    ) -> AgentRunOutcome:
        """交付真人输入并运行一轮对话，先闭合遗留工具协议，再由 Agent 提交最终响应。

        Args:
            message (str): 真人文本或类型声明对应的内部通知。
            images (list[str] | None): 真人附件的图片 data URL 列表。
            origin_channel (str): 输入或本轮回复所属的渠道。
            inbox_input (InboxEnvelope | None): 随本次 checkpoint 原子交付的单条输入快照。
            inbox_inputs (tuple[InboxEnvelope, ...] | None): 随本次 checkpoint 原子交付的输入快照集合。
            turn_control (TurnControl | None): 当前 run 的停止与引导控制器。
            receive_task_notifications (bool): 是否在模型安全点交付任务通知。

            emit (Callable[[AgentEvent], Awaitable[None]]): 接收本轮已确定执行事件的异步输出入口。

        Returns:
            AgentRunOutcome: 本次执行的明确结果，过程事件由 emit 交付。
        """
        await self.initialize()
        runtime_context = RuntimeContext.capture()
        runtime_context.origin_channel = origin_channel
        runtime_context.turn_control = turn_control

        # 1. Agent 自行加载状态，再报告执行开始；Scheduler 不预加载执行材料。
        state = await self.session_state_manager.load()
        await emit(AgentEvent(EventType.RUN_STARTED, {"origin_channel": origin_channel, "resumed": False}))
        # 2. 新输入前结束旧协议，业务状态准备和保存统一交给 Manager。
        await self.session_state_manager.close_previous_tools(state)

        # 3. 原子提交本批内部通知与真人输入，再进入可停止的模型工具循环。
        activation_data: dict = {}
        try:
            # 3.1. 显式关闭嵌套生成器；取消或停止读取后不补跑 ON_INVOKE。
            async with aclosing(self._commit_user_input(
                state, message, images, origin_channel=origin_channel,
                inbox_input=inbox_input, inbox_inputs=inbox_inputs,
                activation_data=activation_data,
            )) as committed_events:
                async for event in committed_events:
                    await emit(event)
        except InputNoLongerPending:
            # 3.2. 保存事务已回滚；丢弃本次内存状态，不执行正文和工具的打断保存。
            await emit(AgentEvent(EventType.DONE, {"input_cancelled": True}))
            return AgentRunOutcome(AgentRunOutcomeKind.STOPPED)

        # 4. 执行管道；最终提交与 DONE 只由 Agent 负责。
        # 显式等待下层迭代关闭，保证响应和借用资源在返回前完成清理。
        return await self._stream_and_finalize(
            state,
            self.pipeline.execute(
                state,
                session_state_manager=self.session_state_manager,
                runtime_context=runtime_context, receive_task_notifications=receive_task_notifications,
                activation_data=activation_data,
            ),
            failure_message="本轮执行出错，正在保存失败结果和已完成工具进度",
            origin_channel=origin_channel,
            turn_control=turn_control,
            release_queued=inbox_input is not None,
            emit=emit,
        )

    async def _commit_runtime_activation_inputs(
        self, state: AgentState, messages: tuple[HumanMessage | RuntimeInputMessage, ...] | None,
        inputs: tuple[InboxEnvelope, ...],
        *, activation_data: dict,
    ) -> None:
        """在父指令或内部通知启动新轮时，追加消息、执行提交前钩子并保存。

        仅由 run_runtime_inputs 在启动模型循环前调用。当前轮次中途收到
        通知由 SessionStateManager 直接交付，不再次进入本生命周期。

        Args:
            state (AgentState): 本次新激活加载的会话状态。
            messages (tuple[HumanMessage | RuntimeInputMessage, ...]): 本次激活需要交付的用户输入或内部通知。
            inputs (tuple[InboxEnvelope, ...]): 与通知一同持久交付的输入快照。
            activation_data (dict): 输入前置钩子准备的数据，保存成功后供 ON_INVOKE 使用。
        """
        # 1. 新轮清空上一轮背景，将本次输入追加到内存，供插件读取当前消息。
        state.clear_temporary_context()
        self.session_state_manager.append_runtime_inputs(state, messages, inputs)
        # 2. 提交前钩子准备插件状态和派生任务材料，后续 ON_INVOKE 继续使用这些材料。
        await self.plugin_manager.run_hooks(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data=activation_data,
        )
        # 3. 消息与输入交付标记原子保存；失败时直接退出，不启动后台派生任务。
        await self.session_state_manager.save(state, checkpoint_type=CheckpointType.INTERMEDIATE, inbox_inputs=inputs)

    async def _run_runtime_inputs(
        self, messages: tuple[HumanMessage | RuntimeInputMessage, ...] | None = None, *,
        inbox_inputs: tuple[InboxEnvelope, ...],
        turn_control: TurnControl | None = None,
        receive_task_notifications: bool = False,
        emit: Callable[[AgentEvent], Awaitable[None]],
    ) -> AgentRunOutcome:
        """交付父命令或内部通知，父命令先结束上一轮工具调用再启动模型循环。

        Args:
            messages (tuple[HumanMessage | RuntimeInputMessage, ...]): 需要转换或交付的用户输入或内部通知。
            inbox_inputs (tuple[InboxEnvelope, ...]): 随本次 checkpoint 原子交付的输入快照集合。
            turn_control (TurnControl | None): 当前 run 的停止与引导控制器。
            receive_task_notifications (bool): 是否在模型安全点交付任务通知。

            emit (Callable[[AgentEvent], Awaitable[None]]): 接收本轮已确定执行事件的异步输出入口。

        Returns:
            AgentRunOutcome: 本次执行的明确结果，过程事件由 emit 交付。
        """
        await self.initialize()
        state = await self.session_state_manager.load()
        # 1. 用户输入开始新执行，为旧工具补上取消或结束结果；父命令来源由 Inbox 保留。
        has_user_input = (
            any(isinstance(message, HumanMessage) for message in messages)
            if messages is not None else any(item.kind == InboxKind.AGENT_COMMAND for item in inbox_inputs)
        )
        if has_user_input:
            await self.session_state_manager.close_previous_tools(state)
        elif state.is_interrupted():
            # 1.1. 自动通知继续等待原审批恢复，不主动结束正在等待决定的工具调用。
            raise RuntimeError("待处理工具中断期间不能交付后台通知")
        await emit(AgentEvent(EventType.RUN_STARTED, {"origin_channel": DESKTOP_CHANNEL, "resumed": False}))
        # 2. 新激活先准备并保存输入，再以 desktop 来源进入 ON_INVOKE 和模型循环。
        # 2.1. 通知可以启动上一批已到期的记忆提取，但自身不增加真人计数。
        activation_data: dict = {}
        try:
            await self._commit_runtime_activation_inputs(
                state, messages, inbox_inputs, activation_data=activation_data,
            )
        except InputNoLongerPending:
            # 2.2. 整批输入未交付时直接结束，不把回滚后的内存状态传给模型。
            await emit(AgentEvent(EventType.DONE, {"input_cancelled": True}))
            return AgentRunOutcome(AgentRunOutcomeKind.STOPPED)
        runtime_context = RuntimeContext.capture()
        runtime_context.turn_control = turn_control
        runtime_context.origin_channel = DESKTOP_CHANNEL
        # 显式等待下层迭代关闭，保证响应和借用资源在返回前完成清理。
        return await self._stream_and_finalize(
            state,
            self.pipeline.execute(
                state, session_state_manager=self.session_state_manager,
                runtime_context=runtime_context, receive_task_notifications=receive_task_notifications,
                activation_data=activation_data,
            ),
            failure_message="后台通知执行出错",
            origin_channel=DESKTOP_CHANNEL,
            turn_control=turn_control,
            emit=emit,
        )

    async def _commit_user_input(
        self,
        state: AgentState,
        message: str,
        images: list[str] | None,
        *,
        activation_data: dict,
        origin_channel: str = DESKTOP_CHANNEL,
        inbox_input: InboxEnvelope | None = None,
        inbox_inputs: tuple[InboxEnvelope, ...] | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """准备并提交一次真人输入激活，保存同批通知与真人消息。

        由 run 在模型循环开始前调用。先追加消息，再让提交前钩子准备插件状态；
        消息保存成功后先发布真人输入事实，调用方随后进入 ON_INVOKE 启动派生任务。

        Args:
            state (AgentState): 当前 Agent 状态。
            message (str): 真人文本或类型声明对应的内部通知。
            images (list[str] | None): 真人附件的图片 data URL 列表。
            activation_data (dict): 输入前置钩子准备的数据，保存成功后供 ON_INVOKE 使用。
            origin_channel (str): 输入或本轮回复所属的渠道。
            inbox_input (InboxEnvelope | None): 随本次 checkpoint 原子交付的单条输入快照。
            inbox_inputs (tuple[InboxEnvelope, ...] | None): 随本次 checkpoint 原子交付的输入快照集合。

        Yields:
            AgentEvent: 事务成功后的真人输入提交事实。
        """
        # 1. 新轮清空上一轮背景，将同批通知和真人消息追加到内存状态。
        state.clear_temporary_context()
        inputs = inbox_inputs if inbox_inputs is not None else ((inbox_input,) if inbox_input is not None else ())
        human = self.session_state_manager.append_human_input(
            state, message, images, origin_channel=origin_channel, inputs=inputs,
        )
        # 2. 提交前钩子读取本次消息，插件状态随本次输入一起持久保存。
        await self.plugin_manager.run_hooks(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT,
            state,
            data=activation_data,
        )

        # 3. 消息、插件状态与交付标记一起保存，提交后取得已外置图片的真人消息。
        await self.session_state_manager.save(state, checkpoint_type=CheckpointType.INTERMEDIATE, inbox_inputs=inputs)
        human_message = next(item for item in state.messages
                             if isinstance(item, HumanMessage) and item.message_id == human.message_id)

        # 4. 输入已提交后立即通知观察方，不能等到模型首片才显示真人消息。
        human_input = next((item for item in inputs if item.kind == InboxKind.HUMAN), None)
        yield AgentEvent(EventType.HUMAN_INPUT_COMMITTED, {
            "message": human_message,
            "input_sequence": human_input.id if human_input is not None else None,
        })

    async def _resume(
        self, request_id: str, *, resume_data: dict | None = None,
        turn_control: TurnControl | None = None,
        receive_task_notifications: bool = False,
        emit: Callable[[AgentEvent], Awaitable[None]],
    ) -> AgentRunOutcome:
        """由 Agent 加载持久中断，核验请求后继续原轮次，不重新提交激活输入。

        Args:
            request_id (str): 要恢复的原中断编号。
            resume_data (dict | None): 桌面续执行的实际回报；审批决定从 checkpoint 读取。
            turn_control (TurnControl | None): 原轮次的停止控制。
            receive_task_notifications (bool): 是否在模型安全点交付任务通知。

            emit (Callable[[AgentEvent], Awaitable[None]]): 接收本轮已确定执行事件的异步输出入口。

        Returns:
            AgentRunOutcome: 本次执行的明确结果，过程事件由 emit 交付。
        """
        await self.initialize()
        state = await self.session_state_manager.load_for_resume(request_id)
        if state is None:
            return AgentRunOutcome(AgentRunOutcomeKind.STOPPED)
        interrupt = state.interrupt_data or {}
        origin = str(interrupt.get("origin_channel") or next(
            (getattr(item, "origin_channel") for item in reversed(state.messages) if hasattr(item, "origin_channel")),
            DESKTOP_CHANNEL,
        ))
        runtime_context = RuntimeContext.capture()
        runtime_context.turn_control = turn_control
        runtime_context.origin_channel = origin
        # 1. 只有同一份持久状态核验成功才报告恢复开始，随后才可能执行工具。
        await emit(AgentEvent(EventType.RUN_STARTED, {"origin_channel": origin, "resumed": True}))
        decision = interrupt.get("runtime_decision") if interrupt.get("type") == "authorization_request" else resume_data
        # 2. Agent 保持最终提交与 DONE 所有权，Pipeline 只借用状态业务对象。
        # 显式等待下层迭代关闭，保证响应和借用资源在返回前完成清理。
        return await self._stream_and_finalize(
            state,
            self.pipeline.resume_tools(state, decision or {},
                session_state_manager=self.session_state_manager,
                runtime_context=runtime_context, receive_task_notifications=receive_task_notifications),
            failure_message="恢复执行出错，正在保存失败结果和已完成工具进度",
            origin_channel=origin, turn_control=turn_control, release_queued=True,
            emit=emit,
        )


    async def _stream_and_finalize(
        self, state: AgentState, event_stream: AsyncGenerator[AgentEvent, None], *,
        failure_message: str, origin_channel: str = DESKTOP_CHANNEL,
        turn_control: TurnControl | None = None, release_queued: bool = False,
        emit: Callable[[AgentEvent], Awaitable[None]],
    ) -> AgentRunOutcome:
        """发布模型工具进展，并将已保存的完成、等待或停止结果交还执行对象。

        Args:
            state (AgentState): 本次执行的消息和工具协议。
            event_stream (AsyncIterator[AgentEvent]): Pipeline 的模型工具进展。
            failure_message (str): 业务失败日志说明。
            origin_channel (str): 本次回答所属渠道。
            turn_control (TurnControl | None): 所属执行提供的安全点能力。
            release_queued (bool): 正常结束时是否放行排队真人输入。
            emit (Callable): 执行所有者提供的事件投递能力。

        Returns:
            AgentRunOutcome: 业务结果或保持原轮次的已保存等待。
        """
        # 1. 模型工具循环只运行一次，必需保存失败直接交还入口。
        visible_partial = ""
        try:
            async with aclosing(event_stream) as stream:
                async for event in stream:
                    if event.type == EventType.ERROR:
                        logger.warning("%s: %s", failure_message, self.config.session_id)
                        if turn_control is not None:
                            turn_control.check()
                        return await self._finalize_failed_response(state, str(event.data), turn_control,
                            origin_channel, partial=visible_partial)
                    if event.type == EventType.TEXT_CHUNK:
                        visible_partial += str(event.data)
                    elif event.type == EventType.TOOL_CALL:
                        visible_partial = ""
                    await emit(event)
                    if event.type == EventType.INTERRUPT:
                        return AgentRunOutcome(AgentRunOutcomeKind.INTERRUPTED,
                                               wait=turn_control.wait if turn_control else None)
            if turn_control is not None:
                turn_control.check()
            outcome = await self._finalize_response(state, release_queued=release_queued, turn_control=turn_control)
        except (TurnInterrupted, asyncio.CancelledError):
            # 2. 已接受的控制结束旧协议，提交确认仍由 DAO 保护。
            if turn_control is None or turn_control.reason is None:
                raise
            outcome = await self._finalize_interrupted_response(state, turn_control, origin_channel)
            if turn_control.partial.strip():
                await emit(AgentEvent(EventType.TEXT_CHUNK, "……（被用户打断）"))
        # 3. 完成事件只在最终保存成功后发送，结果不依赖观察方读取事件。
        await emit(AgentEvent(EventType.DONE, None))
        return outcome

    async def _finalize_interrupted_response(
        self, state: AgentState, turn_control: TurnControl, origin_channel: str,
    ) -> AgentRunOutcome:
        """保存已展示正文和工具结束协议，再返回停止或引导的真实结果。

        Args:
            state (AgentState): 被打断的执行材料。
            turn_control (TurnControl): 原执行的停止原因和已展示正文。
            origin_channel (str): 被打断回答所属渠道。

        Returns:
            AgentRunOutcome: 已保存的停止或引导结果。
        """
        reason = turn_control.reason
        assert reason is not None
        turn_control.begin_interrupted_commit()
        await self.session_state_manager.finish_interrupted_turn(
            state, reason=reason, partial=turn_control.partial, partial_persisted=turn_control.partial_persisted,
            origin_channel=origin_channel)
        return AgentRunOutcome(
            AgentRunOutcomeKind.STEERED if reason == TurnInterruptReason.STEER else AgentRunOutcomeKind.STOPPED,
            result=turn_control.partial)

    async def _finalize_response(
        self, state: AgentState, *, release_queued: bool = False,
        turn_control: TurnControl | None = None,
    ) -> AgentRunOutcome:
        """由 Agent 唯一提交最终回答，钩子在 DAO 的保存重试之外执行一次。

        Args:
            state (AgentState): 模型工具循环产生的最终消息。
            release_queued (bool): 正常真人轮次完成时放行后续排队输入。
            turn_control (TurnControl | None): 最终保存前的执行安全点。

        Returns:
            AgentRunOutcome: 已确认保存的最终正文及消息编号。
        """
        commit_context: dict = {}
        await self.plugin_manager.run_hooks(PluginHook.BEFORE_RESPONSE_COMMIT, state, data=commit_context)
        if turn_control is not None:
            turn_control.begin_commit()
        await self.session_state_manager.complete_turn(state, release_queued=release_queued)
        final = next((item for item in reversed(state.messages) if isinstance(item, AssistantMessage)), None)
        outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED, result=final.text if final else "",
                                  final_message_id=final.message_id if final else None)
        await self.plugin_manager.run_hooks(PluginHook.AFTER_RESPONSE_COMMIT, state, data=commit_context)
        return outcome

    async def _finalize_failed_response(self, state: AgentState, error: str, turn_control: TurnControl | None,
                                        origin_channel: str, *, partial: str = "") -> AgentRunOutcome:
        """保存模型或执行失败的最终结果，已经完成的工具效果和历史保持可追踪。

        Args:
            state (AgentState): 失败前可确认的执行状态。
            error (str): 最终业务错误说明。
            turn_control (TurnControl | None): 当前执行的提交保护及可见片段。
            origin_channel (str): 本轮结果所属渠道。
            partial (str): 独立调用本步骤已显示的正文。

        Returns:
            AgentRunOutcome: 失败收尾已经保存的明确结果。
        """
        if turn_control is not None:
            turn_control.begin_failed_commit()
            already_saved = any(message.message_id == turn_control.partial_message_id for message in state.messages)
            partial = "" if already_saved else turn_control.partial
        await self.session_state_manager.finish_failed_turn(state, error=error, partial=partial, origin_channel=origin_channel)
        return AgentRunOutcome(AgentRunOutcomeKind.FAILED, error=error)

    async def _perform(self, operation: Awaitable[AgentRunOutcome], *, turn_control: TurnControl | None,
                       origin_channel: str = DESKTOP_CHANNEL) -> AgentRunOutcome:
        """接收一次执行的最终结果，保存故障与业务错误分别交还调用者。

        Args:
            operation (Awaitable[AgentRunOutcome]): 已准备好、只运行一次的执行入口。
            turn_control (TurnControl | None): 当前执行的提交资格和已显示正文。
            origin_channel (str): 失败结果应归属的输入渠道。

        Returns:
            AgentRunOutcome: 明确的执行结果，原始异常保留在日志链中。
        """
        try:
            return await operation
        except (RequiredSaveError, sqlite3.Error, OSError, OperationTimeoutError, SqliteRollbackError) as exc:
            return AgentRunOutcome(AgentRunOutcomeKind.SAVE_FAILED, error=str(exc))
        except Exception as exc:
            logger.exception("Agent 执行失败：%s", self.config.session_id)
            try:
                state = await self.session_state_manager.load()
                return await self._finalize_failed_response(state, str(exc), turn_control, origin_channel)
            except Exception as save_error:
                return AgentRunOutcome(AgentRunOutcomeKind.SAVE_FAILED, error=str(save_error))

    async def run(self, message: str, images: list[str] | None = None, *,
                  origin_channel: str = DESKTOP_CHANNEL, inbox_input: InboxEnvelope | None = None,
                  inbox_inputs: tuple[InboxEnvelope, ...] | None = None, turn_control: TurnControl | None = None,
                  receive_task_notifications: bool = False, emit: Callable[[AgentEvent], Awaitable[None]]) -> AgentRunOutcome:
        """处理一次真人输入，返回执行结果并通过 emit 发布过程事件。

        Args:
            message (str): 真人正文。
            images (list[str] | None): 真人附件。
            origin_channel (str): 输入来源渠道。
            inbox_input (InboxEnvelope | None): 单条受理输入。
            inbox_inputs (tuple[InboxEnvelope, ...] | None): 本次原子交付的输入批次。
            turn_control (TurnControl | None): 执行安全点能力。
            receive_task_notifications (bool): 是否交付执行中通知。
            emit (Callable): 流式事件接收能力。

        Returns:
            AgentRunOutcome: 已保存的完成、停止、等待或明确失败。
        """
        return await self._perform(self._run(message, images, origin_channel=origin_channel, inbox_input=inbox_input,
            inbox_inputs=inbox_inputs, turn_control=turn_control, receive_task_notifications=receive_task_notifications, emit=emit), turn_control=turn_control, origin_channel=origin_channel)

    async def run_runtime_inputs(self, messages: tuple[HumanMessage | RuntimeInputMessage, ...] | None = None, *,
                                 inbox_inputs: tuple[InboxEnvelope, ...], turn_control: TurnControl | None = None,
                                 receive_task_notifications: bool = False,
                                 emit: Callable[[AgentEvent], Awaitable[None]]) -> AgentRunOutcome:
        """处理父消息或普通通知，输入保存和模型循环共用一次执行。

        Args:
            messages (tuple | None): 已准备的业务消息，省略时从 Inbox 转换。
            inbox_inputs (tuple[InboxEnvelope, ...]): 本次待交付输入。
            turn_control (TurnControl | None): 执行安全点能力。
            receive_task_notifications (bool): 是否交付执行中通知。
            emit (Callable): 流式事件接收能力。

        Returns:
            AgentRunOutcome: 已保存结果或明确故障。
        """
        return await self._perform(self._run_runtime_inputs(messages, inbox_inputs=inbox_inputs, turn_control=turn_control,
            receive_task_notifications=receive_task_notifications, emit=emit), turn_control=turn_control)

    async def resume(self, request_id: str, *, resume_data: dict | None = None, turn_control: TurnControl | None = None,
                     receive_task_notifications: bool = False, emit: Callable[[AgentEvent], Awaitable[None]]) -> AgentRunOutcome:
        """恢复已确认决定的原审批，保持输入和 run 身份。

        Args:
            request_id (str): 已保存的等待编号。
            resume_data (dict | None): 工具继续执行材料。
            turn_control (TurnControl | None): 原执行控制。
            receive_task_notifications (bool): 是否交付执行中通知。
            emit (Callable): 流式事件接收能力。

        Returns:
            AgentRunOutcome: 本段执行的明确结果。
        """
        return await self._perform(self._resume(request_id, resume_data=resume_data, turn_control=turn_control,
            receive_task_notifications=receive_task_notifications, emit=emit), turn_control=turn_control)

    # ==================== 运行时操作 ====================


    def register_tool(self, tool: BaseTool) -> None:
        """将工具加入当前 Agent 的内存注册表，后续模型请求可按权限公开它。

        Args:
            tool (BaseTool): 需要注册或执行的工具对象。
        """
        self.tool_manager.register(tool)

    def unregister_tool(self, name: str) -> bool:
        """从当前 Agent 的工具索引移除指定名称，后续模型请求不再公开该项。

        Args:
            name (str): 当前对象的名称。
        """
        return self.tool_manager.unregister(name)

    def register_plugin(self, plugin: BasePlugin) -> None:
        """登记插件实例及其执行时机，让后续输入、模型或工具步骤调用对应钩子。

        Args:
            plugin (BasePlugin): 需要注册或调用的插件对象。
        """
        self.plugin_manager.register(plugin)

    def unregister_plugin(self, name: str) -> bool:
        """移除插件及其钩子登记，避免后续步骤继续调用已卸载插件。

        Args:
            name (str): 当前对象的名称。
        """
        return self.plugin_manager.unregister(name)

    def get_tools(self) -> list[str]:
        """读取当前 Agent 已注册的工具名称，实际向模型公开时仍需检查权限。"""
        return self.tool_manager.list_tools()

    def get_plugins(self) -> list[str]:
        """读取当前 Agent 已注册的插件名称。"""
        return self.plugin_manager.list_plugins()

    # ==================== 状态操作 ====================

    async def get_state(self) -> AgentState:
        """从会话共享数据库读取最新 checkpoint，尚无记录时返回空状态。"""
        await self.initialize()
        return await self.session_state_manager.load()

    async def clear_state(self) -> int:
        """清除当前会话 checkpoint，历史协议日志由存储实现继续保留。"""
        return await self.session_state_manager.clear_session()

    # ==================== 生命周期 ====================

    async def close(self) -> None:
        """在共同预算内释放自有客户端和独立调用所拥有的 Manager，最终失败记日志。"""
        self.plugin_manager.clear()
        assert self.llm_client is not None
        resources: list[tuple[str, Callable[[], Awaitable[None]]]] = [("模型客户端", self.llm_client.close)]
        if self._owns_state_manager:
            resources.append(("独立 Agent 状态连接", self.session_state_manager.close))
        await close_resources(tuple(resources), logger=logger, retryable=(Exception,))

    async def __aenter__(self) -> "Agent":
        """调用方进入异步上下文时初始化工具和插件。

        Returns:
            Agent: 已初始化的当前 Agent。
        """
        await self.initialize()
        return self

    async def __aexit__(self, *args) -> None:
        """调用方退出异步上下文时关闭 Agent 资源，不吞掉执行异常。

        Args:
            args (Any): 当前工具调用或被包装函数的参数。
        """
        await self.close()
