"""截屏工具（可恢复）。

一次工具调用被中断切成两个执行阶段：
- 首次执行（context.resume_data is None）：返回 needs_input 中断，请求用户批准截屏。
- 恢复执行（context.resume_data 含 {approved, screenshot_data, width, height}）：
  批准则把截图作为 image_url 返回，由 pipeline 注入为用户消息让模型「看见」。
"""

import logging

from app.agent.context import ToolContext, ToolResult
from app.agent.tools.decorator import tool

logger = logging.getLogger(__name__)


@tool(is_resumable=True)
async def screenshot(context: ToolContext) -> ToolResult:
    """向用户请求截屏。用户确认后系统会携带截图继续对话。"""
    # 首次执行：请求用户批准
    if context.resume_data is None:
        return ToolResult.needs_input(
            type="screenshot_request",
            message="Agent 请求截取屏幕，是否允许？",
        )

    # 恢复执行：处理用户回传
    resp = context.resume_data
    if not resp.get("approved"):
        return ToolResult.success("截屏请求被用户阻止")

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
    return ToolResult.success("截屏成功", image_url=screenshot_data)
