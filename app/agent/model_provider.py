from functools import lru_cache

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.schemas.chat_settings import ChatSettings


# HTTP 请求超时时间（秒）
HTTP_TIMEOUT = 60
# 最大重试次数
MAX_RETRIES = 3


@lru_cache(maxsize=1)
def get_model(chat_settings: ChatSettings) -> ChatOpenAI:
    """按会话配置创建并缓存主对话模型实例。"""
    # 模型构建集中在 provider，避免在多个模块重复初始化参数。
    # timeout 和 max_retries 在 HTTP 层面生效，能正确关闭连接，避免流式数据混乱。
    return ChatOpenAI(
        model=chat_settings.model_name,
        base_url=chat_settings.openai_base_url,
        api_key=SecretStr(chat_settings.openai_api_key),
        temperature=chat_settings.temperature,
        streaming=True,
        timeout=HTTP_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
