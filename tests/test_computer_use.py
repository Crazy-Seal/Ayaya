"""从原生 REPL 和插件公开入口验证屏幕能力。"""

import asyncio
from pathlib import Path

import pytest

from app.repl.contracts import PreparedReplCapability, ReplCallContext, ReplText
from app.repl.repl_service import ReplService


class RecordingHost:
    """记录公共 host 输出，替代 runner 以检查能力可见结果。"""

    execution_id = "execution"

    def __init__(self) -> None:
        """初始化图片、事件和执行清理记录。"""
        self.images = []
        self.events = []
        self.cleanups = []
        self.interrupted = False

    def emit_image(self, image_bytes: bytes) -> None:
        """记录观察显式输出的编码图片。"""
        self.images.append(image_bytes)

    def publish_event(self, execution_id: str, name: str, payload: dict) -> None:
        """记录当前执行发布的插件提示。"""
        self.events.append((execution_id, name, payload))

    def request_interrupt(self, execution_id: str) -> None:
        """标记用户请求停止当前执行。"""
        if execution_id == self.execution_id:
            self.interrupted = True

    def raise_if_interrupted(self, execution_id: str) -> None:
        """在桌面动作安全点交付匹配执行的停止。"""
        if execution_id == self.execution_id and self.interrupted:
            raise KeyboardInterrupt()

    def defer_cleanup(self, execution_id: str, callback) -> None:
        """记录执行结束必须尝试的清理。"""
        self.cleanups.append(callback)


class FakeEscapeListener:
    """以可观察的关闭回调替代系统键盘钩子。"""

    def __init__(self, close) -> None:
        """保存解除监听的系统边界替身。"""
        self._close = close

    def start(self) -> None:
        """模拟监听安装完成。"""

    def close(self) -> None:
        """解除本次执行的监听。"""
        self._close()


def test_computer_use_is_prebound_without_starting_control(tmp_path: Path) -> None:
    """正式 runner 预装 cua，读取帮助后仍没有屏幕控制事件。

    Args:
        tmp_path (Path): 本次 REPL 的隔离目录。
    """
    from app.crud.chat_settings_dao import ChatSettingsDao
    from app.schemas.chat_settings import ChatSettings
    from app.services.chat_settings_service import ChatSettingsService

    async def scenario() -> None:
        """调用真实能力工厂并收集公开输出和插件事件。"""
        events = []

        async def handle(event) -> None:
            """记录能力发布的事件及执行结束通知。"""
            events.append(event)

        settings = ChatSettings(session_id="computer", model_name="test", openai_api_key="test",
            openai_base_url="http://localhost", temperature=.7, system_prompt="test", context_strategy={},
            agent_plugins={"computer_use": {"enabled": True, "config": {}}})
        preparation = ChatSettingsService(ChatSettingsDao(tmp_path / "settings.yaml"))
        prepared = preparation.prepare(settings)
        assert prepared.plugins["computer_use"].config == {"inject_screen_state": False}
        context = ReplCallContext("computer", "run", "call", prepared.repl_capabilities)
        service = ReplService(workdir=tmp_path, on_event=handle)
        try:
            result = await service.execute("print(type(cua).__name__)\ncua.help()", context)
            text = "".join(output.text for output in result.outputs if isinstance(output, ReplText))
            assert "Cua" in text and "get_ax_state" in text
            assert not result.error
            assert len(events) == 1
            settings.agent_plugins["computer_use"].enabled = False
            disabled = preparation.prepare(settings)
            result = await service.execute("print('cua' in globals())", ReplCallContext("computer", "run", "next", disabled.repl_capabilities))
            assert not result.error and result.outputs == (ReplText("False"), ReplText("\n"))
        finally:
            await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(("window_id", "diagnostic"), [
    (132198, "收到 int"),
    (132198.0, "收到 float"),
    (True, "收到 bool"),
    (None, "收到 NoneType"),
    ("", "非空十进制字符串"),
    (" 132198", "非空十进制字符串"),
    ("0x20466", "非空十进制字符串"),
    ("window_id=132198", "非空十进制字符串"),
])
def test_window_binding_reports_invalid_id_before_system_lookup(window_id, diagnostic: str) -> None:
    """窗口编号类型或格式错误时，绑定失败并提示调用者如何传入编号。

    Args:
        window_id (object): 类型或格式不符合要求的窗口编号。
        diagnostic (str): 调用者需要看到的具体参数错误原因。
    """
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.api import Cua
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.screen_helper import ScreenHelper

    desktop = ScreenHelper.__new__(ScreenHelper)
    cua = Cua(RecordingHost(), desktop=desktop)
    with pytest.raises(ValueError, match=diagnostic) as error:
        cua.get_app(window_id=window_id)
    assert "window.window_id" in str(error.value)
    if not isinstance(window_id, str):
        assert 'cua.get_app(window_id="132198")' in str(error.value)


def test_window_binding_reports_closed_window_separately() -> None:
    """格式正确的窗口编号已失效时，提示调用者重新列举窗口。"""
    from unittest.mock import Mock
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.api import Cua
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.screen_helper import ScreenHelper

    desktop = ScreenHelper.__new__(ScreenHelper)
    desktop.user = Mock()
    desktop.user.IsWindow.return_value = False
    cua = Cua(RecordingHost(), desktop=desktop)
    with pytest.raises(ValueError, match="窗口已经失效"):
        cua.get_app(window_id="132198")
    desktop.user.IsWindow.assert_called_once_with(132198)


def test_window_observation_outputs_tree_and_image_separately(capsys) -> None:
    """窗口绑定不输出，控件观察只打印控件树，截图观察只输出一张图片。"""
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.api import Cua
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.types import AxNode, AxState, CaptureRegion, Screenshot, WindowInfo

    class Desktop:
        """提供确定的系统窗口和观察结果，避免读取真实桌面。"""

        def window(self, window_id: str) -> WindowInfo:
            """按准确编号解析测试窗口。"""
            assert window_id == "42"
            return WindowInfo("42", "same title", 7, "test.exe")

        def ax_state(self, window: WindowInfo, controls) -> AxState:
            """提供一个可执行 Invoke 的按钮。"""
            return AxState((AxNode(1, None, "Run", "Button", (310, 210, 350, 240), True, False, "", ("Invoke",)),))

        def screenshot(self, window: WindowInfo | None = None) -> Screenshot:
            """提供窗口编码图片和独立物理坐标范围。"""
            return Screenshot(b"encoded PNG", CaptureRegion(300, 200, 800, 600, 96), 800, 600)

    host = RecordingHost()
    cua = Cua(host, desktop=Desktop())
    app = cua.get_app(window_id="42")
    assert not capsys.readouterr().out

    state = app.get_ax_state()
    assert state.nodes[0].name == "Run"
    assert host.images == []

    screenshot = app.get_screenshot()
    assert screenshot.region.left == 300
    assert host.images == [b"encoded PNG"]
    assert "Run" in capsys.readouterr().out
    assert host.events == [] and host.cleanups == []


def test_window_coordinates_follow_move_and_reject_resize() -> None:
    """最近窗口截图的中心点随窗口移动，尺寸变化后要求重新观察。"""
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.api import Cua
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.types import CaptureRegion, Screenshot, WindowInfo

    class Desktop:
        """以确定的窗口几何和输入记录替代 Windows。"""

        region = CaptureRegion(300, 200, 800, 600, 96)

        def __init__(self) -> None:
            """记录实际鼠标输入与清理结果。"""
            self.clicks = []
            self.stopped = False
            self.released = False

        def window(self, window_id: str) -> WindowInfo:
            """提供绑定窗口的身份。"""
            return WindowInfo(window_id, "test", 7, "test.exe")

        def verify(self, window: WindowInfo) -> None:
            """确认测试窗口仍然存在。"""

        def screenshot(self, window=None) -> Screenshot:
            """返回已知尺寸和原点的截图。"""
            return Screenshot(b"PNG", self.region, 800, 600)

        def get_region(self, window=None) -> CaptureRegion:
            """提供当前窗口移动或缩放后的几何。"""
            return self.region

        def activate(self, window) -> None:
            """模拟目标窗口已经激活。"""
            if hasattr(self, "move_on_activate"):
                self.region = self.move_on_activate

        def ensure_point(self, window, point) -> None:
            """模拟点击位置未被其他窗口遮挡。"""

        def create_escape_listener(self, callback):
            """提供可在启动前登记清理的测试监听。"""
            self.callback = callback

            def stop() -> None:
                """记录监听已解除。"""
                self.stopped = True
            return FakeEscapeListener(stop)

        def release_inputs(self) -> None:
            """记录本次输入资源已释放。"""
            self.released = True

        def click(self, point, button, clicks) -> None:
            """记录发给操作系统的物理坐标。"""
            self.clicks.append((point, button, clicks))

    host, desktop = RecordingHost(), Desktop()
    cua = Cua(host, desktop=desktop)
    window = cua.get_app(window_id="42")
    window.get_screenshot()
    window.click(x=500, y=500)
    desktop.region = CaptureRegion(400, 100, 800, 600, 96)
    window.click(x=1000, y=0, button="right")
    assert desktop.clicks == [((700, 500), "left", 1), ((1199, 100), "right", 1)]
    assert len(host.events) == 1 and len(host.cleanups) == 1
    desktop.move_on_activate = CaptureRegion(500, 150, 800, 600, 96)
    window.click(x=500, y=500)
    assert desktop.clicks[-1][0] == (900, 450)
    desktop.move_on_activate = CaptureRegion(500, 150, 800, 600, 144)
    with pytest.raises(ValueError, match="DPI"):
        window.click(x=500, y=500)
    del desktop.move_on_activate
    desktop.region = CaptureRegion(400, 100, 1000, 600, 96)
    with pytest.raises(ValueError, match="重新"):
        window.click(x=500, y=500)
    for cleanup in reversed(host.cleanups):
        cleanup()
    assert desktop.stopped and desktop.released


def test_escape_listener_consumes_physical_but_passes_injected(monkeypatch) -> None:
    """系统钩子消费物理 ESC，模型注入的 Escape 继续交给目标窗口。"""
    import ctypes
    from ctypes import wintypes as w
    import threading
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.escape import EscapeListener

    class Function:
        """模拟可声明 ctypes 参数类型的 Win32 函数。"""

        def __init__(self, function) -> None:
            """保存操作系统函数的测试实现。"""
            self.function = function

        def __call__(self, *args):
            """通过系统调用边界交付测试参数。"""
            return self.function(*args)

    class Keyboard(ctypes.Structure):
        """按 Windows 定义提供低级键盘事件数据。"""
        _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD), ("flags", w.DWORD),
                    ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class Native:
        """模拟钩子安装和消息队列，测试期间不安装真实全局监听。"""

        def __init__(self) -> None:
            """准备能由解除调用结束的系统消息等待。"""
            self.stopped = threading.Event()
            self.SetWindowsHookExW = Function(self.install)
            self.CallNextHookEx = Function(lambda *args: 99)
            self.UnhookWindowsHookEx = Function(lambda *args: 1)
            self.PeekMessageW = Function(lambda *args: 0)
            self.GetMessageW = Function(self.wait)
            self.TranslateMessage = Function(lambda *args: 1)
            self.DispatchMessageW = Function(lambda *args: 0)
            self.PostThreadMessageW = Function(lambda *args: self.stopped.set() or 1)
            self.GetCurrentThreadId = Function(lambda: 7)
            self.GetModuleHandleW = Function(lambda *args: 1)

        def install(self, kind, callback, module, thread) -> int:
            """保存系统将调用的钩子函数。"""
            self.callback = callback
            return 1

        def wait(self, *args) -> int:
            """等待测试线程收到 WM_QUIT。"""
            self.stopped.wait(2)
            return 0

    native = Native()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: native)
    interrupted = []
    listener = EscapeListener(lambda: interrupted.append(True))
    listener.start()
    try:
        injected = Keyboard(27, 0, 0x10, 0, 0)
        physical = Keyboard(27, 0, 0, 0, 0)
        assert native.callback(0, 0x100, ctypes.addressof(injected)) == 99
        assert not interrupted
        assert native.callback(0, 0x100, ctypes.addressof(physical)) == 1
        assert interrupted == [True]
    finally:
        listener.close()
    assert native.stopped.is_set()


def test_semantic_actions_never_fall_back_to_mouse() -> None:
    """语义动作明确失败时直接报告，Agent 可以主动重新观察再选其他方式。"""
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.api import Cua
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.types import WindowInfo

    class Desktop:
        """记录发往 UIA 的动作，鼠标输入在此测试中没有可用入口。"""

        def __init__(self) -> None:
            """准备已执行语义动作记录。"""
            self.actions = []

        def window(self, window_id: str) -> WindowInfo:
            """提供准确测试窗口身份。"""
            return WindowInfo(window_id, "test", 7, "test.exe")

        def verify(self, window) -> None:
            """确认测试目标仍然存活。"""

        def create_escape_listener(self, callback):
            """返回可重复结束的测试监听。"""
            return FakeEscapeListener(lambda: None)

        def release_inputs(self) -> None:
            """结束测试输入范围。"""

        def set_value(self, window, controls, index, value) -> None:
            """记录值设置，明确失效的控件编号直接失败。"""
            if index != 12:
                raise ValueError("控件已失效，请重新观察")
            self.actions.append(("SetValue", index, value))

        def perform(self, window, controls, index, action) -> None:
            """记录一次命名语义动作。"""
            self.actions.append((action, index))

    host, desktop = RecordingHost(), Desktop()
    window = Cua(host, desktop=desktop).get_app(window_id="42")
    window.set_value(element_index=12, value="中文")
    window.perform_secondary_action(element_index=13, action="Invoke")
    with pytest.raises(ValueError, match="重新观察"):
        window.set_value(element_index=99, value="other")
    assert desktop.actions == [("SetValue", 12, "中文"), ("Invoke", 13)]
    assert host.images == [] and len(host.events) == 1


def test_main_screen_inputs_share_control_until_execution_cleanup() -> None:
    """主屏输入使用截图坐标，多个动作共享提示，ESC 后不再执行下一动作。"""
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.api import Cua
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.types import CaptureRegion, Screenshot

    class Desktop:
        """记录发给 Windows 输入边界的独立动作。"""

        def __init__(self) -> None:
            """初始化输入记录。"""
            self.actions = []

        def screenshot(self, window=None) -> Screenshot:
            """提供固定主屏幕画面。"""
            return Screenshot(b"PNG", self.get_region(), 1000, 1000)

        def get_region(self, window=None) -> CaptureRegion:
            """返回具有负原点的物理屏幕范围。"""
            return CaptureRegion(-100, 20, 1000, 1000, 96)

        def create_escape_listener(self, callback):
            """保存物理 ESC 的外部中断入口。"""
            self.escape = callback
            return FakeEscapeListener(lambda: self.actions.append("unhook"))

        def release_inputs(self) -> None:
            """记录执行结束释放输入。"""
            self.actions.append("release")

        def ensure_point(self, window, point) -> None:
            """主屏动作接受已经换算的物理位置。"""

        def keyboard(self, keys, check) -> None:
            """记录单键或组合键。"""
            self.actions.append(("keys", keys))

        def type_text(self, text, check) -> None:
            """记录 Unicode 文本输入。"""
            self.actions.append(("text", text))

        def scroll(self, point, steps) -> None:
            """记录滚轮位置和方向。"""
            self.actions.append(("scroll", point, steps))

        def drag(self, start, end, check) -> None:
            """记录拖动的物理起终点。"""
            self.actions.append(("drag", start, end))

    host, desktop = RecordingHost(), Desktop()
    cua = Cua(host, desktop=desktop)
    cua.get_screenshot()
    cua.hotkey("ctrl", "a")
    cua.type_text("中文")
    cua.press_key("esc")
    cua.scroll(x=500, y=500, direction="down", amount=2)
    cua.drag(start_x=0, start_y=0, end_x=1000, end_y=1000)
    assert desktop.actions == [("keys", ("ctrl", "a")), ("text", "中文"), ("keys", ("esc",)),
                               ("scroll", (400, 520), -2), ("drag", (-100, 20), (899, 1019))]
    assert len(host.events) == 1
    desktop.escape()
    with pytest.raises(KeyboardInterrupt):
        cua.press_key("enter")
    host.cleanups[0]()
    assert desktop.actions[-2:] == ["unhook", "release"]


def test_environment_hook_refreshes_saved_screen_and_drops_failed_capture(monkeypatch) -> None:
    """默认关闭环境捕获，开启后每请求保存新图，捕获失败时删除旧条目。"""
    from io import BytesIO
    from PIL import Image
    from app.agent.plugins.base import HookContext, PluginContext, PluginHook
    from app.crud.sqlite_factory import SqliteFactory
    from app.agent.plugins.computer_use.plugin import ComputerUsePlugin, SCREEN_KEY
    from app.agent.plugins.computer_use.repl.ayaya_computer_use import capture
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.types import CaptureRegion, Screenshot
    from app.agent.state.model import AgentState
    from app.environment import get_images_dir

    output = BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, "PNG")
    captures = []

    def screen() -> Screenshot:
        """替代操作系统捕获，记录每次模型请求的观察。"""
        captures.append(True)
        return Screenshot(output.getvalue(), CaptureRegion(0, 0, 2, 2, 96), 2, 2)

    monkeypatch.setattr(capture, "capture_desktop", screen)
    state = AgentState(session_id="environment")
    context = HookContext.create(PluginHook.BEFORE_LLM, state)
    plugin_context = PluginContext(sqlite_factory=SqliteFactory())
    asyncio.run(ComputerUsePlugin(context=plugin_context).execute(context))
    assert not captures and SCREEN_KEY not in state.temporary_context
    plugin = ComputerUsePlugin(context=plugin_context, inject_screen_state=True)
    asyncio.run(plugin.execute(context))
    first = state.temporary_context[SCREEN_KEY].images[0]
    assert first.storage_ref.startswith("environment/")
    assert (get_images_dir() / first.storage_ref).read_bytes() == output.getvalue()
    asyncio.run(plugin.execute(context))
    second = state.temporary_context[SCREEN_KEY].images[0]
    assert second.storage_ref != first.storage_ref and len(captures) == 2

    def fail() -> Screenshot:
        """模拟捕获失去当前桌面。"""
        raise OSError("capture unavailable")

    monkeypatch.setattr(capture, "capture_desktop", fail)
    with pytest.raises(OSError):
        asyncio.run(plugin.execute(context))
    assert SCREEN_KEY not in state.temporary_context


def test_interrupt_during_listener_start_still_closes_listener() -> None:
    """ESC 监听安装阶段被停止时，已登记清理能回收尚未交付的监听资源。"""
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.api import Cua

    released = []

    class Listener:
        """在安装完成前模拟聊天停止。"""

        def start(self) -> None:
            """模拟注册全局钩子后立即收到中断。"""
            raise KeyboardInterrupt()

        def close(self) -> None:
            """解除已经安装的系统钩子。"""
            released.append("listener")

    class Desktop:
        """在资源创建与启动之间暴露安全的清理登记时机。"""

        def create_escape_listener(self, callback):
            """创建尚未安装的监听对象。"""
            return Listener()

        def release_inputs(self) -> None:
            """释放同一次控制的其他输入状态。"""
            released.append("input")

    host = RecordingHost()
    with pytest.raises(KeyboardInterrupt):
        Cua(host, desktop=Desktop()).press_key("enter")
    assert len(host.cleanups) == 1
    host.cleanups[0]()
    assert released == ["listener", "input"]


def test_observation_checks_interrupt_after_native_capture() -> None:
    """原生捕获返回时已受理停止，能力在输出图片前交付用户中断。"""
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.api import Cua
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.types import CaptureRegion, Screenshot
    host = RecordingHost()

    class Desktop:
        """模拟系统捕获期间收到已绑定执行编号的中断。"""

        def screenshot(self) -> Screenshot:
            """交付捕获结果，同时让待处理停止变为可见。"""
            host.interrupted = True
            return Screenshot(b"PNG", CaptureRegion(0, 0, 2, 2, 96), 2, 2)

    with pytest.raises(KeyboardInterrupt):
        Cua(host, desktop=Desktop()).get_screenshot()
    assert not host.images and not host.events


def test_unknown_api_and_argument_errors_point_to_help() -> None:
    """拼错方法或参数时，错误说明直接指向已注入能力的帮助。"""
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.api import Cua
    cua = Cua(RecordingHost())
    with pytest.raises(AttributeError, match="cua.help"):
        cua.unknown_method()
    with pytest.raises(TypeError, match="cua.help"):
        cua.click(unknown=1)


def test_tool_catalog_exposes_native_python_and_retires_old_screen_entries() -> None:
    """新目录保留原生 Python 与 computer_use，旧工具和旧插件已退出。"""
    from app.agent.tools.core.registry import ToolRegistry
    from app.agent.plugins.registry import PluginRegistry
    assert ToolRegistry.get("python") is not None
    assert ToolRegistry.get("python_reset") is not None
    for name in ("screenshot", "control_screen", "control_screen_with_vlm"):
        assert ToolRegistry.get(name) is None
    assert PluginRegistry.get("computer_use") is not None
    assert PluginRegistry.get("desktop_perception") is None


def test_uia_observation_replaces_indices_and_checks_live_control_ownership(monkeypatch) -> None:
    """通过真实窗口 API 读取 UIA 替身，新观察作废旧编号，脱离窗口的控件不可再改值。"""
    import sys
    from types import SimpleNamespace
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.api import Cua
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.accessibility import Accessibility
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.types import WindowInfo

    class Value:
        """模拟 UIA provider 提供的可写 ValuePattern。"""
        IsReadOnly = False
        Value = "before"

        def SetValue(self, value: str) -> None:
            """记录实际交给 provider 的完整值。"""
            self.Value = value

    value = Value()
    control_view = object()
    client = SimpleNamespace(ViewWalker=None,
        IUIAutomation=SimpleNamespace(ControlViewWalker=control_view))

    class Control:
        """模拟跨进程 SDK 提供的控件属性与父子关系。"""
        BoundingRectangle = SimpleNamespace(left=0, top=0, right=100, bottom=100)
        IsPassword = False
        IsEnabled = True
        IsControlElement = True
        HasKeyboardFocus = False

        def __init__(self, name: str, parent=None) -> None:
            """保存可由测试改变的系统控件归属。"""
            self.Name, self.parent = name, parent
            self.ControlTypeName = "EditControl" if parent else "WindowControl"
            self.children = []

        def GetPattern(self, pattern):
            """仅编辑控件具有 ValuePattern。"""
            return value if self.ControlTypeName == "EditControl" and pattern == 1 else None

        def GetChildren(self):
            """按库当前遍历器视图返回子控件。"""
            if client.ViewWalker is control_view:
                return [child for child in self.children if child.IsControlElement]
            return list(self.children)

        def GetParentControl(self):
            """返回系统当前提供的父控件。"""
            return self.parent

    root = Control("window")
    edit = Control("editor", root)
    decoration = Control("decoration", root)
    decoration.IsControlElement = False
    root.children.extend([edit, decoration])
    sdk = SimpleNamespace(ControlFromHandle=lambda hwnd: root, ControlsAreSame=lambda a, b: a is b,
        uiautomation=SimpleNamespace(_AutomationClient=SimpleNamespace(instance=lambda: client)),
        PatternId=SimpleNamespace(ValuePattern=1, TextPattern=2, InvokePattern=3, TogglePattern=4,
            SelectionItemPattern=5, ExpandCollapsePattern=6))
    monkeypatch.setitem(sys.modules, "uiautomation", sdk)

    class Desktop:
        """保留真实可访问性实现，只替换系统窗口和 provider。"""

        def __init__(self) -> None:
            """为准确测试窗口创建引用缓存。"""
            pass

        def window(self, window_id):
            """提供稳定窗口身份。"""
            return WindowInfo(window_id, "window", 7, "test.exe")

        def ax_state(self, window, controls):
            """从实际可访问性实现读取本次控件树。"""
            return controls.observe()

        def verify(self, window) -> None:
            """确认测试窗口仍存在。"""

        def set_value(self, window, controls, index, text) -> None:
            """通过实际引用校验执行 ValuePattern。"""
            controls.set_value(index, text)

        def create_escape_listener(self, callback):
            """提供无全局系统钩子的监听替身。"""
            return FakeEscapeListener(lambda: None)

        def release_inputs(self) -> None:
            """测试的语义动作没有按键资源。"""

    host = RecordingHost()
    cua = Cua(host, desktop=Desktop())
    window = cua.get_app(window_id="42")
    first_state = window.get_ax_state()
    assert [node.name for node in first_state.nodes] == ["window", "editor"]
    previous = first_state.nodes[-1].element_index
    current = window.get_ax_state().nodes[-1].element_index
    assert current != previous
    with pytest.raises(ValueError, match="失效"):
        window.set_value(element_index=previous, value="wrong")
    assert value.Value == "before"
    window.set_value(element_index=current, value="updated")
    assert value.Value == "updated"
    other = cua.get_app(window_id="42")
    with pytest.raises(ValueError, match="失效"):
        other.set_value(element_index=current, value="wrong")
    other.get_ax_state()
    with pytest.raises(ValueError, match="失效"):
        other.set_value(element_index=current, value="wrong")
    window.set_value(element_index=current, value="still owned")
    assert value.Value == "still owned"
    edit.parent = None
    with pytest.raises(ValueError, match="离开"):
        window.set_value(element_index=current, value="wrong")
    assert value.Value == "still owned"


@pytest.mark.parametrize("mismatch", [False, True])
def test_hwnd_capture_scales_exact_frame_and_rejects_uncertain_bounds(monkeypatch, capsys, mismatch) -> None:
    """准确 HWND 捕获保留物理范围，尺寸不匹配时停止并报告边界错误。"""
    import sys
    from types import SimpleNamespace
    from io import BytesIO
    from PIL import Image
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.api import Cua
    from app.agent.plugins.computer_use.repl.ayaya_computer_use import capture
    from app.agent.plugins.computer_use.repl.ayaya_computer_use.types import CaptureRegion, WindowInfo

    handles, stopped = [], []
    region = CaptureRegion(-400, 50, 3840, 2160, 144)

    class Control:
        """模拟 WGC 后台捕获资源。"""

        def stop(self) -> None:
            """记录正常和失败路径的捕获回收。"""
            stopped.append(True)

        def wait(self) -> None:
            """首帧交付后捕获线程已经停止。"""

    control = Control()

    class Pixels:
        """按 SDK 的帧有效期规则提供 BGRA 数据副本。"""

        def copy(self):
            """交付由回调持有的独立像素。"""
            return self

        def tobytes(self) -> bytes:
            """提供目标窗口的确定像素。"""
            return bytes([20, 40, 80, 255]) * width * 2160

    width = 3839 if mismatch else 3840
    frame = SimpleNamespace(frame_buffer=Pixels(), width=width, height=2160)

    class WindowsCapture:
        """只替换第三方捕获 SDK，保留正式捕获边界核对及编码。"""

        def __init__(self, *, window_hwnd, cursor_capture, draw_border) -> None:
            """记录传给 SDK 的准确窗口，不接受标题作为替代。"""
            handles.append(window_hwnd)
            self.callbacks = {}

        def event(self, callback):
            """登记实际 SDK 要求的帧和关闭回调。"""
            self.callbacks[callback.__name__] = callback
            return callback

        def start_free_threaded(self):
            """交付首帧，模拟 SDK 已复制有效的目标窗口像素。"""
            self.callbacks["on_frame_arrived"](frame, control)
            return control

    monkeypatch.setitem(sys.modules, "windows_capture", SimpleNamespace(
        WindowsCapture=WindowsCapture, Frame=object, InternalCaptureControl=object))
    monkeypatch.setattr(capture, "window_region", lambda hwnd: region)

    class Desktop:
        """提供系统身份，窗口画面使用正式 WGC 捕获函数。"""

        def window(self, window_id):
            """两个同名窗口按准确句柄区分。"""
            return WindowInfo(window_id, "same title", 7, "test.exe")

        def screenshot(self, window):
            """将本对象的准确 HWND 传入真实捕获实现。"""
            return capture.capture_window(int(window.window_id))

    host = RecordingHost()
    cua = Cua(host, desktop=Desktop())
    cua.get_app(window_id="41")
    target = cua.get_app(window_id="42")
    if mismatch:
        with pytest.raises(RuntimeError, match="边界"):
            target.get_screenshot()
        assert not host.images
    else:
        shot = target.get_screenshot()
        assert shot.region == region and (shot.width, shot.height) == (1920, 1080)
        with Image.open(BytesIO(host.images[0])) as image:
            assert image.size == (1920, 1080)
        assert "left=-400" in capsys.readouterr().out
    assert handles == [42] and len(stopped) == 2
