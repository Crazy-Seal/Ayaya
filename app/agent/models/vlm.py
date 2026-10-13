"""VLM 屏幕定位服务。

- VLMService.locate(intent, image_data_url) -> bbox：在截图中定位元素并返回 2D 边界框
"""

import json
import logging
import os
import re
from typing import Any

from app.agent.models.llm_client import LLMClient, LLMConfig
from app.agent.state.messages import SystemMessage, HumanMessage, ImageContent, TextContent

logger = logging.getLogger(__name__)

VLM_DEFAULT_MODEL = "qwen3-vl-plus"
VLM_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

_LOCATE_SYSTEM_PROMPT = (
    "你是一个PC屏幕视觉分析助手。你的任务是根据用户的文字描述，"
    "在提供的截图中定位目标元素，并以JSON格式返回该元素的2D边界框。"
    "返回格式必须是 {\"bbox_2d\": [x1, y1, x2, y2]}，不要输出Markdown格式或其他内容。"
)


def _vlm_config() -> tuple[str, str, str]:
    """读取截图定位模型的环境配置，缺少凭据时立即拒绝调用。

    Returns:
        tuple[str, str, str]: API 凭据、服务地址和模型名称。

    Raises:
        RuntimeError: 未配置 VLM_API_KEY。
    """
    api_key = os.getenv("VLM_API_KEY")
    if not api_key:
        raise RuntimeError("缺少VLM_API_KEY环境变量。")
    base_url = os.getenv("VLM_BASE_URL", VLM_DEFAULT_BASE_URL).strip()
    model = os.getenv("VLM_MODEL", VLM_DEFAULT_MODEL).strip()
    return api_key.strip(), base_url, model


def extract_bbox(content: Any) -> list[int]:
    """从 VLM 文本响应中解析 bbox_2d [x1,y1,x2,y2]（容错 JSON 提取）。

    Args:
        content (Any): 当前处理的正文。
    """
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    text = str(content or "").strip()
    if not text:
        raise ValueError("视觉模型未返回可解析内容。")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"{[\s\S]*}", text)
        if not match:
            raise ValueError(f"模型返回不是JSON: {text[:200]}")
        parsed = json.loads(match.group(0))

    bbox = parsed.get("bbox_2d") if isinstance(parsed, dict) else None
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError(f"缺少合法bbox_2d字段: {parsed}")
    try:
        x1, y1, x2, y2 = [int(float(v)) for v in bbox]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"bbox_2d坐标不是数字: {bbox}") from exc
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"bbox_2d范围无效: {bbox}")
    return [x1, y1, x2, y2]


def clamp_bbox(bbox: list[int], width: int, height: int) -> list[int]:
    """把 bbox 夹到图像范围内。

    Args:
        bbox (list[int]): 归一化画布中的 xyxy 边界框。
        width (int): 截图宽度，用于坐标范围判断。
        height (int): 截图高度，用于坐标范围判断。
    """
    x1, y1, x2, y2 = bbox
    x1 = max(0, min(x1, width - 1))
    y1 = max(0, min(y1, height - 1))
    x2 = max(1, min(x2, width))
    y2 = max(1, min(y2, height))
    if x2 <= x1 or y2 <= y1:
        raise ValueError("bbox超出屏幕有效范围。")
    return [x1, y1, x2, y2]


class VLMService:
    """视觉模型服务。无状态，按需读取 VLM_* 环境变量。"""

    async def locate(self, intent: str, image_data_url: str) -> list[int]:
        """在截图中定位元素，返回 bbox_2d。

        Args:
            intent (str): 元素描述，如"屏幕中间的确定按钮"
            image_data_url (str): 完整 data URL（data:image/...;base64,...）
        """
        api_key, base_url, model = _vlm_config()
        # 1. 保留原 SDK 默认超时与重试，只统一模型调用接口。
        client = LLMClient(LLMConfig(model=model, api_key=api_key, base_url=base_url,
                                     temperature=0, timeout=600, max_retries=2))
        try:
            response = await client.ainvoke([
                SystemMessage(text=_LOCATE_SYSTEM_PROMPT),
                HumanMessage(content=(ImageContent(image_data_url), TextContent(intent.strip()))),
            ])
            return extract_bbox(response.text)
        finally:
            await client.close()
