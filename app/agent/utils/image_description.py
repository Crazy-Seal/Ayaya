"""图片描述生成服务"""

import logging
import os
from typing import Any, cast

from openai import AsyncOpenAI

from app.agent.utils.image_utils import ImageTaskResult, save_multiple_images

logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_VISION_MODEL = "qwen3-vl-plus"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _get_vision_config() -> tuple[str, str, str]:
    """获取视觉模型配置。

    复用 VLM_* 环境变量配置。

    Returns:
        (api_key, base_url, model)
    """
    api_key = os.getenv("VLM_API_KEY")
    if not api_key:
        raise ValueError("缺少 VLM_API_KEY 环境变量")

    base_url = os.getenv("VLM_BASE_URL", DEFAULT_BASE_URL).strip()
    model = os.getenv("VLM_MODEL", DEFAULT_VISION_MODEL).strip()
    return api_key.strip(), base_url, model


async def generate_multiple_image_descriptions(
    images: list[str],
    context: str = "",
    max_length: int = 200,
) -> ImageTaskResult:
    """使用视觉模型生成多张图片描述，并保存图片到磁盘。

    Args:
        images: 图片数据列表，可以是完整 data URL 或纯 base64
        context: 用户消息文本，帮助理解图片上下文
        max_length: 描述最大长度（字数）

    Returns:
        ImageTaskResult 包含描述字符串和文件名列表
    """
    filenames: list[str] = []

    try:
        # 保存所有图片到磁盘
        filenames = save_multiple_images(images)
        if not filenames:
            return ImageTaskResult(description="图片", filenames=[])

        api_key, base_url, model = _get_vision_config()

        client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=3)

        # 构建多图消息内容
        content: list[dict] = []

        # 添加所有图片
        for image_data in images:
            # 如果已经是完整的 data URL，直接使用；否则添加前缀
            if image_data.startswith("data:image"):
                image_url = image_data
            else:
                image_url = f"data:image/png;base64,{image_data}"
            content.append({"type": "image_url", "image_url": {"url": image_url}})

        # 添加文本提示
        prompt = ("你是一个视觉信息总结专家，擅长总结提取图片包含的主要内容，并用一段流畅的中文进行表达。\n"
                  "这些图片是从用户和AI助手的聊天记录片段中截取出来的，与之对应的用户文字输入也会被提供给你，请重点关注用户强调的部分的信息（如有）。\n"
                  "如有多张图片，请按顺序描述每张图片，格式例如：\"共有3张图片。第1张图片......。第2张图片......。第3张图片......。\"等。\n"
                  "注意：你的结果不应包含任何图片内容以外的信息。如图片中含有大量文字，无需详细提取，大体总结即可")
        if context:
            prompt += f"\n对应的用户文字输入：{context}"
        prompt += f"\n请开始总结图片包含的内容（{max_length}字以内）："
        content.append({"type": "text", "text": prompt})

        messages = [
            {
                "role": "user",
                "content": content,
            },
        ]

        response = await client.chat.completions.create(
            model=model,
            messages=cast(Any, messages),
            stream=False,
            temperature=0.1,
        )

        if response.choices and response.choices[0].message:
            description = response.choices[0].message.content or ""
            # 截断过长的描述
            if len(description) > max_length:
                description = description[:max_length] + "..."
            logger.info("[ImageDescription] 生成图片描述: %s", description)
            return ImageTaskResult(description=description.strip(), filenames=filenames)

        return ImageTaskResult(description="图片", filenames=filenames)

    except Exception as e:
        logger.warning("[ImageDescription] 图片描述生成失败: %s", e)
        return ImageTaskResult(description="图片", filenames=filenames)
