"""主模型上下文预算配置。"""

from pydantic import BaseModel, ConfigDict, Field


class ContextStrategyConfig(BaseModel):
    """上下文预算配置。"""

    model_config = ConfigDict(extra="forbid")

    compression_trigger_tokens_k: int = Field(default=100, ge=10)
    max_screenshots_in_context: int = Field(default=2, ge=0)
    screenshot_ttl_human_messages: int = Field(default=2, ge=0)

    @property
    def compression_trigger_tokens(self) -> int:
        """把会话配置中的千 token 单位转换为预算判断使用的整数 token 数。

        Returns:
            int: 压缩触发阈值对应的 token 数。
        """
        return self.compression_trigger_tokens_k * 1_000
