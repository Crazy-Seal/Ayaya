import logging
from typing import Any

from langchain_core.messages import SystemMessage

from app.agent.memory.manager import MemoryManager
from app.agent.state import (
    AgentState,
    RECENT_CONTEXT_HUMAN_MESSAGES,
    SUMMARY_EVERY_HUMAN_MESSAGES,
)
from app.agent.utils.background_tasks import create_background_task
from app.agent.utils.llm_utils import ainvoke_with_retry
from app.agent.utils.messages import normalize_messages_for_model
from app.agent.utils.text_utils import extract_text, get_last_human_text, split_context
from app.agent.utils.work_memory import slice_recent_messages_by_human
from app.schemas.chat_settings import ChatSettings

logger = logging.getLogger(__name__)


class ChatNode:
    def __init__(self, model: Any, chat_settings: ChatSettings, memory_manager: MemoryManager):
        self.model = model
        self.chat_settings = chat_settings
        self.memory_manager = memory_manager

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        # 先裁剪上下文窗口，再做消息格式清洗，控制 token 并避免格式异常。
        recent_messages = slice_recent_messages_by_human(
            state.messages,
            max_human_messages=RECENT_CONTEXT_HUMAN_MESSAGES,
        )
        recent_messages = normalize_messages_for_model(recent_messages)

        # 本轮首次进入 chatbot 才检索记忆，避免工具回环阶段重复查询。
        if state.memory_text is None:
            last_user_text = get_last_human_text(recent_messages)

            # 新系统：get_context() 返回已格式化的上下文，直接使用
            state.memory_text = await self.memory_manager.get_context(query=last_user_text)

        # 动态拼装系统提示词：基础 prompt + 记忆上下文
        system_prompt = self.chat_settings.system_prompt
        if state.memory_text:
            system_prompt = f"{system_prompt}\n\n以下文本是你的记忆，其中，[你的历史日记和摘要]是你对前段时间和当前对话的记忆，[相关情景记忆]和[相关语义知识]是系统根据用户输入检索到的，你记忆的更早之前的事情。\n\n{state.memory_text}"

        messages = [SystemMessage(content=system_prompt)] + recent_messages
        response = await ainvoke_with_retry(self.model, messages)
        return {
            "messages": [response],
            "memory_text": state.memory_text,
        }


class MemoryFinalizeNode:
    def __init__(self, chat_settings: ChatSettings, memory_manager: MemoryManager):
        self.chat_settings = chat_settings
        self.memory_manager = memory_manager

    async def __call__(self, state: AgentState) -> dict[str, int]:
        # 通过计数器控制记忆提取频率
        next_counter = state.summary_counter + 1

        # try_summary 每轮都触发（保存对话 + 检查摘要/日记）
        create_background_task(
            self._try_summary(list(state.messages)),
            logger=logger,
            task_name="memory_finalize.try_summary",
        )

        # add() 每 10 轮触发一次（情景记忆 + 语义记忆提取）
        if next_counter >= SUMMARY_EVERY_HUMAN_MESSAGES:
            create_background_task(
                self._persist_memory(list(state.messages)),
                logger=logger,
                task_name="memory_finalize.persist",
            )
            return {"summary_counter": 0}

        return {"summary_counter": next_counter}

    async def _try_summary(self, messages: list[Any]) -> None:
        """每轮触发：保存对话并检查摘要/日记"""
        last_human = get_last_human_text(messages)
        last_ai = None
        for msg in reversed(messages):
            if msg.type == "ai":
                last_ai = extract_text(msg.content)
                break

        if last_human and last_ai:
            await self.memory_manager.try_summary(last_human, last_ai)

    async def _persist_memory(self, messages: list[Any]) -> None:
        """每 10 轮触发：提取情景记忆和语义记忆"""
        # 使用 split_context 提取前情提要和待提取消息
        history_messages, recent_messages = split_context(
            messages,
            later_human_count=10,      # 10对待提取
            previous_human_count=5,    # 5对前情提要
        )

        # 添加情景记忆和语义记忆
        await self.memory_manager.add(recent_messages, history_messages)
