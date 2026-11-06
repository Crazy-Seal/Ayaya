"""
Agent 核心类

门面类，协调各管理器，提供统一的接口。
"""

import asyncio
from uuid import uuid4
import logging
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, TYPE_CHECKING, Awaitable, Callable

from app.agent.models.contracts import OPENAI_CHAT_COMPLETIONS
from app.schemas.runtime_types import (
    DESKTOP_CHANNEL, InboxKind,
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
from app.agent.plugins.base import BasePlugin, PluginHook
from app.agent.state import AgentState
from app.agent.state.manager import SessionStateManager
from app.agent.runtime.session_inbox import InboxEnvelope, InputNoLongerPending
from app.agent.runtime.turn_control import TurnControl, TurnInterrupted
from app.agent.utils.infra.owned_work import complete_owned_work
from app.schemas.desktop import DesktopOperations
from app.agent.state.messages import RuntimeInputMessage, AssistantMessage, HumanMessage
from app.schemas.tool_identity import ToolIdentity
from app.schemas.runtime_types import AgentRunOutcome, AgentRunOutcomeKind, TurnInterruptReason
from app.agent.tools.core.runtime import ToolRuntimeFactory
from app.agent.tools.core.base import BaseTool
from app.agent.tools.authorization.manager import ToolAuthorization
from app.schemas.mcp import MCPModelSettings, MCPConnectionConfig
from app.mcp.types import PluginMCPBinder

if TYPE_CHECKING:
    from app.schemas.chat_settings import ChatSettings
    from app.mcp.mcp_connection_manager import MCPConnectionManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PendingResponse:
    """保留尚未保存成功的回答，由原 Agent 在收尾重试时使用。

    Attributes:
        state (AgentState): 尚待保存的原执行状态，中断后缀在各次保存时独立准备。
        partial_persisted (bool): 正文是否已经加入原状态中的助手消息。
        origin_channel (str): 回答所属的界面或渠道。
        outcome (AgentRunOutcome): 保存成功后交还 Scheduler 的正文及停止原因。
        release_queued (bool): 正常完成时是否原子放行排队输入。
        commit_context (dict): 正常响应提交钩子共用的本次材料。
        turn_control (TurnControl | None): 保存成功后确认原轮进入收尾的控制对象。
    """
    state: AgentState
    partial_persisted: bool
    origin_channel: str
    outcome: AgentRunOutcome
    release_queued: bool = False
    commit_context: dict = field(default_factory=dict)
    turn_control: TurnControl | None = None


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
        _pending_response (_PendingResponse | None): 保存失败时保留正文和原状态，供收尾重试。
    """

    def __init__(
        self,
        config: AgentConfig,
        db_path: str | None = None,
        mcp_connection_manager: "MCPConnectionManager | None" = None,
        *,
        llm_client: LLMClient | None = None,
        chat_settings: "ChatSettings | None" = None,
        tool_authorization: ToolAuthorization | None = None,
        tool_runtime_factory: ToolRuntimeFactory | None = None,
        desktop: DesktopOperations | None = None,
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
            db_path (str | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
            mcp_connection_manager (MCPConnectionManager | None): 共享 MCP 连接管理器。
            llm_client (LLMClient | None): 可替换的流式模型客户端。
            chat_settings (ChatSettings | None): 会话模型和插件配置。
            tool_authorization (ToolAuthorization | None): 工具单次授权验证器。
            tool_runtime_factory (ToolRuntimeFactory | None): 按工具、轮次及调用编号创建运行时。
            desktop (DesktopOperations | None): 应用所有的共享桌面接口，Agent 仅传给 Pipeline。
            session_state_manager (SessionStateManager | None): worker 借给本次执行的对象；省略时由 Agent 创建并关闭。
            plugin_runtime_factory (PluginRuntimeFactory | None): 按插件名称和来源轮次创建 PluginRuntime 的注入工厂。
            bind_plugin_mcp (PluginMCPBinder | None): 连接成功后由应用层保存身份与工具默认权限。
            permission_session_id (str | None): 子执行使用父会话的实时授权和共享连接身份。
            tool_whitelist (frozenset[ToolIdentity] | None): 子普通工具可见范围；空集合表示无普通工具。
            enable_plugin_hooks (bool): 是否执行已登记插件的自动业务钩子。
            environment_provider (Callable | None): 每次模型请求前异步读取当前子名单的入口。
        """
        self.config = config
        self.mcp_connection_manager = mcp_connection_manager
        self._bind_plugin_mcp = bind_plugin_mcp

        # 核心管理器
        self.tool_authorization = tool_authorization
        if self.tool_authorization is None and chat_settings is not None:
            self.tool_authorization = ToolAuthorization(lambda _session_id: chat_settings)
        self.tool_manager = ToolManager(
            session_id=permission_session_id or config.session_id,
            tool_whitelist=tool_whitelist,
            authorization=self.tool_authorization,
        )
        self._permission_session_id = permission_session_id or config.session_id
        self.last_outcome: AgentRunOutcome | None = None
        self._pending_response: _PendingResponse | None = None
        self.plugin_manager = PluginManager(plugin_runtime_factory, enable_plugin_hooks=enable_plugin_hooks)
        self._owns_state_manager = session_state_manager is None
        self.session_state_manager = session_state_manager or SessionStateManager(config.session_id, db_path)
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
            desktop=desktop,
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
        """按工具权限和会话配置加载普通工具，委派管理工具由应用工厂装配依赖。"""
        # 从工具注册表加载
        from app.agent.tools.core.registry import ToolRegistry
        from app.schemas.subagent_types import SUBAGENT_PARENT_TOOL_NAMES

        tool_names = (
            ToolRegistry.list_tools()
            if self.tool_authorization is not None
            else list(self.config.tools or [])
        )
        for tool_name in tool_names:
            if tool_name in SUBAGENT_PARENT_TOOL_NAMES or self.tool_manager.has(tool_name):
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

            plugin_class = PluginRegistry.get(plugin_name)
            if plugin_class:
                try:
                    plugin = plugin_class(**plugin_params)
                    self.plugin_manager.register(plugin)
                    from app.agent.plugins.tool_adapter import PluginToolAdapter
                    for tool_class in plugin_class.tools:
                        adapter = PluginToolAdapter(plugin_name, tool_class())
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
                server_config = self.mcp_connection_manager.get_server_config(server_id)
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

    async def run(
        self,
        message: str,
        images: list[str] | None = None,
        *,
        origin_channel: str = DESKTOP_CHANNEL,
        inbox_input: InboxEnvelope | None = None,
        inbox_inputs: tuple[InboxEnvelope, ...] | None = None,
        turn_control: TurnControl | None = None,
        receive_task_notifications: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """交付真人输入并运行一轮对话，先闭合遗留工具协议，再由 Agent 提交最终响应。

        Args:
            message (str): 真人文本或类型声明对应的内部通知。
            images (list[str] | None): 真人附件的图片 data URL 列表。
            origin_channel (str): 输入或本轮回复所属的渠道。
            inbox_input (InboxEnvelope | None): 随本次 checkpoint 原子交付的单条输入快照。
            inbox_inputs (tuple[InboxEnvelope, ...] | None): 随本次 checkpoint 原子交付的输入快照集合。
            turn_control (TurnControl | None): 当前 run 的停止与引导控制器。
            receive_task_notifications (bool): 是否在模型安全点交付任务通知。

        Yields:
            AgentEvent: 按执行顺序产生的 Agent 事件。

        """
        self.last_outcome = None
        await self.initialize()
        self.plugin_manager.run_id = turn_control.run_id if turn_control else uuid4().hex
        runtime_context = RuntimeContext.capture()
        runtime_context.origin_channel = origin_channel
        runtime_context.turn_control = turn_control

        # 1. Agent 自行加载状态，再报告执行开始；Scheduler 不预加载执行材料。
        state = await self.session_state_manager.load()
        yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": origin_channel, "resumed": False})
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
                    yield event
        except InputNoLongerPending:
            # 3.2. 保存事务已回滚；丢弃本次内存状态，不执行正文和工具的打断保存。
            yield AgentEvent(EventType.DONE, {"input_cancelled": True})
            return

        # 4. 执行管道；最终提交与 DONE 只由 Agent 负责。
        # 显式等待下层迭代关闭，保证响应和借用资源在返回前完成清理。
        async with aclosing(self._stream_and_finalize(
            state,
            self.pipeline.execute(
                state,
                session_state_manager=self.session_state_manager,
                runtime_context=runtime_context, receive_task_notifications=receive_task_notifications,
                activation_data=activation_data,
            ),
            failure_message="本轮执行出错，已保留最近工具进度，未写入最终 checkpoint",
            origin_channel=origin_channel,
            turn_control=turn_control,
            release_queued=inbox_input is not None,
        )) as managed_events:
            async for event in managed_events:
                yield event

    async def _commit_runtime_activation_inputs(
        self, state: AgentState, messages: tuple[HumanMessage | RuntimeInputMessage, ...] | None,
        inputs: tuple[InboxEnvelope, ...],
        *, activation_data: dict,
    ) -> None:
        """在父指令或内部通知启动新轮时，协调输入提交和上一批记忆任务。

        仅由 run_runtime_inputs 在启动模型循环前调用。当前轮次中途收到
        通知由 SessionStateManager 直接交付，不再次进入本生命周期。

        Args:
            state (AgentState): 本次新激活加载的会话状态。
            messages (tuple[HumanMessage | RuntimeInputMessage, ...]): 本次激活需要交付的用户输入或内部通知。
            inputs (tuple[InboxEnvelope, ...]): 与通知一同持久交付的输入快照。
            activation_data (dict): 输入前置钩子准备的数据，保存成功后供 ON_INVOKE 使用。
        """
        # 1. 新轮先清空上一轮背景，再让插件准备本次激活的数据。
        state.clear_temporary_context()
        # 2. 本批输入加入前准备上一批派生任务，将准备数据交给后续 ON_INVOKE。
        await self.plugin_manager.run_hooks(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data=activation_data,
        )
        # 3. 消息与输入交付标记原子保存；失败时直接退出，不启动后台派生任务。
        await self.session_state_manager.commit_runtime_inputs(state, messages, inputs)

    async def run_runtime_inputs(
        self, messages: tuple[HumanMessage | RuntimeInputMessage, ...] | None = None, *,
        inbox_inputs: tuple[InboxEnvelope, ...],
        turn_control: TurnControl | None = None,
        receive_task_notifications: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """交付父命令或内部通知，父命令先结束上一轮工具调用再启动模型循环。

        Args:
            messages (tuple[HumanMessage | RuntimeInputMessage, ...]): 需要转换或交付的用户输入或内部通知。
            inbox_inputs (tuple[InboxEnvelope, ...]): 随本次 checkpoint 原子交付的输入快照集合。
            turn_control (TurnControl | None): 当前 run 的停止与引导控制器。
            receive_task_notifications (bool): 是否在模型安全点交付任务通知。

        Yields:
            AgentEvent: 按执行顺序产生的 Agent 事件。

        Raises:
            RuntimeError: 只有内部通知时，待处理工具中断期间不能交付。
        """
        self.last_outcome = None
        await self.initialize()
        self.plugin_manager.run_id = turn_control.run_id if turn_control else uuid4().hex
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
        yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": DESKTOP_CHANNEL, "resumed": False})
        # 2. 新激活先准备并保存输入，再以 desktop 来源进入 ON_INVOKE 和模型循环。
        # 2.1. 通知可以启动上一批已到期的记忆提取，但自身不增加真人计数。
        activation_data: dict = {}
        try:
            await self._commit_runtime_activation_inputs(
                state, messages, inbox_inputs, activation_data=activation_data,
            )
        except InputNoLongerPending:
            # 2.2. 整批输入未交付时直接结束，不把回滚后的内存状态传给模型。
            yield AgentEvent(EventType.DONE, {"input_cancelled": True})
            return
        runtime_context = RuntimeContext.capture()
        runtime_context.turn_control = turn_control
        runtime_context.origin_channel = DESKTOP_CHANNEL
        # 显式等待下层迭代关闭，保证响应和借用资源在返回前完成清理。
        async with aclosing(self._stream_and_finalize(
            state,
            self.pipeline.execute(
                state, session_state_manager=self.session_state_manager,
                runtime_context=runtime_context, receive_task_notifications=receive_task_notifications,
                activation_data=activation_data,
            ),
            failure_message="后台通知执行出错",
            origin_channel=DESKTOP_CHANNEL,
            turn_control=turn_control,
        )) as managed_events:
            async for event in managed_events:
                yield event

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
    ) -> AsyncIterator[AgentEvent]:
        """准备并提交一次真人输入激活，保存同批通知与真人消息。

        由 run 在模型循环开始前调用。前置钩子处理本次输入之前的记忆批次；
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
        # 1. 新轮先清空上一轮背景，再让插件准备本次激活的数据。
        state.clear_temporary_context()
        # 2. 本次激活输入尚未追加，先准备上一批派生任务，不包含本次真人或通知。
        await self.plugin_manager.run_hooks(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT,
            state,
            data=activation_data,
        )

        # 3. Manager 统一准备同批任务通知、真人图片与计数，保存成功才返回真人事实。
        inputs = inbox_inputs if inbox_inputs is not None else ((inbox_input,) if inbox_input is not None else ())
        human_message = await self.session_state_manager.commit_human_input(
            state, message, images, origin_channel=origin_channel, inputs=inputs,
        )

        # 4. 输入已提交后立即通知观察方，不能等到模型首片才显示真人消息。
        human_input = next((item for item in inputs if item.kind == InboxKind.HUMAN), None)
        yield AgentEvent(EventType.HUMAN_INPUT_COMMITTED, {
            "message": human_message,
            "input_sequence": human_input.id if human_input is not None else None,
        })

    async def resume(
        self, request_id: str, *, resume_data: dict | None = None,
        turn_control: TurnControl | None = None,
        receive_task_notifications: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """由 Agent 加载持久中断，核验请求后继续原轮次，不重新提交激活输入。

        Args:
            request_id (str): 要恢复的原中断编号。
            resume_data (dict | None): 桌面续执行的实际回报；审批决定从 checkpoint 读取。
            turn_control (TurnControl | None): 原轮次的停止控制。
            receive_task_notifications (bool): 是否在模型安全点交付任务通知。

        Yields:
            AgentEvent: 核验成功后的开始事件及恢复过程；过期请求不产生事件。
        """
        self.last_outcome = None
        await self.initialize()
        self.plugin_manager.run_id = turn_control.run_id if turn_control else uuid4().hex
        state = await self.session_state_manager.load_for_resume(request_id)
        if state is None:
            return
        interrupt = state.interrupt_data or {}
        origin = str(interrupt.get("origin_channel") or next(
            (item.origin_channel for item in reversed(state.messages) if hasattr(item, "origin_channel")),
            DESKTOP_CHANNEL,
        ))
        runtime_context = RuntimeContext.capture()
        runtime_context.turn_control = turn_control
        runtime_context.origin_channel = origin
        # 1. 只有同一份持久状态核验成功才报告恢复开始，随后才可能执行工具。
        yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": origin, "resumed": True})
        decision = interrupt.get("runtime_decision") if interrupt.get("type") == "authorization_request" else resume_data
        # 2. Agent 保持最终提交与 DONE 所有权，Pipeline 只借用状态业务对象。
        # 显式等待下层迭代关闭，保证响应和借用资源在返回前完成清理。
        async with aclosing(self._stream_and_finalize(
            state,
            self.pipeline.resume_tools(state, decision or {},
                session_state_manager=self.session_state_manager,
                runtime_context=runtime_context, receive_task_notifications=receive_task_notifications),
            failure_message="恢复执行出错，已保留最近工具进度，未写入最终 checkpoint",
            origin_channel=origin, turn_control=turn_control, release_queued=True,
        )) as managed_events:
            async for event in managed_events:
                yield event


    async def _stream_and_finalize(
        self,
        state: AgentState,
        event_stream: AsyncIterator[AgentEvent],
        *,
        failure_message: str,
        origin_channel: str = DESKTOP_CHANNEL,
        turn_control: TurnControl | None = None,
        release_queued: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """转发管道事件，在正常完成或用户打断时执行对应的持久化收尾。

        Args:
            state (AgentState): 当前 Agent 状态。
            event_stream (AsyncIterator[AgentEvent]): 待收尾的 Agent 内部事件迭代器。
            failure_message (str): 运行失败时使用的中文日志说明。
            origin_channel (str): 输入或本轮回复所属的渠道。
            turn_control (TurnControl | None): 当前 run 的停止与引导控制器。
            release_queued (bool): 是否随正常完成的 checkpoint 原子放行排队输入。

        Yields:
            AgentEvent: 按执行顺序产生的 Agent 事件。
        """
        self.last_outcome = None
        # 1. 管道错误保留最近进度，中断先保存来源后返回，二者都不伪造正常完成。
        try:
            # 显式等待下层迭代关闭，保证响应和借用资源在返回前完成清理。
            async with aclosing(event_stream) as managed_events:
                async for event in managed_events:
                    if event.type == EventType.ERROR:
                        # 1.1. 失败时清空内存背景；持久进度仍按原失败规则保留，新激活会重新重置。
                        state.clear_temporary_context()
                        self.last_outcome = AgentRunOutcome(AgentRunOutcomeKind.FAILED, error=str(event.data))
                        if turn_control is not None:
                            turn_control.run_state.begin_finishing(turn_control.run_id)
                        yield event
                        logger.warning("%s: %s", failure_message, self.config.session_id)
                        return
                    if event.type == EventType.INTERRUPT:
                        if turn_control and turn_control.reason:
                            turn_control.check()
                        self.last_outcome = AgentRunOutcome(AgentRunOutcomeKind.INTERRUPTED)
                        yield event
                        return
                    yield event
            if turn_control is not None:
                turn_control.check()
            await self._finalize_response(
                state, release_queued=release_queued, turn_control=turn_control,
            )
        # 2. 用户打断统一收尾并保存已显示文本，DONE 仍由 Agent 在保存之后产生。
        except TurnInterrupted:
            if turn_control is None:
                raise
            await complete_owned_work(self._finalize_interrupted_response(state, turn_control, origin_channel))
            if turn_control.partial.strip():
                yield AgentEvent(EventType.TEXT_CHUNK, "……（被用户打断）")
            yield AgentEvent(EventType.DONE, None)
            return
        except (asyncio.CancelledError, GeneratorExit):
            # 3. 销毁取消挂起工具，或输出消费者关闭迭代器时，仍保存已受理的中断。
            # 等待保存完成后继续传播取消；Scheduler 随后关闭资源并读取实际结果。
            if turn_control is not None and turn_control.reason is not None:
                await complete_owned_work(self._finalize_interrupted_response(state, turn_control, origin_channel))
            raise

        final = next((message for message in reversed(state.messages) if isinstance(message, AssistantMessage)), None)
        self.last_outcome = AgentRunOutcome(AgentRunOutcomeKind.COMPLETED,
                                           result=final.text if final else "",
                                           final_message_id=final.message_id if final else None)
        yield AgentEvent(EventType.DONE, None)


    async def _finalize_interrupted_response(
        self, state: AgentState, turn_control: TurnControl, origin_channel: str,
    ) -> None:
        """保存中断正文及工具结束记录，再交还已保存的运行结果。

        Args:
            state (AgentState): 当前执行持有的会话状态，包含尚未保存的正文及工具进度。
            turn_control (TurnControl): 已受理的停止或引导信号，以及本步骤已生成的正文。
            origin_channel (str): 被打断回答所属的界面或渠道。
        """
        # 1. 原执行停止后保留未修改的状态；数据库暂时失败时，清理 worker 可继续保存。
        self._pending_response = _PendingResponse(
            state.model_copy(deep=True), turn_control.partial_persisted, origin_channel,
            AgentRunOutcome(
                AgentRunOutcomeKind.STEERED if turn_control.reason == TurnInterruptReason.STEER else AgentRunOutcomeKind.STOPPED,
                result=turn_control.partial,
            ),
            turn_control=turn_control,
        )
        # 2. 首次保存仍更新原执行状态，保持正常 stop 的内存消息及工具记录一致。
        await self._save_response(self._pending_response, state)

    async def finish_pending_response(self) -> AgentRunOutcome | None:
        """提交待保存的回答，保存与结果登记完成后才交还执行所有者。

        父子执行都在释放 Agent 前调用。保存失败时继续持有原状态，调用方只需
        重试本方法；再次取消等待也先完成本次事务和结果登记，资源关闭另行执行。

        Returns:
            AgentRunOutcome | None: 本次成功保存的正常或中断结果；没有待保存回答时为空。

        Raises:
            Exception: 保存失败，调用方继续持有 Agent 及原回答并按收尾流程重试。
        """
        pending = self._pending_response
        if pending is None:
            return None
        # 1. 每次从原状态准备工具结束记录和正文后缀，失败重试不会重复修改正文。
        return await complete_owned_work(self._save_response(pending, pending.state.model_copy(deep=True)))

    async def _save_response(
        self, pending: _PendingResponse, state: AgentState,
    ) -> AgentRunOutcome:
        """保存本次准备的中断回答，事务成功后释放待保存材料并登记结果。

        Args:
            pending (_PendingResponse): 原执行的正文、停止原因和回答来源。
            state (AgentState): 首次保存使用原执行状态，失败重试使用原状态的副本。

        Returns:
            AgentRunOutcome: 已与结束检查点一同保存的实际中断结果。
        """
        # 1. 原状态及结果保留到事务成功；重试不重新请求模型或执行工具。
        if pending.outcome.kind == AgentRunOutcomeKind.COMPLETED:
            await self.session_state_manager.complete_turn(state, release_queued=pending.release_queued)
        else:
            await self.session_state_manager.finish_interrupted_turn(
                state, partial=pending.outcome.result, partial_persisted=pending.partial_persisted,
                origin_channel=pending.origin_channel)
        # 2. 保存成功到登记结果之间不等待，取消也保留真实提交结果。
        self.last_outcome = pending.outcome
        self._pending_response = None
        if pending.turn_control is not None:
            pending.turn_control.run_state.begin_finishing(pending.turn_control.run_id)
        if pending.outcome.kind == AgentRunOutcomeKind.COMPLETED:
            await self.plugin_manager.run_hooks(PluginHook.AFTER_RESPONSE_COMMIT, state, data=pending.commit_context)
        return pending.outcome


    async def _finalize_response(
        self, state: AgentState, *, release_queued: bool = False,
        turn_control: TurnControl | None = None,
    ) -> None:
        """在响应提交钩子之间写入 completed checkpoint，并按需原子放行排队输入。

        Args:
            state (AgentState): 当前 Agent 状态。
            release_queued (bool): 是否随正常完成的 checkpoint 原子放行排队输入。
            turn_control (TurnControl | None): 当前 run 的停止与引导控制器。
        """
        commit_context: dict = {}
        await self.plugin_manager.run_hooks(
            PluginHook.BEFORE_RESPONSE_COMMIT,
            state,
            data=commit_context,
        )
        # 1. 提交前最后检查打断信号，进入 committing 后拒绝停止。
        if turn_control is not None:
            turn_control.begin_commit()
        # 2. 正常正文和排队放行也保留为待保存材料，临时失败只重试本次提交。
        final = next((message for message in reversed(state.messages) if isinstance(message, AssistantMessage)), None)
        self._pending_response = _PendingResponse(state.model_copy(deep=True), False,
            final.origin_channel if final else DESKTOP_CHANNEL,
            AgentRunOutcome(AgentRunOutcomeKind.COMPLETED, result=final.text if final else "",
                            final_message_id=final.message_id if final else None),
            release_queued=release_queued, commit_context=commit_context, turn_control=turn_control)
        await complete_owned_work(self._save_response(self._pending_response, state))

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
        """清空插件索引，关闭模型客户端和本实例拥有的状态连接。

        Raises:
            Exception: 模型客户端或状态连接关闭失败。
        """
        self.plugin_manager.clear()
        # 1. 关闭模型客户端；失败时仍释放本实例拥有的状态连接。
        try:
            await self.llm_client.close()
        finally:
            if self._owns_state_manager:
                await self.session_state_manager.close()
        logger.info(f"Agent 已关闭: {self.config.session_id}")

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
