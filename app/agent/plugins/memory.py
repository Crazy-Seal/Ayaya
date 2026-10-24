"""记忆检索和长期记忆抽取插件。"""

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.plugins.base import BasePlugin, HookContext, PluginHook
from app.agent.state.model import AgentState, TemporaryContextItem
from app.agent.memory.manager import get_memory_manager
from app.agent.state.messages import (
    HumanMessage,
)
from app.agent.utils.infra.background_tasks import create_background_task

logger = logging.getLogger(__name__)

MEMORY_HISTORY_HUMAN_MESSAGES = 5
MEMORY_CONTEXT_KEY = "memory.recalled"


class MemoryPluginConfig(BaseModel):
    """描述记忆插件的检索开关、图片处理及长期记忆提取间隔，供设置页校验和展示。"""
    model_config = ConfigDict(extra="forbid")

    enable_diary: bool = Field(default=True, description="是否启用日记记忆。")
    enable_episodic: bool = Field(default=True, description="是否启用情景记忆。")
    enable_semantic: bool = Field(default=True, description="是否启用语义记忆。")
    long_term_memory_every_human_messages: int = Field(
        default=10,
        ge=1,
        description="每多少轮真实用户消息触发长期记忆总结/抽取。",
    )


MEMORY_PREAMBLE = (
    "以下文本是你的记忆，其中，[你的历史日记]是你对前段时间的每日记忆，"
    "[相关情景记忆]和[相关语义知识]是系统根据用户输入检索到的，你记忆的更早之前的事情。"
)


class MemoryPlugin(BasePlugin):
    """在新激活提交时准备上一批记忆，在模型调用时检索本轮真人相关记忆。

    真人或纯内部通知都能启动新激活；当前轮次中途交付通知不进入激活提交钩子。

    Attributes:
        config (MemoryPluginConfig): 日记、长期记忆类别和真人输入提取间隔。
    """
    name = "memory"
    description = "检索和沉淀长期记忆。"
    inherent = True
    config_model = MemoryPluginConfig
    version = "1.0.0"
    priority = 100

    def __init__(self, **config: Any) -> None:
        """校验并保存当前插件的记忆开关和真人输入提取间隔。

        Args:
            **config (Any): MemoryPluginConfig 接受的记忆配置字段。
        """
        self.config = MemoryPluginConfig(**config)

    @property
    def hooks(self) -> list[PluginHook]:
        """订阅新激活输入提交前后和本轮模型调用开始的事件。

        Returns:
            list[PluginHook]: 准备、启动上一批派生任务及召回本轮记忆的钩子。
        """
        return [
            PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT,
            PluginHook.AFTER_ACTIVATION_INPUT_COMMIT,
            PluginHook.ON_INVOKE,
        ]

    def _mm(self, state: AgentState):
        """取得当前会话的记忆管理器，供激活准备和本轮召回使用。

        Args:
            state (AgentState): 提供会话编号的当前状态。

        Returns:
            MemoryManager: 当前会话缓存的记忆管理器。
        """
        return get_memory_manager(state.session_id)

    async def on_register(self) -> None:
        """注册插件时验证长期记忆提取的模型输入预算配置。

        Raises:
            ValueError: 已启用长期记忆但没有配置正整数提取预算。
        """
        if not (self.config.enable_episodic or self.config.enable_semantic):
            return
        from app.agent.memory.config import MemoryConfig
        limit = MemoryConfig.from_env().extraction_max_context_tokens
        if not limit or limit <= 0:
            raise ValueError(
                "启用情景或语义记忆时必须配置正整数 MEMORY_EXTRACTION_MAX_CONTEXT_TOKENS"
            )

    async def execute(self, context: HookContext) -> None:
        """按当前生命周期事件准备任务、启动已提交任务或召回记忆。

        Args:
            context (HookContext): Agent 提供的状态、提交上下文或本轮临时上下文。
        """
        state = context.agent_state
        if context.hook == PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT:
            await self._prepare_activation_input_commit(state, context.data)
        elif context.hook == PluginHook.AFTER_ACTIVATION_INPUT_COMMIT:
            self._schedule_committed_memory(context.data)
        elif context.hook == PluginHook.ON_INVOKE:
            runtime_context = (context.data or {}).get("runtime_context") if isinstance(context.data, dict) else None
            if runtime_context is not None:
                await self._inject_context(state, runtime_context)

    async def _inject_context(self, state, runtime_context) -> None:
        """仅用本轮真人消息检索语义和情景记忆，后台唤醒仍可取得日记上下文。

        Args:
            state (AgentState): 当前 Agent 状态。
            runtime_context (对象): 提供本轮真人输入编号的运行数据。
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
            ctx = await self._mm(state).get_context(
                query=query,
                include_diary=self.config.enable_diary,
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

    async def _prepare_activation_input_commit(self, state: AgentState, commit_context: dict) -> None:
        """在新激活输入加入前，记录上一批派生任务的历史范围并消费到期计数。

        由 Agent 的激活前置钩子调用，真人或纯内部通知激活都适用。
        这里只准备任务；计数随本次输入 checkpoint 保存，后置钩子才启动任务。

        Args:
            state (AgentState): 新激活加载的状态，保存尚未消费的真人输入计数。
            commit_context (dict): 前后钩子共用的数据，接收本次准备的记忆任务。
        """
        # 1. 使用此前已提交的真人计数判断批次到期，当前激活输入尚未加入。
        pending_count = state.long_term_memory_turn_counter
        reached_interval = (
            pending_count >= self.config.long_term_memory_every_human_messages
        )
        should_extract = reached_interval and (
            self.config.enable_episodic or self.config.enable_semantic
        )

        # 1.1. 长期记忆关闭时直接消费到期计数，不依赖后续日记准备是否成功。
        if reached_interval and not should_extract:
            state.reset_long_term_memory_turn_counter()
        if not self.config.enable_diary and not should_extract:
            return

        # 2. 记录本次输入之前的协议高水位，后台查询不能吸收后来追加的新轮次。
        try:
            memory_manager = self._mm(state)
            memory_job: dict[str, Any] = {
                "manager": memory_manager,
                "session_id": state.session_id,
                "protocol_history_cutoff": (
                    await memory_manager.get_protocol_history_cutoff()
                ),
                "check_diary": self.config.enable_diary,
                "extract_long_term_memory": should_extract,
                "long_term_memory_turns": pending_count,
            }
            commit_context["memory"] = memory_job
        except Exception as e:
            # 2.1. 准备失败时保留待消费计数，让后续新激活重新准备该批次。
            logger.warning("[MemoryPlugin] 准备上一批记忆任务失败: %s", e)
            return

        # 3. 清零只发生在内存；Agent 随输入事务保存，失败时持久旧计数仍在。
        if should_extract:
            state.reset_long_term_memory_turn_counter()

    def _schedule_committed_memory(self, commit_context: dict) -> None:
        """新激活输入提交成功后，启动前置钩子准备的上一批派生记忆任务。

        Args:
            commit_context (dict): 激活前后钩子共用的数据，含已固定范围的记忆任务。
        """
        # 1. 没有准备成功的任务就结束，不在后台重新判断计数或读取最新高水位。
        memory_job = commit_context.get("memory")
        if not memory_job:
            return

        memory_manager = memory_job["manager"]
        # 2. 日记使用准备时记录的历史范围；只在本次输入持久提交后启动。
        if memory_job["check_diary"]:
            create_background_task(
                memory_manager.check_diary(
                    max_history_id=memory_job["protocol_history_cutoff"],
                ),
                logger=logger,
                task_name="memory.check_diary",
            )
        # 3. 长期记忆使用上一批真人计数和固定高水位，不包含本次激活的新输入。
        if memory_job["extract_long_term_memory"]:
            create_background_task(
                self._persist_from_protocol(
                    memory_job["session_id"],
                    max_history_id=memory_job["protocol_history_cutoff"],
                    batch_turns=memory_job["long_term_memory_turns"],
                ),
                logger=logger,
                task_name="memory.persist_long_term",
            )

    async def _persist_from_protocol(
        self,
        session_id: str,
        *,
        max_history_id: int,
        batch_turns: int,
    ) -> None:
        """从新激活准备时固定的协议范围读取上一批完整轮次并提取记忆。

        Args:
            session_id (str): 本批派生任务所属的会话。
            max_history_id (int): 新激活输入加入前的协议日志截止编号。
            batch_turns (int): 准备阶段记录的待提取真人轮次数。
        """
        # 1. 查询只使用已固定的截止位置；后台执行时不能读取后来新增的激活输入。
        mm = get_memory_manager(session_id)
        store = mm.chat_history_store
        if store is None:
            logger.warning("[MemoryPlugin] 协议日志查询器未启用，跳过长期记忆")
            return
        history, recent = await store.get_recent_completed_turns(
            session_id,
            current_turns=batch_turns,
            history_turns=MEMORY_HISTORY_HUMAN_MESSAGES,
            max_history_id=max_history_id,
        )
        # 2. 材料不足时沿用现有跳过行为；新激活入口负责选择正确的准备时机。
        recent_humans = sum(1 for message in recent if isinstance(message, HumanMessage))
        if recent_humans < batch_turns:
            logger.warning("[MemoryPlugin] 完整协议轮次不足，跳过本次长期记忆提取")
            return
        # 3. 提取当前批次，前情仅作为参考，不改变日记或运行上下文。
        await mm.add(
            recent,
            history,
            enable_episodic=self.config.enable_episodic,
            enable_semantic=self.config.enable_semantic,
        )
