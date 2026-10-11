"""使用独立 VLM 定位目标的屏幕控制工具。

本文件保留原 control_screen 的 VLM 截图定位实现，仅修改工具名称，作为主模型视觉定位
能力不足时的兼容选项。
"""

from typing import Annotated, Literal

from app.schemas.runtime_types import (
    AUTHORIZATION_GRANTED_KIND,
)
from app.agent.tools._screen_result import screen_action, screen_tool_result
from app.agent.tools.base import ToolContext, ToolResult
from app.agent.models.vlm import VLMService, clamp_bbox
from app.agent.tools.decorator import tool

Operation = Literal["click", "double", "right", "scroll"]
ScrollDirection = Literal["none", "down", "up"]

VLM_CANVAS_SIZE = 1000


def _validate_args(
    target: str | None,
    operation: str | None,
    text: str | None,
    press_enter: bool | None,
    wait_seconds: int | None,
    scroll_direction: str | None,
) -> tuple[str, Operation, str, bool, int, ScrollDirection] | str:
    """请求截图定位前校验元素描述和动作配置，避免为无效操作启动定位与执行。

    Args:
        target (str | None): 用户希望操作的界面元素描述。
        operation (str | None): 鼠标动作，支持 click、double、right 或 scroll。
        text (str | None): 当前处理的文本。
        press_enter (bool | None): 输入文本后是否按回车键。
        wait_seconds (int | None): 动作完成后等待界面稳定的秒数。
        scroll_direction (str | None): 滚轮方向，支持 up 或 down。

    Returns:
        tuple[str, Operation, str, bool, int, ScrollDirection] | str: 规范化动作参数元组，或错误说明文本。
    """
    resolved_target = (target or "").strip()
    if not resolved_target:
        return "缺少要操作的元素描述。"

    resolved_operation = operation or "double"
    if resolved_operation not in {"click", "double", "right", "scroll"}:
        return "操作方式必须是 click、double、right 或 scroll。"

    resolved_scroll_direction = scroll_direction or "down"
    if resolved_operation == "scroll":
        if resolved_scroll_direction not in {"down", "up"}:
            return "滚动操作的 scroll_direction 必须是 down 或 up。"
    else:
        resolved_scroll_direction = "down"

    resolved_wait_seconds = 3 if wait_seconds is None else wait_seconds
    if resolved_wait_seconds < 0 or resolved_wait_seconds > 60:
        return "等待时长必须在 0 到 60 秒之间。"

    return (
        resolved_target,
        resolved_operation,  # type: ignore[return-value]
        text or "",
        True if press_enter is None else bool(press_enter),
        resolved_wait_seconds,
        resolved_scroll_direction,  # type: ignore[return-value]
    )


def restore_vlm_point(
    bbox: list[int],
    width: int,
    height: int,
) -> dict:
    """把 VLM 的 1000x1000 bbox 中心点还原为截图坐标和比例坐标。

    Args:
        bbox (list[int]): 归一化画布中的 xyxy 边界框。
        width (int): 截图宽度，用于坐标范围判断。
        height (int): 截图高度，用于坐标范围判断。
    """
    if width <= 0 or height <= 0:
        raise ValueError("截图尺寸无效。")

    x1, y1, x2, y2 = clamp_bbox(bbox, VLM_CANVAS_SIZE, VLM_CANVAS_SIZE)
    center_x = (x1 + x2) / 2
    center_y = (y1 + y2) / 2
    x_ratio = max(0.0, min(center_x / VLM_CANVAS_SIZE, 1.0))
    y_ratio = max(0.0, min(center_y / VLM_CANVAS_SIZE, 1.0))

    return {
        "bbox": [x1, y1, x2, y2],
        "x_ratio": x_ratio,
        "y_ratio": y_ratio,
        "x": round(x_ratio * width),
        "y": round(y_ratio * height),
        "width": width,
        "height": height,
    }


@tool(
    is_resumable=True,
    compressible=True,
    authorization_timing="after_prepare",
)
async def control_screen_with_vlm(
    target: Annotated[str, "操作时的光标位置描述，换句话说就是操作的时候把光标放在哪，例如'页面上方的搜索框'。必须输入。"],
    operation: Annotated[Operation, "操作方式：click 单击、double 双击、right 右键、scroll 滚动。"] = "double",
    text: Annotated[str, "操作后要在该位置输入的文字；为空则不输入。"] = "",
    press_enter: Annotated[bool, "输入文字后是否按回车。"] = True,
    wait_seconds: Annotated[int, "操作后的等待时长，单位秒，用于等待页面加载。"] = 3,
    scroll_direction: Annotated[ScrollDirection, "滚动方向：none、down 或 up。仅 scroll 操作使用，默认 down。"] = "down",
    context: ToolContext | None = None,
) -> ToolResult:
    """操作用户的屏幕，并回传操作后的屏幕截图。

    调用此工具前，你需要知道屏幕上的情况，如不知道，可以通过截屏工具获取。
    注意：每轮工具调用中，只允许使用一次该工具；该工具传回操作后的截图后再判断下一步操作。

    Args:
        target (str): 操作时的光标位置描述，换句话说就是操作的时候把光标放在哪，例如'页面上方的搜索框'。必须输入。
        operation (Operation): 操作方式：click 单击、double 双击、right 右键、scroll 滚动。
        text (str): 操作后要在该位置输入的文字；为空则不输入。
        press_enter (bool): 输入文字后是否按回车。
        wait_seconds (int): 操作后的等待时长，单位秒，用于等待页面加载。
        scroll_direction (ScrollDirection): 滚动方向：none、down 或 up。仅 scroll 操作使用，默认 down。
        context (ToolContext | None): 执行管道提供的工具运行上下文。
    """
    # 1. 先校验操作参数，再区分首次截图、定位后授权和动作完成三个处理阶段。
    normalized = _validate_args(target, operation, text, press_enter, wait_seconds, scroll_direction)
    if isinstance(normalized, str):
        return ToolResult(content=f"错误: {normalized}")

    resolved_target, resolved_operation, resolved_text, resolved_press_enter, resolved_wait, resolved_scroll = normalized
    action_state = {
        "target": resolved_target,
        "operation": resolved_operation,
        "text": resolved_text,
        "press_enter": resolved_press_enter,
        "wait_seconds": resolved_wait,
        "scroll_direction": resolved_scroll,
    }

    if context is None:
        return ToolResult(content="错误: 缺少工具上下文。")

    # 2. 获准重入只使用持久准备动作，不重新截图定位或另行复核授权。
    if context.desktop is None:
        return ToolResult(content="错误: 未装配后端桌面模块")
    if context.resume_data is not None:
        if context.resume_data.get("kind") != AUTHORIZATION_GRANTED_KIND:
            return ToolResult(content="错误: 屏幕操作授权恢复状态无效。")
        saved = context.resume_state or {}
        execute_data = {**saved.get("action", action_state), "coordinates": saved.get("coordinates")}
        result = await context.desktop.perform_action(screen_action(execute_data), stop_requested=context.stop_requested)
        return screen_tool_result(result)

    # 3. 截图调用结束已恢复窗口，VLM 等待和审批期间不隐藏聊天。
    image = await context.desktop.capture_screen(stop_requested=context.stop_requested)
    bbox = await VLMService().locate(resolved_target, image.data_url)
    # 3.1. 定位等待中收到停止时不再创建审批；窗口在截图完成时已经恢复。
    if context.stop_requested and context.stop_requested():
        return ToolResult(content="屏幕操作未执行：定位期间已停止")
    coordinates = restore_vlm_point(bbox, image.width, image.height)
    execute_data = {**action_state, "coordinates": coordinates}
    return ToolResult.prepared(execute_data, resume_state={
        "phase": "awaiting_authorization", "action": action_state, "coordinates": coordinates,
    })
