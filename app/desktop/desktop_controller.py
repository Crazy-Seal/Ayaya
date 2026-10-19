"""串行执行本机屏幕操作，工具不需要理解窗口、线程与清理顺序。"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

from app.desktop.driver import DesktopDriver
from app.schemas.desktop import (
    DesktopSettings, DesktopWindowRegistration, ScreenAction, ScreenActionResult,
    ScreenCapture, StopRequested,
)

T = TypeVar("T")


class DesktopStopped(RuntimeError):
    """桌面工作在安全点收到停止信号，已有副作用不能撤回。"""


class DesktopController:
    """持有全应用唯一的执行线程，协调平台驱动完成截图、批量输入与清理。

    Attributes:
        _settings_reader (Callable[[], DesktopSettings]): 每次操作开始读取共享设置。
        _driver_factory (Callable[[], DesktopDriver]): 在工作线程首次需要时创建平台驱动。
        _driver (DesktopDriver | None): 工作线程独占的驱动，控制器不解释原生窗口身份。
        _lock (asyncio.Lock): 截图、动作和登记共同串行，清理结束前不能释放。
    """

    def __init__(self, settings_reader: Callable[[], DesktopSettings], *,
                 driver_factory: Callable[[], DesktopDriver]) -> None:
        """保存应用注入的设置与驱动工厂，构造时不操作桌面或创建原生资源。

        Args:
            settings_reader (Callable[[], DesktopSettings]): 共享配置 DAO 的读取方法。
            driver_factory (Callable[[], DesktopDriver]): 应用选择的平台驱动工厂，仅在工作线程调用。
        """
        self._settings_reader = settings_reader
        self._driver_factory = driver_factory
        self._driver: DesktopDriver | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ayaya-desktop")
        self._lock = asyncio.Lock()
        self._shutdown = threading.Event()
        self._accepting = False
        self._closed = False

    def start(self) -> None:
        """运行时完成旧轮次收尾后开放入口，不主动执行任何桌面动作。"""
        if self._closed:
            raise RuntimeError("桌面模块已经关闭")
        self._accepting = True

    def begin_shutdown(self) -> None:
        """拒绝新操作并通知工作线程在下一安全点清理，调用方仍等待原任务退出。"""
        self._accepting = False
        self._shutdown.set()

    def _get_driver(self) -> DesktopDriver:
        """在唯一工作线程中延迟创建并复用应用选择的平台驱动。

        Returns:
            DesktopDriver: 本控制器独占的驱动或隔离操作系统的测试替身。
        """
        if self._driver is None:
            self._driver = self._driver_factory()
        return self._driver

    async def _run(self, operation: Callable[[threading.Event], T], stop_requested: StopRequested | None = None) -> T:
        """序列化工作并屏蔽线程清理等待的取消，不让下一动作进入旧线程。

        Args:
            operation (Callable[[threading.Event], T]): 工作线程执行的一个完整操作。
            stop_requested (StopRequested | None): Pipeline 提供的只读打断检查。

        Returns:
            T: 已完成窗口与输入清理的结果。
        """
        # 1. 等锁期间定期检查停止，尚未进入工作线程就不能产生副作用。
        while True:
            if not self._accepting or (stop_requested and stop_requested()):
                raise DesktopStopped("屏幕操作已停止")
            try:
                async with asyncio.timeout(0.05):
                    await self._lock.acquire()
                break
            except TimeoutError:
                pass
        cancelled = threading.Event()
        worker = None
        try:
            if not self._accepting or (stop_requested and stop_requested()):
                raise DesktopStopped("屏幕操作已停止")
            worker = asyncio.get_running_loop().run_in_executor(self._executor, operation, cancelled)
            # 2. 只把停止信号交给线程；不取消 executor Future，否则无法确认 finally 已结束。
            while not worker.done():
                if stop_requested and stop_requested():
                    cancelled.set()
                await asyncio.wait({worker}, timeout=0.025)
            return worker.result()
        except asyncio.CancelledError:
            cancelled.set()
            # 3. 应用关闭取消 worker 时，继续等真实线程清理；重复取消也不能提前放行锁。
            if worker is not None:
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if worker.done() and not worker.cancelled():
                    worker.exception()
            raise
        finally:
            self._lock.release()

    async def register_windows(self, registration: DesktopWindowRegistration) -> dict:
        """串行交给平台驱动登记窗口，确保登记不能发生在输入或恢复中途。

        Args:
            registration (DesktopWindowRegistration): Electron 提供的完整窗口身份快照。
        Returns:
            dict: 驱动返回的接收结果和崩溃恢复文件位置。
        """
        def register(cancelled: threading.Event) -> dict:
            """由工作线程先检查停止，再让驱动验证和保存窗口身份。

            Args:
                cancelled (threading.Event): 当前登记请求的停止通知。
            Returns:
                dict: 平台驱动的登记回执。
            """
            self._check(cancelled)
            return self._get_driver().register_windows(registration)

        return await self._run(register)

    def _check(self, cancelled: threading.Event) -> None:
        """线程在鼠标、键盘和等待安全点检查停止，关闭信号与单次取消共同生效。

        Args:
            cancelled (threading.Event): 当前操作的取消通知。
        Raises:
            DesktopStopped: 当前轮次停止、等待被取消或应用正在关闭。
        """
        if self._shutdown.is_set() or cancelled.is_set():
            raise DesktopStopped("屏幕操作已停止，已经发生的动作不会撤回")

    def _pause(self, seconds: float, cancelled: threading.Event) -> None:
        """按短间隔等待目标页面更新，停止时立即进入清理路径。

        Args:
            seconds (float): 当前需要等待的秒数。
            cancelled (threading.Event): 当前操作的停止通知。
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check(cancelled)
            cancelled.wait(min(0.025, max(0, deadline - time.monotonic())))
        self._check(cancelled)

    async def capture_screen(self, *, stop_requested: StopRequested | None = None) -> ScreenCapture:
        """隐藏配置要求的窗口、取得主屏图像并清理，再返回给工具。

        Args:
            stop_requested (StopRequested | None): 当前轮次已受理停止或引导时返回 True。
        Returns:
            ScreenCapture: 已恢复窗口后的主屏图片和输出尺寸。
        """
        def capture(cancelled: threading.Event) -> ScreenCapture:
            """在独占线程的显隐上下文中截图，异常也执行恢复。

            Args:
                cancelled (threading.Event): 当前捕获的停止通知。
            Returns:
                ScreenCapture: 经过窗口清理的观察结果。
            """
            driver = self._get_driver()
            with driver.window_guard(hide=self._settings_reader().hide_on_screenshot,
                                     check=lambda: self._check(cancelled)):
                self._check(cancelled)
                return driver.capture()
        return await self._run(capture, stop_requested)

    async def perform_actions(
        self, actions: tuple[ScreenAction, ...], *, stop_requested: StopRequested | None = None,
    ) -> ScreenActionResult:
        """在同一把锁及窗口隐藏期间顺序执行整批动作，结束时只捕获一次截图。

        Args:
            actions (tuple[ScreenAction, ...]): Pipeline 已整批获准的鼠标、输入和等待配置。
            stop_requested (StopRequested | None): 当前轮次的只读停止状态。

        Returns:
            ScreenActionResult: 真实执行程度、已完成数量及可选的最终截图；失败不重试余下动作。
        """
        actions = tuple(actions)

        def perform(cancelled: threading.Event) -> ScreenActionResult:
            """由独占桌面线程执行整批动作，异常也先清理剪贴板和窗口再返回。

            Args:
                cancelled (threading.Event): 当前批次的停止通知。

            Returns:
                ScreenActionResult: 保留已发生输入的程度和完成数量，以及捕获或清理错误。
            """
            status = "not_started"
            completed = 0
            screenshot = None
            try:
                # 1. 先校验全部动作，避免执行了前面的动作才发现后面的坐标或配置无效。
                if not actions:
                    raise ValueError("屏幕动作数组不能为空")
                for action in actions:
                    if not all(math.isfinite(value) and 0 <= value <= 1 for value in (action.x_ratio, action.y_ratio)):
                        raise ValueError("屏幕比例坐标无效")
                    if action.operation not in {"click", "double", "right", "scroll"}:
                        raise ValueError("屏幕动作参数无效")
                    if type(action.wait_seconds) is not int or not 0 <= action.wait_seconds <= 60:
                        raise ValueError("等待时长必须是 0 到 60 之间的整数")
                    if action.operation == "scroll" and action.scroll_direction not in {"up", "down"}:
                        raise ValueError("滚动方向必须是 up 或 down")
                    if not isinstance(action.text, str) or type(action.press_enter) is not bool:
                        raise ValueError("文字或回车参数无效")
                driver = self._get_driver()
                # 2. 整批只保存一次剪贴板。只要有任意动作需要输入，就必须在第一次点击前预检查。
                clipboard_text = next((action.text for action in actions if action.text), "")
                with driver.input_guard(clipboard_text):
                    with driver.window_guard(hide=self._settings_reader().hide_on_screenshot,
                                             check=lambda: self._check(cancelled)):
                        for action in actions:
                            # 3. 每个鼠标和键盘阶段检查停止；一旦失败就退出循环，绝不重试或继续。
                            self._check(cancelled)
                            status = "partial_or_unknown"
                            driver.mouse_action(action, lambda: self._check(cancelled))
                            self._check(cancelled)
                            if action.text:
                                driver.paste(action.text, lambda: self._check(cancelled))
                                self._check(cancelled)
                                if action.press_enter:
                                    driver.press_enter()
                            completed += 1
                            status = "completed" if completed == len(actions) else "partial_or_unknown"
                            # 3.1. 当前动作的等待失败不会抹去已完成数量，也不会放行下一动作。
                            self._pause(action.wait_seconds, cancelled)
                        # 4. 完成整个数组后才截图；捕获或清理失败仍保留全部输入已完成的事实。
                        screenshot = driver.capture()
                return ScreenActionResult(status, screenshot, completed_actions=completed, total_actions=len(actions))
            except Exception as exc:
                return ScreenActionResult(status, screenshot, str(exc), completed, len(actions))

        try:
            return await self._run(perform, stop_requested)
        except DesktopStopped as exc:
            return ScreenActionResult("not_started", error=str(exc), total_actions=len(actions))

    async def close(self) -> None:
        """封住入口，等当前工作及清理退出，最后在同一线程释放原生资源。"""
        self.begin_shutdown()
        async with self._lock:
            if self._closed:
                return
            if self._driver is not None:
                try:
                    await asyncio.get_running_loop().run_in_executor(self._executor, self._driver.close)
                finally:
                    self._executor.shutdown(wait=True)
                    self._closed = True
            else:
                self._executor.shutdown(wait=True)
                self._closed = True
