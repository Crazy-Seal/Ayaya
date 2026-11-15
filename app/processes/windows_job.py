"""使用 Windows Job Object 约束受管工具产生的整个进程树。"""

from __future__ import annotations

import ctypes
import os
import time


class WindowsJob:
    """约束 Windows 进程树，关闭 Job 句柄时终止其中进程。

    Attributes:
        _kernel (ctypes.WinDLL): 配置好调用约定的 Windows 内核函数集合。
        _handle (Windows HANDLE | None): 当前实例持有的 Windows Job 句柄，关闭后为空。
    """
    def __init__(self, pid: int) -> None:
        """创建带关闭清理规则的 Job 并关联挂起进程，失败时回收本地句柄。

        Args:
            pid (int): 需要加入 Job 或恢复线程的进程标识。

        Raises:
            ctypes.WinError: 输入、持久状态或系统调用不满足当前执行约束。
        """
        # 1. 非 Windows 平台不调用系统句柄接口。
        if os.name != "nt":
            self._handle = None
            return
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            """定义 Windows 系统调用使用的 BasicLimit 内存布局。"""
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            """定义 Windows 系统调用使用的 IoCounters 内存布局。"""
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimit(ctypes.Structure):
            """定义 Windows 系统调用使用的 ExtendedLimit 内存布局。"""
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        # 2. 声明 ctypes 参数和返回类型，保证 64 位句柄不会被默认整数截断。
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = ctypes.c_void_p
        kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._kernel = kernel
        self._handle = handle
        process = None
        try:
            # 3. 设置关闭 Job 时终止进程树，并在放行进程前完成 Job 关联。
            limits = ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # 使用 KILL_ON_JOB_CLOSE 清理进程树
            if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.AssignProcessToJobObject(handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        # 4. 关联失败释放 Job；临时进程句柄无论成功失败都在 finally 关闭。
        except BaseException:
            kernel.CloseHandle(handle)
            self._handle = None
            raise
        finally:
            if process:
                kernel.CloseHandle(process)

    def terminate(self) -> None:
        """结束 Job 内进程树，非 Windows 实例不执行系统调用。"""
        if self._handle is not None:
            self._kernel.TerminateJobObject(self._handle, 1)

    def terminate_and_wait(self) -> None:
        """REPL 回收时等待整棵进程树结束，再允许调用方释放 Job 句柄。

        Raises:
            OSError: Windows 无法终止进程树或查询实际存活数量。
        """
        if self._handle is None:
            return
        from ctypes import wintypes

        class Accounting(ctypes.Structure):
            """读取 Windows Job 中已经结束和仍在运行的进程数量。"""

            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self._kernel.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                         wintypes.DWORD, ctypes.c_void_p]
        self._kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self._kernel.WaitForSingleObject.restype = wintypes.DWORD
        # 1. 先保留现有成员的同步句柄，退出码及 Job 计数更新可能早于句柄结束信号。
        capacity = 16
        while True:
            buffer = ctypes.create_string_buffer(8 + capacity * ctypes.sizeof(ctypes.c_size_t))
            if self._kernel.QueryInformationJobObject(self._handle, 3, buffer, len(buffer), None):
                break
            error = ctypes.get_last_error()
            if error != 234:
                raise ctypes.WinError(error)
            capacity *= 2
        count = (wintypes.DWORD * 2).from_buffer(buffer)[1]
        handles = []
        try:
            for pid in (ctypes.c_size_t * count).from_buffer(buffer, 8):
                handle = self._kernel.OpenProcess(0x00100000, False, pid)
                if handle:
                    handles.append(handle)
                elif ctypes.get_last_error() != 87:
                    raise ctypes.WinError(ctypes.get_last_error())
            # 2. 停止整棵树，等待成员数量归零，覆盖查询后仍短暂派生的进程。
            if not self._kernel.TerminateJobObject(self._handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            accounting = Accounting()
            while True:
                if not self._kernel.QueryInformationJobObject(self._handle, 1, ctypes.byref(accounting),
                                                              ctypes.sizeof(accounting), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if accounting.ActiveProcesses == 0:
                    break
                time.sleep(0.01)
            # 3. 现有成员的内核退出已完成，应用才能交还进程回收结果。
            for handle in handles:
                if self._kernel.WaitForSingleObject(handle, 0xFFFFFFFF) != 0:
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            for handle in handles:
                self._kernel.CloseHandle(handle)

    @staticmethod
    def resume_suspended_process(pid: int) -> None:
        """枚举并恢复已加入 Job 的挂起进程线程，避免子进程先于约束启动。

        Args:
            pid (int): 需要加入 Job 或恢复线程的进程标识。

        Raises:
            ctypes.WinError: 输入、持久状态或系统调用不满足当前执行约束。
            RuntimeError: 进程 {pid} 的挂起线程不存在。
        """
        if os.name != "nt":
            return
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            """定义 Windows 系统调用使用的 ThreadEntry 内存布局。"""
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        # 1. 配置线程枚举及恢复接口，进程此时已经属于受管 Job。
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel.Thread32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32First.restype = wintypes.BOOL
        kernel.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32Next.restype = wintypes.BOOL
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = ctypes.c_void_p
        kernel.ResumeThread.argtypes = [ctypes.c_void_p]
        kernel.ResumeThread.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = wintypes.BOOL
        # 2. 枚举目标进程线程，只恢复该 PID 的挂起线程。
        snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        resumed = 0
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(ThreadEntry)
            has_entry = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while has_entry:
                if entry.th32OwnerProcessID == pid:
                    thread = kernel.OpenThread(0x0002, False, entry.th32ThreadID)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        resumed += 1
                    finally:
                        kernel.CloseHandle(thread)
                has_entry = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            if resumed == 0:
                raise RuntimeError(f"进程 {pid} 的挂起线程不存在")
        # 3. 无论恢复成功还是异常都释放枚举快照句柄。
        finally:
            kernel.CloseHandle(snapshot)

    def close(self) -> None:
        """释放 Job 句柄并清空引用，关闭操作可重复调用。"""
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
