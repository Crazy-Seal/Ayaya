"""屏幕控制工具。

后端负责标准化模型给出的坐标并编排恢复；实际鼠标键盘操作由前端在用户批准后执行，
执行结果和操作后的截图会回传给 Agent。
"""

from typing import Annotated, Literal

from app.agent.context import ToolContext, ToolResult
from app.agent.message import SCREENSHOT_MESSAGE_NAME
from app.agent.tools.decorator import tool

Operation = Literal["click", "double", "right", "scroll"]
ScrollDirection = Literal["none", "down", "up"]

NORMALIZED_CANVAS_SIZE = 1000


def normalize_bbox(bbox: object) -> list[int]:
    """校验并返回 canonical ``[x1, y1, x2, y2]``。"""
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError("bbox 必须是包含四个整数的数组。")
    if any(type(value) is not int for value in bbox):
        raise ValueError("bbox 坐标必须是整数。")
    if any(value < 0 or value > NORMALIZED_CANVAS_SIZE for value in bbox):
        raise ValueError("bbox 坐标必须在 0 到 1000 之间。")

    x1, y1, x2, y2 = bbox

    if x2 <= x1 or y2 <= y1:
        raise ValueError("bbox 必须满足 x1 < x2 且 y1 < y2。")
    return [x1, y1, x2, y2]


def _coordinates_from_bbox(bbox: list[int]) -> dict:
    x1, y1, x2, y2 = bbox
    return {
        "bbox": bbox,
        "x_ratio": ((x1 + x2) / 2) / NORMALIZED_CANVAS_SIZE,
        "y_ratio": ((y1 + y2) / 2) / NORMALIZED_CANVAS_SIZE,
    }


def _validate_args(
    target: str | None,
    bbox: object,
    operation: str | None,
    text: str | None,
    press_enter: bool | None,
    wait_seconds: int | None,
    scroll_direction: str | None,
) -> tuple[str, list[int], Operation, str, bool, int, ScrollDirection] | str:
    resolved_target = (target or "").strip()
    if not resolved_target:
        return "缺少要操作的元素描述。"

    try:
        resolved_bbox = normalize_bbox(bbox)
    except ValueError as exc:
        return str(exc)

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
    if type(resolved_wait_seconds) is not int or not 0 <= resolved_wait_seconds <= 60:
        return "等待时长必须是 0 到 60 之间的整数。"

    return (
        resolved_target,
        resolved_bbox,
        resolved_operation,  # type: ignore[return-value]
        text or "",
        True if press_enter is None else bool(press_enter),
        resolved_wait_seconds,
        resolved_scroll_direction,  # type: ignore[return-value]
    )


@tool(is_resumable=True, compressible=True)
async def control_screen(
    bbox: Annotated[list[int], "目标元素边界框 [x1, y1, x2, y2]，坐标相对于当前屏幕归一化到 0 至 1000。必须输入。"],
    target: Annotated[str, "操作时的光标位置描述，例如‘页面上方的搜索框’。必须输入。"],
    operation: Annotated[Operation, "操作方式：click 单击、double 双击、right 右键、scroll 滚动。"] = "double",
    text: Annotated[str, "操作后要在该位置输入的文字；为空则不输入。"] = "",
    press_enter: Annotated[bool, "输入文字后是否按回车。"] = True,
    wait_seconds: Annotated[int, "操作后的等待时长，单位秒，用于等待页面加载。"] = 3,
    scroll_direction: Annotated[ScrollDirection, "滚动方向：none、down 或 up。仅 scroll 操作使用，默认 down。"] = "down",
    context: ToolContext | None = None,
) -> ToolResult:
    """操作用户的屏幕，并回传操作后的屏幕截图。

    调用此工具前，你需要知道屏幕上的情况；如不知道，可先通过截屏工具获取。
    每轮工具调用中只允许使用一次该工具；收到操作后的截图后再判断下一步操作。
    """
    if context is None:
        return ToolResult.error("缺少工具上下文。")

    normalized = _validate_args(
        target,
        bbox,
        operation,
        text,
        press_enter,
        wait_seconds,
        scroll_direction,
    )
    if isinstance(normalized, str):
        return ToolResult.error(normalized)

    (
        resolved_target,
        resolved_bbox,
        resolved_operation,
        resolved_text,
        resolved_press_enter,
        resolved_wait,
        resolved_scroll,
    ) = normalized
    coordinates = _coordinates_from_bbox(resolved_bbox)
    action_state = {
        "target": resolved_target,
        "operation": resolved_operation,
        "text": resolved_text,
        "press_enter": resolved_press_enter,
        "wait_seconds": resolved_wait,
        "scroll_direction": resolved_scroll,
        "coordinates": coordinates,
    }

    if context.resume_data is not None:
        phase = (context.resume_state or {}).get("phase")
        if phase != "awaiting_execution":
            return ToolResult.error("屏幕操作恢复状态无效。")
        if not context.resume_data.get("approved"):
            return ToolResult.success("屏幕操作请求被用户拒绝。")
        if not context.resume_data.get("executed", False):
            error_message = context.resume_data.get("error") or "前端未完成屏幕操作。"
            return ToolResult.success(f"屏幕操作失败: {error_message}")

        screenshot_data = context.resume_data.get("screenshot_data")
        if not screenshot_data:
            return ToolResult.success("屏幕操作完成，但未收到操作后的截图。")
        return ToolResult.success(
            "屏幕操作完成，已收到操作后的截图。",
            image_url=screenshot_data,
            image_message_name=SCREENSHOT_MESSAGE_NAME,
        )

    return ToolResult.needs_input(
        type="control_screen_execute_request",
        message=(
            f"Agent 请求对“{resolved_target}”执行 {resolved_operation} 操作。"
            "请确认是否允许。"
        ),
        data=action_state,
        resume_state={"phase": "awaiting_execution"},
    )
