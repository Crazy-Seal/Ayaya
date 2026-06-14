"""VLM 服务 - 屏幕定位与图片描述的可复用能力。

为未来的屏幕操纵工具（control_screen）预留：
- locate(intent, image_data_url) -> bbox：让 VLM 在截图中定位元素并返回 2D 边界框
- describe(images, context) -> str：图片描述（复用 utils.image_description）

GUI 依赖（pyautogui/PIL）由调用方延迟导入，本模块保持可在无 GUI 环境导入。
"""

import json
import logging
import os
import re
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

DEFAULT_VISION_MODEL = "qwen3-vl-plus"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_LOCATE_SYSTEM_PROMPT = (
    "你是一个PC屏幕视觉分析助手。你的任务是根据用户的文字描述，"
    "在提供的截图中定位目标元素，并以JSON格式返回该元素的2D边界框。"
    "返回格式必须是 {\"bbox_2d\": [x1, y1, x2, y2]}，不要输出Markdown格式或其他内容。"
)


def _vlm_config() -> tuple[str, str, str]:
    api_key = os.getenv("VLM_API_KEY")
    if not api_key:
        raise RuntimeError("缺少VLM_API_KEY环境变量。")
    base_url = os.getenv("VLM_BASE_URL", DEFAULT_BASE_URL).strip()
    model = os.getenv("VLM_MODEL", DEFAULT_VISION_MODEL).strip()
    return api_key.strip(), base_url, model


def extract_bbox(content: Any) -> list[int]:
    """从 VLM 文本响应中解析 bbox_2d [x1,y1,x2,y2]（容错 JSON 提取）。"""
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
    """把 bbox 夹到图像范围内。"""
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
            intent: 元素描述，如"屏幕中间的确定按钮"
            image_data_url: 完整 data URL（data:image/...;base64,...）
        """
        api_key, base_url, model = _vlm_config()
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _LOCATE_SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                        {"type": "text", "text": intent.strip()},
                    ]},
                ],
                stream=False,
                temperature=0,
            )
            content = ""
            if response.choices and response.choices[0].message:
                content = response.choices[0].message.content
            return extract_bbox(content)
        finally:
            await client.close()
