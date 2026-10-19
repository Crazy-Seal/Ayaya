"""Windows 桌面驱动：原生窗口、物理坐标截图及可恢复剪贴板输入。"""

from __future__ import annotations

import base64
import ctypes
import io
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from app.schemas.desktop import DesktopWindowRegistration, ScreenAction, ScreenCapture


class WindowsDesktopDriver:
    """只由 DesktopController 的唯一线程使用，不向工具暴露系统句柄。

    Attributes:
        _restore_path (Path): 与 Electron 共用的窗口恢复记录位置。
        _owner_pid (int): 允许登记窗口的 Electron 主进程编号。
        _registration (DesktopWindowRegistration | None): 已校验 HWND 的完整快照。
        _clipboard_window (int | None): 本线程创建的隐藏剪贴板所有者窗口。
        _clipboard_written_sequence (int | None): 本次写入后的系统序号，用于保护用户新复制内容。
    """

    def __init__(self, *, restore_path: Path, owner_pid: int = 0) -> None:
        """在工作线程保存窗口恢复配置并初始化 Windows 原生输入资源。

        Args:
            restore_path (Path): 应用装配处指定的当前环境恢复文件位置。
            owner_pid (int): 受管 Electron 主进程编号；零表示尚未确定所有者。
        """
        self._restore_path = restore_path
        self._owner_pid = owner_pid
        self._registration: DesktopWindowRegistration | None = None
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
            "IsWindow": ([w.HWND], w.BOOL), "IsWindowVisible": ([w.HWND], w.BOOL),
            "IsIconic": ([w.HWND], w.BOOL), "ShowWindowAsync": ([w.HWND, ctypes.c_int], w.BOOL),
            "GetWindowThreadProcessId": ([w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            "SetThreadDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_void_p),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
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
        except Exception:
            # 2.1. 依赖加载失败时构造对象尚未交给控制器，必须在这里恢复线程 DPI。
            self._user.SetThreadDpiAwarenessContext(self._previous_dpi)
            raise
        self._input = pyautogui
        self._clipboard_window = None
        self._clipboard_written_sequence: int | None = None
        self._snapshot_sequence: int | None = None
        self._pressed: set[int] = set()

    def register_windows(self, registration: DesktopWindowRegistration) -> dict:
        """在桌面工作线程校验 HWND 并保存完整登记，过期快照不能替换当前身份。

        Args:
            registration (DesktopWindowRegistration): Electron 提供的 pet/chat 窗口快照。
        Returns:
            dict: 是否接受快照，以及与 Electron 共用的恢复文件路径。
        """
        # 1. 先核对进程与版本；拒绝快照时不改变已经验证的窗口身份。
        if self._owner_pid and registration.owner_pid != self._owner_pid:
            raise ValueError("窗口所有者与当前 Electron 进程不一致")
        if self._registration and registration.revision <= self._registration.revision:
            return {"accepted": False, "window_restore_path": str(self._restore_path)}
        # 2. 全部句柄验证通过后才能替换登记，避免错误快照污染后续隐藏与恢复。
        for handle in registration.windows.model_dump().values():
            if handle is not None and int(handle) >= 1 << (ctypes.sizeof(ctypes.c_void_p) * 8):
                raise ValueError("桌面窗口句柄超出指针范围")
            if handle is not None and self.window_owner(int(handle)) != registration.owner_pid:
                raise ValueError("桌面窗口句柄不存在或不属于登记进程")
        # 3. 仅保存已核对的身份；文件仍在实际隐藏窗口前写入。
        self._owner_pid = registration.owner_pid
        self._registration = registration
        return {"accepted": True, "window_restore_path": str(self._restore_path)}

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
    def window_guard(self, *, hide: bool, check: Callable[[], None]) -> Iterator[None]:
        """操作期间隐藏指定窗口，结束后只恢复原本可见且没有被最小化的对象。

        Args:
            hide (bool): 控制器根据当前设置决定是否隐藏应用窗口。
            check (Callable[[], None]): 控制器的停止检查，恢复阶段不再调用。
        Yields:
            None: 窗口已完成隐藏和缓冲，可以执行本次桌面观察与输入。
        """
        check()
        if self._owner_pid and self._registration is None:
            raise RuntimeError("桌面窗口尚未登记，不能开始屏幕操作")
        # 1. 上次清理未完成时先重试匹配窗口；不允许新记录覆盖仍隐藏窗口的恢复身份。
        if self._restore_path.exists():
            record = json.loads(self._restore_path.read_text(encoding="utf-8"))
            known = self._registration.windows.model_dump() if self._registration else {}
            if record.get("owner_pid") != self._owner_pid:
                # 1.1. 整个应用重启后 PID 已变化；旧窗口均销毁的记录不能永久阻塞新应用。
                if any(self.window_owner(int(item["hwnd"])) == record.get("owner_pid")
                       for item in record.get("windows", [])):
                    raise RuntimeError("已有窗口恢复记录仍属于存活的旧 Electron，不能覆盖")
            else:
                remaining = [item for item in record.get("windows", [])
                             if item.get("role") in known and known[item["role"]] == item.get("hwnd")]
                self._restore_windows(remaining)
            self._restore_path.unlink(missing_ok=True)
        hidden: list[dict] = []
        if hide and self._registration:
            # 2. 只处理登记角色；已销毁的句柄跳过，绝不枚举同进程其他窗口。
            for role, handle in self._registration.windows.model_dump().items():
                if handle is not None:
                    hwnd = int(handle)
                    if self.window_owner(hwnd) == self._owner_pid and self.visible(hwnd) and not self.minimized(hwnd):
                        hidden.append({"role": role, "hwnd": handle})
        if hidden:
            self._write_restore_record(hidden)
        try:
            # 3. 隐藏请求发出不等于完成，确认系统状态后才进入捕获缓冲。
            deadline = time.monotonic() + 1
            for window in hidden:
                hwnd = int(window["hwnd"])
                self.hide(hwnd)
                while self.visible(hwnd):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("隐藏桌面窗口超时")
                    self._wait(0.01, check)
            if hidden:
                self._wait(0.12, check)
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
        failures = []
        for window in windows:
            hwnd = int(window["hwnd"])
            try:
                if self.window_owner(hwnd) == self._owner_pid and not self.minimized(hwnd):
                    self.show_inactive(hwnd)
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError("桌面窗口恢复失败：" + "; ".join(failures))

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

    def window_owner(self, hwnd: int) -> int | None:
        """返回存活窗口所属 PID，销毁的句柄返回 None。

        Args:
            hwnd (int): Electron 登记的精确原生窗口句柄。
        Returns:
            int | None: 系统确认的所属进程编号，不存在时为空。
        """
        if not self._user.IsWindow(hwnd):
            return None
        pid = self._w.DWORD()
        if not self._user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return None
        return pid.value

    def visible(self, hwnd: int) -> bool:
        """查询 Win32 可见状态，供确认异步隐藏已经生效。

        Args:
            hwnd (int): 正在确认显隐的原生句柄。
        Returns:
            bool: 窗口是否具有可见样式，最小化单独判断。
        """
        return bool(self._user.IsWindowVisible(hwnd))

    def minimized(self, hwnd: int) -> bool:
        """查询用户最小化状态，临时恢复不能把最小化窗口弹出。

        Args:
            hwnd (int): 登记或恢复记录中的原生句柄。
        Returns:
            bool: 系统是否将此窗口标记为最小化。
        """
        return bool(self._user.IsIconic(hwnd))

    def hide(self, hwnd: int) -> None:
        """投递 SW_HIDE；调用方随后检查实际状态，不把投递成功当作已经隐藏。

        Args:
            hwnd (int): 本次原本可见的登记窗口。
        Raises:
            OSError: 系统拒绝异步隐藏请求。
        """
        if not self._user.ShowWindowAsync(hwnd, 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def show_inactive(self, hwnd: int) -> None:
        """用 SW_SHOWNA 保持尺寸与窗口样式，不主动取得输入焦点。

        Args:
            hwnd (int): 本次隐藏且仍存活、未最小化的窗口。
        Raises:
            OSError: 系统拒绝恢复请求。
            RuntimeError: 一秒内未确认可见状态。
        """
        if not self._user.ShowWindowAsync(hwnd, 8):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + 1
        while self.window_owner(hwnd) is not None and not self.visible(hwnd):
            if time.monotonic() >= deadline:
                raise RuntimeError("恢复桌面窗口超时")
            time.sleep(0.01)

    def capture(self) -> ScreenCapture:
        """取得包含透明窗口的主屏物理像素，等比例缩小后编码为 PNG。

        Returns:
            ScreenCapture: 图片尺寸是观察尺寸，不是鼠标输入的物理尺寸。
        """
        from PIL import Image, ImageGrab
        image = ImageGrab.grab(include_layered_windows=True, all_screens=False)
        image.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="PNG")
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return ScreenCapture("data:image/png;base64," + encoded, image.width, image.height)

    def mouse_action(self, action: ScreenAction, check: Callable[[], None]) -> None:
        """把比例映射为主屏物理像素，只执行现有四种鼠标动作。

        Args:
            action (ScreenAction): 已校验的中心比例、鼠标操作及滚动方向。
            check (Callable[[], None]): 移动光标后、点击或滚动前检查停止的安全点。
        Raises:
            RuntimeError: 主屏尺寸不可用。
            ValueError: 滚动方向未知。
        """
        width, height = self._user.GetSystemMetrics(0), self._user.GetSystemMetrics(1)
        if width <= 0 or height <= 0:
            raise RuntimeError("主显示器尺寸无效")
        x = min(width - 1, max(0, round(action.x_ratio * width)))
        y = min(height - 1, max(0, round(action.y_ratio * height)))
        self._input.moveTo(x, y)
        check()
        if action.operation == "click":
            self._input.click()
        elif action.operation == "double":
            self._input.doubleClick(interval=0.1)
        elif action.operation == "right":
            self._input.click(button="right")
        elif action.operation == "scroll":
            if action.scroll_direction not in {"up", "down"}:
                raise ValueError("滚动方向无效")
            self._input.scroll(6 if action.scroll_direction == "up" else -6)

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

    def press_enter(self) -> None:
        """显式记录回车按下，输入失败时 finally 仍可原生释放。"""
        self._pressed.add(0x0D)
        self._input.keyDown("enter")
        self._input.keyUp("enter")
        self._pressed.discard(0x0D)

    def close(self) -> None:
        """在创建线程中销毁私有窗口并恢复原线程 DPI 设置。"""
        if self._clipboard_window:
            self._user.DestroyWindow(self._clipboard_window)
            self._clipboard_window = None
        if self._previous_dpi:
            self._user.SetThreadDpiAwarenessContext(self._previous_dpi)
            self._previous_dpi = None
