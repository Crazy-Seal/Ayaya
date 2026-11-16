"""在独立消息线程区分物理 ESC 和模型注入的 Escape。"""

from collections.abc import Callable
import ctypes
from ctypes import wintypes as w
import threading

WH_KEYBOARD_LL = 13
VK_ESCAPE = 27
WM_QUIT = 0x12
WM_KEYDOWN = 0x100
WM_SYSKEYDOWN = 0x104
INJECTED_FLAGS = 0x12


class KeyboardEvent(ctypes.Structure):
    """读取 Windows 低级键盘钩子提供的按键和注入来源。"""

    _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class EscapeListener:
    """仅在本次控制期间监听物理 ESC，结束时解除钩子和消息线程。"""

    def __init__(self, interrupt: Callable[[], None]) -> None:
        """保存已经捕获执行编号的中断请求。

        Args:
            interrupt (Callable[[], None]): 停止所属本次 Python 执行的回调。
        """
        self._interrupt = interrupt
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._error: Exception | None = None
        self._triggered = False
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)
        definitions = {
            "SetWindowsHookExW": ([ctypes.c_int, self._callback_type, w.HINSTANCE, w.DWORD], w.HHOOK),
            "CallNextHookEx": ([w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM], ctypes.c_ssize_t),
            "UnhookWindowsHookEx": ([w.HHOOK], w.BOOL),
            "PeekMessageW": ([ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT], w.BOOL),
            "GetMessageW": ([ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT], w.BOOL),
            "TranslateMessage": ([ctypes.POINTER(w.MSG)], w.BOOL),
            "DispatchMessageW": ([ctypes.POINTER(w.MSG)], ctypes.c_ssize_t),
            "PostThreadMessageW": ([w.DWORD, w.UINT, w.WPARAM, w.LPARAM], w.BOOL),
        }
        for name, (arguments, result) in definitions.items():
            function = getattr(self._user, name)
            function.argtypes, function.restype = arguments, result
        self._kernel.GetCurrentThreadId.restype = w.DWORD
        self._kernel.GetModuleHandleW.argtypes, self._kernel.GetModuleHandleW.restype = [w.LPCWSTR], w.HMODULE

    def start(self) -> None:
        """等待钩子安装成功，失败时报告系统原因并回收线程。"""
        self._thread = threading.Thread(target=self._run, name="cua-escape", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            self.close()
            raise RuntimeError("ESC 监听未就绪，已取消桌面操作")
        if self._error is not None:
            self.close()
            raise self._error

    def _run(self) -> None:
        """在同一线程安装、消费消息并最终解除系统钩子。"""
        hook = None
        try:
            self._thread_id = self._kernel.GetCurrentThreadId()
            message = w.MSG()
            self._user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)

            @self._callback_type
            def keyboard(code: int, first: int, second: int) -> int:
                """消费物理 ESC，其他按键及注入输入交给系统后续钩子。"""
                if code >= 0:
                    event = ctypes.cast(second, ctypes.POINTER(KeyboardEvent)).contents
                    if event.vkCode == VK_ESCAPE and not event.flags & INJECTED_FLAGS:
                        if first in (WM_KEYDOWN, WM_SYSKEYDOWN) and not self._triggered:
                            self._triggered = True
                            self._interrupt()
                        return 1
                return self._user.CallNextHookEx(None, code, first, second)

            hook = self._user.SetWindowsHookExW(WH_KEYBOARD_LL, keyboard, self._kernel.GetModuleHandleW(None), 0)
            if not hook:
                raise ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            while True:
                result = self._user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._user.TranslateMessage(ctypes.byref(message))
                self._user.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self._error = exc
        finally:
            if hook:
                self._user.UnhookWindowsHookEx(hook)
            self._ready.set()

    def close(self) -> None:
        """通知消息线程退出并等待系统钩子解除。"""
        if self._thread is None:
            return
        if self._thread.is_alive() and self._thread_id:
            if not self._user.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0):
                raise ctypes.WinError(ctypes.get_last_error())
        self._thread.join(5)
        if self._thread.is_alive():
            raise RuntimeError("ESC 消息线程未能结束")
        self._thread = None
