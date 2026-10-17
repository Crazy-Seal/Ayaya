"""串行执行本机屏幕操作，工具不需要理解窗口、线程与清理顺序。"""

from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from app.runtime import get_data_dir
from app.schemas.desktop import (
    DesktopSettings, DesktopWindowRegistration, ScreenAction, ScreenActionResult,
    ScreenCapture, StopRequested,
)

T = TypeVar("T")


class DesktopStopped(RuntimeError):
    """桌面工作在安全点收到停止信号，已有副作用不能撤回。"""


class DesktopController:
    """持有全应用唯一的输入线程、窗口身份与关闭状态。

    Attributes:
        _settings_reader (Callable[[], DesktopSettings]): 每次操作开始读取后端 YAML。
        _adapter (Any | None): 工作线程专用的 Windows adapter，测试替换操作系统。
        _restore_path (Path): 隐藏前原子保存的窗口恢复记录，不是工具执行队列。
        _lock (asyncio.Lock): 截图、动作和登记共同串行，清理结束前不能释放。
    """

    def __init__(self, settings_reader: Callable[[], DesktopSettings], *, adapter: Any = None,
                 restore_path: Path | None = None, owner_pid: int | None = None) -> None:
        """保存依赖，构造时不截图、创建原生窗口或读取生产配置。

        Args:
            settings_reader (Callable[[], DesktopSettings]): 共享配置 DAO 的读取方法。
            adapter (Any): 可替换的操作系统 adapter，生产在工作线程内延迟创建。
            restore_path (Path | None): 当前环境的数据目录或测试独立恢复文件。
            owner_pid (int | None): 受管 Electron 所有者，省略时读取启动环境。
        """
        self._settings_reader = settings_reader
        self._adapter = adapter
        self._restore_path = restore_path or get_data_dir() / "desktop" / "window_restore.json"
        self._owner_pid = owner_pid or int(os.getenv("AYAYA_PARENT_PID") or 0)
        self._registration: DesktopWindowRegistration | None = None
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

    def _native(self) -> Any:
        """仅在唯一工作线程中创建操作系统对象，避免测试导入产生桌面副作用。

        Returns:
            Any: 此线程唯一的 Windows adapter 或隔离测试替身。
        """
        if self._adapter is None:
            from app.desktop.windows_desktop_driver import WindowsDesktopDriver
            self._adapter = WindowsDesktopDriver()
        return self._adapter

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
        """校验 HWND 归属后替换完整快照，旧 revision 不覆盖当前窗口。

        Args:
            registration (DesktopWindowRegistration): 仅包含 pet/chat 的主进程快照。

        Returns:
            dict: 是否接受及供 Electron 退出兜底使用的恢复记录路径。
        """
        def register(_cancelled: threading.Event) -> dict:
            """登记与操作共用工作线程，身份变化不能发生在输入或恢复中途。

            Args:
                _cancelled (threading.Event): 本次等待的取消状态，关闭时不能迟到登记。
            Returns:
                dict: 接受标记及隔离恢复记录路径。
            """
            self._check(_cancelled)
            if self._owner_pid and registration.owner_pid != self._owner_pid:
                raise ValueError("窗口所有者与当前 Electron 进程不一致")
            if self._registration and registration.revision <= self._registration.revision:
                return {"accepted": False, "window_restore_path": str(self._restore_path)}
            native = self._native()
            for handle in registration.windows.model_dump().values():
                if handle is not None and int(handle) >= 1 << (ctypes.sizeof(ctypes.c_void_p) * 8):
                    raise ValueError("桌面窗口句柄超出指针范围")
                if handle is not None and native.window_owner(int(handle)) != registration.owner_pid:
                    raise ValueError("桌面窗口句柄不存在或不属于登记进程")
            self._owner_pid = registration.owner_pid
            self._registration = registration
            return {"accepted": True, "window_restore_path": str(self._restore_path)}
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

    def _write_restore_record(self, windows: list[dict]) -> None:
        """先原子保存可恢复窗口，后续才允许调用系统隐藏。

        Args:
            windows (list[dict]): 本次原本可见窗口的角色及十进制句柄。
        """
        self._restore_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._restore_path.with_suffix(".tmp")
        record = {"owner_pid": self._owner_pid, "backend_pid": os.getpid(), "windows": windows}
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(self._restore_path)

    @contextmanager
    def _window_guard(self, cancelled: threading.Event) -> Iterator[None]:
        """操作期间隐藏指定窗口，结束后只恢复原本可见且没有被最小化的对象。

        Args:
            cancelled (threading.Event): 当前操作的停止通知，恢复阶段不再读取。
        Yields:
            None: 窗口已完成隐藏和缓冲，可以执行本次桌面观察与输入。
        """
        self._check(cancelled)
        if self._owner_pid and self._registration is None:
            raise RuntimeError("桌面窗口尚未登记，不能开始屏幕操作")
        native = self._native()
        # 1. 上次清理未完成时先重试匹配窗口；不允许新记录覆盖仍隐藏窗口的恢复身份。
        if self._restore_path.exists():
            record = json.loads(self._restore_path.read_text(encoding="utf-8"))
            known = self._registration.windows.model_dump() if self._registration else {}
            if record.get("owner_pid") != self._owner_pid:
                # 1.1. 整个应用重启后 PID 已变化；旧窗口均销毁的记录不能永久阻塞新应用。
                if any(native.window_owner(int(item["hwnd"])) == record.get("owner_pid")
                       for item in record.get("windows", [])):
                    raise RuntimeError("已有窗口恢复记录仍属于存活的旧 Electron，不能覆盖")
            else:
                remaining = [item for item in record.get("windows", [])
                             if item.get("role") in known and known[item["role"]] == item.get("hwnd")]
                self._restore_windows(remaining)
            self._restore_path.unlink(missing_ok=True)
        hidden: list[dict] = []
        if self._settings_reader().hide_on_screenshot and self._registration:
            # 2. 只处理登记角色；已销毁的句柄跳过，绝不枚举同进程其他窗口。
            for role, handle in self._registration.windows.model_dump().items():
                if handle is not None:
                    hwnd = int(handle)
                    if native.window_owner(hwnd) == self._owner_pid and native.visible(hwnd) and not native.minimized(hwnd):
                        hidden.append({"role": role, "hwnd": handle})
        if hidden:
            self._write_restore_record(hidden)
        try:
            # 3. 隐藏请求发出不等于完成，确认系统状态后才进入捕获缓冲。
            deadline = time.monotonic() + 1
            for window in hidden:
                hwnd = int(window["hwnd"])
                native.hide(hwnd)
                while native.visible(hwnd):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("隐藏桌面窗口超时")
                    self._pause(0.01, cancelled)
            if hidden:
                self._pause(0.12, cancelled)
            yield
        finally:
            # 4. 清理不再检查停止；失败时保留原记录，供下次操作或 Electron 崩溃恢复使用。
            self._restore_windows(hidden)
            if hidden:
                self._restore_path.unlink(missing_ok=True)

    def _restore_windows(self, windows: list[dict]) -> None:
        """尝试全部本次匹配窗口，一个恢复失败不会跳过其余窗口。

        Args:
            windows (list[dict]): 已核对角色身份的本次隐藏窗口或未完成恢复记录。
        Raises:
            RuntimeError: 至少一个存活且未最小化的窗口恢复失败。
        """
        native = self._native()
        failures = []
        for window in windows:
            hwnd = int(window["hwnd"])
            try:
                if native.window_owner(hwnd) == self._owner_pid and not native.minimized(hwnd):
                    native.show_inactive(hwnd)
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError("桌面窗口恢复失败：" + "; ".join(failures))

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
            with self._window_guard(cancelled):
                self._check(cancelled)
                return self._native().capture()
        return await self._run(capture, stop_requested)

    async def perform_action(self, action: ScreenAction, *, stop_requested: StopRequested | None = None) -> ScreenActionResult:
        """执行已获准动作并截图，清理失败或捕获失败均保留已发生动作的状态。

        Args:
            action (ScreenAction): Pipeline 已获准的鼠标位置、输入内容及等待配置。
            stop_requested (StopRequested | None): 当前轮次的只读停止状态。
        Returns:
            ScreenActionResult: 未开始、部分或不确定、已完成及可选截图和错误。
        """
        def perform(cancelled: threading.Event) -> ScreenActionResult:
            """按输入阶段记录副作用，异常时返回具体执行程度。

            Args:
                cancelled (threading.Event): 当前动作的停止通知。
            Returns:
                ScreenActionResult: 保留已发生输入的程度以及捕获或清理错误。
            """
            status = "not_started"
            screenshot = None
            try:
                # 1. 参数和剪贴板预检查必须先于鼠标动作，避免拒绝输入后仍产生点击。
                if not all(math.isfinite(value) and 0 <= value <= 1 for value in (action.x_ratio, action.y_ratio)):
                    raise ValueError("屏幕比例坐标无效")
                if action.operation not in {"click", "double", "right", "scroll"} or not 0 <= action.wait_seconds <= 60:
                    raise ValueError("屏幕动作参数无效")
                native = self._native()
                with native.input_guard(action.text):
                    with self._window_guard(cancelled):
                        self._check(cancelled)
                        status = "partial_or_unknown"
                        native.mouse_action(action, lambda: self._check(cancelled))
                        self._check(cancelled)
                        if action.text:
                            native.paste(action.text, lambda: self._check(cancelled))
                            self._check(cancelled)
                            if action.press_enter:
                                native.press_enter()
                        status = "completed"
                        # 2. 页面等待和截图失败不能把已完成的输入改成未执行。
                        self._pause(action.wait_seconds, cancelled)
                        screenshot = native.capture()
                return ScreenActionResult(status, screenshot)
            except Exception as exc:
                return ScreenActionResult(status, screenshot, str(exc))
        try:
            return await self._run(perform, stop_requested)
        except DesktopStopped as exc:
            return ScreenActionResult("not_started", error=str(exc))

    async def close(self) -> None:
        """封住入口，等当前工作及清理退出，最后在同一线程释放原生资源。"""
        self.begin_shutdown()
        async with self._lock:
            if self._closed:
                return
            if self._adapter is not None:
                try:
                    await asyncio.get_running_loop().run_in_executor(self._executor, self._adapter.close)
                finally:
                    self._executor.shutdown(wait=True)
                    self._closed = True
            else:
                self._executor.shutdown(wait=True)
                self._closed = True
