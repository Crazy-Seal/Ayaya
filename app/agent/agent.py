"""
Agent 核心类

门面类，协调各管理器，提供统一的接口。
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator, Awaitable, Callable, TYPE_CHECKING

from app.schemas.runtime_types import (
    DESKTOP_CHANNEL,
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
from app.agent.core.event_router import EventRouter, EventType, AgentEvent
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.models.llm_client import LLMClient, LLMConfig
from app.agent.plugins.base import BasePlugin, PluginHook
from app.agent.state import AgentState
from app.agent.state.manager import StateManager
from app.agent.runtime.session_inbox import InboxEnvelope
from app.agent.runtime.turn_control import TurnControl, TurnInterrupted
from app.agent.state.messages import AssistantMessage, HumanMessage, ToolCall, ToolMessage, RuntimeInputMessage
from app.agent.tools.base import BaseTool, TaskLauncher
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.utils.domain.images import save_multiple_images
from app.schemas.mcp import MCPModelSettings

if TYPE_CHECKING:
    from app.schemas.chat_settings import ChatSettings
    from app.services.mcp_connection_manager import MCPConnectionManager

logger = logging.getLogger(__name__)


INTERRUPTED_TOOL_RESULT = (
    "上次工具链中断，未获得该工具的可靠结果；"
    "为避免重复副作用，本次未自动重试。"
)
USER_REJECTED_INTERRUPTED_TOOL_RESULT = (
    "用户发送了新的消息，待批准工具调用已拒绝。"
)
USER_CANCELLED_PENDING_TOOL_RESULT = (
    "用户发送了新的消息，本批后续工具调用已取消。"
)
APPLICATION_EXIT_RESULT = "应用退出，本轮已终止；本次工具调用已取消。"
APPLICATION_EXIT_UNKNOWN_RESULT = "应用退出，本轮已终止；工具执行结果未知，本次不会自动重试。"


@dataclass
class AgentConfig:
    """Agent 配置"""
    session_id: str
    model_name: str
    api_key: str
    context_strategy: ContextStrategyConfig
    base_url: str = "https://api.openai.com/v1"
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
        event_router (EventRouter): Agent 内部事件订阅器。
        llm_client (LLMClient | None): 可替换的流式模型客户端。
        pipeline (ExecutionPipeline): 模型工具执行管道。
        _initialized (bool): 工具和插件是否已经完成初始化。
        state_manager (StateManager): 当前会话的 checkpoint 和协议日志管理器。
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
    ):
        """装配 Agent 执行依赖，将后台任务启动端口注入工具管道。

        Args:
            config (AgentConfig): 当前会话的 Agent 配置。
            db_path (str | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
            mcp_connection_manager ('MCPConnectionManager | None'): 共享 MCP 连接管理器。
            llm_client (LLMClient | None): 可替换的流式模型客户端。
            diary_coverage (DiaryCoverage | None): 查询已生成日记覆盖范围的端口。
            chat_settings ('ChatSettings | None'): 会话模型和插件配置。
            tool_authorization (ToolAuthorization | None): 工具单次授权验证器。
            task_launcher (TaskLauncher | None): 工具使用的共享进程启动端口。
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
        if db_path:
            self.state_manager = StateManager(config.session_id, db_path=db_path)
        else:
            self.state_manager = StateManager(config.session_id)
        self.event_router = EventRouter()
        # LLM 客户端
        self.llm_client = llm_client
        if self.llm_client is None:
            self.llm_client = LLMClient(LLMConfig(
                model=config.model_name,
                api_key=config.api_key,
                base_url=config.base_url,
                temperature=config.temperature,
            ))

        # 模型输入与执行管道
        model_input = ModelInputCoordinator(
            config.context_strategy,
            self.llm_client,
            diary_coverage=diary_coverage,
            chat_settings=chat_settings,
            accounting_key=build_token_accounting_key(
                protocol="openai_chat_completions",
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
            emit_event=self.event_router.emit,
            tool_authorization=self.tool_authorization,
            task_launcher=task_launcher,
        )

        # 初始化标志
        self._initialized = False


    async def initialize(self) -> None:
        """初始化 Agent（异步）"""
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
        """根据配置加载工具"""
        # 从工具注册表加载
        from app.agent.tools.registry import ToolRegistry

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
        """根据配置加载插件"""
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
        from app.agent.tools.registry import ToolRegistry
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
        from app.services.mcp_connection_manager import MCPConnectionError

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
        initial_state: AgentState | None = None,
        origin_channel: str = DESKTOP_CHANNEL,
        inbox_claim: InboxEnvelope | None = None,
        inbox_claims: tuple[InboxEnvelope, ...] | None = None,
        runtime_inputs: tuple[RuntimeInputMessage, ...] = (),
        turn_control: TurnControl | None = None,
        before_next_llm: Callable[[AgentState], Awaitable[None]] | None = None,
        on_input_committed: Callable[[HumanMessage], Awaitable[None]] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """交付真人输入并运行一轮对话，先闭合遗留工具协议，再由 Agent 提交最终响应。

        Args:
            message (str): 真人文本或类型声明对应的内部通知。
            images (list[str] | None): 真人附件的图片 data URL 列表。
            initial_state (AgentState | None): 调用方已加载的状态，省略时读取当前 checkpoint。
            origin_channel (str): 输入或本轮回复所属的渠道。
            inbox_claim (InboxEnvelope | None): 随本次 checkpoint 原子交付的单条领取快照。
            inbox_claims (tuple[InboxEnvelope, ...] | None): 随本次 checkpoint 原子交付的领取快照集合。
            runtime_inputs (tuple[RuntimeInputMessage, ...]): 同次激活中先于真人输入追加的内部通知。
            turn_control (TurnControl | None): 当前 run 的停止与引导控制器。
            before_next_llm (Callable[[AgentState], Awaitable[None]] | None): 下一次模型调用前注入内部通知的异步回调。
            on_input_committed (Callable[[HumanMessage], Awaitable[None]] | None): 真人输入事务提交后、模型调用前的通知回调。

        Yields:
            AgentEvent: 按执行顺序产生的 Agent 事件。

        Raises:
            ValueError: 预加载状态与当前 Agent 会话不匹配。
        """
        await self.initialize()
        runtime_context = RuntimeContext.capture()
        runtime_context.origin_channel = origin_channel
        runtime_context.turn_control = turn_control
        runtime_context.before_next_llm = before_next_llm

        # 1. 加载或创建状态
        state = (
            initial_state
            if initial_state is not None
            else await self.state_manager.load()
        )
        if state.session_id != self.config.session_id:
            raise ValueError("预加载状态与当前 Agent 会话不匹配")

        # 2. 新输入终止旧工具协议，状态操作集中维护整批闭合与媒体顺序。
        if state.close_unfinished_tools(
            USER_REJECTED_INTERRUPTED_TOOL_RESULT,
            USER_CANCELLED_PENDING_TOOL_RESULT,
            INTERRUPTED_TOOL_RESULT,
        ):
            await self.state_manager.save(state, checkpoint_type="completed")

        # 4. 原子提交本批内部通知与真人输入，再进入可停止的模型工具循环。
        state = await self._commit_user_input(
            state, message, images, origin_channel=origin_channel,
            inbox_claim=inbox_claim, inbox_claims=inbox_claims,
            runtime_inputs=runtime_inputs,
            on_input_committed=on_input_committed,
        )

        # 5. 执行管道；最终提交与 DONE 只由 Agent 负责。
        async for event in self._stream_and_finalize(
            state,
            self.pipeline.execute(
                state,
                checkpoint=self.state_manager.save,
                runtime_context=runtime_context,
            ),
            failure_message="本轮执行出错，已保留最近工具进度，未写入最终 checkpoint",
            origin_channel=origin_channel,
            turn_control=turn_control,
            release_queued=inbox_claim is not None,
        ):
            yield event

    async def validate_pending_continuation(self, interrupt_data: dict) -> bool:
        """初始化当前工具目录，并在前端副作用发生前复核 continuation。"""
        await self.initialize()
        return self.pipeline.validate_pending_continuation({
            **interrupt_data,
            "session_id": self.config.session_id,
        })

    async def run_runtime_input(
        self, message: RuntimeInputMessage, *,
        inbox_claim: InboxEnvelope,
        turn_control: TurnControl | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """将单条内部通知转交批量运行入口，不伪装成真人消息。

        Args:
            message (RuntimeInputMessage): 真人文本或类型声明对应的内部通知。
            inbox_claim (InboxEnvelope): 随本次 checkpoint 原子交付的单条领取快照。
            turn_control (TurnControl | None): 当前 run 的停止与引导控制器。

        Yields:
            AgentEvent: 按执行顺序产生的 Agent 事件。
        """
        async for event in self.run_runtime_inputs(
            (message,), inbox_claims=(inbox_claim,), turn_control=turn_control,
        ):
            yield event

    async def commit_runtime_inputs(
        self, state: AgentState, messages: tuple[RuntimeInputMessage, ...],
        claims: tuple[InboxEnvelope, ...],
    ) -> None:
        """追加内部通知并原子保存消息与 Inbox 交付状态，不触发新激活钩子。

        Scheduler 在当前轮次的下一次模型调用前使用本方法交付任务通知。
        新一轮纯内部通知激活也复用这里的持久提交，但其前后钩子由
        _commit_runtime_activation_inputs 单独协调。

        Args:
            state (AgentState): 当前 Agent 状态。
            messages (tuple[RuntimeInputMessage, ...]): 需要转换或交付的领域消息集合。
            claims (tuple[InboxEnvelope, ...]): 待交付内部通知的领取快照集合。
        """
        # 1. 通知只补入当前消息，不消费真人记忆计数，也不准备新的激活派生任务。
        for message in messages:
            state.append_input(message)
        # 2. 通知、协议历史和领取交付标记在同一事务保存；失败时由状态管理器回滚。
        await self.state_manager.save(
            state, checkpoint_type="intermediate", inbox_claims=claims,
        )

    async def _commit_runtime_activation_inputs(
        self, state: AgentState, messages: tuple[RuntimeInputMessage, ...],
        claims: tuple[InboxEnvelope, ...],
    ) -> None:
        """在新一轮纯内部通知激活时，协调输入提交和上一批记忆任务。

        仅由 run_runtime_inputs 在启动模型循环前调用。当前轮次中途收到
        通知时直接使用 commit_runtime_inputs，不再次进入本生命周期。

        Args:
            state (AgentState): 本次新激活加载的会话状态。
            messages (tuple[RuntimeInputMessage, ...]): 本次激活需要交付的内部通知。
            claims (tuple[InboxEnvelope, ...]): 与通知一同持久交付的领取快照。
        """
        # 1. 本次通知加入前准备上一批派生任务，前后钩子共用同一提交上下文。
        commit_context: dict = {}
        await self.plugin_manager.run_hooks(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data=commit_context,
        )
        # 2. 消息与领取交付标记原子保存；失败时直接退出，不启动后台派生任务。
        await self.commit_runtime_inputs(state, messages, claims)
        # 3. 只有输入事务成功后才通知插件启动已经准备好的上一批任务。
        await self.plugin_manager.run_hooks(
            PluginHook.AFTER_ACTIVATION_INPUT_COMMIT, state, data=commit_context,
        )

    async def run_runtime_inputs(
        self, messages: tuple[RuntimeInputMessage, ...], *,
        inbox_claims: tuple[InboxEnvelope, ...],
        turn_control: TurnControl | None = None,
        before_next_llm: Callable[[AgentState], Awaitable[None]] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """交付后台终态通知并自行启动模型循环，待处理工具中断期间禁止注入。

        Args:
            messages (tuple[RuntimeInputMessage, ...]): 需要转换或交付的领域消息集合。
            inbox_claims (tuple[InboxEnvelope, ...]): 随本次 checkpoint 原子交付的领取快照集合。
            turn_control (TurnControl | None): 当前 run 的停止与引导控制器。
            before_next_llm (Callable[[AgentState], Awaitable[None]] | None): 下一次模型调用前注入内部通知的异步回调。

        Yields:
            AgentEvent: 按执行顺序产生的 Agent 事件。

        Raises:
            RuntimeError: 待处理工具中断期间不能交付后台通知。
        """
        await self.initialize()
        state = await self.state_manager.load()
        # 1. 工具协议尚在等待时不能直接追加通知，先由恢复链闭合工具。
        if state.is_interrupted():
            raise RuntimeError("待处理工具中断期间不能交付后台通知")
        # 2. 新激活只执行一组输入提交钩子，再以 desktop 来源启动内部轮次。
        # 2.1. 通知可以启动上一批已到期的记忆提取，但自身不增加真人计数。
        await self._commit_runtime_activation_inputs(state, messages, inbox_claims)
        runtime_context = RuntimeContext.capture()
        runtime_context.turn_control = turn_control
        runtime_context.origin_channel = DESKTOP_CHANNEL
        runtime_context.before_next_llm = before_next_llm
        async for event in self._stream_and_finalize(
            state,
            self.pipeline.execute(
                state, checkpoint=self.state_manager.save,
                runtime_context=runtime_context,
            ),
            failure_message="后台通知执行出错",
            origin_channel=DESKTOP_CHANNEL,
            turn_control=turn_control,
        ):
            yield event

    async def _commit_user_input(
        self,
        state: AgentState,
        message: str,
        images: list[str] | None,
        *,
        origin_channel: str = DESKTOP_CHANNEL,
        inbox_claim: InboxEnvelope | None = None,
        inbox_claims: tuple[InboxEnvelope, ...] | None = None,
        runtime_inputs: tuple[RuntimeInputMessage, ...] = (),
        on_input_committed: Callable[[HumanMessage], Awaitable[None]] | None = None,
    ) -> AgentState:
        """提交一次真人输入激活，同批通知与真人消息共用一组前后钩子。

        由 run 在模型循环开始前调用。前置钩子处理本次输入之前的记忆批次；
        消息保存成功后先发布真人输入事实，再执行后置钩子启动派生任务。

        Args:
            state (AgentState): 当前 Agent 状态。
            message (str): 真人文本或类型声明对应的内部通知。
            images (list[str] | None): 真人附件的图片 data URL 列表。
            origin_channel (str): 输入或本轮回复所属的渠道。
            inbox_claim (InboxEnvelope | None): 随本次 checkpoint 原子交付的单条领取快照。
            inbox_claims (tuple[InboxEnvelope, ...] | None): 随本次 checkpoint 原子交付的领取快照集合。
            runtime_inputs (tuple[RuntimeInputMessage, ...]): 同次激活中先于真人输入追加的内部通知。
            on_input_committed (Callable[[HumanMessage], Awaitable[None]] | None): 在 checkpoint 和 Inbox 原子提交后发布输入事实的回调。

        Returns:
            AgentState: 已追加并持久交付当前输入的 Agent 状态。
        """
        # 1. 本次激活输入尚未追加，先准备上一批派生任务，不包含本次真人或通知。
        commit_context: dict = {}
        await self.plugin_manager.run_hooks(
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT,
            state,
            data=commit_context,
        )

        # 2. 同批任务终态先追加，再追加真人输入；不为内部通知增加真人轮次计数。
        for runtime_input in runtime_inputs:
            state.append_input(runtime_input)

        if images:
            try:
                image_filenames = await asyncio.to_thread(
                    save_multiple_images,
                    images,
                )
            except Exception as exc:
                logger.warning("[Agent] 用户图片保存失败: %s", exc)
                image_filenames = []
            human_message = state.add_user_message(
                message,
                image_urls=images,
                image_filenames=image_filenames,
                origin_channel=origin_channel,
            )
        else:
            human_message = state.add_user_message(message, origin_channel=origin_channel)
        # 真人输入一旦进入 intermediate checkpoint 就属于已提交事实；后续模型、
        # 工具或 completed checkpoint 失败都不能回退或重复推进该计数。
        state.increment_long_term_memory_turn_counter()
        # 3. 保存 checkpoint 时同时完成领取交付，模型调用失败不能撤销已提交真人事实。
        claims = inbox_claims if inbox_claims is not None else (
            (inbox_claim,) if inbox_claim is not None else ()
        )
        if not claims:
            await self.state_manager.save(state, checkpoint_type="intermediate")
        else:
            await self.state_manager.save(
                state, checkpoint_type="intermediate", inbox_claims=claims,
            )

        # 4. 输入已提交后立即通知观察方，不能等到模型首片才显示真人消息。
        if on_input_committed is not None:
            await on_input_committed(human_message)

        # 5. 只有输入保存成功才启动上一批任务；保存失败时数据库保留原待消费计数。
        await self.plugin_manager.run_hooks(
            PluginHook.AFTER_ACTIVATION_INPUT_COMMIT,
            state,
            data=commit_context,
        )
        return state

    async def resume(
        self,
        resume_data: dict,
        *,
        initial_state: AgentState | None = None,
        turn_control: TurnControl | None = None,
        before_next_llm: Callable[[AgentState], Awaitable[None]] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """恢复 checkpoint 中的工具中断，沿用原轮次来源和停止控制。

        Args:
            resume_data (dict): 审批决定或桌面执行的实际结果。
            initial_state (AgentState | None): 调用方已加载的状态，省略时读取当前 checkpoint。
            turn_control (TurnControl | None): 当前 run 的停止与引导控制器。
            before_next_llm (Callable[[AgentState], Awaitable[None]] | None): 下一次模型调用前注入内部通知的异步回调。

        Yields:
            AgentEvent: 按执行顺序产生的 Agent 事件。

        Raises:
            ValueError: 预加载状态与当前 Agent 会话不匹配。
        """
        await self.initialize()
        runtime_context = RuntimeContext.capture()
        runtime_context.turn_control = turn_control
        runtime_context.before_next_llm = before_next_llm

        # 1. 加载状态
        state = (
            initial_state
            if initial_state is not None
            else await self.state_manager.load()
        )
        if state.session_id != self.config.session_id:
            raise ValueError("预加载状态与当前 Agent 会话不匹配")
        runtime_context.origin_channel = str(
            (state.interrupt_data or {}).get("origin_channel")
            or next(
                (item.origin_channel for item in reversed(state.messages)
                 if hasattr(item, "origin_channel")),
                DESKTOP_CHANNEL,
            )
        )

        # 2. 检查是否有中断状态
        if not state.is_interrupted():
            logger.warning("没有中断状态需要恢复")
            yield AgentEvent(EventType.ERROR, "没有中断状态需要恢复")
            return

        # 3. 恢复执行；最终提交与 DONE 只由 Agent 负责。
        async for event in self._stream_and_finalize(
            state,
            self.pipeline.resume_tools(
                state,
                resume_data,
                checkpoint=self.state_manager.save,
                runtime_context=runtime_context,
            ),
            failure_message="恢复执行出错，已保留最近工具进度，未写入最终 checkpoint",
            origin_channel=runtime_context.origin_channel,
            turn_control=turn_control,
            release_queued=True,
        ):
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

        Raises:
            TurnInterrupted: 输入、持久状态或系统调用不满足当前执行约束。
        """
        # 1. 管道错误保留最近进度，中断先保存来源后返回，二者都不伪造正常完成。
        try:
            async for event in event_stream:
                if event.type == EventType.ERROR:
                    yield event
                    logger.warning("%s: %s", failure_message, self.config.session_id)
                    return
                if event.type == EventType.INTERRUPT:
                    if turn_control and turn_control.reason:
                        turn_control.check()
                    if state.interrupt_data and "origin_channel" not in state.interrupt_data:
                        state.set_interrupt(
                            {**state.interrupt_data, "origin_channel": origin_channel},
                            state.pending_actions,
                        )
                        await self.state_manager.save(state, checkpoint_type="intermediate")
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
            await self._finish_interrupted_turn(state, turn_control, origin_channel)
            if turn_control.partial.strip():
                yield AgentEvent(EventType.TEXT_CHUNK, "……（被用户打断）")
            yield AgentEvent(EventType.DONE, None)
            return

        yield AgentEvent(EventType.DONE, None)

    async def _finish_interrupted_turn(
        self, state: AgentState, control: TurnControl, origin_channel: str
    ) -> None:
        """闭合未执行工具调用并保存可见片段，同一已保存消息只追加一次打断后缀。

        Args:
            state (AgentState): 当前 Agent 状态。
            control (TurnControl): 当前轮次的打断控制和已展示片段。
            origin_channel (str): 输入或本轮回复所属的渠道。

        Raises:
            RuntimeError: 已保存的流式文本缺少对应 assistant 消息。
        """
        # 1. 整批闭合由状态操作负责，工具或模型等待不因收尾而重新执行。
        cancelled = "用户打断，本次工具调用已取消。"
        state.close_unfinished_tools(cancelled, cancelled, cancelled)
        rewritten_message_id = None
        # 2. 尚未保存的片段新增消息；已记录的工具前文本只更新原消息后缀。
        if control.partial.strip() and not control.partial_persisted:
            state.add_assistant_message(
                control.partial + "……（被用户打断）",
                origin_channel=origin_channel,
            )
        elif control.partial_persisted and control.partial.strip():
            previous = next(
                (item for item in reversed(state.messages)
                 if isinstance(item, AssistantMessage)
                 and item.text == control.partial
                 and item.origin_channel == origin_channel),
                None,
            )
            if previous is None:
                raise RuntimeError("已保存的流式文本缺少对应 assistant 消息")
            state.replace_message(replace(
                previous, text=previous.text + "……（被用户打断）",
            ))
            rewritten_message_id = previous.message_id
        # 3. 同时写 completed checkpoint 和必要的原消息重写，不放行停止轮次的队列。
        await self.state_manager.save(
            state, checkpoint_type="completed",
            rewritten_message_id=rewritten_message_id,
        )

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
        if release_queued:
            await self.state_manager.save(
                state, checkpoint_type="completed", release_queued=True,
            )
        else:
            await self.state_manager.save(state, checkpoint_type="completed")
        await self.plugin_manager.run_hooks(
            PluginHook.AFTER_RESPONSE_COMMIT,
            state,
            data=commit_context,
        )

    # ==================== 运行时操作 ====================

    @staticmethod
    async def finalize_abandoned_turn(
        state_manager: StateManager, *, partial: str = "", partial_persisted: bool = False,
        origin_channel: str = DESKTOP_CHANNEL, partial_message_id: str | None = None,
    ) -> bool:
        """只加载和持久结束旧轮次，不创建执行对象或运行模型、工具与插件。

        Args:
            state_manager (StateManager): 调用方提供的会话持久管理器。
            partial (str): 正常关闭前捕获的当前模型步骤可见片段。
            partial_persisted (bool): 片段是否已记录到内存 Assistant 步骤，仍需核对落盘状态。
            origin_channel (str): 片段所属渠道。
            partial_message_id (str | None): 当前步骤的消息身份，避免匹配到以前相同正文。

        Returns:
            bool: 是否提交了本次终止；已完成状态重复调用不再写入。

        Raises:
            Exception: 持久保存失败，原 checkpoint 和协议记录保持不变。
        """
        state = await state_manager.load()
        if state_manager.last_loaded_checkpoint_type != "intermediate" and not (
            state.interrupt_data or state.pending_tool_calls
        ):
            return False
        # 1. 使用持久回报形成事实记录，恢复不经过工具执行或 VLM。
        interrupt = state.interrupt_data or {}
        receipt = interrupt.get("desktop_result")
        content = APPLICATION_EXIT_RESULT
        images: tuple[str, ...] = ()
        if receipt is not None:
            screenshot = receipt.get("screenshot_data")
            images = (screenshot,) if isinstance(screenshot, str) and screenshot else ()
            facts = {key: value for key, value in receipt.items() if key != "screenshot_data"}
            content = "应用退出，本轮已终止；已保存桌面执行回报：" + json.dumps(facts, ensure_ascii=False)
        elif (interrupt.get("resume_state") or {}).get("continuation_claimed"):
            content = APPLICATION_EXIT_UNKNOWN_RESULT
        state.close_unfinished_tools(content, APPLICATION_EXIT_RESULT, APPLICATION_EXIT_UNKNOWN_RESULT,
                                     interrupted_images=images)
        # 2. 只保留捕获到的真实片段；已持久文本沿用原消息身份，不复制。
        rewritten_message_id = None
        if partial.strip():
            suffix = "……（应用退出，本轮已终止）"
            previous = None
            if partial_message_id is not None:
                previous = next((item for item in state.messages
                                 if isinstance(item, AssistantMessage)
                                 and item.message_id == partial_message_id), None)
            elif partial_persisted:
                previous = next((item for item in reversed(state.messages)
                                 if isinstance(item, AssistantMessage) and item.text == partial
                                 and item.origin_channel == origin_channel), None)
            if previous is not None:
                state.replace_message(replace(previous, text=previous.text + suffix))
                rewritten_message_id = previous.message_id
            else:
                # 2.1. 最终提交或工具前 checkpoint 被取消时，内存步骤尚未落盘，需补保存。
                state.add_assistant_message(partial + suffix, origin_channel=origin_channel)
        # 3. 终止是存储维护，不能运行正常完成钩子或放行旧排队输入。
        await state_manager.save(state, checkpoint_type="completed", rewritten_message_id=rewritten_message_id)
        return True

    def register_tool(self, tool: BaseTool) -> None:
        """运行时注册工具"""
        self.tool_manager.register(tool)

    def unregister_tool(self, name: str) -> bool:
        """运行时移除工具"""
        return self.tool_manager.unregister(name)

    async def register_plugin(self, plugin: BasePlugin) -> None:
        """运行时注册插件"""
        await self.plugin_manager.register(plugin)

    async def unregister_plugin(self, name: str) -> bool:
        """运行时移除插件"""
        return await self.plugin_manager.unregister(name)

    def get_tools(self) -> list[str]:
        """获取所有工具名称"""
        return self.tool_manager.list_tools()

    def get_plugins(self) -> list[str]:
        """获取所有插件名称"""
        return self.plugin_manager.list_plugins()

    # ==================== 状态操作 ====================

    async def get_state(self) -> AgentState:
        """获取当前状态"""
        await self.initialize()
        return await self.state_manager.load()

    async def clear_state(self) -> int:
        """清空状态"""
        return await self.state_manager.clear_session()

    # ==================== 生命周期 ====================

    async def close(self) -> None:
        """关闭 Agent"""
        await self.plugin_manager.close()
        await self.llm_client.close()
        await self.state_manager.close()
        logger.info(f"Agent 已关闭: {self.config.session_id}")

    async def __aenter__(self) -> "Agent":
        await self.initialize()
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()
