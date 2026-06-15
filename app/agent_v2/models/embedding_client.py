"""Embedding API 客户端"""

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingConfig:
    """Embedding 配置"""
    api_key: str
    model: str = "text-embedding-3-small"
    base_url: str = "https://api.openai.com/v1"
    dimension: int | None = None  # 仅对 text-embedding-3 系列有效
    timeout: float = 30.0


class EmbeddingClient:
    """Embedding API 客户端"""

    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            timeout=config.timeout,
        )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量文本向量化

        Args:
            texts: 文本列表

        Returns:
            向量列表
        """
        if not texts:
            return []

        payload: dict[str, Any] = {
            "model": self.config.model,
            "input": texts,
        }
        if self.config.dimension:
            payload["dimensions"] = self.config.dimension

        try:
            response = await self._client.post("/embeddings", json=payload)
            response.raise_for_status()
            data = response.json()

            # 按 index 排序返回
            embeddings = [None] * len(texts)
            for item in data["data"]:
                embeddings[item["index"]] = item["embedding"]

            return embeddings

        except Exception as e:
            logger.exception("[EmbeddingClient] 向量化失败: %s", e)
            raise

    async def embed_query(self, text: str) -> list[float]:
        """单个查询向量化

        Args:
            text: 查询文本

        Returns:
            向量
        """
        embeddings = await self.embed_documents([text])
        return embeddings[0]

    async def close(self) -> None:
        """关闭连接"""
        await self._client.aclose()
