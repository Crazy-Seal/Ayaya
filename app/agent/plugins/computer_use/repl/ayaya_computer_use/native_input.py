"""保存插件输入过程中的剪贴板和按键，保证异常及中断后释放资源。"""

import ctypes
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager


class NativeInput:
    """在 REPL 主线程执行真实输入，保留并恢复用户剪贴板。"""

    def __init__(self) -> None:
        """为本次桌面输入准备物理坐标、原生按键和剪贴板资源。"""
        if os.name != "nt":
            raise RuntimeError("屏幕操作仅支持 Windows")
        from ctypes import wintypes as w
        self._w = w
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ole = ctypes.WinDLL("ole32", use_last_error=True)
        self._gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        # 1. 所有 HWND/HANDLE 使用指针宽度，不能沿 ctypes 默认 int 返回值截断。
        definitions = {
            "SetThreadDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_void_p),
            "OpenClipboard": ([w.HWND], w.BOOL), "CloseClipboard": ([], w.BOOL),
            "EmptyClipboard": ([], w.BOOL), "EnumClipboardFormats": ([w.UINT], w.UINT),
            "GetClipboardData": ([w.UINT], w.HANDLE), "SetClipboardData": ([w.UINT, w.HANDLE], w.HANDLE),
            "GetClipboardSequenceNumber": ([], w.DWORD),
            "GetClipboardFormatNameW": ([w.UINT, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            "CreateWindowExW": ([w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p], w.HWND),
            "DestroyWindow": ([w.HWND], w.BOOL),
            "keybd_event": ([w.BYTE, w.BYTE, w.DWORD, ctypes.c_size_t], None),
        }
        for name, (arguments, result) in definitions.items():
            function = getattr(self._user, name)
            function.argtypes, function.restype = arguments, result
        for name, arguments, result in (
            ("GlobalAlloc", [w.UINT, ctypes.c_size_t], w.HANDLE),
            ("GlobalLock", [w.HANDLE], ctypes.c_void_p),
            ("GlobalUnlock", [w.HANDLE], w.BOOL), ("GlobalFree", [w.HANDLE], w.HANDLE),
        ):
            function = getattr(self._kernel, name)
            function.argtypes, function.restype = arguments, result
        self._ole.OleDuplicateData.argtypes = [w.HANDLE, w.UINT, w.UINT]
        self._ole.OleDuplicateData.restype = w.HANDLE
        self._gdi.DeleteObject.argtypes, self._gdi.DeleteObject.restype = [w.HANDLE], w.BOOL
        # 2. 在线程中使用物理像素；导入 PyAutoGUI 的进程 DPI 尝试不能覆盖线程设置。
        self._previous_dpi = self._user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not self._previous_dpi:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            import pyautogui
        except BaseException:
            # 2.1. 依赖加载失败或被用户中断时，先恢复已取得的线程 DPI。
            self._user.SetThreadDpiAwarenessContext(self._previous_dpi)
            raise
        self._input = pyautogui
        self._clipboard_window = None
        self._clipboard_written_sequence: int | None = None
        self._snapshot_sequence: int | None = None
        self._pressed: set[int] = set()
        self._mouse_pressed: set[str] = set()
        self._user.mouse_event.argtypes = [w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.c_size_t]

    def click(self, point: tuple[int, int], button: str, clicks: int) -> None:
        """向已确认目标发送点击，所有退出路径释放本次鼠标按下状态。

        Args:
            point (tuple[int, int]): 桌面物理位置。
            button (str): 鼠标按钮。
            clicks (int): 点击次数。
        """
        self._input.moveTo(*point)
        self._mouse_pressed.add(button)
        try:
            self._input.click(button=button, clicks=clicks, interval=.1)
        finally:
            self.release_buttons()

    def release_buttons(self) -> None:
        """使用原生释放事件清除本次鼠标状态，鼠标撞角时也能清理。"""
        for button in tuple(self._mouse_pressed):
            self._user.mouse_event({"left": 4, "right": 16, "middle": 64}[button], 0, 0, 0, 0)
            self._mouse_pressed.discard(button)

    def keyboard(self, keys: tuple[str, ...], check: Callable[[], None]) -> None:
        """发送完整组合键，所有退出路径用原生事件释放已按下的键。

        Args:
            keys (tuple[str, ...]): 按下顺序的按键名称。
            check (Callable[[], None]): 当前执行停止检查。
        """
        names = {"ctrl": 17, "control": 17, "shift": 16, "alt": 18, "win": 91,
                 "enter": 13, "tab": 9, "esc": 27, "escape": 27, "space": 32,
                 "backspace": 8, "delete": 46, "insert": 45, "home": 36, "end": 35,
                 "left": 37, "up": 38, "right": 39, "down": 40, "pageup": 33, "pagedown": 34}
        names.update({f"f{number}": 111 + number for number in range(1, 25)})
        codes = []
        self._user.VkKeyScanW.argtypes, self._user.VkKeyScanW.restype = [self._w.WCHAR], ctypes.c_short
        for key in keys:
            normalized = key.lower()
            if normalized in names:
                codes.append(names[normalized])
            elif len(normalized) == 1:
                code = self._user.VkKeyScanW(normalized)
                if code == -1:
                    raise ValueError("按键不可映射，文字请使用 type_text；请参考 cua.help()")
                for mask, modifier in ((0x100, 16), (0x200, 17), (0x400, 18)):
                    if code & mask:
                        codes.append(modifier)
                codes.append(code & 0xff)
            else:
                raise ValueError(f"未知按键 {key}，请参考 cua.help()")
        try:
            for code in codes:
                check()
                self._pressed.add(code)
                self._user.keybd_event(code, 0, 0, 0)
        finally:
            for code in reversed(codes):
                if code in self._pressed:
                    self._user.keybd_event(code, 0, 2, 0)
                    self._pressed.discard(code)

    def scroll(self, point: tuple[int, int], steps: int) -> None:
        """移动到明确位置后发送滚轮格数。

        Args:
            point (tuple[int, int]): 桌面物理位置。
            steps (int): 向上为正、向下为负的格数。
        """
        self._input.moveTo(*point)
        self._input.scroll(steps)

    def drag(self, start: tuple[int, int], end: tuple[int, int], check: Callable[[], None]) -> None:
        """执行一次左键拖动，停止或异常后原生释放鼠标。

        Args:
            start (tuple[int, int]): 桌面物理起点。
            end (tuple[int, int]): 桌面物理终点。
            check (Callable[[], None]): 当前执行停止检查。
        """
        self._input.moveTo(*start)
        check()
        self._mouse_pressed.add("left")
        try:
            self._input.mouseDown(button="left")
            check()
            self._input.moveTo(*end, duration=.3)
            check()
        finally:
            self.release_buttons()

    def _owner_window(self) -> int:
        """本线程创建 message-only 窗口，写入剪贴板时不能使用空所有者。

        Returns:
            int: 仅作为剪贴板所有者的私有窗口句柄。
        """
        if self._clipboard_window is None:
            self._clipboard_window = self._user.CreateWindowExW(
                0, "STATIC", "Ayaya clipboard", 0, 0, 0, 0, 0,
                self._w.HWND(-3), None, None, None,
            )
            if not self._clipboard_window:
                raise ctypes.WinError(ctypes.get_last_error())
        return self._clipboard_window

    @contextmanager
    def _clipboard(self) -> Iterator[None]:
        """有限重试打开剪贴板，成功打开的路径一定关闭，不能长时间锁住其他应用。

        Yields:
            None: 已取得系统剪贴板锁的操作范围。
        Raises:
            RuntimeError: 半秒内未能取得剪贴板锁。
        """
        deadline = time.monotonic() + 0.5
        while not self._user.OpenClipboard(self._owner_window()):
            if time.monotonic() >= deadline:
                raise RuntimeError("剪贴板正被其他程序占用")
            time.sleep(0.01)
        try:
            yield
        finally:
            self._user.CloseClipboard()

    def _format_supported(self, value: int) -> bool:
        """只接受文本、富文本和图像及其本地编码信息，不接受文件或未知格式。

        Args:
            value (int): Windows 枚举出的剪贴板格式编号。
        Returns:
            bool: 此格式是否能按当前句柄复制与释放规则可靠恢复。
        """
        if value in {1, 2, 7, 8, 13, 16, 17}:
            return True
        name = ctypes.create_unicode_buffer(256)
        self._user.GetClipboardFormatNameW(value, name, len(name))
        return name.value.lower() in {"html format", "rich text format", "png", "jfif",
                                      "text/html", "text/rtf", "image/png", "image/jpeg"}

    def _free_snapshot(self, snapshot: list[tuple[int, int]]) -> None:
        """释放尚未移交 Windows 的复制句柄；位图使用 GDI 删除。

        Args:
            snapshot (list[tuple[int, int]]): 格式编号和复制句柄；释放后原列表清空。
        """
        for format_id, handle in snapshot:
            if format_id == 2:
                self._gdi.DeleteObject(handle)
            else:
                self._kernel.GlobalFree(handle)
        snapshot.clear()

    def _snapshot_clipboard(self) -> list[tuple[int, int]]:
        """先检查所有格式，再复制实际数据，未知格式不允许清空用户剪贴板。

        Returns:
            list[tuple[int, int]]: 本次拥有的独立复制句柄，调用方必须移交或释放。
        Raises:
            RuntimeError: 出现未知格式、文件或不能读取复制的数据。
        """
        snapshot: list[tuple[int, int]] = []
        try:
            with self._clipboard():
                formats = []
                value = self._user.EnumClipboardFormats(0)
                while value:
                    if not self._format_supported(value):
                        raise RuntimeError("剪贴板包含文件或无法可靠恢复的格式，已取消文本输入")
                    formats.append(value)
                    value = self._user.EnumClipboardFormats(value)
                # 1. OLE 复制位图会创建独立 GDI 对象，不能保存随后被 EmptyClipboard 释放的旧句柄。
                for format_id in formats:
                    original = self._user.GetClipboardData(format_id)
                    if not original:
                        raise RuntimeError("无法读取剪贴板原内容")
                    copied = self._ole.OleDuplicateData(original, format_id, 0)
                    if not copied:
                        raise RuntimeError("无法复制剪贴板原内容")
                    snapshot.append((format_id, copied))
                self._snapshot_sequence = self._user.GetClipboardSequenceNumber()
            return snapshot
        except BaseException:
            self._free_snapshot(snapshot)
            raise

    def _write_text(self, text: str) -> None:
        """确认用户没有新复制内容后写入 UTF-16，允许同一批次连续替换自己的输入文本。

        Args:
            text (str): 本次需要粘贴到目标程序的完整文本。
        Raises:
            RuntimeError: 内容已被用户更新或系统拒绝内存及剪贴板操作。
        """
        data = text.encode("utf-16-le") + b"\0\0"
        handle = self._kernel.GlobalAlloc(2, len(data))
        if not handle:
            raise RuntimeError("分配剪贴板内存失败")
        try:
            pointer = self._kernel.GlobalLock(handle)
            if not pointer:
                raise RuntimeError("锁定剪贴板内存失败")
            ctypes.memmove(pointer, data, len(data))
            self._kernel.GlobalUnlock(handle)
            with self._clipboard():
                # 1. 首次写入比对原快照，后续写入比对本批次上一次写入的序号。
                # 1.1. 只信任自己的上一次写入；用户在两个动作间复制的新内容仍会阻止覆盖。
                expected_sequence = self._clipboard_written_sequence
                if expected_sequence is None:
                    expected_sequence = self._snapshot_sequence
                if self._user.GetClipboardSequenceNumber() != expected_sequence:
                    raise RuntimeError("用户已复制新内容，取消本次文本输入")
                if not self._user.EmptyClipboard():
                    raise RuntimeError("清空剪贴板失败")
                # 2. 即使后续 SetClipboardData 失败，也必须凭此序号恢复已经被清空的旧内容。
                self._clipboard_written_sequence = self._user.GetClipboardSequenceNumber()
                if not self._user.SetClipboardData(13, handle):
                    raise RuntimeError("写入剪贴板失败")
                handle = None
                self._clipboard_written_sequence = self._user.GetClipboardSequenceNumber()
        finally:
            if handle:
                self._kernel.GlobalFree(handle)

    @contextmanager
    def input_guard(self, text: str) -> Iterator[None]:
        """输入前复制剪贴板；所有退出路径释放按键，并在用户未更新内容时恢复。

        Args:
            text (str): 非空时预检查并保存全部支持的剪贴板格式。
        Yields:
            None: 允许操作鼠标与输入的范围，结束后完成按键及剪贴板清理。
        """
        snapshot = self._snapshot_clipboard() if text else []
        self._clipboard_written_sequence = None
        try:
            yield
        finally:
            # 1. 原生 keyup 不受 PyAutoGUI FAILSAFE 限制，鼠标撞角后也不能遗留 Ctrl。
            for key in self._pressed:
                self._user.keybd_event(key, 0, 2, 0)
            self._pressed.clear()
            try:
                if self._clipboard_written_sequence is not None:
                    with self._clipboard():
                        # 2. 检查时仍持有剪贴板锁，用户新内容不能在检查和清空之间被覆盖。
                        if self._user.GetClipboardSequenceNumber() == self._clipboard_written_sequence:
                            if not self._user.EmptyClipboard():
                                raise RuntimeError("恢复剪贴板失败")
                            while snapshot:
                                format_id, handle = snapshot[0]
                                if not self._user.SetClipboardData(format_id, handle):
                                    raise RuntimeError("恢复剪贴板格式失败")
                                snapshot.pop(0)
            finally:
                self._free_snapshot(snapshot)
                self._clipboard_written_sequence = None

    def paste(self, text: str, check: Callable[[], None]) -> None:
        """用 Ctrl+V 输入 Unicode，安全点之后不再发出新的按键。

        Args:
            text (str): 要输入的 Unicode 文本。
            check (Callable[[], None]): 控制器提供的安全点，停止时抛出异常进入清理。
        """
        self._write_text(text)
        check()
        self._pressed.add(0x11)
        self._input.keyDown("ctrl")
        check()
        self._pressed.add(0x56)
        self._input.keyDown("v")
        self._input.keyUp("v")
        self._pressed.discard(0x56)
        self._input.keyUp("ctrl")
        self._pressed.discard(0x11)
        # 1. 给目标程序处理粘贴消息的短暂时间，不能立即把剪贴板换回旧内容。
        self._wait(0.1, check)

    def close(self) -> None:
        """释放本次输入状态、销毁剪贴板窗口并恢复调用线程 DPI。"""
        self.release_buttons()
        for key in tuple(self._pressed):
            self._user.keybd_event(key, 0, 2, 0)
            self._pressed.discard(key)
        if self._clipboard_window:
            self._user.DestroyWindow(self._clipboard_window)
            self._clipboard_window = None
        if self._previous_dpi:
            self._user.SetThreadDpiAwarenessContext(self._previous_dpi)
            self._previous_dpi = None

    def _wait(self, seconds: float, check: Callable[[], None]) -> None:
        """等待原生窗口或粘贴消息生效，期间定期调用控制器的停止检查。

        Args:
            seconds (float): 当前平台操作需要等待的秒数。
            check (Callable[[], None]): 控制器提供的检查，停止时抛出异常。
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            check()
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        check()
