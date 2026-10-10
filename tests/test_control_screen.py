import asyncio
import json

import pytest

from app.agent.tools.base import ToolContext
from app.agent.tools.control_screen import control_screen, normalize_bbox


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
    return args


def test_control_screen_schema_only_describes_xyxy() -> None:
    """验证桌面工具 Schema 只描述 xyxy 坐标。"""
    function = control_screen().to_openai_tool()["function"]
    serialized = json.dumps(function, ensure_ascii=False).lower()

    assert set(function["parameters"]["required"]) == {"target", "bbox"}
    assert "[x1, y1, x2, y2]" in function["parameters"]["properties"]["bbox"]["description"]
    assert "gemini" not in serialized
    assert "y1, x1, y2, x2" not in serialized


def test_bbox_is_always_interpreted_as_xyxy() -> None:
    """验证边界框始终按 xyxy 解读。"""
    assert normalize_bbox([200, 400, 400, 600]) == [200, 400, 400, 600]


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


def test_initial_call_directly_requests_execution() -> None:
    """验证直接定位工具首次调用直接请求执行。"""
    async def run():
        """由外层测试调用，完成“直接定位工具首次调用直接请求执行”的异步步骤。"""
        return await control_screen().execute(
            _args(
                operation="click",
                text="hello",
                press_enter=False,
                wait_seconds=1,
            ),
            _context(),
        )

    result = asyncio.run(run())

    assert result.interrupt is not None
    assert result.interrupt.type == "continuation_request"
    assert result.interrupt.data["kind"] == "perform_screen_action"
    payload = result.interrupt.data["payload"]
    assert payload["target"] == "search box"
    assert payload["operation"] == "click"
    assert payload["text"] == "hello"
    assert payload["press_enter"] is False
    assert payload["coordinates"] == {
        "bbox": [400, 200, 600, 400],
        "x_ratio": 0.5,
        "y_ratio": 0.3,
    }
    assert result.resume_state == {"phase": "awaiting_execution"}


def test_initial_call_does_not_reorder_xyxy_coordinates() -> None:
    """验证首次调用不会重排 xyxy 坐标。"""
    async def run():
        """由外层测试调用，完成“首次调用不会重排 xyxy 坐标”的异步步骤。"""
        return await control_screen().execute(
            _args(bbox=[200, 400, 400, 600]),
            _context(),
        )

    result = asyncio.run(run())

    assert result.interrupt is not None
    assert result.interrupt.data["payload"]["coordinates"] == {
        "bbox": [200, 400, 400, 600],
        "x_ratio": 0.3,
        "y_ratio": 0.5,
    }


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
    assert result.content.startswith("错误:")


def test_control_screen_defaults_scroll_direction_to_down() -> None:
    """验证滚轮方向默认向下。"""
    async def run():
        """由外层测试调用，完成“滚轮方向默认向下”的异步步骤。"""
        return await control_screen().execute(
            _args(operation="scroll"),
            _context(),
        )

    result = asyncio.run(run())

    assert result.interrupt is not None
    assert result.interrupt.data["payload"]["scroll_direction"] == "down"


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
    assert "scroll_direction" in result.content


def test_control_screen_execution_resume_injects_screenshot() -> None:
    """验证执行恢复后向模型注入截图。"""
    async def run():
        """由外层测试调用，完成“执行恢复后向模型注入截图”的异步步骤。"""
        return await control_screen().execute(
            _args(),
            _context(
                resume_data={
                    "approved": True,
                    "executed": True,
                    "screenshot_data": "data:image/png;base64,BBB",
                },
                resume_state={"phase": "awaiting_execution"},
            ),
        )

    result = asyncio.run(run())

    assert result.content == "屏幕操作完成，已收到操作后的截图。"
    assert result.image_url == "data:image/png;base64,BBB"


def test_control_screen_execution_resume_requires_frontend_result() -> None:
    """验证执行恢复必须提供前端结果。"""
    async def run():
        """由外层测试调用，完成“执行恢复必须提供前端结果”的异步步骤。"""
        return await control_screen().execute(
            _args(),
            _context(
                resume_data={},
                resume_state={"phase": "awaiting_execution"},
            ),
        )

    result = asyncio.run(run())

    assert result.content == "屏幕操作失败: 前端未完成屏幕操作。"
    assert result.image_url is None


def test_control_screen_execution_resume_reports_frontend_failure() -> None:
    """验证前端动作失败在工具结果中明确报告。"""
    async def run():
        """由外层测试调用，完成“前端动作失败在工具结果中明确报告”的异步步骤。"""
        return await control_screen().execute(
            _args(),
            _context(
                resume_data={
                    "approved": True,
                    "executed": False,
                    "error": "mouse unavailable",
                },
                resume_state={"phase": "awaiting_execution"},
            ),
        )

    result = asyncio.run(run())

    assert result.content == "屏幕操作失败: mouse unavailable"
    assert result.image_url is None


def test_control_screen_rejects_legacy_resume_phase() -> None:
    """验证旧恢复阶段标记被拒绝。"""
    async def run():
        """由外层测试调用，完成“旧恢复阶段标记被拒绝”的异步步骤。"""
        return await control_screen().execute(
            _args(),
            _context(
                resume_data={"screenshot_data": "legacy"},
                resume_state={"phase": "awaiting_capture"},
            ),
        )

    result = asyncio.run(run())

    assert result.interrupt is None
    assert "恢复状态无效" in result.content
