"""主模型上下文预算配置。"""

from pydantic import BaseModel, ConfigDict, Field


class ContextStrategyConfig(BaseModel):
    """上下文预算配置。"""

    model_config = ConfigDict(extra="forbid")

    compression_trigger_tokens_k: int = Field(default=100, ge=10)

    @property
    def compression_trigger_tokens(self) -> int:
        return self.compression_trigger_tokens_k * 1_000
