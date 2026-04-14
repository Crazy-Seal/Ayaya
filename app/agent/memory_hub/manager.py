from __future__ import annotations

import uuid
from typing import cast

from langchain_core.messages import AnyMessage

from app.agent.memory_hub.base import BaseMemory, MemoryItem
from app.agent.memory_hub.config import MemoryConfig, memory_config_from_chat_settings
from app.agent.memory_hub.memory_types import (
    EpisodicMemory,
    MultimodalMemory,
    SemanticMemory,
    ShortTermMemory,
)
from app.agent.memory_hub.storage.adapters import get_short_term_adapter
from app.agent.memory_hub.summarizers import (
    summarize_episodic_memory_items_async,
    summarize_semantic_items_async,
    summarize_short_memory_async,
)
from app.agent.memory_hub.text_utils import extract_multimodal_signals
from app.agent.memory_hub.types import MemoryContext
from app.schemas.chat_settings import ChatSettings


class MemoryManager:
    """记忆核心层：统一管理各记忆类型实例并编排读写。"""

    _SHORT_TERM_TYPE = "short_term"
    _EPISODIC_TYPE = "episodic"
    _SEMANTIC_TYPE = "semantic"
    _MULTIMODAL_TYPE = "multimodal"

    _TYPE_FACTORIES = {
        "short_term_default": ShortTermMemory,
        "episodic_default": EpisodicMemory,
        "semantic_default": SemanticMemory,
        "multimodal_default": MultimodalMemory,
    }

    def __init__(
        self,
        user_id: str,
        config: MemoryConfig,
        memories: list[BaseMemory] | None = None,
    ):
        self.user_id = user_id
        self.config = config
        self.memories = memories if memories is not None else self._build_memories(user_id, config)

    def _build_memories(self, user_id: str, config: MemoryConfig) -> list[BaseMemory]:
        plugin_ids = config.memory_plugins or ["short_term_default", "episodic_default"]

        # 对 plugin_ids 去重但保持顺序
        deduped: list[str] = []
        seen: set[str] = set()
        for plugin_id in plugin_ids:
            if plugin_id in seen:
                continue
            seen.add(plugin_id)
            deduped.append(plugin_id)

        memories: list[BaseMemory] = []
        for plugin_id in deduped:
            factory = self._TYPE_FACTORIES.get(plugin_id)
            if factory is None:
                raise RuntimeError(f"Unknown memory plugin: {plugin_id}")
            memories.append(factory(user_id, config))
        return memories

    def _ensure_user_memory(self, memory: BaseMemory) -> BaseMemory:
        if getattr(memory, "user_id", self.user_id) == self.user_id:
            return memory
        return memory.__class__(self.user_id, self.config)

    async def recall(
        self,
        messages: list[object],
        query_text: str,
        top_k: int = 3,
    ) -> MemoryContext:
        entries: list[MemoryItem] = []
        short_memory = ""
        normalized_messages = list(messages)

        for memory in self.memories:
            memory = self._ensure_user_memory(memory)
            items = await memory.retrieve(
                query_text,
                limit=top_k,
                messages=normalized_messages,
            )
            for item in items:
                entries.append(item)
                if item.memory_type == self._SHORT_TERM_TYPE and not short_memory:
                    short_memory = item.content

        return MemoryContext(short_memory=short_memory, entries=entries)

    async def persist(self, messages: list[object]) -> None:
        normalized_messages = list(messages)
        message_list = cast(list[AnyMessage], normalized_messages)
        short_term_adapter = get_short_term_adapter()

        for memory in self.memories:
            memory = self._ensure_user_memory(memory)
            memory_type = str(getattr(memory, "memory_type", ""))

            if memory_type == self._SHORT_TERM_TYPE:
                previous = await short_term_adapter.get_latest_async(self.user_id)
                short_text = await summarize_short_memory_async(self.config, message_list, previous)
                if short_text and short_text != previous:
                    await memory.add(
                        MemoryItem(
                            id=str(uuid.uuid4()),
                            user_id=self.user_id,
                            memory_type=memory_type,
                            content=short_text,
                        )
                    )
                continue

            if memory_type == self._EPISODIC_TYPE:
                short_text = await short_term_adapter.get_latest_async(self.user_id)
                for text in await summarize_episodic_memory_items_async(self.config, message_list, short_text):
                    await memory.add(
                        MemoryItem(
                            id=str(uuid.uuid4()),
                            user_id=self.user_id,
                            memory_type=memory_type,
                            content=text,
                        )
                    )
                continue

            if memory_type == self._SEMANTIC_TYPE:
                for text in await summarize_semantic_items_async(self.config, message_list):
                    await memory.add(
                        MemoryItem(
                            id=str(uuid.uuid4()),
                            user_id=self.user_id,
                            memory_type=memory_type,
                            content=text,
                        )
                    )
                continue

            if memory_type == self._MULTIMODAL_TYPE:
                for text in extract_multimodal_signals(message_list):
                    await memory.add(
                        MemoryItem(
                            id=str(uuid.uuid4()),
                            user_id=self.user_id,
                            memory_type=memory_type,
                            content=text,
                        )
                    )
                continue




def build_default_memory_manager(chat_settings: ChatSettings) -> MemoryManager:
    memory_config = memory_config_from_chat_settings(chat_settings)
    return MemoryManager(user_id=chat_settings.session_id, config=memory_config)


