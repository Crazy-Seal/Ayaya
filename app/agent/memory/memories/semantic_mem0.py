"""语义记忆 - 基于 Mem0 的实现"""

import asyncio
import logging
import os
from datetime import datetime
from functools import lru_cache
from typing import Any

from langchain_core.messages import AnyMessage
from mem0 import Memory

from app.agent.memory.base import MemoryItem
from app.agent.memory.config import MemoryConfig
from app.schemas.chat_settings import ChatSettings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_cached_mem0(
    collection_name: str,
    qdrant_path: str,
    embedding_dimension: int,
    embedding_model: str,
    embedding_base_url: str,
    embedding_api_key: str,
) -> Memory:
    """获取缓存的 Mem0 实例（单例）

    Qdrant 本地模式不支持多个客户端实例同时访问同一个存储路径，
    因此需要缓存 Mem0 实例避免重复初始化。
    """
    mem0_config: dict[str, Any] = {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": collection_name,
                "path": qdrant_path,
                "embedding_model_dims": embedding_dimension,
            }
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": embedding_model,
                "embedding_dims": embedding_dimension,
                "openai_base_url": embedding_base_url,
                "api_key": embedding_api_key,
            }
        },
        "llm": {
            "provider": "openai",
            "config": {
                "model": os.getenv("MEM0_EXTRACTION_MODEL", "gpt-4o"),
                "openai_base_url": os.getenv("MEM0_EXTRACTION_BASE_URL", ""),
                "api_key": os.getenv("MEM0_EXTRACTION_API_KEY", ""),
                "temperature": 0.0,
            }
        },
        "custom_fact_extraction_prompt": MEM0_CUSTOM_INSTRUCTIONS,
        "version": "v1.1",
    }

    return Memory.from_config(mem0_config)


# Mem0 自定义提示词 - 中文第一人称视角
MEM0_CUSTOM_INSTRUCTIONS = """
请根据给定的对话历史记录和对话内容，以**AI助手的第一人称视角**，仅提取**语义记忆**。

**语义记忆**是指关于用户或AI助手的既定事实、偏好、属性、重要经历等信息。例如：
- 性别、生日、居住地等基本信息
- 职业、爱好、性格特点
- 社交关系、生活习惯、长期状态
- 重要经历、带来长期影响或留下难忘回忆的重大事件（如：用户在2005年去过北京旅行、我的记忆系统在2026年5月1日被开发完成）
- 长期约定（如：用户约定永远不会丢下我不管）

## 命名规范：使用AI助手视角的**第一人称**
- 提到用户时，统一使用"用户"
- 提到AI助手时，统一使用"我"

## 提取要求
- **语言表达**：使用流畅、简练的中文，不要添加“我知道”“我推断”等前缀，要像写笔记一样记录记忆条目内容。不同内容需要分成不同的记忆条目。
- **实事求是**：没有能提取的记忆就是没有，有几条就返回几条。

## 禁止事项
- 禁止提取问候语、日常情绪表达、稀松平常的日常事件（例如"用户昨晚和朋友吃了火锅""用户在x年x月x日熬夜写代码"）；
- 禁止提取非长期有效的信息，如短期状态（例如"最近用户在做xxx"）等；
- 禁止提取对未来事件的预估；
- 禁止强行推断（例如"用户今天去了公园"无法推断出"用户喜欢去公园"），禁止歪曲意思，禁止忽略关键信息。
- 描述事件时禁止使用"最近""今天"等模糊时间表达，如有需要，请使用具体时间。

## 前情提要
- **注意**：标有[前情提要]的对话是之前的对话，仅作为上下文补充使用，无需提取其中的信息
"""


class Mem0SemanticMemory:
    """基于 Mem0 的语义记忆实现

    职责：
    - 存储抽象知识和概念
    - Mem0 自动提取事实
    - 向量检索（Qdrant）

    存储架构：
    - Qdrant：向量存储（Mem0 默认）
    """

    def __init__(
        self,
        session_id: str,
        config: MemoryConfig,
        chat_settings: ChatSettings,
    ):
        self.session_id = session_id
        self.config = config
        self.chat_settings = chat_settings
        self._memory = self._init_mem0()

    def _init_mem0(self) -> Memory:
        """初始化 Mem0 实例（使用缓存）"""
        return _get_cached_mem0(
            collection_name=self.config.mem0_collection_name,
            qdrant_path=self.config.mem0_qdrant_path,
            embedding_dimension=self.config.embedding_dimension,
            embedding_model=self.config.embedding_model,
            embedding_base_url=self.config.embedding_base_url,
            embedding_api_key=self.config.embedding_api_key,
        )

    # ==================== 核心方法 ====================

    async def add(
        self,
        messages: list[AnyMessage],
        history: list[AnyMessage] | None = None,
    ) -> list[str]:
        """添加语义记忆

        将聊天记录交由 Mem0 提取语义知识。
        前情提要会在消息前添加，作为上下文补充。

        Args:
            messages: 当前聊天记录（LangGraph 消息格式）
            history: 历史聊天记录（前情提要，标记后作为上下文传入）

        Returns:
            记忆 ID 列表
        """
        if not messages:
            return []

        # 转换历史消息（带前情提要标记）
        mem0_history = self._convert_messages(history, mark_as_history=True) if history else []

        # 转换当前消息
        mem0_messages = self._convert_messages(messages)
        if not mem0_messages:
            return []

        # 组合：前情提要 + 当前消息
        combined_messages = mem0_history + mem0_messages

        try:
            # Mem0 同步调用，使用 asyncio.to_thread 避免阻塞
            result = await asyncio.to_thread(
                self._memory.add, combined_messages, agent_id=self.session_id
            )

            memory_ids = []
            if result and result.get("results"):
                for item in result["results"]:
                    memory_id = item.get("id")
                    if memory_id:
                        memory_ids.append(memory_id)

            logger.info(
                "[Mem0SemanticMemory] 处理 %d 条消息（前情提要 %d 条），提取 %d 条记忆, session_id=%s",
                len(combined_messages), len(mem0_history), len(memory_ids), self.session_id
            )
            return memory_ids

        except Exception as e:
            logger.warning("[Mem0SemanticMemory] 添加失败: %s", e)
            return []

    async def search(self, query: str, top_k: int = 3) -> list[MemoryItem]:
        """搜索相关记忆

        Args:
            query: 查询文本
            top_k: 返回数量

        Returns:
            相关记忆列表
        """
        if not query.strip():
            return []

        try:
            # 直接传 agent_id 参数隔离不同会话
            results = await asyncio.to_thread(
                self._memory.search,
                query,
                agent_id=self.session_id,
                limit=top_k
            )

            if not results or not results.get("results"):
                return []

            memory_items = []
            for item in results["results"]:
                memory_id = item.get("id", "")
                memory_text = item.get("memory", "")
                score = item.get("score", 0.5)

                # 获取创建时间
                created_at_str = item.get("created_at") or ""
                try:
                    created_at = datetime.fromisoformat(
                        created_at_str.replace("Z", "+00:00")
                    ) if created_at_str else datetime.now()
                except ValueError:
                    created_at = datetime.now()

                memory_items.append(MemoryItem(
                    id=memory_id,
                    session_id=self.session_id,
                    content=memory_text,
                    timestamp=created_at,  # Mem0 没有事件时间，使用创建时间
                    created_at=created_at,
                    importance=0.5,  # Mem0 不提供重要性评分
                    metadata={"score": score},
                ))

            return memory_items

        except Exception as e:
            logger.warning("[Mem0SemanticMemory] 搜索失败: %s", e)
            return []

    async def get_all(self) -> list[dict]:
        """获取所有记忆

        Returns:
            记忆列表
        """
        try:
            # 直接传 agent_id 参数隔离不同会话
            results = await asyncio.to_thread(
                self._memory.get_all, agent_id=self.session_id
            )

            if not results or not results.get("results"):
                return []

            return results["results"]

        except Exception as e:
            logger.warning("[Mem0SemanticMemory] 获取所有记忆失败: %s", e)
            return []

    async def clear(self) -> None:
        """清除所有记忆"""
        try:
            # Mem0 同步调用，使用 asyncio.to_thread 避免阻塞
            await asyncio.to_thread(self._memory.delete_all, agent_id=self.session_id)
            logger.info("[Mem0SemanticMemory] 清除所有记忆, session_id=%s", self.session_id)
        except Exception as e:
            logger.warning("[Mem0SemanticMemory] 清除记忆失败: %s", e)

    # ==================== 工具方法 ====================

    def _convert_messages(
        self,
        messages: list[AnyMessage],
        mark_as_history: bool = False,
    ) -> list[dict]:
        """转换 LangGraph 消息为 Mem0 格式

        Args:
            messages: LangGraph 消息列表
            mark_as_history: 是否标记为前情提要

        Returns:
            Mem0 消息列表 [{"role": "user/assistant", "content": "..."}]
        """
        mem0_messages = []

        for msg in messages:
            # 确定角色
            if msg.type == "human":
                role = "user"
            elif msg.type == "ai":
                role = "assistant"
            else:
                # 跳过 system、tool 等其他类型消息
                continue

            # 提取内容
            content = msg.content
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                # 处理多模态消息，提取文本部分
                text_parts = []
                for part in content:
                    if isinstance(part, str):
                        text_parts.append(part)
                    elif isinstance(part, dict) and part.get("type") == "text":
                        text_val = part.get("text")
                        if text_val:
                            text_parts.append(text_val)
                text = " ".join(text_parts)
            else:
                text = str(content)

            if text.strip():
                # 如果是前情提要，添加标记
                if mark_as_history:
                    text = f"[前情提要] {text.strip()}"
                mem0_messages.append({"role": role, "content": text.strip()})

        return mem0_messages
