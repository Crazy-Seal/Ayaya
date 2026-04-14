import os

from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr

from app.agent.memory_hub.embedding.interfaces import EmbeddingProvider


def _require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Environment variable not found: {name}")
    return value


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """默认 OpenAI 兼容嵌入提供器。"""

    def build(self) -> OpenAIEmbeddings:
        return OpenAIEmbeddings(
            model=_require_env("EMBEDDING_MODEL"),
            api_key=SecretStr(_require_env("EMBEDDING_API_KEY")),
            base_url=_require_env("EMBEDDING_BASE_URL"),
            check_embedding_ctx_length=False,
            tiktoken_enabled=False,
        )

