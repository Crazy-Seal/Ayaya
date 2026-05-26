# -*- coding: utf-8 -*-
"""截屏工具 - 使用 interrupt 等待用户确认，接收前端传来的截图数据"""
import logging
import uuid

from langchain.tools import tool
from langgraph.types import interrupt

from app.agent.utils.log import log_tool_call


logger = logging.getLogger(__name__)

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

    # 获取前端传来的截图数据
    screenshot_data = user_response.get("screenshot_data")
    if not screenshot_data:
        return "截屏失败: 未收到截图数据"

    # 记录尺寸信息
    width = user_response.get("width")
    height = user_response.get("height")
    if width and height:
        logger.info("[screenshot] 收到截图: %dx%d, 数据长度: %d", width, height, len(screenshot_data))

    # screenshot_data 应为完整的 data URL 格式，如 "data:image/png;base64,xxx"
    # 直接传递完整数据，保留格式信息
    return f"{SCREENSHOT_SUCCESS_PREFIX}{screenshot_data}"
