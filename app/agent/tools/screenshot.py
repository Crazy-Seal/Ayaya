# -*- coding: utf-8 -*-
import base64
import io
from typing import Any

from langchain.tools import tool

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


@tool
@log_tool_call()
def screenshot() -> str:
    """截图工具：执行后返回截图是否成功。截图会在下一个HumanMessage中以系统消息的形式携带。"""
    try:
        capture_screenshot_base64()
        return "截图成功"
    except Exception:
        return "截图失败"

