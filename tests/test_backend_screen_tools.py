from tests.model_capture import image_urls, image_kind
"""屏幕工具直接返回结果，VLM 只在准备阶段定位，不依赖 Electron 回报。"""

import asyncio
import pytest

from app.agent.tools.core.base import ToolContext
from app.agent.tools.screenshot import screenshot
from app.agent.tools.control_screen import control_screen
from app.schemas.desktop import ScreenCapture, ScreenActionResult


class DesktopPort:
    """提供已确认桌面接口的隔离测试 adapter。"""
    def __init__(self):
        """记录模型请求的动作，返回预设观察。"""
        self.actions = []
        self.image = ScreenCapture("data:image/png;base64,test", 1920, 1080)

    async def capture_screen(self, *, stop_requested=None):
        """只返回预设图片，不访问实际屏幕。

        Args:
            stop_requested (StopRequested | None): 当前轮次已受理停止或引导时返回 True 的只读回调。
        """
        return self.image

    async def perform_action(self, action, *, stop_requested=None):
        """记录已经获准的动作，并返回真实接口形状的结果。

        Args:
            action (ScreenAction): 已经由工具准备并获准的鼠标位置和输入配置。
            stop_requested (StopRequested | None): 当前轮次已受理停止或引导时返回 True 的只读回调。
        """
        self.actions.append(action)
        return ScreenActionResult("completed", self.image)


def context(desktop, **kwargs):
    """构造工具收到的显式桌面依赖和审批材料。

    Args:
        desktop (DesktopOperations): 当前测试显式注入的桌面接口，不访问生产屏幕。
        kwargs (dict): 本用例传给工具的审批材料或停止回调。
    """
    async def emit(_kind, _data):
        """本用例只观察工具结果，不处理展示事件。

        Args:
            _kind (str): 工具输出的事件类别，本用例不向窗口发送。
            _data (Any): 本次事件的业务载荷，本用例只检查工具结果。
        """
        pass
    return ToolContext("s", None, emit, lambda: {}, lambda _data: None, desktop=desktop, **kwargs)


def test_screenshot_returns_image_without_execution_interrupt() -> None:
    """工具首次调用即取得后端图片，不产生领取和恢复中断。"""
    result = asyncio.run(screenshot().execute({}, context(DesktopPort())))
    assert result.interrupt is None
    assert image_urls(result)[0] == "data:image/png;base64,test"


def test_control_screen_directly_executes_normalized_action() -> None:
    """模型 bbox 仍按中心点归一化，工具直接执行后端动作。"""
    desktop = DesktopPort()
    result = asyncio.run(control_screen().execute({"bbox": [450, 450, 550, 550], "target": "目标"}, context(desktop)))
    assert result.interrupt is None
    assert desktop.actions[0].x_ratio == 0.5
    assert image_urls(result)[0] == desktop.image.data_url


def test_vlm_prepares_once_and_approved_resume_uses_saved_coordinates(monkeypatch) -> None:
    """定位与审批之间没有执行动作，审批重入不会再次调用 VLM。

    Args:
        monkeypatch (pytest.MonkeyPatch): 替换操作系统或环境依赖，用例结束后自动恢复。
    """
    from app.agent.tools.control_screen_with_vlm import control_screen_with_vlm
    from app.agent.models.vlm import VLMService
    from app.schemas.runtime_types import AUTHORIZATION_GRANTED_KIND
    calls = []

    async def locate(_self, target, _image):
        """记录一次准备阶段定位，返回固定归一化目标框。

        Args:
            _self (VLMService): 正在执行定位的 VLM 实例，用例替换其模型请求。
            target (str): 准备阶段请求 VLM 定位的界面目标描述。
            _image (str): 桌面接口提供的截图 data URL，本替身不读取真实图像。
        """
        calls.append(target)
        return [400, 300, 600, 500]

    monkeypatch.setattr(VLMService, "locate", locate)
    desktop = DesktopPort()

    async def scenario():
        """准备完毕后仅模拟 Pipeline 已获准的重入材料。"""
        tool = control_screen_with_vlm()
        prepared = await tool.execute({"target": "搜索框"}, context(desktop))
        assert prepared.prepared_action is not None
        assert not desktop.actions
        result = await tool.execute({"target": "搜索框"}, context(desktop,
            resume_data={"kind": AUTHORIZATION_GRANTED_KIND}, resume_state=prepared.resume_state))
        assert result.interrupt is None and image_urls(result)[0] == desktop.image.data_url
        assert desktop.actions[0].x_ratio == 0.5
        assert desktop.actions[0].y_ratio == 0.4
        assert calls == ["搜索框"]
    asyncio.run(scenario())


def test_completed_action_without_capture_is_not_reported_unexecuted() -> None:
    """工具显示输入已完成而截图失败，防止模型根据错误说明重复点击。"""
    from app.agent.tools.screen.adapter import screen_tool_result
    result = screen_tool_result(ScreenActionResult("completed", error="截图失败"))
    assert "屏幕操作完成" in result.text and "截图失败" in result.text
    assert "未执行" not in result.text


@pytest.mark.parametrize("policy", ["allow", "ask", "deny"])
def test_pipeline_screen_permissions_and_first_call_stop_port(policy) -> None:
    """真实 Pipeline 使用统一权限，并把同一个轮次的停止状态传到首次桌面调用。

    Args:
        policy (str): 本轮使用的统一工具权限 allow、ask 或 deny。
    """
    from app.agent.context import ContextStrategyConfig, ModelInputCoordinator, RuntimeContext
    from app.agent.core.pipeline import ExecutionPipeline
    from app.agent.core.plugin_manager import PluginManager
    from app.agent.core.tool_manager import ToolManager
    from app.agent.models.llm_client import StreamChunk
    from app.agent.runtime.turn_control import TurnControl, TurnInterrupted
    from app.agent.state import AgentState
    from app.agent.state.messages import ToolCall
    from app.agent.tools.authorization.manager import ToolAuthorization
    from app.schemas.chat_settings import ChatSettings

    class ScriptedModel:
        """输出一个真实屏幕工具调用，然后结束回复，不访问网络。"""
        def __init__(self):
            """保存两个模型步骤供 Pipeline 消费。"""
            self.steps = [[StreamChunk(tool_call=ToolCall(id="screen-call", name="control_screen",
                                                         args={"bbox": [400, 400, 600, 600], "target": "目标"}))],
                          [StreamChunk(content="完成")]]

        async def astream(self, _messages, tools=None):
            """消费当前模型步骤，工具目录由统一授权管理器提供。

            Args:
                _messages (list[dict]): Pipeline 投影的模型输入，本替身只输出脚本。
                tools (list[dict] | None): 统一权限管理器公开给模型的工具目录。
            """
            for chunk in self.steps.pop(0):
                yield chunk

    class CheckedDesktop(DesktopPort):
        """从公共工具端口验证停止读取，并记录已经获准的动作。"""
        async def perform_action(self, action, *, stop_requested=None):
            """调用开始和请求停止后，检查提供给桌面线程的状态及时改变。

            Args:
                action (ScreenAction): 已经由工具准备并获准的鼠标位置和输入配置。
                stop_requested (StopRequested | None): 当前轮次已受理停止或引导时返回 True 的只读回调。
            """
            assert stop_requested is not None and not stop_requested()
            control.request("stop")
            assert stop_requested()
            return await super().perform_action(action, stop_requested=stop_requested)

    async def scenario():
        """执行一轮屏幕调用，ask 审批恢复后使用同一个桌面接口。"""
        nonlocal control
        control = TurnControl("screen-run")
        model = ScriptedModel()
        settings = ChatSettings(session_id="s", model_name="test", openai_api_key="test",
                                openai_base_url="http://127.0.0.1:1/v1", temperature=0.1, system_prompt="test",
                                context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
                                tool_permissions={"local:control_screen": policy})
        authorization = ToolAuthorization(lambda _session_id: settings)
        manager = ToolManager(session_id="s", authorization=authorization)
        manager.register(control_screen())
        desktop = CheckedDesktop()

        async def emit(_kind, _data):
            """本用例消费事件流，不建立前端连接。

            Args:
                _kind (str): 工具输出的事件类别，本用例不向窗口发送。
                _data (Any): 本次事件的业务载荷，本用例只检查工具结果。
            """
            pass

        pipeline = ExecutionPipeline(prompt_source=settings, llm_client=model, tool_manager=manager,
                                     plugin_manager=PluginManager(), model_input=ModelInputCoordinator(
                                         settings.context_strategy, model, accounting_key="screen-test"),
                                     emit_event=emit, tool_authorization=authorization, desktop=desktop)
        state = AgentState(session_id="s")
        state.add_user_message("点击目标")
        runtime = RuntimeContext.capture()
        runtime.turn_control = control
        if policy == "allow":
            with pytest.raises(TurnInterrupted):
                await consume(pipeline.execute(state, runtime_context=runtime))
            events = []
        else:
            events = await consume(pipeline.execute(state, runtime_context=runtime))
        if policy == "ask":
            assert not desktop.actions and events[-1].data["type"] == "authorization_request"
            with pytest.raises(TurnInterrupted):
                await consume(pipeline.resume_tools(state, {"kind": "approve"}, runtime_context=runtime))
        assert len(desktop.actions) == (0 if policy == "deny" else 1)

    async def consume(stream):
        """审批恢复入口的流必须完整消费才发生工具执行。

        Args:
            stream (AsyncIterator): 实际 Pipeline 执行或审批恢复事件流，消费后才完成工具。
        """
        return [event async for event in stream]

    control = None
    asyncio.run(scenario())
