# -*- coding: utf-8 -*-
import json
import os
import re
from typing import Any, cast

from langchain.tools import tool
from openai import AsyncOpenAI

from app.agent.tools.screenshot import capture_screenshot_base64
from app.agent.utils.log import log_tool_call

DEFAULT_VISION_MODEL = "qwen3-vl-plus"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
SYSTEM_PROMPT = (
    "你是一个PC屏幕视觉分析助手。你的任务是根据用户的文字描述，"
    "在提供的截图中定位目标元素，并以JSON格式返回该元素的2D边界框。"
    "返回格式必须是 {\"bbox_2d\": [x1, y1, x2, y2]}，不要输出Markdown格式或其他内容。"
)


def _require_screen_dependencies():
    """Lazily import GUI dependencies so test/runtime without GUI can still import this module."""
    try:
        import pyautogui  # type: ignore
        from PIL import ImageGrab  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "缺少依赖，请安装: pip install pyautogui pillow"
        ) from exc
    return pyautogui, ImageGrab


def _read_screen_tool_config() -> tuple[str, str, str]:
    api_key = os.getenv("VLM_API_KEY")
    if not api_key:
        raise RuntimeError("缺少VLM_API_KEY环境变量。")

    base_url = os.getenv("VLM_BASE_URL", DEFAULT_BASE_URL).strip()
    model = os.getenv("VLM_MODEL", DEFAULT_VISION_MODEL).strip()
    return api_key.strip(), base_url, model


def _extract_bbox_from_response(content: Any) -> list[int]:
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )

    text = str(content or "").strip()
    if not text:
        raise ValueError("视觉模型未返回可解析内容。")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Some models prepend notes; extract the first JSON object for robustness.
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


def _clamp_bbox_to_image(bbox: list[int], image: Any) -> list[int]:
    width, height = image.size
    x1, y1, x2, y2 = bbox
    x1 = max(0, min(x1, width - 1))
    y1 = max(0, min(y1, height - 1))
    x2 = max(1, min(x2, width))
    y2 = max(1, min(y2, height))
    if x2 <= x1 or y2 <= y1:
        raise ValueError("bbox超出屏幕有效范围。")
    return [x1, y1, x2, y2]


@tool
@log_tool_call()
async def execute_screen(element_description: str) -> str:
    """基于当前屏幕截图识别并双击目标元素。

    Args:
        element_description: 要点击的元素描述，如"屏幕中间的确定按钮"、"网页上方的灰色搜索框"、"搜索结果页的第一个链接"。
    """
    if not element_description or not element_description.strip():
        return "错误: 缺少元素描述参数。"

    try:
        pyautogui, image_grab_cls = _require_screen_dependencies()
        api_key, base_url, model = _read_screen_tool_config()

        image_data, screenshot = capture_screenshot_base64(image_grab_cls)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
                    {"type": "text", "text": element_description.strip()},
                ],
            },
        ]

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        response = await client.chat.completions.create(
            model=model,
            messages=cast(Any, messages),
            stream=False,
            temperature=0,
        )

        content = ""
        if response.choices and response.choices[0].message:
            content = response.choices[0].message.content

        bbox = _extract_bbox_from_response(content)
        bbox = _clamp_bbox_to_image(bbox, screenshot)

        center_x = (bbox[0] + bbox[2]) // 2
        center_y = (bbox[1] + bbox[3]) // 2

        pyautogui.moveTo(center_x, center_y, duration=0.2)
        pyautogui.doubleClick()
        return f"成功点击了: {element_description.strip()}"
    except Exception as exc:
        return f"错误: 屏幕点击失败: {exc}"
