import os
from functools import lru_cache

from app.agent.memory_hub.embedding.interfaces import EmbeddingProvider
from app.agent.memory_hub.embedding.providers import OpenAICompatibleEmbeddingProvider


@lru_cache(maxsize=1)
def get_embedding_provider() -> EmbeddingProvider:
    """按配置返回嵌入服务实现，默认 openai_compatible。"""
    provider_id = (os.getenv("EMBEDDING_PROVIDER") or "openai_compatible").strip().lower()
    if provider_id == "openai_compatible":
        return OpenAICompatibleEmbeddingProvider()
    raise RuntimeError(f"Unknown embedding provider: {provider_id}")

