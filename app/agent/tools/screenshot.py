# -*- coding: utf-8 -*-
"""截屏工具 - 使用 interrupt 等待用户确认"""
import base64
import io
import uuid
from typing import Any

from langchain.tools import tool
from langgraph.types import interrupt

from app.agent.utils.log import log_tool_call


def _require_image_grab_dependency():
    """Lazy import to avoid hard GUI dependency at module import time."""
    try:
        from PIL import ImageGrab  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少依赖，请安装: pip install pillow") from exc
    return ImageGrab


def capture_screenshot_base64(image_grab_cls: Any | None = None) -> tuple[str, Any]:
    """Capture the current screen and return base64 data with original image object."""
    grab_cls = image_grab_cls or _require_image_grab_dependency()
    screenshot = grab_cls.grab()
    buffer = io.BytesIO()
    screenshot.save(buffer, format="JPEG")
    image_data = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return image_data, screenshot


# 截屏成功返回值的前缀
SCREENSHOT_SUCCESS_PREFIX = "SCREENSHOT_SUCCESS:"


@tool
@log_tool_call()
def screenshot() -> str:
    """向用户请求截屏。用户确认后系统会携带截图继续对话。"""
    request_id = str(uuid.uuid4())

    # 暂停执行，等待用户确认
    user_response = interrupt({
        "type": "screenshot_request",
        "request_id": request_id,
        "message": "Agent 请求截取屏幕，是否允许？"
    })

    # 用户拒绝
    if not user_response.get("approved"):
        return "截屏请求被用户阻止"

    # 用户允许，执行截屏
    try:
        image_data, _ = capture_screenshot_base64()
        # 返回成功标记 + 图片数据，由 ScreenshotNode 处理
        return f"{SCREENSHOT_SUCCESS_PREFIX}{image_data}"
    except Exception as e:
        return f"截屏失败: {e}"
