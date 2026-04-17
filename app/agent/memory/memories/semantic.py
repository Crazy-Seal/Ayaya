"""语义记忆 - 存储抽象知识，支持实体关系推理"""

from app.agent.memory.base import Entity, MemoryItem, Relation
from app.agent.memory.config import MemoryConfig
from app.agent.memory.store.chroma_store import ChromaStore
from app.agent.memory.store.neo4j_store import Neo4jStore


class SemanticMemory:
    """语义记忆类

    职责：
    - 存储抽象知识和概念
    - 实体关系抽取（spaCy）
    - 混合检索（向量 + 图融合）
    """

    NAMESPACE = "semantic_memory"

    def __init__(self, session_id: str, config: MemoryConfig):
        self.session_id = session_id
        self.config = config
        self.vector_store = ChromaStore(
            collection_name=self.NAMESPACE,
            embedding_config={
                "api_key": config.embedding_api_key,
                "model": config.embedding_model,
                "dimension": config.embedding_dimension,
                "base_url": config.embedding_base_url,
            }
        )
        self.graph_store = Neo4jStore(
            uri=config.neo4j_uri,
            user=config.neo4j_user,
            password=config.neo4j_password,
        )

    async def add(self, messages: str, history: str) -> str:
        """添加语义记忆

        将聊天记录交由大模型总结，生成记忆条目，
        同时抽取实体/关系保存至图数据库

        Args:
            messages: 当前聊天记录
            history: 历史聊天记录

        Returns:
            记忆ID
        """
        raise NotImplementedError

    async def search(self, query: str, top_k: int = 3) -> list[MemoryItem]:
        """混合检索相关记忆

        综合使用向量数据库和图数据库检索

        Args:
            query: 查询文本
            top_k: 返回数量

        Returns:
            相关记忆列表
        """
        raise NotImplementedError

    def _extract_entities(self, content: str) -> list[Entity]:
        """使用 spaCy NER 抽取实体

        Args:
            content: 文本内容

        Returns:
            实体列表
        """
        raise NotImplementedError

    def _extract_relations(self, content: str, entities: list[Entity]) -> list[Relation]:
        """抽取实体间关系

        Args:
            content: 文本内容
            entities: 已抽取的实体列表

        Returns:
            关系列表
        """
        raise NotImplementedError

    async def _vector_search(self, query: str, top_k: int) -> list[MemoryItem]:
        """向量检索

        Args:
            query: 查询文本
            top_k: 返回数量

        Returns:
            相关记忆列表
        """
        raise NotImplementedError

    async def _graph_search(self, query: str, top_k: int) -> list[dict]:
        """图检索

        Args:
            query: 查询文本
            top_k: 返回数量

        Returns:
            相关记忆列表
        """
        raise NotImplementedError

    def _combine_and_rank(
        self,
        vector_results: list[MemoryItem],
        graph_results: list[dict],
        top_k: int,
    ) -> list[MemoryItem]:
        """融合排序

        向量检索(0.7) + 图检索(0.3) 融合评分

        Args:
            vector_results: 向量检索结果
            graph_results: 图检索结果
            top_k: 返回数量

        Returns:
            排序后的记忆列表
        """
        raise NotImplementedError
