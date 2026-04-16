import logging
from typing import Any

from langchain_core.messages import SystemMessage

from app.agent.memory_hub.manager import MemoryManager
from app.agent.memory_hub.text_utils import get_last_human_text
from app.agent.state import (
    AgentState,
    RECENT_CONTEXT_HUMAN_MESSAGES,
    SUMMARY_EVERY_HUMAN_MESSAGES,
)
from app.agent.utils.background_tasks import create_background_task
from app.agent.utils.messages import normalize_messages_for_model
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
        if state.memory_text is None or state.short_memory is None:
            last_user_text = get_last_human_text(recent_messages)

            memory_context = await self.memory_manager.recall(
                messages=recent_messages,
                query_text=last_user_text,
                top_k=3,
            )
            state.short_memory = memory_context.short_memory
            state.memory_text = memory_context.merged_text

        # 动态拼装系统提示词：基础 prompt + 短期记忆 + 检索到的长期记忆。
        system_prompt = f"{self.chat_settings.system_prompt}\n\n以下文本是你的记忆，其中，[摘要记忆]是你对前段时间和当前对话的记忆，[长期记忆]是系统根据主人输入检索到的，你记忆的更早之前的事情。"
        if state.short_memory:
            system_prompt = f"{system_prompt}\n\n[摘要记忆]\n{state.short_memory}"
        if state.memory_text:
            system_prompt = f"{system_prompt}\n\n[长期记忆]\n{state.memory_text}"

        messages = [SystemMessage(content=system_prompt)] + recent_messages
        response = await self.model.ainvoke(messages)
        return {
            "messages": [response],
            "short_memory": state.short_memory,
            "memory_text": state.memory_text,
        }


class MemoryFinalizeNode:
    def __init__(self, chat_settings: ChatSettings, memory_manager: MemoryManager):
        self.chat_settings = chat_settings
        self.memory_manager = memory_manager

    async def __call__(self, state: AgentState) -> dict[str, int]:
        # 通过计数器控制总结频率，避免每轮都触发记忆归纳。
        next_counter = state.summary_counter + 1
        if next_counter < SUMMARY_EVERY_HUMAN_MESSAGES:
            return {"summary_counter": next_counter}

        # 满足阈值后改为后台提交，不阻塞本轮响应返回。
        create_background_task(
            self.memory_manager.persist(list(state.messages)),
            logger=logger,
            task_name="memory_finalize.persist",
        )
        return {"summary_counter": 0}
