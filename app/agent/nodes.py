from typing import Any

from langchain_core.messages import SystemMessage

from app.agent.memory.memory import (
    enqueue_memory_finalize_task,
    get_last_human_text,
    get_latest_short_memory,
    get_store,
)
from app.agent.state import (
    AgentState,
    RECENT_CONTEXT_HUMAN_MESSAGES,
    SUMMARY_EVERY_HUMAN_MESSAGES,
)
from app.agent.utils.messages import normalize_messages_for_model
from app.agent.utils.work_memory import slice_recent_messages_by_human
from app.schemas.chat_settings import ChatSettings


class ChatNode:
    def __init__(self, model: Any, chat_settings: ChatSettings):
        self.model = model
        self.chat_settings = chat_settings

    def __call__(self, state: AgentState) -> dict[str, Any]:
        # 先裁剪上下文窗口，再做消息格式清洗，控制 token 并避免格式异常。
        recent_messages = slice_recent_messages_by_human(
            state.messages,
            max_human_messages=RECENT_CONTEXT_HUMAN_MESSAGES,
        )
        recent_messages = normalize_messages_for_model(recent_messages)

        # 本轮首次进入 chatbot 才检索长期记忆，避免工具回环阶段重复查询。
        if state.memory_text is None:
            store = get_store()
            namespace = ("long_mem", state.session_id)
            recall_query = get_last_human_text(recent_messages)
            memories = store.search(namespace, query=recall_query, limit=3)
            state.memory_text = "\n".join(
                item.value.get("text", "")
                for item in memories
                if isinstance(item.value, dict)
            )

        # 短期记忆同样只在本轮首次加载，后续节点复用缓存。
        if state.short_memory is None:
            state.short_memory = get_latest_short_memory(state.session_id)

        # 动态拼装系统提示词：基础 prompt + 短期记忆 + 检索到的长期记忆。
        system_prompt = self.chat_settings.system_prompt
        if state.short_memory:
            system_prompt = f"{system_prompt}\n\n[之前对话的短期记忆摘要]\n{state.short_memory}"
        if state.memory_text:
            system_prompt = f"{system_prompt}\n\n[检索到的相关长期记忆]\n{state.memory_text}"

        messages = [SystemMessage(content=system_prompt)] + recent_messages
        response = self.model.invoke(messages)
        return {
            "messages": [response],
            "short_memory": state.short_memory,
            "memory_text": state.memory_text,
        }


class MemoryFinalizeNode:
    def __init__(self, chat_settings: ChatSettings):
        self.chat_settings = chat_settings

    def __call__(self, state: AgentState) -> dict[str, int]:
        # 通过计数器控制总结频率，避免每轮都触发记忆归纳。
        next_counter = state.summary_counter + 1
        if next_counter < SUMMARY_EVERY_HUMAN_MESSAGES:
            return {"summary_counter": next_counter}

        # 满足阈值后异步投递记忆收尾任务，不阻塞主回复链路。
        enqueue_memory_finalize_task(self.chat_settings, list(state.messages))
        return {"summary_counter": 0}
