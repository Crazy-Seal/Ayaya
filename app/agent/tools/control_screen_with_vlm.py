"""一次定位全部目标并准备整批审批，获准后按已保存坐标顺序操作屏幕。"""

from typing import Annotated

from pydantic import Field

from app.agent.models.vlm import VLMService
from app.agent.tools.core.base import ToolContext, ToolResult
from app.agent.tools.core.decorator import tool
from app.agent.tools.screen.actions import (
    BATCH_SCREEN_INSTRUCTIONS, LocatedScreenActionArguments, ScreenActionArguments, validate_actions,
)
from app.agent.tools.screen.adapter import screen_action, screen_tool_result
from app.schemas.runtime_types import AUTHORIZATION_GRANTED_KIND


@tool(
    description=(
        "按 actions 数组顺序操作用户屏幕，返回整批操作后的最后截图。每个动作提供 target 目标描述即可，"
        "VLM 会根据同一张截图，一次定位全部目标。" + BATCH_SCREEN_INSTRUCTIONS
    ),
    compressible=True,
    authorization_timing="after_prepare",
)
async def control_screen_with_vlm(
    actions: Annotated[list[ScreenActionArguments], "按执行顺序排列的动作数组，至少包含一个动作；所有目标必须已存在于当前截图中。", Field(min_length=1)],
    context: ToolContext,
) -> ToolResult:
    """校验整批动作并一次定位全部目标，审批恢复只执行已经保存的动作与坐标。

    Args:
        actions (list[ScreenActionArguments]): 模型提供的有序动作数组，实际值仍是字典或只读映射。
        context (ToolContext): Pipeline 提供的桌面接口、停止检查及整批审批恢复材料。

    Returns:
        ToolResult: 待统一授权的整批动作，或操作后的执行程度与截图。
    """
    # 1. 任何动作无效都在截图和 VLM 调用前拒绝，默认值按每个动作分别补齐。
    try:
        normalized = validate_actions(actions, ScreenActionArguments)
    except ValueError as exc:
        return ToolResult(content=f"错误: {exc}")
    if context.desktop is None:
        return ToolResult(content="错误: 未装配后端桌面模块")

    # 2. Pipeline 已对全部动作和坐标授权；重入只读持久材料，不重新调用 VLM。
    if context.resume_data is not None:
        if context.resume_data.get("kind") != AUTHORIZATION_GRANTED_KIND:
            return ToolResult(content="错误: 屏幕操作授权恢复状态无效。")
        saved = (context.resume_state or {}).get("actions")
        if not isinstance(saved, (list, tuple)) or len(saved) != len(normalized):
            return ToolResult(content="错误: 缺少完整的屏幕动作定位结果，未执行操作。")
        result = await context.desktop.perform_actions(
            tuple(screen_action(action) for action in saved), stop_requested=context.stop_requested,
        )
        return screen_tool_result(result)

    # 3. 同一张截图、一次 VLM 请求返回与动作对应的全部框；缺失时不提交部分批次。
    image = await context.desktop.capture_screen(stop_requested=context.stop_requested)
    boxes = await VLMService().locate_many([action.target for action in normalized], image.data_url)
    if context.stop_requested and context.stop_requested():
        return ToolResult(content="屏幕操作未执行：定位期间已停止")
    if len(boxes) != len(normalized):
        return ToolResult(content="错误: VLM 返回的坐标框数量与动作数量不一致，未执行操作。")
    try:
        prepared = [
            LocatedScreenActionArguments.model_validate({
                **action.model_dump(), "bbox": bbox,
            }).execution_data()
            for action, bbox in zip(normalized, boxes)
        ]
    except (TypeError, ValueError) as exc:
        return ToolResult(content=f"错误: VLM 定位结果无效，未执行操作：{exc}")

    # 4. 整批动作与原始目标一起交给统一授权；等待期间窗口已由截图调用恢复。
    return ToolResult.prepared(
        {"actions": prepared}, resume_state={"phase": "awaiting_authorization", "actions": prepared},
    )
