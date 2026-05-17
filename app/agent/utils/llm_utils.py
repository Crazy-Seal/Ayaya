"""LLM 调用工具"""
import asyncio
import logging
from typing import Any

from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

# API 调用超时时间（秒）
API_TIMEOUT = 60
# API 调用最大重试次数
API_MAX_RETRIES = 3


async def ainvoke_with_retry(
    llm: BaseChatModel,
    inputs: Any,
) -> Any:
    """带超时重试的 LLM 调用

    Args:
        llm: LangChain LLM 实例（带 ainvoke 方法）
        inputs: 输入，可以是字符串或消息列表

    Returns:
        LLM 响应

    Raises:
        RuntimeError: 重试次数耗尽后仍失败
    """
    last_error = None
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            return await asyncio.wait_for(
                llm.ainvoke(inputs),
                timeout=API_TIMEOUT
            )
        except asyncio.TimeoutError:
            last_error = TimeoutError(f"LLM 调用超时（{API_TIMEOUT}秒）")
            if attempt < API_MAX_RETRIES:
                logger.warning(
                    "[LLM] 调用超时，进行第 %d/%d 次重试",
                    attempt + 1, API_MAX_RETRIES
                )
        except Exception as e:
            last_error = e
            if attempt < API_MAX_RETRIES:
                logger.warning(
                    "[LLM] 调用失败: %s，进行第 %d/%d 次重试",
                    e, attempt + 1, API_MAX_RETRIES
                )

    raise RuntimeError(f"LLM 调用失败，重试 {API_MAX_RETRIES} 次后仍失败: {last_error}")
