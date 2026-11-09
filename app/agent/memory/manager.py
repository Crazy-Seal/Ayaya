"""记忆管理器 - 核心调度层"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

from app.agent.memory.base import MemoryExtractionError
from app.agent.memory.config import MemoryConfig
from app.agent.memory.memories.episodic import EpisodicMemory
from app.agent.memory.memories.semantic_mem0 import Mem0SemanticMemory
from app.agent.memory.memories.diary import DiaryGeneration, DiaryMemory
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
        """读取当前会话记忆配置，仅装配启用的日记、情景和语义记忆；全部禁用时不创建历史查询对象。

        Args:
            session_id (str): 当前操作所属的会话编号。
            config (MemoryConfig | None): 调用方提供的配置。
            chat_settings (ChatSettings | None): 当前会话的模型、角色及插件设置。
        """
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
        """并行交付领域消息给启用的提取器，汇总数量并报告部分或全部失败。

        记忆插件提供已选定的当前批次与历史；本方法不格式化或估算子模块的
        输入。每个提取器使用自己的配置处理预算，一个提取器失败不阻止另一个。

        Args:
            messages (list[BaseMessage]): 当前批次的领域消息。
            history (list[BaseMessage] | None): 仅作为前情提要的历史消息。
            enable_episodic (bool): 本次是否执行情景记忆提取。
            enable_semantic (bool): 本次是否执行语义记忆提取。

        Returns:
            dict[str, int]: 情景记忆保存数量和语义记忆处理数量，禁用项为零。

        Raises:
            RuntimeError: 至少一类提取失败，摘要包含两类的完成数量和失败原因。
        """
        # 1. 固定本次交付列表；两个提取器分别接收列表副本，预算处理不影响另一方。
        messages = list(messages)
        history = list(history or [])

        async def process_episodic() -> tuple[int, str | None]:
            """交付当前批次给情景提取器，保留已保存数量和失败原因。

            Returns:
                tuple[int, str | None]: 本次保存数量和可选错误摘要。
            """
            if not enable_episodic or self.episodic_memory is None:
                return 0, None
            try:
                episodic_ids = await self.episodic_memory.add(list(messages), list(history))
                return len(episodic_ids), None
            except MemoryExtractionError as e:
                return e.saved_count, str(e)
            except Exception as e:
                logger.warning("[MemoryManager] 情景记忆处理失败: %s", e)
                return 0, str(e)

        async def process_semantic() -> tuple[int, str | None]:
            """交付当前批次给语义提取器，区分正常空结果和提取失败。

            Returns:
                tuple[int, str | None]: 本次处理数量和可选错误摘要。
            """
            if not enable_semantic or self.semantic_memory is None:
                return 0, None
            try:
                semantic_ids = await self.semantic_memory.add(list(messages), list(history))
                return len(semantic_ids), None
            except Exception as e:
                logger.warning("[MemoryManager] 语义记忆处理失败: %s", e)
                return 0, str(e)

        # 2. 并行等待启用的提取器完成，保持两项结果和异常处理相互独立。
        episodic_result, semantic_result = await asyncio.gather(
            process_episodic(),
            process_semantic(),
        )

        episodic_count, episodic_error = episodic_result
        semantic_count, semantic_error = semantic_result
        # 3. 一类失败仍保留另一类的工作结果；错误交给任务框架保存为失败并展示完整摘要。
        if episodic_error is not None or semantic_error is not None:
            details = []
            for label, verb, count, error, enabled in (
                ("情景记忆", "已保存", episodic_count, episodic_error,
                 enable_episodic and self.episodic_memory is not None),
                ("语义记忆", "处理", semantic_count, semantic_error,
                 enable_semantic and self.semantic_memory is not None),
            ):
                text = f"{verb} {count} 条" if enabled else "未启用"
                if error is not None:
                    text += f"，失败：{error}"
                details.append(f"{label}：{text}")
            enabled_count = int(enable_episodic and self.episodic_memory is not None) + int(
                enable_semantic and self.semantic_memory is not None
            )
            error_count = int(episodic_error is not None) + int(semantic_error is not None)
            status = "部分失败" if error_count < enabled_count else "失败"
            raise RuntimeError(f"长期记忆提取{status}；" + "；".join(details))

        return {"episodic": episodic_count, "semantic": semantic_count}

    async def get_protocol_history_cutoff(self) -> int:
        """返回当前协议日志高水位，0 表示尚无记录。"""
        if self.chat_history_store is None:
            return 0
        return await self.chat_history_store.get_latest_message_id(self.session_id)

    async def prepare_diary(self, *, max_history_id: int | None = None) -> DiaryGeneration | None:
        """新输入保存后检查已结束聊天日，为确实需要生成的日记准备固定材料。

        Args:
            max_history_id (int | None): 本次提取开始前捕获的协议日志最大编号；排除之后到达的消息。

        Returns:
            DiaryGeneration | None: 已持有处理中标记的材料，禁用或无需生成时为空。
        """
        now = datetime.now(self.config.timezone)
        today = self._get_effective_date(now)

        if self.diary_memory is not None:
            return await self.diary_memory.prepare_generation(
                today,
                max_history_id=max_history_id,
            )

    async def generate_diary(self, generation: DiaryGeneration) -> str:
        """后台任务使用准备好的日期和对话生成日记，保存成功后返回面板摘要。

        Args:
            generation (DiaryGeneration): 提交任务前读取的日记日期、对话和历史范围。

        Returns:
            str: 包含日记日期的成功摘要。

        Raises:
            RuntimeError: 日记记忆不可用。
            Exception: 模型生成或日记保存失败，由后台任务记录失败原因。
        """
        if self.diary_memory is None:
            raise RuntimeError("日记记忆未启用，无法生成日记")
        await self.diary_memory.generate(
            generation.target_date, generation.messages, max_history_id=generation.max_history_id,
        )
        return f"日记生成成功：{generation.target_date.isoformat()}，已保存 1 篇日记。"

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
            query (str): 查询文本，用于检索相关情景记忆
            include_diary (bool): 本次背景是否加入最近日记。
            include_episodic (bool): 本次是否检索并加入情景记忆。
            include_semantic (bool): 本次是否检索并加入语义记忆。

        Returns:
            str: 格式化的上下文字符串
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
            query (str): 查询文本
            memory_type (str): 记忆类型 (episodic/semantic/all)
            top_k (int): 返回数量

        Returns:
            str: 格式化的记忆文本
        """
        results = []
        memory_options = self._memory_options()
        enable_episodic = bool(memory_options.get("enable_episodic", True))
        enable_semantic = bool(memory_options.get("enable_semantic", True))

        async def search_episodic():
            """由本次并行检索调用，只在情景记忆启用且请求类型匹配时查询。

            Returns:
                list[MemoryEntry]: 匹配的情景记忆列表，禁用或类型不匹配时为空。
            """
            if (
                not enable_episodic
                or self.episodic_memory is None
                or memory_type not in ("episodic", "all")
            ):
                return []
            return await self.episodic_memory.search(query, top_k)

        async def search_semantic():
            """由本次并行检索调用，语义检索异常时记录日志并返回空列表，不阻止其他结果。

            Returns:
                list[MemoryEntry]: 匹配的语义记忆列表，禁用、不匹配或检索失败时为空。
            """
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
        """查询包含起止日期的日记，按日期从早到晚返回前五篇并提示剩余篇数。

        Args:
            start (date): 查询范围的开始日期。
            end (date): 查询范围的结束日期，包含当天。

        Returns:
            str: 最多五篇带日期的日记及剩余篇数，或日记未启用、范围内没有日记的说明。
        """
        if self.diary_memory is None:
            return "日记记忆未启用"

        diaries = await self.diary_memory.search_diary(start, end)
        if not diaries:
            return "该时间范围内没有日记"

        result = "\n\n".join(
            f"【{d}】\n{content}"
            for d, content in diaries[:5]
        )
        if len(diaries) > 5:
            result += f"\n\n【后面还有 {len(diaries) - 5} 篇日记未显示】"
        return result

    # ==================== 日期相关方法 ====================

    def _get_effective_date(self, now: datetime) -> date:
        """获取有效日期

        凌晨 N 点前算前一天，N 点后算当天

        Args:
            now (datetime): 按记忆配置时区表示的当前时间。
        """
        if now.hour < self.config.day_boundary_hour:
            return (now - timedelta(days=1)).date()
        return now.date()

    def _memory_options(self) -> dict[str, Any]:
        """将记忆默认配置与会话插件配置合并，整个插件禁用时关闭三种记忆能力。

        Returns:
            dict[str, Any]: 本次使用的日记、情景和语义记忆开关及配置副本。
        """
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
    """获取或创建 MemoryManager 实例（带缓存）。

    Args:
        session_id (str): 当前操作所属的会话编号。
    """
    config = MemoryConfig.from_env()
    chat_settings = ChatSettingsDao().get_chat_settings(session_id)
    return MemoryManager(session_id, config, chat_settings)
