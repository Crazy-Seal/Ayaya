"""通过真实桌面模块和 Windows adapter 验证剪贴板，替代底层系统调用。"""

import sys
import ctypes
from ctypes import wintypes

import pytest

from app.agent.plugins.computer_use.repl.ayaya_computer_use.native_input import NativeInput


class ClipboardSystem:
    """模拟系统剪贴板和内存所有权，不修改用户真实剪贴板或键盘。"""

    def __init__(self, content):
        """保留原有格式字节和可观察输入状态。

        Args:
            content (dict[int, bytes]): 测试的初始剪贴板格式与对应二进制内容。
        """
        self.content = dict(content)
        self.sequence = 1
        self.buffers = {}
        self.next_handle = 100
        self.keys = set()
        self.pasted = None
        self.new_user_copy = False
        self.fail_v = False
        self.fail_write = False
        self.clicks = 0

    def GlobalAlloc(self, _flags, size):
        """返回本测试管理的可移动内存句柄。

        Args:
            _flags (int): 生产代码传入的内存或键盘操作标志，替身不改变其语义。
            size (int): 测试全局内存需要分配的字节数。
        """
        self.next_handle += 1
        self.buffers[self.next_handle] = ctypes.create_string_buffer(size)
        return self.next_handle

    def GlobalLock(self, handle):
        """将复制内存映射成真实 ctypes 地址供生产 adapter 写入。

        Args:
            handle (int): 测试系统管理的全局内存句柄。
        """
        return ctypes.addressof(self.buffers[handle])

    def GlobalUnlock(self, _handle):
        """模拟解锁，不改变已有字节。

        Args:
            _handle (int): 待解除内存锁的测试句柄。
        """
        return True

    def GlobalFree(self, handle):
        """释放尚未移交给系统的测试内存。

        Args:
            handle (int): 测试系统管理的全局内存句柄。
        """
        self.buffers.pop(handle, None)

    def DeleteObject(self, handle):
        """位图复制句柄通过 GDI 生命周期释放。

        Args:
            handle (int): 测试系统管理的全局内存句柄。
        """
        self.GlobalFree(handle)

    def GetClipboardData(self, format_id):
        """取得系统持有的原格式字节，不把引用当作独立副本。

        Args:
            format_id (int): Windows 文本、富文本、图像或文件格式编号。
        """
        data = self.content[format_id]
        handle = self.GlobalAlloc(2, len(data))
        ctypes.memmove(self.GlobalLock(handle), data, len(data))
        return handle

    def OleDuplicateData(self, handle, _format_id, _flags):
        """按 Win32 规则复制独立内存，清空剪贴板后仍保持有效。

        Args:
            handle (int): 测试系统管理的全局内存句柄。
            _flags (int): 生产代码传入的内存或键盘操作标志，替身不改变其语义。
            _format_id (int): 当前复制的剪贴板格式编号。
        """
        original = self.buffers[handle].raw
        copied = self.GlobalAlloc(2, len(original))
        ctypes.memmove(self.GlobalLock(copied), original, len(original))
        return copied

    def GetClipboardSequenceNumber(self):
        """返回系统内容变更序号。"""
        return self.sequence

    def OpenClipboard(self, owner):
        """写入必须使用私有窗口所有者，禁止空 HWND。

        Args:
            owner (int): 剪贴板私有窗口所有者的测试句柄。
        """
        assert owner == 99
        return True

    def CloseClipboard(self):
        """释放模拟锁。"""
        return True

    def EnumClipboardFormats(self, previous):
        """依次返回当前原格式，不合成其他文件格式。

        Args:
            previous (int): 上一个枚举格式编号，零表示开始枚举。
        """
        return next((value for value in sorted(self.content) if value > previous), 0)

    def GetClipboardFormatNameW(self, format_id, name, _length):
        """模拟 HTML 与 RTF 注册格式，未知格式保持未知。

        Args:
            format_id (int): Windows 文本、富文本、图像或文件格式编号。
            _length (int): 输出缓冲长度，替身使用固定测试名称。
            name (ctypes.Array): 系统格式名称的输出缓冲。
        """
        name.value = {500: "HTML Format", 501: "Rich Text Format"}.get(format_id, "unknown")
        return len(name.value)

    def EmptyClipboard(self):
        """清空也推进序号，失败写入必须按此序号恢复。"""
        self.content = {}
        self.sequence += 1
        return True

    def SetClipboardData(self, format_id, handle):
        """写入实际复制字节；可注入清空后的第一次写入失败。

        Args:
            format_id (int): Windows 文本、富文本、图像或文件格式编号。
            handle (int): 测试系统管理的全局内存句柄。
        """
        if self.fail_write:
            self.fail_write = False
            return None
        self.content[format_id] = self.buffers[handle].raw
        self.sequence += 1
        return handle

    def GetSystemMetrics(self, metric):
        """输入坐标使用固定 1920×1080 的物理主屏。

        Args:
            metric (int): GetSystemMetrics 主屏宽或高的索引。
        """
        return 1920 if metric == 0 else 1080

    def moveTo(self, x, y):
        """观察独立的预期物理中心点，避免重复 DPI 缩放。

        Args:
            x (int): 主屏物理像素横坐标。
            y (int): 主屏物理像素纵坐标。
        """
        assert (x, y) == (960, 540)

    def click(self, **_kwargs):
        """记录模拟桌面实际发生的点击。

        Args:
            _kwargs (dict): 生产鼠标接口提供的命名配置，替身只记录调用。
        """
        self.clicks += 1

    def keyDown(self, key):
        """模拟系统收到的 Ctrl+V，支持故障和用户新复制内容。

        Args:
            key (str | int): PyAutoGUI 按键名称或 Win32 虚拟键编号。
        """
        self.keys.add({"ctrl": 0x11, "v": 0x56, "enter": 0x0D}[key])
        if key == "v":
            if self.fail_v:
                raise RuntimeError("粘贴失败")
            self.pasted = self.content[13].decode("utf-16-le").rstrip("\0")
            if self.new_user_copy:
                self.content = {13: "用户新复制内容\0".encode("utf-16-le")}
                self.sequence += 1

    def keyUp(self, key):
        """模拟正常松开输入键。

        Args:
            key (str | int): PyAutoGUI 按键名称或 Win32 虚拟键编号。
        """
        self.keys.discard({"ctrl": 0x11, "v": 0x56, "enter": 0x0D}[key])

    def keybd_event(self, key, _scan, _flags, _extra):
        """故障清理使用原生 keyup，不能依赖可能抛 FAILSAFE 的输入库。

        Args:
            key (str | int): PyAutoGUI 按键名称或 Win32 虚拟键编号。
            _scan (int): 原生键盘扫描码，替身只记录按键释放。
            _flags (int): 生产代码传入的内存或键盘操作标志，替身不改变其语义。
            _extra (int): 原生键盘附加信息，本用例不使用。
        """
        self.keys.discard(key)

    def DestroyWindow(self, _hwnd):
        """关闭 adapter 的私有测试所有者，不释放应用窗口。

        Args:
            _hwnd (int): 当前需要关闭的测试剪贴板所有者窗口。
        """
        return True


def native_adapter(system, monkeypatch):
    """在系统调用边界替换 DLL 和输入库，正常构造真实剪贴板实现。

    Args:
        system (ClipboardSystem): 剪贴板和原生内存替身。
        monkeypatch (pytest.MonkeyPatch): 隔离 Win32 和输入库。

    Returns:
        NativeInput: 由真实构造函数创建的插件输入对象。
    """
    class Function:
        """允许生产构造函数为测试系统调用声明 ctypes 签名。"""

        def __init__(self, function):
            """保存系统边界操作。"""
            self.function = function

        def __call__(self, *args):
            """交付真实实现传给 Win32 的参数。"""
            return self.function(*args)

    class Library:
        """提供剪贴板 DLL 及无桌面副作用的初始化函数。"""

        def __getattr__(self, name):
            """读取模拟 Win32 函数并保留其类型声明。"""
            result = Function((lambda *args: 99) if name == "CreateWindowExW" else getattr(system, name, lambda *args: 1))
            setattr(self, name, result)
            return result

    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: Library())
    monkeypatch.setitem(sys.modules, "pyautogui", system)
    return NativeInput()


def test_interrupt_while_importing_input_library_restores_thread_dpi(monkeypatch):
    """输入库加载被 ESC 打断时，尚未交付的输入对象也会恢复线程 DPI。"""
    import builtins
    system = ClipboardSystem({})
    contexts = []

    def set_dpi(context):
        """记录系统线程设置并返回原设置。"""
        contexts.append(context)
        return 17

    original_import = builtins.__import__

    def load_module(name, *args, **kwargs):
        """只在输入库实际导入时交付用户中断。"""
        if name == "pyautogui":
            raise KeyboardInterrupt()
        return original_import(name, *args, **kwargs)

    system.SetThreadDpiAwarenessContext = set_dpi
    monkeypatch.setattr(builtins, "__import__", load_module)
    with pytest.raises(KeyboardInterrupt):
        native_adapter(system, monkeypatch)
    assert contexts[-1] == 17


@pytest.mark.parametrize("fault", ["none", "paste", "write", "new-copy", "files"])
def test_chinese_input_and_clipboard_protection(monkeypatch, fault):
    """中文粘贴结束或失败后恢复支持的格式，保留用户新复制内容。

    Args:
        monkeypatch (pytest.MonkeyPatch): 替换系统边界。
        fault (str): 模拟的输入故障或用户操作。
    """
    original = {13: "原文本\0".encode("utf-16-le"), 500: b"<b>HTML</b>\0", 501: b"{rtf}\0", 8: b"image-bytes"}
    if fault == "files":
        original[15] = b"files"
    system = ClipboardSystem(original)
    system.fail_v, system.fail_write = fault == "paste", fault == "write"
    system.new_user_copy = fault == "new-copy"
    driver = native_adapter(system, monkeypatch)
    try:
        if fault in {"paste", "write", "files"}:
            with pytest.raises(RuntimeError):
                with driver.input_guard("中文输入测试"):
                    driver.paste("中文输入测试", lambda: None)
        else:
            with driver.input_guard("中文输入测试"):
                driver.paste("中文输入测试", lambda: None)
            assert system.pasted == "中文输入测试"
        assert not system.keys
        assert system.content == ({13: "用户新复制内容\0".encode("utf-16-le")} if fault == "new-copy" else original)
    finally:
        driver.close()


@pytest.mark.parametrize("fault", ["none", "new-copy", "files"])
def test_batch_text_input_reuses_clipboard_snapshot(monkeypatch, fault):
    """连续粘贴复用同一快照，用户中途复制会阻止后续覆盖。

    Args:
        monkeypatch (pytest.MonkeyPatch): 替换系统边界。
        fault (str): 正常输入、用户复制或不支持格式。
    """
    original = {13: "原始文字\0".encode("utf-16-le"), 500: b"<b>HTML</b>\0"}
    if fault == "files":
        original[15] = b"files"
    system = ClipboardSystem(original)
    system.new_user_copy = fault == "new-copy"
    driver = native_adapter(system, monkeypatch)

    def paste_twice():
        """通过真实上下文管理器执行两段 Unicode 粘贴。"""
        with driver.input_guard("第一段中文"):
            driver.paste("第一段中文", lambda: None)
            driver.paste("第二段中文", lambda: None)

    try:
        if fault == "none":
            paste_twice()
            assert system.pasted == "第二段中文" and system.content == original
        else:
            with pytest.raises(RuntimeError):
                paste_twice()
            assert system.content == ({13: "用户新复制内容\0".encode("utf-16-le")} if fault == "new-copy" else original)
        assert not system.keys
    finally:
        driver.close()
