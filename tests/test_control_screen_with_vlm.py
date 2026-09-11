import asyncio

from app.agent.tools.base import ToolContext
from app.agent.tools.control_screen_with_vlm import (
    control_screen_with_vlm,
    restore_vlm_point,
)
from app.agent.tools.registry import ToolRegistry


def _context(resume_data=None, resume_state=None) -> ToolContext:
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


def test_vlm_tool_is_discovered_by_registry() -> None:
    tool_class = ToolRegistry.get("control_screen_with_vlm")

    assert tool_class is not None
    assert tool_class.name == "control_screen_with_vlm"


def test_restore_vlm_point_keeps_original_coordinate_payload() -> None:
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


def test_vlm_tool_initial_interrupt_requests_capture() -> None:
    async def run():
        return await control_screen_with_vlm().execute(
            {"target": "search box"},
            _context(),
        )

    result = asyncio.run(run())

    assert result.interrupt is not None
    assert result.interrupt.type == "control_screen_capture_request"
    assert result.interrupt.data == {}
    assert result.resume_state["phase"] == "awaiting_capture"
    assert result.resume_state["action"]["operation"] == "double"


def test_vlm_tool_capture_resume_locates_and_requests_execution(monkeypatch) -> None:
    async def fake_locate(_self, intent, image_data_url):
        assert intent == "search box"
        assert image_data_url == "data:image/png;base64,AAA"
        return [400, 200, 600, 400]

    monkeypatch.setattr("app.agent.models.vlm.VLMService.locate", fake_locate)

    async def run():
        return await control_screen_with_vlm().execute(
            {
                "target": "search box",
                "operation": "click",
                "text": "hello",
                "press_enter": False,
                "wait_seconds": 1,
            },
            _context(
                resume_data={
                    "screenshot_data": "data:image/png;base64,AAA",
                    "width": 1920,
                    "height": 1080,
                },
                resume_state={"phase": "awaiting_capture"},
            ),
        )

    result = asyncio.run(run())

    assert result.interrupt is not None
    assert result.interrupt.type == "control_screen_execute_request"
    assert result.interrupt.data["target"] == "search box"
    assert result.interrupt.data["operation"] == "click"
    assert result.interrupt.data["coordinates"]["x"] == 960
    assert result.interrupt.data["coordinates"]["y"] == 324
    assert result.resume_state["phase"] == "awaiting_execution"


def test_vlm_tool_execution_resume_injects_post_action_screenshot() -> None:
    async def run():
        return await control_screen_with_vlm().execute(
            {"target": "search box"},
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
