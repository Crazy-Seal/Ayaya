import asyncio

from app.agent.tools.core.base import ToolContext
from app.agent.tools.control_screen_with_vlm import (
    control_screen_with_vlm,
    restore_vlm_point,
)
from app.agent.tools.core.registry import ToolRegistry


def _context(resume_data=None, resume_state=None) -> ToolContext:
    """为桌面或文件工具构造当前用例的运行上下文。

    Args:
        resume_data (Any): 本次模拟审批或桌面回报的数据。
        resume_state (Any): 工具中断时保存的恢复材料。

    Returns:
        ToolContext: 供外层用例断言使用的预设结果。
    """
    async def emit_event(_event_type, _data):
        """接收本次工具发布的事件；该用例不依赖事件内容。

        Args:
            _event_type (Any): 工具回调发布的事件类型。
            _data (Any): 需要解析或保存的结构化数据。
        """
        return None

    return ToolContext(
        session_id="test",
        state=None,
        emit_event=emit_event,
        get_checkpoint=lambda: {},
        set_checkpoint=lambda _data: None,
        resume_data=resume_data,
        resume_state=resume_state,
    )


def test_vlm_tool_is_discovered_by_registry() -> None:
    """验证截图定位工具由注册表自动发现。"""
    tool_class = ToolRegistry.get("control_screen_with_vlm")

    assert tool_class is not None
    assert tool_class.name == "control_screen_with_vlm"


def test_restore_vlm_point_keeps_original_coordinate_payload() -> None:
    """验证恢复定位点时保留原坐标载荷。"""
    result = restore_vlm_point([400, 200, 600, 400], 1920, 1080)

    assert result == {
        "bbox": [400, 200, 600, 400],
        "x_ratio": 0.5,
        "y_ratio": 0.3,
        "x": 960,
        "y": 324,
        "width": 1920,
        "height": 1080,
    }
