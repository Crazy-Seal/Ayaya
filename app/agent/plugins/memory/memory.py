"""记忆检索和长期记忆抽取插件。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING


from app.agent.plugins.base import BasePlugin, HookContext, PluginContext, PluginHook
from app.agent.state.model import AgentState, TemporaryContextItem
from app.agent.plugins.memory.config import MemoryPluginConfig, MemoryConfig
from app.agent.plugins.memory.operation import MemoryOperation
from app.agent.plugins.memory.state import MemoryState, MEMORY_STATE_KEY
from app.agent.tools.core.base import BaseTool
from app.agent.plugins.runtime import PluginRuntime
from app.agent.utils.infra.owned_work import complete_owned_work
from app.agent.state.messages import HumanMessage
from app.agent.plugins.memory.tasks.memory_tasks import DiaryTask, LongTermMemoryTask
from .tools import SearchDiaryTool, SearchMemoryTool

if TYPE_CHECKING:
    from app.agent.context.runtime import RuntimeContext

logger = logging.getLogger(__name__)

MEMORY_CONTEXT_KEY = "memory.recalled"


@dataclass(frozen=True)
class MemoryJob:
    """保存输入提交前选定的历史边界和到期批次，提交后再派生任务。

    Attributes:
        session_id (str): 本次激活的会话。
        protocol_history_cutoff (int): 本次输入提交前已持久化的历史最大编号。
        check_diary (bool): 是否检查上一聊天日的日记。
        extract_long_term_memory (bool): 是否提取已到期的长期记忆。
        long_term_memory_turns (int): 待提取批次的真人轮次数。
    """

    session_id: str
    protocol_history_cutoff: int
    check_diary: bool
    extract_long_term_memory: bool
    long_term_memory_turns: int


MEMORY_PREAMBLE = (
    "以下文本是你的记忆，其中，[你的历史日记]是你对前段时间的每日记忆，"
    "[相关情景记忆]和[相关语义知识]是系统根据用户输入检索到的，你记忆的更早之前的事情。"
)


class MemoryPlugin(BasePlugin):
    """准备和整理长期记忆，并检索本轮相关记忆。

    真人或纯内部通知都能启动新激活；当前轮次中途交付通知不进入激活提交钩子。

    Attributes:
        config (MemoryPluginConfig): 日记、长期记忆类别和真人输入提取间隔。
    """
    name = MEMORY_STATE_KEY
    description = "检索和沉淀长期记忆。"
    config_model = MemoryPluginConfig
    tools = (SearchMemoryTool, SearchDiaryTool)
    version = "1.0.0"
    priority = 100

    def __init__(self, *, context: PluginContext, **config: Any) -> None:
        """从显式配置生成记忆操作材料，具体连接在检索或任务执行时打开。

        Args:
            context (PluginContext): 宿主提供的本次会话快照及数据库连接配置。
            config (Any): 记忆插件自己的能力开关和提取间隔。

        Raises:
            ValueError: 宿主未提供记忆生成所需的会话配置。
        """
        if context.chat_settings is None:
            raise ValueError("启用记忆插件需要显式提供本次会话配置")
        self.config = MemoryPluginConfig(**config)
        self._operation = MemoryOperation(context.chat_settings.model_copy(deep=True), MemoryConfig.from_env(),
                                          self.config, context.sqlite_factory)

    def create_tools(self) -> tuple[BaseTool, ...]:
        """创建使用当前配置、按实际调用会话查询的两个记忆工具。

        Returns:
            tuple[BaseTool, ...]: 由各次调用自行打开资源的记忆工具。
        """
        return SearchMemoryTool(self._operation), SearchDiaryTool(self._operation)

    @property
    def hooks(self) -> list[PluginHook]:
        """订阅输入提交和执行开始的记忆处理时机。

        Returns:
            list[PluginHook]: 准备、启动记忆任务及召回记忆的钩子。
        """
        return [
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT,
            PluginHook.ON_INVOKE,
        ]

    async def execute(self, context: HookContext) -> None:
        """按执行时机准备和启动记忆任务，并召回本轮相关记忆。

        Args:
            context (HookContext): Agent 提供的状态、提交上下文或本轮临时上下文。
        """
        state = context.agent_state
        if context.hook == PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT:
            memory_state = MemoryState.load(state)
            try:
                await self._prepare_activation_input_commit(state, context.data, memory_state)
            finally:
                # 1. 先消费此前批次，再把本次真人输入计入即将保存的状态。
                if state.messages and isinstance(state.messages[-1], HumanMessage):
                    memory_state.long_term_memory_turn_counter += 1
                memory_state.save(state)
        elif context.hook == PluginHook.ON_INVOKE:
            # 1. 新输入已经保存，先启动提交前准备好的上一批记忆任务。
            memory_job = context.data.get(MEMORY_STATE_KEY)
            if memory_job:
                assert context.runtime is not None
                cutoff = memory_job.protocol_history_cutoff
                if memory_job.check_diary:
                    # 1.1. 检查与登记作为短操作完成后再传播取消，已接管任务的标记交给任务清理。
                    try:
                        await complete_owned_work(self._submit_diary_task(state.session_id, cutoff, context.runtime))
                    except Exception:
                        # 1.2. 日记检查失败保留日志，长期记忆任务和本轮记忆召回继续处理。
                        logger.exception("[MemoryPlugin] 准备或提交日记任务失败")
                if memory_job.extract_long_term_memory:
                    await context.runtime.submit_task(LongTermMemoryTask(self._operation, memory_job.session_id, cutoff,
                        memory_job.long_term_memory_turns, self.config.enable_episodic, self.config.enable_semantic),
                        name="memory.persist_long_term", title="提取长期记忆",
                        notify_agent=False)
            # 2. 用本轮真人输入检索记忆，写入后续模型请求共用的临时背景。
            runtime_context = (context.data or {}).get("runtime_context") if isinstance(context.data, dict) else None
            if runtime_context is not None:
                await self._inject_context(state, runtime_context)

    async def _submit_diary_task(self, session_id: str, cutoff: int, runtime: PluginRuntime) -> None:
        """先检查是否需要日记，成功准备后登记任务；登记失败时释放日期标记。

        Args:
            session_id (str): 本次日记检查所属的会话。
            cutoff (int): 本次输入保存前的历史最大编号。
            runtime (PluginRuntime): 固定来源会话和轮次的任务提交入口。
        """
        generation = None
        try:
            async with self._operation.open(session_id, diary=True) as manager:
                generation = await manager.prepare_diary(max_history_id=cutoff)
            if generation is None:
                return
            await runtime.submit_task(
                DiaryTask(self._operation, generation), name="memory.check_diary",
                title=f"生成 {generation.target_date.isoformat()} 日记",
                notify_agent=False,
            )
        except BaseException:
            # 1. 外层保护确保取消不打断任务交接；实际登记失败时由此释放未交付的材料。
            if generation is not None:
                generation.release()
            raise

    async def _inject_context(self, state: AgentState, runtime_context: RuntimeContext) -> None:
        """仅用本轮真人消息检索语义和情景记忆，后台唤醒仍可取得日记上下文。

        Args:
            state (AgentState): 当前 Agent 状态。
            runtime_context (RuntimeContext): 提供本轮真人输入编号的运行数据。
        """
        # 1. 只找本轮 latest_human_message_id，不从旧真人消息推断后台唤醒的查询。
        current_human = next(
            (message for message in reversed(state.messages)
             if isinstance(message, HumanMessage)
             and message.message_id == runtime_context.latest_human_message_id),
            None,
        )
        query = current_human.text if current_human else ""
        # 2. 没有本轮真人查询时关闭语义和情景检索，日记仍可提供背景。
        try:
            async with self._operation.open(state.session_id, diary=True,
                                            episodic=bool(query), semantic=bool(query)) as manager:
                ctx = await manager.get_context(
                    query=query, include_diary=self.config.enable_diary,
                    include_episodic=self.config.enable_episodic and bool(query),
                    include_semantic=self.config.enable_semantic and bool(query),
                )
        except Exception as e:
            logger.warning("[MemoryPlugin] 记忆检索失败: %s", e)
            ctx = ""
        # 3. 召回结果随本轮 checkpoint 保存，审批恢复后继续由统一组装流程读取。
        if ctx and ctx.strip():
            state.set_temporary_context(MEMORY_CONTEXT_KEY, TemporaryContextItem(
                title="相关记忆", text=f"{MEMORY_PREAMBLE}\n\n{ctx}",
            ))
        else:
            state.remove_temporary_context(MEMORY_CONTEXT_KEY)

    async def _prepare_activation_input_commit(self, state: AgentState, commit_context: dict, memory_state: MemoryState) -> None:
        """在新激活输入提交前，记录上一批派生任务的历史范围并消费到期计数。

        由 Agent 的激活前置钩子调用，真人或纯内部通知激活都适用。
        这里只准备任务；计数随本次输入 checkpoint 保存，ON_INVOKE 再启动任务。

        Args:
            state (AgentState): 新激活加载的状态，保存尚未消费的真人输入计数。
            commit_context (dict): 与 ON_INVOKE 共用的数据，接收本次准备的记忆任务。
            memory_state (MemoryState): 插件命名空间中的待消费计数。
        """
        # 1. 使用此前已提交的真人计数判断批次到期，本次真人消息在批次准备后计数。
        pending_count = memory_state.long_term_memory_turn_counter
        reached_interval = (
            pending_count >= self.config.long_term_memory_every_human_messages
        )
        should_extract = reached_interval and (
            self.config.enable_episodic or self.config.enable_semantic
        )

        # 1.1. 长期记忆关闭时直接消费到期计数，不依赖后续日记准备是否成功。
        if reached_interval and not should_extract:
            memory_state.long_term_memory_turn_counter = 0
        if not self.config.enable_diary and not should_extract:
            return

        # 2. 记录本次输入之前的协议高水位，后台查询不能吸收后来追加的新轮次。
        try:
            async with self._operation.open(state.session_id) as manager:
                cutoff = await manager.get_protocol_history_cutoff()
            commit_context[MEMORY_STATE_KEY] = MemoryJob(
                state.session_id, cutoff, self.config.enable_diary, should_extract, pending_count,
            )
        except Exception as e:
            # 2.1. 准备失败时保留待消费计数，让后续新激活重新准备该批次。
            logger.warning("[MemoryPlugin] 准备上一批记忆任务失败: %s", e)
            return

        # 3. 清零只发生在内存；Agent 随输入事务保存，失败时持久旧计数仍在。
        if should_extract:
            memory_state.long_term_memory_turn_counter = 0
