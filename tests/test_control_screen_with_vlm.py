import asyncio

from app.agent.tools.base import ToolContext
from app.agent.tools.control_screen_with_vlm import (
    control_screen_with_vlm,
    restore_vlm_point,
)
from app.agent.tools.registry import ToolRegistry


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


def test_vlm_tool_initial_interrupt_requests_capture() -> None:
    """验证定位工具首次中断请求截图。"""
    async def run():
        """由外层测试调用，完成“定位工具首次中断请求截图”的异步步骤。"""
        return await control_screen_with_vlm().execute(
            {"target": "search box"},
            _context(),
        )

    result = asyncio.run(run())

    assert result.interrupt is not None
    assert result.interrupt.type == "continuation_request"
    assert result.interrupt.data == {"kind": "capture_screen", "payload": {}}
    assert result.resume_state["phase"] == "awaiting_capture"
    assert result.resume_state["action"]["operation"] == "double"


def test_vlm_tool_capture_resume_locates_and_requests_execution(monkeypatch) -> None:
    """验证截图恢复后定位并请求动作执行。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    async def fake_locate(_self, intent, image_data_url):
        """返回用例指定的截图定位结果，替代真实视觉模型。

        该定义仅用于外层用例：验证截图恢复后定位并请求动作执行。

        Args:
            _self (Any): 被替换方法原本接收的测试对象。
            intent (Any): 当前桌面动作的准备信息。
            image_data_url (Any): 用例提供的内存图片 data URL。
        """
        assert intent == "search box"
        assert image_data_url == "data:image/png;base64,AAA"
        return [400, 200, 600, 400]

    monkeypatch.setattr("app.agent.models.vlm.VLMService.locate", fake_locate)

    async def run():
        """由外层测试调用，完成“截图恢复后定位并请求动作执行”的异步步骤。"""
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

    assert result.interrupt is None
    assert result.prepared_action["target"] == "search box"
    assert result.prepared_action["operation"] == "click"
    assert result.prepared_action["coordinates"]["x"] == 960
    assert result.prepared_action["coordinates"]["y"] == 324
    assert result.resume_state["phase"] == "awaiting_authorization"


def test_vlm_tool_emits_execution_continuation_only_after_authorization() -> None:
    """验证动作继续请求只在授权后发布。"""
    async def run():
        """由外层测试调用，完成“动作继续请求只在授权后发布”的异步步骤。"""
        return await control_screen_with_vlm().execute(
            {"target": "search box", "operation": "click"},
            _context(
                resume_data={"kind": "authorization_granted"},
                resume_state={
                    "phase": "awaiting_authorization",
                    "coordinates": {
                        "bbox": [400, 200, 600, 400],
                        "x_ratio": 0.5,
                        "y_ratio": 0.3,
                    },
                },
            ),
        )

    result = asyncio.run(run())

    assert result.interrupt is not None
    assert result.interrupt.type == "continuation_request"
    assert result.interrupt.data["kind"] == "perform_screen_action"
    assert result.resume_state["phase"] == "awaiting_execution"


def test_vlm_tool_execution_resume_injects_post_action_screenshot() -> None:
    """验证动作恢复后注入执行后的截图。"""
    async def run():
        """由外层测试调用，完成“动作恢复后注入执行后的截图”的异步步骤。"""
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
