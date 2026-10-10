"""截屏工具（可恢复）。

授权由统一 ToolAuthorization 在调用前处理；工具只负责请求前端截屏并消费结果。
"""

import logging

from app.schemas.runtime_types import (
    DesktopExecutionKind,
)
from app.agent.state.messages import SCREENSHOT_MESSAGE_NAME
from app.agent.tools.base import ToolContext, ToolResult
from app.agent.tools.decorator import tool

logger = logging.getLogger(__name__)


@tool(is_resumable=True, compressible=True)
async def screenshot(context: ToolContext) -> ToolResult:
    """向用户请求截屏。用户确认后系统会携带截图继续对话。

    Args:
        context (ToolContext): 执行管道提供的工具运行上下文。
    """
    if context.resume_data is None:
        return ToolResult.continuation(
            DesktopExecutionKind.CAPTURE_SCREEN,
            "正在截取屏幕。",
        )

    resp = context.resume_data
    screenshot_data = resp.get("screenshot_data")
    if not screenshot_data:
        return ToolResult.success("截屏失败: 未收到截图数据")

    width, height = resp.get("width"), resp.get("height")
    if width and height:
        logger.info(
            "[screenshot] 收到截图: %sx%s, 数据长度: %d",
            width, height, len(screenshot_data),
        )

    # 截图作为 image_url 返回；pipeline 会注入为 system_screenshot 用户消息
    return ToolResult.success(
        "截屏成功",
        image_url=screenshot_data,
        image_message_name=SCREENSHOT_MESSAGE_NAME,
    )
