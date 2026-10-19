from tests.model_capture import encoded_tool
import asyncio
import json

import pytest

from app.agent.tools.core.base import ToolContext
from app.agent.tools.control_screen import control_screen
from app.agent.tools.screen.actions import normalize_bbox
from app.agent.tools.control_screen_with_vlm import control_screen_with_vlm


def _context(
    *,
    resume_data=None,
    resume_state=None,
) -> ToolContext:
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


def _args(**overrides) -> dict:
    """构造桌面操作的默认参数并应用本用例覆盖值。

    Args:
        overrides (Any): 覆盖当前测试配置或工具参数默认值的字段。

    Returns:
        dict: 供外层用例断言使用的预设结果。
    """
    args = {"target": "search box", "bbox": [400, 200, 600, 400]}
    args.update(overrides)
    return {"actions": [args]}


def test_control_screen_schema_only_describes_xyxy() -> None:
    """验证桌面工具 Schema 只描述 xyxy 坐标。"""
    function = encoded_tool(control_screen())["function"]
    serialized = json.dumps(function, ensure_ascii=False).lower()

    assert function["parameters"]["required"] == ["actions"]
    actions = function["parameters"]["properties"]["actions"]
    assert actions["minItems"] == 1
    assert set(actions["items"]["required"]) == {"target", "bbox"}
    assert "[x1, y1, x2, y2]" in actions["items"]["properties"]["bbox"]["description"]
    assert "gemini" not in serialized
    assert "y1, x1, y2, x2" not in serialized


def test_bbox_is_always_interpreted_as_xyxy() -> None:
    """验证边界框始终按 xyxy 解读。"""
    assert normalize_bbox([200, 400, 400, 600]) == [200, 400, 400, 600]


@pytest.mark.parametrize("tool_class", [control_screen, control_screen_with_vlm])
def test_screen_wait_range_is_visible_to_model(tool_class) -> None:
    """验证两种屏幕工具发送的等待范围与后端校验一致，默认等待仍为三秒。

    Args:
        tool_class (type[BaseTool]): 使用坐标框或文字定位的屏幕工具类。
    """
    schema = encoded_tool(tool_class())["function"]["parameters"]
    item = schema["properties"]["actions"]["items"]
    wait = item["properties"]["wait_seconds"]
    assert wait["type"] == "integer"
    assert wait["minimum"] == 0
    assert wait["maximum"] == 60
    assert wait["default"] == 3
    assert "wait_seconds" not in item["required"]


@pytest.mark.parametrize(
    "bbox",
    [
        None,
        [],
        [1, 2, 3],
        [1, 2, 3, 4, 5],
        [1.0, 2, 3, 4],
        [True, 2, 3, 4],
        [-1, 2, 3, 4],
        [1, 2, 1001, 4],
        [500, 200, 500, 400],
        [600, 200, 500, 400],
        [400, 500, 600, 400],
    ],
)
def test_invalid_bbox_is_rejected(bbox: object) -> None:
    """验证无效边界框被拒绝。

    Args:
        bbox (object): 归一化画布中的 xyxy 边界框。
    """
    with pytest.raises(ValueError):
        normalize_bbox(bbox)






def test_invalid_bbox_does_not_create_interrupt() -> None:
    """验证无效边界框不会创建中断请求。"""
    async def run():
        """由外层测试调用，完成“无效边界框不会创建中断请求”的异步步骤。"""
        return await control_screen().execute(
            _args(bbox=[1000, 0, 1000, 50]),
            _context(),
        )

    result = asyncio.run(run())

    assert result.interrupt is None
    assert result.text.startswith("错误:")




def test_control_screen_rejects_invalid_scroll_direction() -> None:
    """验证无效滚轮方向被拒绝。"""
    async def run():
        """由外层测试调用，完成“无效滚轮方向被拒绝”的异步步骤。"""
        return await control_screen().execute(
            _args(operation="scroll", scroll_direction="none"),
            _context(),
        )

    result = asyncio.run(run())

    assert result.interrupt is None
    assert "scroll_direction" in result.text
