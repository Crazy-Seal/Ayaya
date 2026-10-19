"""通过控制器公开接口验证平台驱动工厂、工作线程和应用关闭的约定。"""

import asyncio
import threading
from contextlib import contextmanager

import pytest

from app.desktop.desktop_controller import DesktopController, DesktopStopped
from app.schemas.desktop import DesktopSettings, DesktopWindowRegistration, ScreenAction, ScreenCapture


def test_driver_is_lazy_reused_and_closed_on_the_same_worker() -> None:
    """不提供任何 Windows 窗口方法的驱动也能完成登记、截图和批量操作。"""
    calls = []
    hidden_settings = []
    settings = DesktopSettings()

    def record(name):
        """记录公开平台接口发生在哪个线程，用于检查原生资源的线程归属。

        Args:
            name (str): 当前调用的方法或清理阶段名称。
        """
        calls.append((name, threading.get_ident()))

    class PlatformDriver:
        """仅实现控制器所需的平台接口，不提供 HWND 或恢复文件处理方法。"""

        def register_windows(self, registration):
            """接收原始登记，控制器无需解释窗口身份。

            Args:
                registration (DesktopWindowRegistration): 模拟 Electron 提供的完整快照。
            Returns:
                dict: 驱动自己的登记回执。
            """
            record("register")
            return {"accepted": True, "revision": registration.revision}

        @contextmanager
        def window_guard(self, *, hide, check):
            """记录控制器传入的隐藏设置，并检查正常结束前执行窗口清理。

            Args:
                hide (bool): 当前设置是否要求临时隐藏应用窗口。
                check (Callable): 控制器的停止检查。
            Yields:
                None: 平台窗口已经准备就绪。
            """
            check()
            hidden_settings.append(hide)
            record("window_enter")
            try:
                yield
            finally:
                record("window_exit")

        @contextmanager
        def input_guard(self, text):
            """检查整批输入资源在窗口恢复之后清理。

            Args:
                text (str): 本批次需要粘贴的文字。
            Yields:
                None: 允许输入的范围。
            """
            record("input_enter")
            try:
                yield
            finally:
                record("input_exit")

        def capture(self):
            """返回平台无关的观察内容。

            Returns:
                ScreenCapture: 隔离测试的图片与尺寸。
            """
            record("capture")
            return ScreenCapture("data:image/png;base64,test", 20, 10)

        def mouse_action(self, action, check):
            """记录批次中的鼠标阶段。

            Args:
                action (ScreenAction): 当前动作配置。
                check (Callable): 控制器的停止检查。
            """
            check()
            record(action.operation)

        def paste(self, text, check):
            """记录当前动作的文字阶段。

            Args:
                text (str): 当前动作的文字。
                check (Callable): 控制器的停止检查。
            """
            check()
            record(text)

        def press_enter(self):
            """记录回车阶段。"""
            record("enter")

        def close(self):
            """记录驱动资源释放线程。"""
            record("close")

    def create_driver():
        """仅由控制器工作线程创建驱动，记录实际创建次数。

        Returns:
            PlatformDriver: 不依赖操作系统的完整平台接口实现。
        """
        record("create")
        return PlatformDriver()

    async def scenario():
        """在单个应用生命周期内复用驱动，再检查关闭及拒绝新操作。"""
        desktop = DesktopController(lambda: settings, driver_factory=create_driver)
        desktop.start()
        assert not calls
        try:
            receipt = await desktop.register_windows(DesktopWindowRegistration(owner_pid=100, revision=2, windows={}))
            assert receipt == {"accepted": True, "revision": 2}
            assert (await desktop.capture_screen()).width == 20
            settings.hide_on_screenshot = False
            result = await desktop.perform_actions((ScreenAction(0.5, 0.5, "click", text="文字", wait_seconds=0),))
            assert result.status == "completed" and result.completed_actions == 1
            assert hidden_settings == [True, False]
        finally:
            await desktop.close()
        await desktop.close()
        with pytest.raises(DesktopStopped):
            await desktop.capture_screen()

    asyncio.run(scenario())
    assert [name for name, _thread in calls] == [
        "create", "register", "window_enter", "capture", "window_exit",
        "input_enter", "window_enter", "click", "文字", "enter", "capture",
        "window_exit", "input_exit", "close",
    ]
    worker_threads = {thread for _name, thread in calls}
    assert len(worker_threads) == 1 and threading.get_ident() not in worker_threads


def test_unused_controller_closes_without_creating_a_driver() -> None:
    """应用没有发起桌面操作时，启动和退出都不应该创建平台资源。"""
    def create_driver():
        """若未使用桌面仍创建驱动，立即使测试失败。

        Raises:
            AssertionError: 工厂在没有桌面请求时被调用。
        """
        raise AssertionError("未使用桌面时不应创建驱动")

    async def scenario():
        """只开放并关闭控制器，再确认不允许重新启动已关闭对象。"""
        desktop = DesktopController(lambda: DesktopSettings(), driver_factory=create_driver)
        desktop.start()
        await desktop.close()
        await desktop.close()
        with pytest.raises(RuntimeError, match="已经关闭"):
            desktop.start()

    asyncio.run(scenario())
