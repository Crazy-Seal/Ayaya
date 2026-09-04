"""
Agent 核心类

门面类，协调各管理器，提供统一的接口。
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, TYPE_CHECKING

from app.agent.state import AgentState
from app.agent.message import ContentPart, ToolCall
from app.agent.context import BaseTool, BasePlugin, PluginHook
from app.agent.core.tool_manager import ToolManager
from app.agent.core.plugin_manager import PluginManager
from app.agent.core.state_manager import StateManager
from app.agent.core.event_router import EventRouter, EventType, AgentEvent
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.models.llm_client import LLMClient, LLMConfig
from app.agent.context_strategy import ContextStrategyConfig, ContextStrategyManager
from app.agent.message_time import RuntimeContext
from app.agent.model_input import DiaryCoverage
from app.agent.utils.domain.images import save_multiple_images
from app.schemas.mcp import MCPModelSettings

if TYPE_CHECKING:
    from app.schemas.chat_settings import ChatSettings
    from app.services.mcp_connection_manager import MCPConnectionManager
    from app.services.mcp_policy_resolver import MCPPolicyResolver

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


@dataclass
class AgentConfig:
    """Agent 配置"""
    session_id: str
    model_name: str
    api_key: str
    context_strategy: ContextStrategyConfig
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.7
    max_tokens: int = 4096
    system_prompt: str = ""
    tools: list[str] = field(default_factory=list)
    plugins: list[str | dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp: MCPModelSettings = field(default_factory=MCPModelSettings)


class Agent:
    """Agent 核心类"""

    def __init__(
        self,
        config: AgentConfig,
        db_path: str | None = None,
        mcp_connection_manager: "MCPConnectionManager | None" = None,
        mcp_policy_resolver: "MCPPolicyResolver | None" = None,
        *,
        llm_client: LLMClient | None = None,
        diary_coverage: DiaryCoverage | None = None,
        chat_settings: "ChatSettings | None" = None,
    ):
        self.config = config
        self.mcp_connection_manager = mcp_connection_manager
        self.mcp_policy_resolver = mcp_policy_resolver

        # 核心管理器
        self.tool_manager = ToolManager()
        self.plugin_manager = PluginManager()
        if db_path:
            self.state_manager = StateManager(config.session_id, db_path=db_path)
        else:
            self.state_manager = StateManager(config.session_id)
        self.event_router = EventRouter()
        self.context_strategy = ContextStrategyManager(config.context_strategy)

        # LLM 客户端
        self.llm_client = llm_client
        if self.llm_client is None:
            self.llm_client = LLMClient(LLMConfig(
                model=config.model_name,
                api_key=config.api_key,
                base_url=config.base_url,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
            ))

        # 执行管道
        self.pipeline = ExecutionPipeline(
            prompt_source=self.config,
            llm_client=self.llm_client,
            tool_manager=self.tool_manager,
            plugin_manager=self.plugin_manager,
            context_strategy=self.context_strategy,
            emit_event=self.event_router.emit,
            diary_coverage=diary_coverage,
            chat_settings=chat_settings,
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

        for tool_name in self.config.tools or []:
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
        if self.mcp_connection_manager is None or self.mcp_policy_resolver is None:
            logger.warning("MCP 配置已启用，但连接管理器或权限解析器未注入")
            return

        from app.agent.mcp.adapter import MCPToolAdapter
        from app.schemas.mcp import MCPToolPolicy
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
                policy = server_policy.tools.get(descriptor.name, MCPToolPolicy.ASK)
                if policy == MCPToolPolicy.DENY:
                    continue
                adapter = MCPToolAdapter(
                    session_id=self.config.session_id,
                    server_id=server_id,
                    server_name=server_config.name,
                    descriptor=descriptor,
                    policy=policy,
                    connection_manager=self.mcp_connection_manager,
                    policy_resolver=self.mcp_policy_resolver,
                )
                if self.tool_manager.has(adapter.name):
                    logger.error("MCP 工具名称与现有工具冲突，已跳过: %s", adapter.name)
                    continue
                self.tool_manager.register(adapter)

    async def run(
        self,
        message: str,
        images: list[str] | None = None
    ) -> AsyncIterator[AgentEvent]:
        """运行 Agent

        Args:
            message: 用户消息
            images: 图片列表（data URL 格式）

        Yields:
            AgentEvent: 事件流
        """
        await self.initialize()
        runtime_context = RuntimeContext.capture()

        # 1. 加载或创建状态
        state = await self.state_manager.load()

        if state.is_interrupted():
            interrupt_data = state.interrupt_data or {}
            cancelled_ids = {
                message.get("tool_call_id")
                for message in state.messages
                if message.get("role") == "tool"
            }
            interrupted_id = interrupt_data.get("tool_call_id", "")
            interrupted_name = interrupt_data.get("tool_name", "")
            if interrupted_id and interrupted_id not in cancelled_ids:
                state.add_tool_message(
                    content=USER_REJECTED_INTERRUPTED_TOOL_RESULT,
                    tool_name=interrupted_name,
                    tool_call_id=interrupted_id,
                )
                cancelled_ids.add(interrupted_id)

            for pending in state.pending_actions:
                pending_call = ToolCall.from_dict(pending)
                if pending_call.id in cancelled_ids:
                    continue
                state.add_tool_message(
                    content=USER_CANCELLED_PENDING_TOOL_RESULT,
                    tool_name=pending_call.name,
                    tool_call_id=pending_call.id,
                )
                cancelled_ids.add(pending_call.id)

            state.clear_interrupt()
            state.clear_pending_tool_calls()
            state.flush_deferred_tool_images()
            await self.state_manager.save(state, checkpoint_type="completed")

        # 上次普通工具链若异常中止，为未完成调用补齐结果，避免自动重试副作用。
        pending_tool_calls = state.get_pending_tool_calls()
        if pending_tool_calls:
            for tool_call in pending_tool_calls:
                state.add_tool_message(
                    content=INTERRUPTED_TOOL_RESULT,
                    tool_name=tool_call.name,
                    tool_call_id=tool_call.id,
                )
            state.clear_pending_tool_calls()
            state.flush_deferred_tool_images()
            await self.state_manager.save(state, checkpoint_type="completed")

        # 2. 在独立输入事务中提交用户消息和上一批派生任务。
        state = await self._commit_user_input(state, message, images)

        # 3. 执行管道；最终提交与 DONE 只由 Agent 负责。
        async for event in self._stream_and_finalize(
            state,
            self.pipeline.execute(
                state,
                checkpoint=self.state_manager.save,
                runtime_context=runtime_context,
            ),
            failure_message="本轮执行出错，已保留最近工具进度，未写入最终 checkpoint",
        ):
            yield event

    async def _commit_user_input(
        self,
        state: AgentState,
        message: str,
        images: list[str] | None,
    ) -> AgentState:
        """冻结上一批派生任务，并原子提交当前用户输入。"""
        commit_context: dict = {}
        state = await self.plugin_manager.run_hooks(
            PluginHook.BEFORE_USER_INPUT_COMMIT,
            state,
            data=commit_context,
        )

        if images:
            try:
                image_filenames = await asyncio.to_thread(
                    save_multiple_images,
                    images,
                )
            except Exception as exc:
                logger.warning("[Agent] 用户图片保存失败: %s", exc)
                image_filenames = []
            content = [ContentPart.text_part(message)]
            content.extend(ContentPart.image_part(image) for image in images)
            state.add_user_message(content)
            if image_filenames:
                state.messages[-1]["image_filenames"] = image_filenames
        else:
            state.add_user_message(message)
        await self.state_manager.save(state, checkpoint_type="intermediate")

        await self.plugin_manager.run_hooks(
            PluginHook.AFTER_USER_INPUT_COMMIT,
            state,
            data=commit_context,
        )
        return state

    async def resume(
        self,
        resume_data: dict
    ) -> AsyncIterator[AgentEvent]:
        """从中断恢复

        Args:
            resume_data: 恢复数据（用户确认结果）

        Yields:
            AgentEvent: 事件流
        """
        await self.initialize()
        runtime_context = RuntimeContext.capture()

        # 1. 加载状态
        state = await self.state_manager.load()

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
        ):
            yield event

    async def _stream_and_finalize(
        self,
        state: AgentState,
        event_stream: AsyncIterator[AgentEvent],
        *,
        failure_message: str,
    ) -> AsyncIterator[AgentEvent]:
        """转发执行事件，并在正常结束后完成唯一一次响应提交。"""
        async for event in event_stream:
            if event.type == EventType.ERROR:
                yield event
                logger.warning("%s: %s", failure_message, self.config.session_id)
                return
            if event.type == EventType.INTERRUPT:
                yield event
                return
            yield event

        await self._finalize_response(state)
        yield AgentEvent(EventType.DONE, None)

    async def _finalize_response(self, state: AgentState) -> None:
        """在成功执行后准备并持久化最终响应。"""
        commit_context: dict = {}
        # 轮次计数属于 Agent 完成生命周期：只有走到最终响应提交才推进。
        state.increment_long_term_memory_turn_counter()
        state = await self.plugin_manager.run_hooks(
            PluginHook.BEFORE_RESPONSE_COMMIT,
            state,
            data=commit_context,
        )
        await self.state_manager.save(state, checkpoint_type="completed")
        await self.plugin_manager.run_hooks(
            PluginHook.AFTER_RESPONSE_COMMIT,
            state,
            data=commit_context,
        )

    # ==================== 运行时操作 ====================

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
