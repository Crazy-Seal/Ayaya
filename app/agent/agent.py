"""
Agent 核心类

门面类，协调各管理器，提供统一的接口。
"""

import asyncio
import logging
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, TYPE_CHECKING

from app.schemas.runtime_types import (
    DESKTOP_CHANNEL, InboxKind,
)
from app.agent.context import (
    ContextStrategyConfig,
    DiaryCoverage,
    ModelInputCoordinator,
    RuntimeContext,
    build_token_accounting_key,
)
from app.agent.core.tool_manager import ToolManager
from app.agent.core.plugin_manager import PluginManager
from app.agent.core.events import EventType, AgentEvent
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.models.llm_client import LLMClient, LLMConfig
from app.agent.plugins.base import BasePlugin, PluginHook
from app.agent.state import AgentState
from app.agent.state.manager import SessionStateManager
from app.agent.runtime.session_inbox import InboxEnvelope, InputNoLongerPending
from app.agent.runtime.turn_control import TurnControl, TurnInterrupted
from app.schemas.desktop import DesktopOperations
from app.agent.state.messages import RuntimeInputMessage
from app.agent.tools.core.base import BaseTool, TaskLauncher
from app.agent.tools.authorization.manager import ToolAuthorization
from app.schemas.mcp import MCPModelSettings

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
    api_format: str = "openai_chat_completions"
    temperature: float = 0.7
    system_prompt: str = ""
    tools: list[str] = field(default_factory=list)
    plugins: list[str | dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp: MCPModelSettings = field(default_factory=MCPModelSettings)


class Agent:
    """管理单次 Agent 实例的模型、工具与插件，并拥有输入和最终响应的提交权。

    Attributes:
        config (AgentConfig): 当前 Agent 配置。
        mcp_connection_manager ('MCPConnectionManager | None'): 共享 MCP 连接管理器。
        tool_authorization (ToolAuthorization | None): 工具单次授权验证器。
        tool_manager (ToolManager): 本地及 MCP 工具注册管理器。
        plugin_manager (PluginManager): 插件生命周期与执行钩子的分发器。
        llm_client (LLMClient | None): 可替换的流式模型客户端。
        pipeline (ExecutionPipeline): 模型工具执行管道。
        _initialized (bool): 工具和插件是否已经完成初始化。
        session_state_manager (SessionStateManager): 当前会话的输入和状态业务对象。
        _owns_state_manager (bool): 独立创建时负责关闭；worker 注入的对象只借用。
    """

    def __init__(
        self,
        config: AgentConfig,
        db_path: str | None = None,
        mcp_connection_manager: "MCPConnectionManager | None" = None,
        *,
        llm_client: LLMClient | None = None,
        diary_coverage: DiaryCoverage | None = None,
        chat_settings: "ChatSettings | None" = None,
        tool_authorization: ToolAuthorization | None = None,
        task_launcher: TaskLauncher | None = None,
        desktop: DesktopOperations | None = None,
        session_state_manager: SessionStateManager | None = None,
    ):
        """装配 Agent 执行依赖，将后台任务启动端口注入工具管道。

        Args:
            config (AgentConfig): 当前会话的 Agent 配置。
            db_path (str | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
            mcp_connection_manager (MCPConnectionManager | None): 共享 MCP 连接管理器。
            llm_client (LLMClient | None): 可替换的流式模型客户端。
            diary_coverage (DiaryCoverage | None): 查询已生成日记覆盖范围的端口。
            chat_settings (ChatSettings | None): 会话模型和插件配置。
            tool_authorization (ToolAuthorization | None): 工具单次授权验证器。
            task_launcher (TaskLauncher | None): 工具使用的共享进程启动端口。
            desktop (DesktopOperations | None): 应用所有的共享桌面接口，Agent 仅传给 Pipeline。
            session_state_manager (SessionStateManager | None): worker 借给本次执行的对象；省略时由 Agent 创建并关闭。
        """
        self.config = config
        self.mcp_connection_manager = mcp_connection_manager

        # 核心管理器
        self.tool_authorization = tool_authorization
        if self.tool_authorization is None and chat_settings is not None:
            self.tool_authorization = ToolAuthorization(lambda _session_id: chat_settings)
        self.tool_manager = ToolManager(
            session_id=config.session_id,
            authorization=self.tool_authorization,
        )
        self.plugin_manager = PluginManager()
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
            diary_coverage=diary_coverage,
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
            task_launcher=task_launcher,
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
        await self._setup_plugins()

        # 加载能力包（Skill）：可引入额外工具/插件/提示词片段
        await self._setup_skills()

        # 初始化 MCP
        await self._setup_mcp()

        self._initialized = True
        logger.info(f"Agent 初始化完成: {self.config.session_id}")

    async def _setup_tools(self) -> None:
        """按会话设置创建本地及 MCP 工具适配器，并装配技能提供的工具。"""
        # 从工具注册表加载
        from app.agent.tools.core.registry import ToolRegistry

        tool_names = (
            ToolRegistry.list_tools()
            if self.tool_authorization is not None
            else list(self.config.tools or [])
        )
        for tool_name in tool_names:
            if self.tool_manager.has(tool_name):
                continue
            tool_class = ToolRegistry.get(tool_name)
            if tool_class:
                tool = tool_class()
                self.tool_manager.register(tool)
                logger.info(f"加载工具: {tool_name}")
            else:
                logger.warning(f"工具 '{tool_name}' 不存在于注册表中")

    async def _setup_plugins(self) -> None:
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
                plugin = plugin_class(**plugin_params)
                await self.plugin_manager.register(plugin)
                logger.info(f"加载插件: {plugin_name}")
            else:
                logger.warning(f"插件 '{plugin_name}' 不存在于注册表中")

    async def _setup_skills(self) -> None:
        """加载能力包：注册其工具/插件并追加提示词片段。"""
        if not self.config.skills:
            return

        from app.agent.skills.registry import SkillRegistry
        from app.agent.tools.core.registry import ToolRegistry
        from app.agent.plugins.registry import PluginRegistry

        for skill_name in self.config.skills:
            skill_cls = SkillRegistry.get(skill_name)
            if not skill_cls:
                logger.warning("Skill '%s' 不存在于注册表中", skill_name)
                continue
            skill = skill_cls()

            # 引入工具
            for tool_name in skill.tools or []:
                if self.tool_manager.has(tool_name):
                    continue
                tool_cls = ToolRegistry.get(tool_name)
                if tool_cls:
                    self.tool_manager.register(tool_cls())
                else:
                    logger.warning("Skill '%s' 引用的工具 '%s' 不存在", skill_name, tool_name)

            # 引入插件
            for plugin_name in skill.plugins or []:
                if plugin_name in self.plugin_manager:
                    continue
                plugin_cls = PluginRegistry.get(plugin_name)
                if plugin_cls:
                    await self.plugin_manager.register(plugin_cls())
                else:
                    logger.warning("Skill '%s' 引用的插件 '%s' 不存在", skill_name, plugin_name)

            # 追加提示词片段
            if skill.system_prompt_fragment:
                self.config.system_prompt = (
                    f"{self.config.system_prompt}\n\n{skill.system_prompt_fragment}"
                    if self.config.system_prompt
                    else skill.system_prompt_fragment
                )

            await skill.on_load()
            logger.info("加载 Skill: %s", skill_name)

    async def _setup_mcp(self) -> None:
        """连接当前模型启用的 Server 并组装 MCP 模型工具空间。"""
        if not self.config.mcp.servers:
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
                    session_id=self.config.session_id,
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
        await self.initialize()
        runtime_context = RuntimeContext.capture()
        runtime_context.origin_channel = origin_channel
        runtime_context.turn_control = turn_control

        # 1. Agent 自行加载状态，再报告执行开始；Scheduler 不预加载执行材料。
        state = await self.session_state_manager.load()
        yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": origin_channel, "resumed": False})
        # 2. 新输入前结束旧协议，业务状态准备和保存统一交给 Manager。
        await self.session_state_manager.close_previous_tools(state)

        # 3. 原子提交本批内部通知与真人输入，再进入可停止的模型工具循环。
        try:
            # 3.1. 显式关闭嵌套生成器；取消或停止读取后不补跑激活后置钩子。
            async with aclosing(self._commit_user_input(
                state, message, images, origin_channel=origin_channel,
                inbox_input=inbox_input, inbox_inputs=inbox_inputs,
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
            ),
            failure_message="本轮执行出错，已保留最近工具进度，未写入最终 checkpoint",
            origin_channel=origin_channel,
            turn_control=turn_control,
            release_queued=inbox_input is not None,
        )) as managed_events:
            async for event in managed_events:
                yield event

    async def _commit_runtime_activation_inputs(
        self, state: AgentState, messages: tuple[RuntimeInputMessage, ...] | None,
        inputs: tuple[InboxEnvelope, ...],
    ) -> None:
        """在新一轮纯内部通知激活时，协调输入提交和上一批记忆任务。

        仅由 run_runtime_inputs 在启动模型循环前调用。当前轮次中途收到
        通知由 SessionStateManager 直接交付，不再次进入本生命周期。

        Args:
            state (AgentState): 本次新激活加载的会话状态。
            messages (tuple[RuntimeInputMessage, ...]): 本次激活需要交付的内部通知。
            inputs (tuple[InboxEnvelope, ...]): 与通知一同持久交付的输入快照。
        """
        # 1. 新轮先清空上一轮背景，再让插件准备本次激活的数据。
        state.clear_temporary_context()
        # 2. 本次通知加入前准备上一批派生任务，前后钩子共用同一提交上下文。
        commit_context: dict = {}
        await self.plugin_manager.run_hooks(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data=commit_context,
        )
        # 3. 消息与输入交付标记原子保存；失败时直接退出，不启动后台派生任务。
        await self.session_state_manager.commit_runtime_inputs(state, messages, inputs)
        # 4. 只有输入事务成功后才通知插件启动已经准备好的上一批任务。
        await self.plugin_manager.run_hooks(
            PluginHook.AFTER_ACTIVATION_INPUT_COMMIT, state, data=commit_context,
        )

    async def run_runtime_inputs(
        self, messages: tuple[RuntimeInputMessage, ...] | None = None, *,
        inbox_inputs: tuple[InboxEnvelope, ...],
        turn_control: TurnControl | None = None,
        receive_task_notifications: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """交付后台终态通知并自行启动模型循环，待处理工具中断期间禁止注入。

        Args:
            messages (tuple[RuntimeInputMessage, ...]): 需要转换或交付的领域消息集合。
            inbox_inputs (tuple[InboxEnvelope, ...]): 随本次 checkpoint 原子交付的输入快照集合。
            turn_control (TurnControl | None): 当前 run 的停止与引导控制器。
            receive_task_notifications (bool): 是否在模型安全点交付任务通知。

        Yields:
            AgentEvent: 按执行顺序产生的 Agent 事件。

        Raises:
            RuntimeError: 待处理工具中断期间不能交付后台通知。
        """
        await self.initialize()
        state = await self.session_state_manager.load()
        # 1. 工具协议尚在等待时不能直接追加通知，先由恢复链闭合工具。
        if state.is_interrupted():
            raise RuntimeError("待处理工具中断期间不能交付后台通知")
        yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": DESKTOP_CHANNEL, "resumed": False})
        # 2. 新激活只执行一组输入提交钩子，再以 desktop 来源启动内部轮次。
        # 2.1. 通知可以启动上一批已到期的记忆提取，但自身不增加真人计数。
        try:
            await self._commit_runtime_activation_inputs(state, messages, inbox_inputs)
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
        origin_channel: str = DESKTOP_CHANNEL,
        inbox_input: InboxEnvelope | None = None,
        inbox_inputs: tuple[InboxEnvelope, ...] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """提交一次真人输入激活，同批通知与真人消息共用一组前后钩子。

        由 run 在模型循环开始前调用。前置钩子处理本次输入之前的记忆批次；
        消息保存成功后先发布真人输入事实，再执行后置钩子启动派生任务。

        Args:
            state (AgentState): 当前 Agent 状态。
            message (str): 真人文本或类型声明对应的内部通知。
            images (list[str] | None): 真人附件的图片 data URL 列表。
            origin_channel (str): 输入或本轮回复所属的渠道。
            inbox_input (InboxEnvelope | None): 随本次 checkpoint 原子交付的单条输入快照。
            inbox_inputs (tuple[InboxEnvelope, ...] | None): 随本次 checkpoint 原子交付的输入快照集合。

        Yields:
            AgentEvent: 事务成功后的真人输入提交事实；继续读取后才运行激活后置钩子。
        """
        # 1. 新轮先清空上一轮背景，再让插件准备本次激活的数据。
        state.clear_temporary_context()
        # 2. 本次激活输入尚未追加，先准备上一批派生任务，不包含本次真人或通知。
        commit_context: dict = {}
        await self.plugin_manager.run_hooks(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT,
            state,
            data=commit_context,
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

        # 5. 只有输入保存成功才启动上一批任务；保存失败时数据库保留原待消费计数。
        await self.plugin_manager.run_hooks(
            PluginHook.AFTER_ACTIVATION_INPUT_COMMIT,
            state,
            data=commit_context,
        )

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
        await self.initialize()
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
        # 1. 管道错误保留最近进度，中断先保存来源后返回，二者都不伪造正常完成。
        try:
            # 显式等待下层迭代关闭，保证响应和借用资源在返回前完成清理。
            async with aclosing(event_stream) as managed_events:
                async for event in managed_events:
                    if event.type == EventType.ERROR:
                        # 1.1. 失败时清空内存背景；持久进度仍按原失败规则保留，新激活会重新重置。
                        state.clear_temporary_context()
                        yield event
                        logger.warning("%s: %s", failure_message, self.config.session_id)
                        return
                    if event.type == EventType.INTERRUPT:
                        if turn_control and turn_control.reason:
                            turn_control.check()
                        await self.session_state_manager.save_interrupt_origin(state, origin_channel)
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
            await self.session_state_manager.finish_interrupted_turn(
                state, partial=turn_control.partial, partial_persisted=turn_control.partial_persisted,
                origin_channel=origin_channel,
            )
            if turn_control.partial.strip():
                yield AgentEvent(EventType.TEXT_CHUNK, "……（被用户打断）")
            yield AgentEvent(EventType.DONE, None)
            return

        yield AgentEvent(EventType.DONE, None)



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
        # 2. 正常真人响应完成和队列放行同事务提交，内部通知轮次不单独放行。
        await self.session_state_manager.complete_turn(state, release_queued=release_queued)
        await self.plugin_manager.run_hooks(
            PluginHook.AFTER_RESPONSE_COMMIT,
            state,
            data=commit_context,
        )

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

    async def register_plugin(self, plugin: BasePlugin) -> None:
        """登记插件实例及其执行时机，让后续输入、模型或工具步骤调用对应钩子。

        Args:
            plugin (BasePlugin): 需要注册或调用的插件对象。
        """
        await self.plugin_manager.register(plugin)

    async def unregister_plugin(self, name: str) -> bool:
        """移除插件及其钩子登记，避免后续步骤继续调用已卸载插件。

        Args:
            name (str): 当前对象的名称。
        """
        return await self.plugin_manager.unregister(name)

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
        """卸载插件、关闭模型客户端和状态连接，释放本次 Agent 实例持有的资源。"""
        # 1. 各资源按原顺序清理；前一个失败仍释放本实例拥有的后续资源。
        try:
            await self.plugin_manager.close()
        finally:
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
