"""集中处理准确窗口身份、UIA、截图、真实输入和系统资源。"""

from collections.abc import Callable
import ctypes
from ctypes import wintypes as w
from pathlib import PureWindowsPath
import time

from .accessibility import Accessibility
from .capture import capture_desktop, capture_window, desktop_region, physical_pixels, window_region
from .types import AxState, CaptureRegion, Screenshot, WindowInfo
from .native_input import NativeInput
from .escape import EscapeListener


class ScreenHelper:
    """向能力 API 提供单个系统边界，桌面资源在实际使用时创建。"""

    def __init__(self) -> None:
        """准备窗口查询函数，输入资源在首次控制时创建。"""
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        definitions = {
            "IsWindow": ([w.HWND], w.BOOL), "IsWindowVisible": ([w.HWND], w.BOOL),
            "IsIconic": ([w.HWND], w.BOOL), "GetWindowTextLengthW": ([w.HWND], ctypes.c_int),
            "GetWindowTextW": ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowThreadProcessId": ([w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            "SetForegroundWindow": ([w.HWND], w.BOOL), "GetForegroundWindow": ([], w.HWND),
            "GetAncestor": ([w.HWND, w.UINT], w.HWND), "WindowFromPoint": ([w.POINT], w.HWND),
        }
        for name, (arguments, result) in definitions.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = arguments, result
        self._input: NativeInput | None = None

    def window(self, window_id: str) -> WindowInfo:
        """读取准确 HWND 对应窗口的当前身份。

        Args:
            window_id (str): 窗口清单中的十进制句柄。

        Returns:
            WindowInfo: 存活窗口的进程与可读名称。

        Raises:
            ValueError: 窗口编号的类型或格式无效，或对应窗口已经失效。
        """
        if not isinstance(window_id, str):
            raise ValueError(
                f"window_id 必须为十进制字符串（str），收到 {type(window_id).__name__}；"
                '请直接使用窗口清单中的 window.window_id，或传入带引号的编号，例如 cua.get_app(window_id="132198")；'
                "请参考 cua.help()"
            )
        if not window_id.isdecimal():
            raise ValueError(
                "window_id 必须为非空十进制字符串；"
                "请使用 cua.list_windows() 返回的 window.window_id；请参考 cua.help()"
            )
        hwnd = int(window_id)
        if hwnd <= 0 or hwnd >= 1 << (ctypes.sizeof(ctypes.c_void_p) * 8) or not self.user.IsWindow(hwnd):
            raise ValueError("窗口已经失效，请重新列举窗口")
        pid = w.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(self.user.GetWindowTextLengthW(hwnd) + 1)
        self.user.GetWindowTextW(hwnd, title, len(title))
        kernel = ctypes.WinDLL("kernel32")
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        process = kernel.OpenProcess(0x1000, False, pid.value)
        name = ""
        if process:
            try:
                buffer, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
                if kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                    name = PureWindowsPath(buffer.value).name
            finally:
                kernel.CloseHandle(process)
        return WindowInfo(window_id, title.value, pid.value, name)

    def list_windows(self) -> list[WindowInfo]:
        """枚举可见顶层窗口，保留同进程及同名窗口各自的准确编号。

        Returns:
            list[WindowInfo]: 可绑定的当前窗口列表。
        """
        windows: list[WindowInfo] = []
        callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        @callback_type
        def collect(hwnd: int, parameter: int) -> bool:
            """将仍存活的可见窗口加入清单。"""
            if self.user.IsWindowVisible(hwnd):
                try:
                    windows.append(self.window(str(hwnd)))
                except ValueError:
                    pass
            return True

        self.user.EnumWindows.argtypes = [callback_type, w.LPARAM]
        self.user.EnumWindows(collect, 0)
        return windows

    def verify(self, window: WindowInfo) -> None:
        """操作前核对准确窗口仍属于绑定进程。

        Args:
            window (WindowInfo): 绑定时记录的身份。
        """
        if self.window(window.window_id).pid != window.pid:
            raise ValueError("窗口所属进程已经变化，请重新绑定")

    def get_region(self, window: WindowInfo | None = None) -> CaptureRegion:
        """读取当前目标的桌面物理范围。

        Args:
            window (WindowInfo | None): 指定窗口或主屏幕。

        Returns:
            CaptureRegion: 与截图对应的物理范围。
        """
        if window is None:
            return desktop_region()
        self.verify(window)
        if self.user.IsIconic(int(window.window_id)):
            raise ValueError("窗口已最小化，请恢复后重新观察")
        return window_region(int(window.window_id))

    def screenshot(self, window: WindowInfo | None = None) -> Screenshot:
        """显式捕获主屏幕或单个准确窗口。

        Args:
            window (WindowInfo | None): 指定窗口或主屏幕。

        Returns:
            Screenshot: 图片字节、观察尺寸和物理范围。
        """
        if window is None:
            return capture_desktop()
        self.get_region(window)
        return capture_window(int(window.window_id))

    def ax_state(self, window: WindowInfo, controls: Accessibility) -> AxState:
        """读取目标窗口最新控件树并替换旧引用。

        Args:
            window (WindowInfo): 已绑定窗口。
            controls (Accessibility): 调用窗口对象持有的观察引用。

        Returns:
            AxState: 最新可访问性观察。
        """
        self.verify(window)
        with physical_pixels():
            return controls.observe()

    def set_value(self, window: WindowInfo, controls: Accessibility, index: int, value: str) -> None:
        """对当前窗口中的可写控件设置值。

        Args:
            window (WindowInfo): 已绑定窗口。
            controls (Accessibility): 调用窗口对象持有的观察引用。
            index (int): 最近控件编号。
            value (str): 需要设置的内容。
        """
        self.verify(window)
        controls.set_value(index, value)

    def perform(self, window: WindowInfo, controls: Accessibility, index: int, action: str) -> None:
        """执行当前窗口控件实际支持的语义动作。

        Args:
            window (WindowInfo): 已绑定窗口。
            controls (Accessibility): 调用窗口对象持有的观察引用。
            index (int): 最近控件编号。
            action (str): 当前观察列出的动作。
        """
        self.verify(window)
        controls.perform(index, action)

    def click_point(self, window: WindowInfo, controls: Accessibility, index: int) -> tuple[int, int]:
        """读取当前控件的物理可点击位置。

        Args:
            window (WindowInfo): 已绑定窗口。
            controls (Accessibility): 调用窗口对象持有的观察引用。
            index (int): 最近控件编号。

        Returns:
            tuple[int, int]: 桌面物理坐标。
        """
        self.verify(window)
        with physical_pixels():
            return controls.point(index)

    def activate(self, window: WindowInfo) -> None:
        """真实键鼠输入前请求并确认准确目标取得前台。

        Args:
            window (WindowInfo): 已绑定目标窗口。
        """
        self.verify(window)
        hwnd = int(window.window_id)
        self.user.SetForegroundWindow(hwnd)
        deadline = time.monotonic() + 1
        while self.user.GetForegroundWindow() != hwnd:
            if time.monotonic() >= deadline:
                raise RuntimeError("系统没有允许激活目标窗口，请确认目标后重试")
            time.sleep(.01)

    def ensure_point(self, window: WindowInfo | None, point: tuple[int, int]) -> None:
        """确认物理点击位置位于绑定窗口，防止遮挡窗口接收输入。

        Args:
            window (WindowInfo | None): 指定窗口或整个主屏幕。
            point (tuple[int, int]): 准备点击的桌面物理位置。
        """
        if window is not None:
            self.verify(window)
            with physical_pixels():
                target = self.user.WindowFromPoint(w.POINT(*point))
                if self.user.GetAncestor(target, 2) != int(window.window_id):
                    raise ValueError("目标位置被其他窗口遮挡或已离开窗口，请重新观察")

    @property
    def input(self) -> NativeInput:
        """在第一次实际输入时准备剪贴板和原生输入资源。

        Returns:
            NativeInput: 本次执行可复用的输入实现。
        """
        if self._input is None:
            self._input = NativeInput()
        return self._input

    def click(self, point: tuple[int, int], button: str, clicks: int) -> None:
        """发送一次已经定位的鼠标点击。

        Args:
            point (tuple[int, int]): 桌面物理位置。
            button (str): 鼠标按钮。
            clicks (int): 点击次数。
        """
        self.input.click(point, button, clicks)

    def release_inputs(self) -> None:
        """本次执行结束时释放按键及剪贴板所有者窗口。"""
        if self._input is not None:
            try:
                self._input.close()
            finally:
                self._input = None

    def keyboard(self, keys: tuple[str, ...], check: Callable[[], None]) -> None:
        """发送本次复合按键并在安全点处理中断。

        Args:
            keys (tuple[str, ...]): 顺序按下的按键名称。
            check (Callable[[], None]): 当前执行中断检查。
        """
        self.input.keyboard(keys, check)

    def type_text(self, text: str, check: Callable[[], None]) -> None:
        """粘贴 Unicode 文本，并在结束时恢复用户剪贴板。

        Args:
            text (str): 输入内容。
            check (Callable[[], None]): 当前执行中断检查。
        """
        with self.input.input_guard(text):
            if text:
                self.input.paste(text, check)

    def scroll(self, point: tuple[int, int], steps: int) -> None:
        """在准确物理位置发送滚轮输入。

        Args:
            point (tuple[int, int]): 滚轮目标位置。
            steps (int): 向上为正、向下为负的格数。
        """
        self.input.scroll(point, steps)

    def drag(self, start: tuple[int, int], end: tuple[int, int], check: Callable[[], None]) -> None:
        """执行拖动，异常或中断后释放鼠标。

        Args:
            start (tuple[int, int]): 物理起点。
            end (tuple[int, int]): 物理终点。
            check (Callable[[], None]): 当前执行中断检查。
        """
        self.input.drag(start, end, check)

    def create_escape_listener(self, callback: Callable[[], None]) -> EscapeListener:
        """创建尚未安装的 ESC 监听，供调用方先登记清理再启动。

        Args:
            callback (Callable[[], None]): 已绑定执行编号的中断请求。

        Returns:
            EscapeListener: 当前执行独占的键盘监听对象。
        """
        return EscapeListener(callback)
