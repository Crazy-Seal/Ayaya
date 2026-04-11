from functools import lru_cache

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.schemas.chat_settings import ChatSettings


@lru_cache
def get_model(chat_settings: ChatSettings) -> ChatOpenAI:
    """Build and cache the base chat model by session settings."""
    # 模型构建集中在 provider，避免在多个模块重复初始化参数。
    return ChatOpenAI(
        model=chat_settings.model_name,
        base_url=chat_settings.openai_base_url,
        api_key=SecretStr(chat_settings.openai_api_key),
        temperature=chat_settings.temperature,
        streaming=True,
    )
