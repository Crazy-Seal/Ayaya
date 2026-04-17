"""Chroma 向量存储"""

from typing import Any


class ChromaStore:
    """Chroma 向量存储类

    负责：
    - 向量存储和检索
    - 支持元数据过滤
    - 按 namespace 隔离不同记忆类型
    """

    def __init__(
        self,
        collection_name: str,
        embedding_config: dict[str, Any],
        persist_path: str | None = None,
    ):
        self.collection_name = collection_name
        self.embedding_config = embedding_config
        self.persist_path = persist_path
        self._collection = None

    @property
    def collection(self):
        """延迟初始化 collection"""
        if self._collection is None:
            import chromadb
            if self.persist_path:
                client = chromadb.PersistentClient(path=self.persist_path)
            else:
                client = chromadb.Client()
            self._collection = client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    def _get_embedding(self, text: str) -> list[float]:
        """获取文本嵌入向量

        Args:
            text: 文本内容

        Returns:
            嵌入向量
        """
        raise NotImplementedError

    async def upsert(
        self,
        id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """插入或更新向量

        Args:
            id: 记录ID
            content: 文本内容
            metadata: 元数据（timestamp, importance, etc.）
        """
        raise NotImplementedError

    async def search(
        self,
        query: str,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """向量检索

        Args:
            query: 查询文本
            top_k: 返回数量
            where: 元数据过滤条件

        Returns:
            检索结果列表，每个元素包含 id, content, metadata, distance
        """
        raise NotImplementedError

    async def delete(self, id: str) -> None:
        """删除向量

        Args:
            id: 记录ID
        """
        raise NotImplementedError

    async def delete_by_metadata(self, where: dict[str, Any]) -> None:
        """按元数据删除

        Args:
            where: 元数据过滤条件
        """
        raise NotImplementedError
