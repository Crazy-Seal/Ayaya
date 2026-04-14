from typing import Protocol

from langchain_core.embeddings import Embeddings


class EmbeddingProvider(Protocol):
    """嵌入服务层接口。"""

    def build(self) -> Embeddings:
        ...

