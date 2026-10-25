"""语义记忆 - 基于 Mem0 的实现"""

from app.agent.utils.infra.owned_work import complete_owned_work

import asyncio
import logging
import os
from datetime import datetime
from functools import lru_cache
from typing import Any

from mem0 import Memory

from app.schemas.runtime_types import (
    DESKTOP_CHANNEL,
)
from app.agent.memory.base import MemoryItem
from app.agent.memory.config import MemoryConfig
from app.agent.memory.materials import compress_tool_results
from app.agent.memory.memories.mem0_llm_compat import (
    MEM0_FACT_EXTRACTION_MARKER,
    Mem0FactExtractionLLMAdapter,
)
from app.agent.state.messages import (
    notification_text, text_content, format_tool_arguments,
    AssistantMessage,
    BaseMessage,
    HumanMessage,
    RuntimeInputMessage,
    ToolMessage,
)
from app.agent.state.time import render_timed_text
from app.agent.utils.domain.token_estimation import RoughTokenEstimator
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
    """按模型、向量及存储配置复用 Mem0 实例，初始化后接入事实提取响应兼容层。

    Args:
        collection_name (str): 当前会话独立使用的 Chroma 集合名称。
        qdrant_path (str): 当前环境的 Mem0/Qdrant 持久目录。
        embedding_dimension (int): 向量模型输出维数。
        embedding_model (str): 该记录使用的向量模型名称。
        embedding_base_url (str): 向量模型兼容接口地址。
        embedding_api_key (str): 向量模型调用凭据，仅用于请求认证。
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

    memory = Memory.from_config(mem0_config)
    memory.llm = Mem0FactExtractionLLMAdapter(memory.llm)
    return memory


# Mem0 自定义提示词
MEM0_CUSTOM_INSTRUCTIONS = MEM0_FACT_EXTRACTION_MARKER + """
请根据给定的对话历史记录和对话内容，以**AI助手的第一人称视角**，仅提取**语义记忆**。

**语义记忆**是指**关于用户或AI助手的**既定事实、偏好、属性、重要经历等信息。包括：
- 性别、生日、居住地等基本信息
- 职业、爱好、性格特点
- 社交关系、生活习惯、长期状态
- 重要经历、带来长期影响或留下难忘回忆的重大事件
- 长期约定
你只需要提取以上信息。

## 命名规范：使用AI助手视角的**第一人称**
- 提到用户时，统一使用"用户"
- 提到AI助手时，统一使用"我"

## 提取要求
- **语言表达**：使用流畅、简练的中文，不要添加"我知道""我推断"等前缀，要像写笔记一样记录记忆条目内容。不同内容需要分成不同的记忆条目。
- **实事求是**：没有能提取的记忆就是没有，有几条就返回几条。
- **长期有效**：你提取的信息必须是长期有效的信息，仅短期内有效的信息无需提取

## 禁止事项
- 禁止提取工作过程中的细节，如代码逻辑、统计数据、文献列表、游戏操作过程、屏幕点击坐标等；
- 禁止提取问候语、日常情绪表达、无意义信息、短期事件；
- 禁止提取非长期有效的信息，如短期状态等；
- 禁止提取对未来事件的预估；
- 禁止强行推断，禁止歪曲意思。
- 描述事件时禁止使用"最近""今天""下个月""今年"等模糊时间表达，如有需要，请使用具体时间。
- 工具调用参数与结果只用于理解对话上下文，不是用户陈述，禁止单独提取为记忆。

## 前情提要
- **注意**：标有[前情提要]的对话是之前的对话，仅作为上下文补充使用，无需提取其中的信息

## 输出格式
- 只返回合法 JSON 对象，不要添加解释、Markdown 或代码块。
- 顶层字段必须命名为 `facts`，其值必须是字符串数组，不得使用 `memories` 等其他字段名。
- 有记忆时严格返回：{"facts": ["记忆1", "记忆2"]}
- 没有可提取记忆时严格返回：{"facts": []}
"""


class Mem0SemanticMemory:
    """拥有语义提取材料转换和预算检查，交给 Mem0 保存并检索记忆。

    Attributes:
        session_id (str): 共享 Ayaya 会话标识。
        config (MemoryConfig): 当前 Agent 配置。
        chat_settings (ChatSettings): 会话模型和插件配置。
        _memory (Mem0 Memory): Mem0 客户端实例，保存语义记忆。
    """

    def __init__(
        self,
        session_id: str,
        config: MemoryConfig,
        chat_settings: ChatSettings,
    ):
        """绑定会话和角色设置，初始化 Mem0 的语义记忆存储及兼容的事实提取客户端。

        Args:
            session_id (str): 当前操作所属的会话编号。
            config (MemoryConfig): 调用方提供的配置。
            chat_settings (ChatSettings): 当前会话的模型、角色及插件设置。
        """
        self.session_id = session_id
        self.config = config
        self.chat_settings = chat_settings
        self._memory = self._init_mem0()

    def _init_mem0(self) -> Memory:
        """按当前记忆配置取得共享 Mem0 实例，会话范围由后续记忆操作指定。"""
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
        messages: list[BaseMessage],
        history: list[BaseMessage] | None = None,
    ) -> list[str]:
        """转换领域对话并处理提取预算，再交给 Mem0 提取语义记忆。

        使用自身配置粗估自定义提示词和已转换材料；超限时只复制并压缩当前
        工具结果，历史和共享原始消息保持不变，沿用一次压缩的现有策略。

        Args:
            messages (list[BaseMessage]): 当前批次的领域消息。
            history (list[BaseMessage] | None): 仅作前情提要、不压缩的历史消息。

        Returns:
            list[str]: Mem0 返回的记忆编号，空材料或外部提取失败时为空。
        """
        if not messages:
            return []

        logger.info("[Mem0SemanticMemory] 开始提取语义记忆")

        # 1. 转换历史和当前材料，保留历史标记、时间和渠道来源，空材料不请求提取。
        mem0_history = self._convert_messages(history, mark_as_history=True) if history else []

        mem0_messages = self._convert_messages(messages)
        if not mem0_messages:
            return []

        # 2. 沿用自定义提示词加转换材料的粗估口径，不模拟 Mem0 内部额外请求。
        limit = self.config.extraction_max_context_tokens
        if limit:
            raw = RoughTokenEstimator().estimate_value(
                MEM0_CUSTOM_INSTRUCTIONS + str(mem0_messages) + str(mem0_history)
            )
            if raw > limit:
                # 2.1. 只重新转换压缩后的当前工具结果，历史仍使用刚构造的原材料。
                mem0_messages = self._convert_messages(compress_tool_results(messages))
                logger.warning("[Mem0SemanticMemory] 语义记忆粗略估算超限，工具结果改为800+200权重")
        # 3. 将实际构造的材料按历史在前的顺序交给 Mem0，不再由上层重建输入。
        combined_messages = mem0_history + mem0_messages

        try:
            result = await complete_owned_work(asyncio.to_thread(
                self._memory.add, combined_messages, agent_id=self.session_id
            ))

            # 4. 沿用 Mem0 返回的结果编号，供协调者汇总本项提取数量。
            memory_ids = []
            if result and result.get("results"):
                for item in result["results"]:
                    memory_id = item.get("id")
                    if memory_id:
                        memory_ids.append(memory_id)

            logger.info(
                "[Mem0SemanticMemory] 添加记忆完成，处理 %d 条消息（前情提要 %d 条），提取 %d 条记忆, session_id=%s",
                len(combined_messages), len(mem0_history), len(memory_ids), self.session_id
            )
            return memory_ids

        except Exception as e:
            logger.warning("[Mem0SemanticMemory] 添加失败: %s", e)
            return []

    async def search(self, query: str, top_k: int = 3) -> list[MemoryItem]:
        """按当前会话范围检索相关语义记忆，并转换为项目记忆条目。

        Args:
            query (str): 用户或工具提供的检索文本。
            top_k (int): 最多检索返回的记忆数量。
        """
        if not query.strip():
            return []

        try:
            results = await complete_owned_work(asyncio.to_thread(
                self._memory.search,
                query,
                agent_id=self.session_id,
                limit=top_k
            ))

            if not results or not results.get("results"):
                return []

            memory_items = []
            for item in results["results"]:
                memory_id = item.get("id", "")
                memory_text = item.get("memory", "")
                score = item.get("score", 0.5)

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
                    timestamp=created_at,
                    created_at=created_at,
                    importance=0.5,
                    metadata={"score": score},
                ))

            return memory_items

        except Exception as e:
            logger.warning("[Mem0SemanticMemory] 搜索失败: %s", e)
            return []

    async def get_all(self) -> list[dict]:
        """读取当前会话已有语义记忆，不修改其他会话记录。"""
        try:
            results = await complete_owned_work(asyncio.to_thread(
                self._memory.get_all, agent_id=self.session_id
            ))

            if not results or not results.get("results"):
                return []

            return results["results"]

        except Exception as e:
            logger.warning("[Mem0SemanticMemory] 获取所有记忆失败: %s", e)
            return []

    async def clear(self) -> None:
        """删除当前会话语义记忆，其他会话的记录不在删除范围内。"""
        try:
            await complete_owned_work(asyncio.to_thread(self._memory.delete_all, agent_id=self.session_id))
            logger.info("[Mem0SemanticMemory] 清除所有记忆, session_id=%s", self.session_id)
        except Exception as e:
            logger.warning("[Mem0SemanticMemory] 清除记忆失败: %s", e)

    # ==================== 工具方法 ====================

    def _convert_messages(
        self,
        messages: list[BaseMessage],
        mark_as_history: bool = False,
    ) -> list[dict]:
        """将领域消息转换为 Mem0 输入，在正文中保留非桌面渠道来源。

        Args:
            messages (list[BaseMessage]): 需要转换或交付的领域消息集合。
            mark_as_history (bool): 是否将材料标记为历史回忆而非当前对话。

        Returns:
            list[dict]: 按角色、来源和工具事实整理的语义记忆提取材料。
        """
        mem0_messages = []

        # 1. 给非桌面输入和回复补来源正文标签，转换后的 role 保持 Mem0 契约。
        for msg in messages:
            if isinstance(msg, ToolMessage) and not msg.text:
                continue
            if isinstance(msg, HumanMessage):
                role = "user"
                text = msg.text if msg.origin_channel == DESKTOP_CHANNEL else f"[来自 {msg.origin_channel}] {msg.text}"
            elif isinstance(msg, AssistantMessage):
                role = "assistant"
                text = msg.text if msg.origin_channel == DESKTOP_CHANNEL else f"[回复 {msg.origin_channel}] {msg.text}"
            elif isinstance(msg, RuntimeInputMessage):
                role = "assistant"
                text = f"运行时输入：{notification_text(msg)}"
            elif isinstance(msg, ToolMessage):
                if mark_as_history:
                    continue
                role = "assistant"
                text = msg.text
            else:
                continue

            if text.strip() or isinstance(msg, AssistantMessage) and msg.tool_calls:
                tool_calls = list(msg.tool_calls) if isinstance(msg, AssistantMessage) else []
                if tool_calls:
                    if mark_as_history:
                        text = "\n".join(f"AI：调用了工具 {call.name}" for call in tool_calls)
                    else:
                        text = "\n".join(
                            f"AI：调用工具 {call.name}，参数："
                            f"{format_tool_arguments(call)}"
                            for call in tool_calls
                        )
                elif isinstance(msg, ToolMessage):
                    text = f"工具结果：{text.strip()}"
                else:
                    text = render_timed_text(text.strip(), msg.created_at)
                if mark_as_history:
                    text = f"[前情提要] {text}"
                mem0_messages.append({"role": role, "content": text})

        return mem0_messages
