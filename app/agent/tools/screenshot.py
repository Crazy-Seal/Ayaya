"""经统一授权后在后端截图，图片沿现有工具消息持久化。"""

from app.agent.tools.core.base import ToolContext, ToolResult
from app.agent.tools.core.decorator import tool


@tool(compressible=True)
async def screenshot(context: ToolContext) -> ToolResult:
    """获取当前主屏观察，不产生前端执行中断。

    Args:
        context (ToolContext): Pipeline 提供共享桌面模块及只读停止信号。
    Returns:
        ToolResult: 清理后取得的截图，保存由 Pipeline 完成。
    """
    if context.desktop is None:
        return ToolResult(content="错误: 未装配后端桌面模块")
    image = await context.desktop.capture_screen(stop_requested=context.stop_requested)
    return ToolResult.success("截屏成功", image_url=image.data_url, media_kind="screenshot")
