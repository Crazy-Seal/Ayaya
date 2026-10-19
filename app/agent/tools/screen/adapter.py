"""在屏幕工具与桌面模块之间转换明确动作和副作用结果。"""

from app.agent.tools.core.base import ToolResult
from app.schemas.desktop import ScreenAction, ScreenActionResult


def screen_action(data: dict) -> ScreenAction:
    """把已校验、已定位的准备动作转换为不依赖前端的进程内动作。

    Args:
        data (dict): 保存的操作参数及比例坐标。
    Returns:
        ScreenAction: 本次已经获准的输入动作。
    """
    coordinates = data["coordinates"]
    return ScreenAction(x_ratio=coordinates["x_ratio"], y_ratio=coordinates["y_ratio"],
                        operation=data["operation"], text=data["text"], press_enter=data["press_enter"],
                        wait_seconds=data["wait_seconds"], scroll_direction=data["scroll_direction"])


def screen_tool_result(result: ScreenActionResult) -> ToolResult:
    """保留输入执行程度，截图失败不能误报成从未执行。

    Args:
        result (ScreenActionResult): 清理后返回的输入及捕获结果。
    Returns:
        ToolResult: 交给现有 Pipeline 保存的文本与可选截图。
    """
    messages = {"completed": "屏幕操作完成", "not_started": "屏幕操作未执行",
                "partial_or_unknown": "屏幕操作可能已部分执行，请先观察屏幕，不要直接重复操作"}
    content = messages[result.status]
    if result.total_actions:
        content += f"；已完整完成 {result.completed_actions}/{result.total_actions} 个动作"
        if result.completed_actions < result.total_actions:
            content += "；后续动作未继续执行，不能直接重放整批动作"
    if result.error:
        content += f"：{result.error}"
    if result.screenshot is None and result.status == "completed":
        content += "；未取得操作后的截图"
    return ToolResult.success(content, image_url=result.screenshot.data_url if result.screenshot else None,
                              media_kind="screenshot")
