"""使用原生 ConPTY 提供 Windows 终端，并在放行线程前加入现有 Job。"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import subprocess

from app.agent.tools.terminal.windows_job import WindowsJob


class Coord(ctypes.Structure):
    """表示 ConPTY 终端列数和行数的系统结构。"""

    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class StartupInfo(ctypes.Structure):
    """声明 CreateProcessW 使用的完整 STARTUPINFOW 内存布局。"""

    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class StartupInfoEx(ctypes.Structure):
    """在基本启动信息后携带 ConPTY 属性列表。"""

    _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class ProcessInformation(ctypes.Structure):
    """接收挂起进程及其初始线程的句柄和编号。"""

    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


def _kernel():
    """声明所有系统调用的参数类型，避免 64 位句柄被默认整数截断。

    Returns:
        ctypes.WinDLL: 完成类型声明的 Windows 内核接口。
    """
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreatePipe": ([ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "CreatePseudoConsole": ([Coord, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], ctypes.c_long),
        "ClosePseudoConsole": ([wintypes.HANDLE], None),
        "InitializeProcThreadAttributeList": ([ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
        "UpdateProcThreadAttribute": ([ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
        "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
        "CreateProcessW": ([wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(StartupInfoEx), ctypes.POINTER(ProcessInformation)], wintypes.BOOL),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        "ReadFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
        "WriteFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes = args
        function.restype = result
    return kernel


class WindowsConPtyProcess:
    """分别读取和写入 ConPTY，同步系统调用只在线程中等待。"""

    def __init__(self, kernel, process: ProcessInformation, console, reader, writer, job: WindowsJob) -> None:
        """接管启动成功后所有需要长期保留的句柄。

        Args:
            kernel (ctypes.WinDLL): 已声明类型的系统函数。
            process (ProcessInformation): 已恢复运行的进程信息。
            console (HANDLE): ConPTY 句柄。
            reader (HANDLE): 终端输出读取句柄。
            writer (HANDLE): 终端输入写入句柄。
            job (WindowsJob): 已关联根进程的进程树约束。
        """
        self.kernel = kernel
        self.process = process.hProcess
        self.pid = int(process.dwProcessId)
        self.console = console
        self.reader = reader
        self.writer = writer
        self.job = job

    @classmethod
    def spawn(cls, argv: list[str], cwd: str) -> WindowsConPtyProcess:
        """创建终端、挂起进程、关联 Job，再恢复初始线程。

        Args:
            argv (list[str]): Shell 和执行参数。
            cwd (str): 已校验工作目录。

        Returns:
            WindowsConPtyProcess: 无可见窗口的持续终端。

        Raises:
            OSError: ConPTY、进程或 Job 初始化失败，所有已创建资源均被回收。
        """
        kernel = _kernel()
        handles = [wintypes.HANDLE() for _ in range(4)]
        input_read, input_write, output_read, output_write = handles
        console = wintypes.HANDLE()
        info = ProcessInformation()
        initialized = False
        attributes = None
        job = None
        success = False
        try:
            # 1. 两组单向管道分别承载输入和输出；父进程不同时阻塞在两个方向。
            if not kernel.CreatePipe(ctypes.byref(input_read), ctypes.byref(input_write), None, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.CreatePipe(ctypes.byref(output_read), ctypes.byref(output_write), None, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            result = kernel.CreatePseudoConsole(Coord(120, 30), input_read, output_write, 0, ctypes.byref(console))
            if result < 0:
                raise OSError(f"ConPTY 初始化失败：HRESULT={result:#x}")
            # 2. 扩展启动属性指定 ConPTY，进程先保持挂起，不能提前创建脱离 Job 的子进程。
            size = ctypes.c_size_t()
            kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attributes = ctypes.create_string_buffer(size.value)
            if not kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            initialized = True
            if not kernel.UpdateProcThreadAttribute(attributes, 0, 0x00020016, console, ctypes.sizeof(console), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            startup = StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            # 2.1. 显式使用空标准句柄，让 ConPTY 分配控制台句柄，避免继承后端或 Pytest 的重定向管道。
            startup.StartupInfo.dwFlags = 0x00000100
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            if not kernel.CreateProcessW(argv[0], command, None, None, False, 0x00080000 | 0x00000004,
                                         None, cwd, ctypes.byref(startup), ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            job = WindowsJob(int(info.dwProcessId))
            if kernel.ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            success = True
            return cls(kernel, info, console, output_read, input_write, job)
        finally:
            # 3. 临时属性和从端由启动函数释放；失败时尚无输出，终止进程后关闭终端。
            if initialized:
                kernel.DeleteProcThreadAttributeList(attributes)
            if info.hThread:
                kernel.CloseHandle(info.hThread)
            for handle in (input_read, output_write):
                if handle:
                    kernel.CloseHandle(handle)
            if not success:
                if job is not None:
                    job.terminate()
                    job.close()
                if info.hProcess:
                    kernel.TerminateProcess(info.hProcess, 1)
                    kernel.CloseHandle(info.hProcess)
                for handle in (input_write, output_read):
                    if handle:
                        kernel.CloseHandle(handle)
                if console:
                    kernel.ClosePseudoConsole(console)

    def _read(self, size: int) -> bytes:
        """在线程中读取一块终端输出，将管道关闭视为 EOF。

        Args:
            size (int): 本次最大读取字节数。

        Returns:
            bytes: 终端输出或 EOF。
        """
        buffer = ctypes.create_string_buffer(size)
        count = wintypes.DWORD()
        if not self.kernel.ReadFile(self.reader, buffer, size, ctypes.byref(count), None):
            error = ctypes.get_last_error()
            if error in {109, 232, 995}:
                return b""
            raise ctypes.WinError(error)
        return buffer.raw[:count.value]

    async def read(self, size: int) -> bytes:
        """等待输出线程读取终端，不占用后端事件循环。

        Args:
            size (int): 读取上限。

        Returns:
            bytes: 当前输出。
        """
        return await asyncio.to_thread(self._read, size)

    def _write(self, data: bytes) -> None:
        """在线程中完整发送输入，部分写入后失败不得自动重试。

        Args:
            data (bytes): 原始 UTF-8 输入。
        """
        offset = 0
        while offset < len(data):
            count = wintypes.DWORD()
            chunk = data[offset:]
            if not self.kernel.WriteFile(self.writer, chunk, len(chunk), ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if count.value == 0:
                raise BrokenPipeError("终端没有接受输入")
            offset += count.value

    async def write(self, data: bytes) -> None:
        """将输入交给独立线程，与输出排空并行。

        Args:
            data (bytes): 原样发送的输入。
        """
        await asyncio.to_thread(self._write, data)

    async def wait(self) -> int:
        """等待根进程，关闭 ConPTY 时保持输出读取运行，收齐最后一帧。"""
        await asyncio.to_thread(self.kernel.WaitForSingleObject, self.process, 0xFFFFFFFF)
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.process, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        # 1. ClosePseudoConsole 可能等待输出排空，因此不能放在输出读取协程里同步调用。
        console, self.console = self.console, None
        if console:
            await asyncio.to_thread(self.kernel.ClosePseudoConsole, console)
        return int(code.value)

    async def interrupt(self) -> None:
        """先通过终端发送 Ctrl+C，宽限期仍未结束时由 Supervisor 强停 Job。"""
        await self.write(b"\x03")

    def kill(self) -> None:
        """强制终止 Job 中的全部受管进程。"""
        self.job.terminate()

    def close(self) -> None:
        """输出收尾后释放数据管道、进程和 Job 句柄。"""
        self.job.close()
        for name in ("reader", "writer", "process"):
            handle = getattr(self, name)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, name, None)
