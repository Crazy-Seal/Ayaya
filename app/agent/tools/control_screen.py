"""按模型提供的坐标框，一次执行已获准的有序屏幕动作数组。"""

from typing import Annotated

from pydantic import Field

from app.agent.tools.core.base import ToolContext, ToolResult
from app.agent.tools.core.decorator import tool
from app.agent.tools.screen.actions import BATCH_SCREEN_INSTRUCTIONS, LocatedScreenActionArguments, validate_actions
from app.agent.tools.screen.adapter import screen_action, screen_tool_result


@tool(
    description=(
        "按 actions 数组顺序操作用户屏幕，最后返回整批操作后的截图。每个动作都必须提供目标的 bbox 坐标框。"
        + BATCH_SCREEN_INSTRUCTIONS
    ),
    compressible=True,
)
async def control_screen(
    actions: Annotated[list[LocatedScreenActionArguments], "按执行顺序排列的动作数组，至少包含一个动作。", Field(min_length=1)],
    context: ToolContext,
) -> ToolResult:
    """校验完整动作数组，在同一次桌面串行执行中完成操作和最终截图。

    Args:
        actions (list[LocatedScreenActionArguments]): 模型提供的动作数组，实际值仍是字典或只读映射。
        context (ToolContext): Pipeline 完成整批统一授权后注入的桌面接口和停止检查。

    Returns:
        ToolResult: 整批操作的真实执行程度、完成数量及可选的最终截图。
    """
    # 1. 整批参数都有效才允许执行，后面的错误不能使前面的动作提前执行。
    try:
        normalized = validate_actions(actions, LocatedScreenActionArguments)
    except ValueError as exc:
        return ToolResult(content=f"错误: {exc}")
    if context.desktop is None:
        return ToolResult(content="错误: 未装配后端桌面模块")

    # 2. 一次交付完整序列；桌面模块持有同一把锁，失败或停止时不再执行后续动作。
    prepared = [action.execution_data() for action in normalized]
    result = await context.desktop.perform_actions(
        tuple(screen_action(action) for action in prepared), stop_requested=context.stop_requested,
    )
    return screen_tool_result(result)
