"""情景记忆 - 存储事件性记忆，支持向量检索"""

from datetime import datetime

from app.agent.memory.base import MemoryItem
from app.agent.memory.config import MemoryConfig
from app.agent.memory.store.chroma_store import ChromaStore


class EpisodicMemory:
    """情景记忆类

    职责：
    - 存储具体事件记忆
    - LLM 提取时间元数据和重要性
    - 支持向量检索
    """

    NAMESPACE = "episodic_memory"

    def __init__(self, session_id: str, config: MemoryConfig):
        self.session_id = session_id
        self.config = config
        self.store = ChromaStore(
            collection_name=self.NAMESPACE,
            embedding_config={
                "api_key": config.embedding_api_key,
                "model": config.embedding_model,
                "dimension": config.embedding_dimension,
                "base_url": config.embedding_base_url,
            }
        )

    async def add(self, messages: str, history: str) -> str:
        """添加情景记忆

        将聊天记录交由大模型总结，生成记忆条目（含时间元数据和重要性）

        Args:
            messages: 当前聊天记录
            history: 历史聊天记录（前情提要）

        Returns:
            记忆ID
        """
        raise NotImplementedError

    async def search(self, query: str, top_k: int = 3) -> list[MemoryItem]:
        """向量检索相关记忆

        Args:
            query: 查询文本
            top_k: 返回数量

        Returns:
            相关记忆列表
        """
        raise NotImplementedError

    async def get_timeline(self, start: datetime, end: datetime) -> list[MemoryItem]:
        """时间线查询

        Args:
            start: 开始时间
            end: 结束时间

        Returns:
            时间范围内的记忆列表
        """
        raise NotImplementedError
