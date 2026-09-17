"""记忆检索和长期记忆抽取插件。"""

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.plugins.base import BasePlugin, HookContext, PluginHook
from app.agent.memory.manager import get_memory_manager
from app.agent.state.messages import (
    is_real_human_message,
    messages_from_openai_format,
)
from app.agent.utils.infra.background_tasks import create_background_task
from app.agent.utils.domain.text import get_last_human_text

logger = logging.getLogger(__name__)

MEMORY_HISTORY_HUMAN_MESSAGES = 5


class MemoryPluginConfig(BaseModel):
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
    name = "memory"
    description = "检索和沉淀长期记忆。"
    inherent = True
    config_model = MemoryPluginConfig
    version = "1.0.0"
    priority = 100

    def __init__(self, **config: Any) -> None:
        self.config = MemoryPluginConfig(**config)

    @property
    def hooks(self) -> list[PluginHook]:
        return [
            PluginHook.BEFORE_USER_INPUT_COMMIT,
            PluginHook.AFTER_USER_INPUT_COMMIT,
            PluginHook.ON_INVOKE,
        ]

    def _mm(self, state):
        return get_memory_manager(state.session_id)

    async def on_register(self) -> None:
        if not (self.config.enable_episodic or self.config.enable_semantic):
            return
        from app.agent.memory.config import MemoryConfig
        limit = MemoryConfig.from_env().extraction_max_context_tokens
        if not limit or limit <= 0:
            raise ValueError(
                "启用情景或语义记忆时必须配置正整数 MEMORY_EXTRACTION_MAX_CONTEXT_TOKENS"
            )

    async def execute(self, context: HookContext) -> None:
        state = context.agent_state
        if context.hook == PluginHook.BEFORE_USER_INPUT_COMMIT:
            await self._prepare_user_input_commit(state, context.data)
        elif context.hook == PluginHook.AFTER_USER_INPUT_COMMIT:
            self._schedule_committed_memory(context.data)
        elif context.hook == PluginHook.ON_INVOKE:
            runtime_context = (context.data or {}).get("runtime_context") if isinstance(context.data, dict) else None
            if runtime_context is not None:
                await self._inject_context(state, runtime_context)

    async def _inject_context(self, state, runtime_context) -> None:
        query = get_last_human_text(state.messages)
        try:
            ctx = await self._mm(state).get_context(
                query=query,
                include_diary=self.config.enable_diary,
                include_episodic=self.config.enable_episodic,
                include_semantic=self.config.enable_semantic,
            )
        except Exception as e:
            logger.warning("[MemoryPlugin] 记忆检索失败: %s", e)
            ctx = ""
        runtime_context.recalled_memory = (
            f"{MEMORY_PREAMBLE}\n\n{ctx}" if ctx and ctx.strip() else ""
        )

    async def _prepare_user_input_commit(self, state, commit_context: dict) -> None:
        """在当前输入加入前冻结上一批派生记忆任务。"""
        pending_count = state.long_term_memory_turn_counter
        reached_interval = (
            pending_count >= self.config.long_term_memory_every_human_messages
        )
        should_extract = reached_interval and (
            self.config.enable_episodic or self.config.enable_semantic
        )

        if reached_interval and not should_extract:
            state.reset_long_term_memory_turn_counter()
        if not self.config.enable_diary and not should_extract:
            return

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
            logger.warning("[MemoryPlugin] 准备上一批记忆任务失败: %s", e)
            return

        if should_extract:
            state.reset_long_term_memory_turn_counter()

    def _schedule_committed_memory(self, commit_context: dict) -> None:
        """当前用户输入提交成功后启动上一批派生记忆任务。"""
        memory_job = commit_context.get("memory")
        if not memory_job:
            return

        memory_manager = memory_job["manager"]
        if memory_job["check_diary"]:
            create_background_task(
                memory_manager.check_diary(
                    max_history_id=memory_job["protocol_history_cutoff"],
                ),
                logger=logger,
                task_name="memory.check_diary",
            )
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
        mm = get_memory_manager(session_id)
        store = mm.chat_history_store
        if store is None:
            logger.warning("[MemoryPlugin] 协议日志查询器未启用，跳过长期记忆")
            return
        history_dicts, recent_dicts = await store.get_recent_completed_turns(
            session_id,
            current_turns=batch_turns,
            history_turns=MEMORY_HISTORY_HUMAN_MESSAGES,
            max_history_id=max_history_id,
        )
        recent_humans = sum(1 for message in recent_dicts if is_real_human_message(message))
        if recent_humans < batch_turns:
            logger.warning("[MemoryPlugin] 完整协议轮次不足，跳过本次长期记忆提取")
            return
        recent = messages_from_openai_format(recent_dicts)
        history = messages_from_openai_format(history_dicts)
        await mm.add(
            recent,
            history,
            enable_episodic=self.config.enable_episodic,
            enable_semantic=self.config.enable_semantic,
        )
