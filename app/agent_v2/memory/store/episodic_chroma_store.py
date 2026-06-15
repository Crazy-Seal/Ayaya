"""Chroma 向量存储"""

import asyncio
import logging
from typing import Any

import chromadb

from app.agent_v2.models.embedding_client import EmbeddingClient, EmbeddingConfig

logger = logging.getLogger(__name__)


class EpisodicChromaStore:
    """情景记忆 Chroma 向量存储类

    负责：
    - 向量存储和检索
    - 支持元数据过滤
    - 按 collection 隔离不同记忆类型

    注意：
    - Chroma PersistentClient 是同步的，使用 asyncio.to_thread 包装
    - dimensions 参数只对 text-embedding-3 及以后的模型有效
    - Collection 和 PersistentClient 线程安全，但跨进程会有问题
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

        # 初始化 Embedding 客户端
        self._embedding_client = EmbeddingClient(EmbeddingConfig(
            api_key=embedding_config["api_key"],
            model=embedding_config["model"],
            base_url=embedding_config.get("base_url", "https://api.openai.com/v1"),
            dimension=embedding_config.get("dimension"),
        ))

        # 同步初始化 Chroma 客户端
        if self.persist_path:
            self._client = chromadb.PersistentClient(path=self.persist_path)
        else:
            logger.warning("[ChromaStore] 未指定Chroma持久化路径，将使用内存存储")
            self._client = chromadb.Client()

        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    # ==================== 核心方法 ====================

    async def upsert(
        self,
        record_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """插入或更新向量

        Args:
            record_id: 记录ID
            content: 文本内容
            metadata: 元数据（timestamp, importance, session_id, etc.）
        """
        embedding = await self._embedding_client.embed_documents([content])

        await asyncio.to_thread(
            self._collection.upsert,
            ids=[record_id],
            embeddings=[embedding[0]],
            documents=[content],
            metadatas=[metadata or {}],
        )

        logger.debug("[ChromaStore] upsert: record_id=%s", record_id)

    async def search(
        self,
        query: str,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """向量检索

        Args:
            query: 查询文本（空字符串则只做元数据过滤）
            top_k: 返回数量
            where: 元数据过滤条件

        Returns:
            检索结果列表，每个元素包含 id, content, metadata, distance
        """
        if query:
            query_embedding = await self._embedding_client.embed_documents([query])
            results = await asyncio.to_thread(
                self._collection.query,
                query_embeddings=[query_embedding[0]],
                n_results=top_k,
                where=where,
            )
            # query 返回嵌套列表
            ids = results["ids"][0] if results.get("ids") else []
            documents = results["documents"][0] if results.get("documents") else []
            metadatas = results["metadatas"][0] if results.get("metadatas") else []
            distances = results["distances"][0] if results.get("distances") else []
        else:
            results = await asyncio.to_thread(
                self._collection.get,
                where=where,
                limit=top_k,
            )
            # get 返回平铺列表
            ids = results.get("ids", [])
            documents = results.get("documents", [])
            metadatas = results.get("metadatas", [])
            distances = [None] * len(ids)

        # 转换结果格式
        items = []
        for i, id_ in enumerate(ids):
            items.append({
                "id": id_,
                "content": documents[i] if i < len(documents) else "",
                "metadata": metadatas[i] if i < len(metadatas) else {},
                "distance": distances[i] if i < len(distances) else None,
            })

        return items

    async def delete(self, record_id: str) -> None:
        """删除向量

        Args:
            record_id: 记录ID
        """
        await asyncio.to_thread(self._collection.delete, ids=[record_id])

    async def delete_by_metadata(self, where: dict[str, Any]) -> int:
        """按元数据删除

        Args:
            where: 元数据过滤条件

        Returns:
            删除的数量
        """
        result = await asyncio.to_thread(self._collection.delete, where=where)
        return len(result.get("ids", []))

    async def count(self) -> int:
        """获取记录数量

        Returns:
            记录数量
        """
        return await asyncio.to_thread(self._collection.count)

    async def get(self, record_id: str) -> dict[str, Any] | None:
        """根据 ID 获取单条记录

        Args:
            record_id: 记录ID

        Returns:
            记录数据，不存在则返回 None
        """
        result = await asyncio.to_thread(self._collection.get, ids=[record_id])

        if result and result.get("ids"):
            return {
                "id": result["ids"][0],
                "content": result["documents"][0] if result.get("documents") else "",
                "metadata": result["metadatas"][0] if result.get("metadatas") else {},
            }
        return None

    async def close(self) -> None:
        """关闭连接"""
        await self._embedding_client.close()
