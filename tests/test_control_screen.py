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
    async def emit_event(_event_type, _data):
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
    args = {"target": "search box", "bbox": [400, 200, 600, 400]}
    args.update(overrides)
    return args


def test_control_screen_schema_only_describes_xyxy() -> None:
    function = control_screen().to_openai_tool()["function"]
    serialized = json.dumps(function, ensure_ascii=False).lower()

    assert set(function["parameters"]["required"]) == {"target", "bbox"}
    assert "[x1, y1, x2, y2]" in function["parameters"]["properties"]["bbox"]["description"]
    assert "gemini" not in serialized
    assert "y1, x1, y2, x2" not in serialized


def test_bbox_is_always_interpreted_as_xyxy() -> None:
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
    with pytest.raises(ValueError):
        normalize_bbox(bbox)


def test_initial_call_directly_requests_execution() -> None:
    async def run():
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
    assert result.interrupt.type == "control_screen_execute_request"
    assert result.interrupt.data["target"] == "search box"
    assert result.interrupt.data["operation"] == "click"
    assert result.interrupt.data["text"] == "hello"
    assert result.interrupt.data["press_enter"] is False
    assert result.interrupt.data["coordinates"] == {
        "bbox": [400, 200, 600, 400],
        "x_ratio": 0.5,
        "y_ratio": 0.3,
    }
    assert result.resume_state == {"phase": "awaiting_execution"}


def test_initial_call_does_not_reorder_xyxy_coordinates() -> None:
    async def run():
        return await control_screen().execute(
            _args(bbox=[200, 400, 400, 600]),
            _context(),
        )

    result = asyncio.run(run())

    assert result.interrupt is not None
    assert result.interrupt.data["coordinates"] == {
        "bbox": [200, 400, 400, 600],
        "x_ratio": 0.3,
        "y_ratio": 0.5,
    }


def test_invalid_bbox_does_not_create_interrupt() -> None:
    async def run():
        return await control_screen().execute(
            _args(bbox=[1000, 0, 1000, 50]),
            _context(),
        )

    result = asyncio.run(run())

    assert result.interrupt is None
    assert result.content.startswith("错误:")


def test_control_screen_defaults_scroll_direction_to_down() -> None:
    async def run():
        return await control_screen().execute(
            _args(operation="scroll"),
            _context(),
        )

    result = asyncio.run(run())

    assert result.interrupt is not None
    assert result.interrupt.data["scroll_direction"] == "down"


def test_control_screen_rejects_invalid_scroll_direction() -> None:
    async def run():
        return await control_screen().execute(
            _args(operation="scroll", scroll_direction="none"),
            _context(),
        )

    result = asyncio.run(run())

    assert result.interrupt is None
    assert "scroll_direction" in result.content


def test_control_screen_execution_resume_injects_screenshot() -> None:
    async def run():
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


def test_control_screen_execution_resume_handles_rejection() -> None:
    async def run():
        return await control_screen().execute(
            _args(),
            _context(
                resume_data={"approved": False},
                resume_state={"phase": "awaiting_execution"},
            ),
        )

    result = asyncio.run(run())

    assert result.content == "屏幕操作请求被用户拒绝。"
    assert result.image_url is None


def test_control_screen_execution_resume_reports_frontend_failure() -> None:
    async def run():
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
    async def run():
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
