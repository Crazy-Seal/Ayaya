"""记忆管理器 - 核心调度层"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

from app.agent.memory.config import MemoryConfig
from app.agent.memory.memories.episodic import EpisodicMemory
from app.agent.memory.memories.semantic_mem0 import Mem0SemanticMemory
from app.agent.memory.memories.diary import DiaryMemory
from app.agent.memory.store.chat_history_store import ChatHistoryStore
from app.agent.state.messages import BaseMessage
from app.crud.chat_settings_dao import ChatSettingsDao
from app.schemas.chat_settings import MEMORY_DEFAULT_CONFIG, ChatSettings

logger = logging.getLogger(__name__)


class MemoryManager:
    """协调记忆提取与召回，各提取器自行构造输入并处理提取预算。

    Attributes:
        session_id (str): 共享 Ayaya 会话标识。
        config (MemoryConfig): 共享给提取器的记忆配置。
        memory_options (dict[str, Any]): 当前会话的记忆启用配置。
        chat_history_store (ChatHistoryStore | None): 领域聊天历史查询端口。
        episodic_memory (EpisodicMemory | None): 可选情景记忆提取与检索实例。
        chat_settings (ChatSettings): 会话模型和插件配置。
        diary_memory (DiaryMemory | None): 可选日记记忆实例。
        semantic_memory (Mem0SemanticMemory | None): 可选语义记忆提取与检索实例。
    """

    def __init__(
        self,
        session_id: str,
        config: MemoryConfig | None = None,
        chat_settings: ChatSettings | None = None,
    ):
        self.session_id = session_id
        self.config = config or MemoryConfig.from_env()
        if chat_settings:
            self.chat_settings = chat_settings
        else:
            self.chat_settings = ChatSettingsDao().get_chat_settings(session_id)
        self.memory_options = self._memory_options()

        needs_protocol_log = any(
            self.memory_options.get(key, True)
            for key in ("enable_diary", "enable_episodic", "enable_semantic")
        )
        self.chat_history_store = (
            ChatHistoryStore(
                timezone=self.config.timezone,
                day_boundary_hour=self.config.day_boundary_hour,
            )
            if needs_protocol_log
            else None
        )
        if self.memory_options.get("enable_diary", True):
            self.diary_memory = DiaryMemory(
                session_id,
                self.config,
                self.chat_settings,
                self.chat_history_store,
            )
        else:
            self.diary_memory = None
        self.episodic_memory = (
            EpisodicMemory(session_id, self.config, self.chat_settings)
            if self.memory_options.get("enable_episodic", True)
            else None
        )

        # 初始化语义记忆
        if not self.memory_options.get("enable_semantic", True):
            self.semantic_memory = None
        else:
            self.semantic_memory = Mem0SemanticMemory(
                session_id, self.config, self.chat_settings
            )

    # ==================== 核心方法 ====================

    async def add(
        self,
        messages: list[BaseMessage],
        history: list[BaseMessage] | None = None,
        *,
        enable_episodic: bool = True,
        enable_semantic: bool = True,
    ) -> dict[str, int]:
        """并行交付领域消息给启用的提取器，独立汇总情景和语义记忆数量。

        记忆插件提供已选定的当前批次与历史；本方法不格式化或估算子模块的
        输入。每个提取器使用自己的配置处理预算，一个提取器失败不阻止另一个。

        Args:
            messages (list[BaseMessage]): 当前批次的领域消息。
            history (list[BaseMessage] | None): 仅作为前情提要的历史消息。
            enable_episodic (bool): 本次是否执行情景记忆提取。
            enable_semantic (bool): 本次是否执行语义记忆提取。

        Returns:
            dict[str, int]: 两种提取器分别保存的记忆数量，禁用或失败时为零。
        """
        # 1. 固定本次交付列表；两个提取器分别接收列表副本，预算处理不影响另一方。
        messages = list(messages)
        history = list(history or [])

        async def process_episodic() -> int:
            """交付当前批次给情景提取器，失败时只将本项数量记为零。

            Returns:
                int: 本次保存的情景记忆数量。
            """
            if not enable_episodic or self.episodic_memory is None:
                return 0
            try:
                episodic_ids = await self.episodic_memory.add(list(messages), list(history))
                return len(episodic_ids)
            except Exception as e:
                logger.warning("[MemoryManager] 情景记忆处理失败: %s", e)
                return 0

        async def process_semantic() -> int:
            """交付当前批次给语义提取器，失败时只将本项数量记为零。

            Returns:
                int: 本次保存的语义记忆数量。
            """
            if not enable_semantic or self.semantic_memory is None:
                return 0
            try:
                semantic_ids = await self.semantic_memory.add(list(messages), list(history))
                return len(semantic_ids)
            except Exception as e:
                logger.warning("[MemoryManager] 语义记忆处理失败: %s", e)
                return 0

        # 2. 并行等待启用的提取器完成，保持两项结果和异常处理相互独立。
        episodic_count, semantic_count = await asyncio.gather(
            process_episodic(),
            process_semantic(),
        )

        return {"episodic": episodic_count, "semantic": semantic_count}

    async def get_protocol_history_cutoff(self) -> int:
        """返回当前协议日志高水位，0 表示尚无记录。"""
        if self.chat_history_store is None:
            return 0
        return await self.chat_history_store.get_latest_message_id(self.session_id)

    async def check_diary(self, *, max_history_id: int | None = None) -> None:
        """检查并生成上一有效聊天日的日记。"""
        now = datetime.now(self.config.timezone)
        today = self._get_effective_date(now)

        if self.diary_memory is not None:
            await self.diary_memory.check_and_generate(
                today,
                max_history_id=max_history_id,
            )

    async def has_diary_for_effective_date(self, effective_date: str) -> bool:
        """供上下文压缩通过窄端口检查日记覆盖。"""
        if self.diary_memory is None:
            return False
        return await self.diary_memory.has_diary(date.fromisoformat(effective_date))

    async def get_context(
        self,
        query: str = "",
        *,
        include_diary: bool = True,
        include_episodic: bool = True,
        include_semantic: bool = True,
    ) -> str:
        """获取当前对话上下文

        组装顺序：
        1. 最近日记
        2. 相关情景记忆（需要 query）
        3. 相关语义记忆

        Args:
            query: 查询文本，用于检索相关情景记忆

        Returns:
            格式化的上下文字符串
        """
        parts = []

        # 计算今天的有效日期
        now = datetime.now(self.config.timezone)
        today = self._get_effective_date(now)

        # 1. 日记部分
        if include_diary and self.diary_memory is not None:
            diary_context = await self.diary_memory.get_context(today)
            if diary_context:
                parts.append(f"[你的历史日记]\n{diary_context}\n")

        # 2. 情景记忆
        if include_episodic and self.episodic_memory is not None and query.strip():
            episodic_memories = await self.episodic_memory.search(query, self.config.episodic_top_k)
            if episodic_memories:
                memory_texts = [f"- {m.content}（{m.timestamp.strftime('%Y-%m-%d')}）" for m in episodic_memories]
                parts.append(f"[相关情景记忆]\n" + "\n".join(memory_texts) + "\n")

        # 3. 语义记忆
        if include_semantic and self.semantic_memory is not None and query.strip():
            try:
                semantic_memories = await self.semantic_memory.search(query, top_k=3)
                if semantic_memories:
                    memory_texts = []
                    for m in semantic_memories:
                        subject = m.metadata.get("subject", "")
                        relation = m.metadata.get("relation", "")
                        obj = m.metadata.get("obj", "")
                        if subject and relation and obj:
                            memory_texts.append(f"- {subject} {relation} {obj}")
                        else:
                            memory_texts.append(f"- {m.content}")
                    parts.append(f"[相关语义知识]\n" + "\n".join(memory_texts))
            except Exception as e:
                logger.warning("[MemoryManager] 语义记忆检索失败: %s", e)

        return "\n\n".join(parts) if parts else ""

    async def search(
        self,
        query: str,
        memory_type: str,
        top_k: int = 3,
    ) -> str:
        """搜索相关记忆

        Args:
            query: 查询文本
            memory_type: 记忆类型 (episodic/semantic/all)
            top_k: 返回数量

        Returns:
            格式化的记忆文本
        """
        results = []
        memory_options = self._memory_options()
        enable_episodic = bool(memory_options.get("enable_episodic", True))
        enable_semantic = bool(memory_options.get("enable_semantic", True))

        async def search_episodic():
            if (
                not enable_episodic
                or self.episodic_memory is None
                or memory_type not in ("episodic", "all")
            ):
                return []
            return await self.episodic_memory.search(query, top_k)

        async def search_semantic():
            if (
                not enable_semantic
                or self.semantic_memory is None
                or memory_type not in ("semantic", "all")
            ):
                return []
            try:
                return await self.semantic_memory.search(query, top_k)
            except Exception as e:
                logger.warning("[MemoryManager] 语义记忆检索失败: %s", e)
                return []

        # 并行检索
        episodic, semantic = await asyncio.gather(
            search_episodic(),
            search_semantic(),
        )

        results.extend([f"[情景记忆] {m.content}（{m.timestamp.strftime('%Y-%m-%d')}）" for m in episodic])
        results.extend([f"[语义记忆] {m.content}" for m in semantic])

        return "\n".join(results) if results else "未找到相关记忆"

    async def search_diary(self, start: date, end: date) -> str:
        """搜索时间范围内的日记

        Args:
            start: 开始日期
            end: 结束日期（最多间隔5天）

        Returns:
            日记内容

        Raises:
            ValueError: 时间范围超过5天
        """
        if self.diary_memory is None:
            return "日记记忆未启用"

        if (end - start).days > 5:
            raise ValueError("时间范围不能超过5天")

        diaries = await self.diary_memory.search_diary(start, end)
        if not diaries:
            return "该时间范围内没有日记"

        return "\n\n".join(
            f"【{d}】\n{content}"
            for d, content in diaries
        )

    # ==================== 日期相关方法 ====================

    def _get_effective_date(self, now: datetime) -> date:
        """获取有效日期

        凌晨 N 点前算前一天，N 点后算当天
        """
        if now.hour < self.config.day_boundary_hour:
            return (now - timedelta(days=1)).date()
        return now.date()

    def _memory_options(self) -> dict[str, Any]:
        memory = self.chat_settings.agent_plugins.get("memory")
        config = dict(MEMORY_DEFAULT_CONFIG)
        if memory:
            config.update(memory.config)
            if not memory.enabled:
                config["enable_diary"] = False
                config["enable_episodic"] = False
                config["enable_semantic"] = False
        return config

# ==================== 工厂函数 ====================

@lru_cache(maxsize=32)
def get_memory_manager(session_id: str) -> MemoryManager:
    """获取或创建 MemoryManager 实例（带缓存）。"""
    config = MemoryConfig.from_env()
    chat_settings = ChatSettingsDao().get_chat_settings(session_id)
    return MemoryManager(session_id, config, chat_settings)
