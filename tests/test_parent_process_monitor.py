"""后端生命周期所有者监控测试。"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Callable

import pytest

from app.parent_process_monitor import (
    ParentProcessMonitorError,
    WindowsProcessWaiter,
    start_parent_process_monitor,
)


class _ImmediateThread:
    """在 start 时同步执行目标函数的测试线程。"""

    def __init__(self, *, target: Callable[[], None], daemon: bool) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        该定义仅用于外层用例：在 start 时同步执行目标函数的测试线程。

        Args:
            target (Callable[[], None]): 测试线程需要执行的目标函数。
            daemon (bool): 调用方为测试线程指定的守护线程标记。
        """
        self._target = target
        self.daemon = daemon

    def start(self) -> None:
        """记录监控线程启动，并按用例需要同步调用线程目标。

        该定义仅用于外层用例：在 start 时同步执行目标函数的测试线程。
        """
        self._target()


class _FakeKernel32:
    """记录 Windows 句柄操作的内核 API 替身。"""

    def __init__(self, *, handle: int = 99, wait_result: int = 0) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        该定义仅用于外层用例：记录 Windows 句柄操作的内核 API 替身。

        Args:
            handle (int): 测试提供的 Windows 进程句柄。
            wait_result (int): Windows 等待接口应返回的测试状态。
        """
        self.handle = handle
        self.wait_result = wait_result
        self.closed: list[int] = []

    def OpenProcess(self, _access: int, _inherit: bool, _pid: int) -> int:
        """模拟打开 Windows 进程，记录参数并返回预设句柄。

        该定义仅用于外层用例：记录 Windows 句柄操作的内核 API 替身。

        Args:
            _access (int): 调用方请求的 Windows 进程访问权限。
            _inherit (bool): 是否继承进程句柄。
            _pid (int): 准备打开的父进程编号。

        Returns:
            int: 供外层用例断言使用的预设结果。
        """
        return self.handle

    def WaitForSingleObject(self, _handle: int, _timeout: int) -> int:
        """模拟等待 Windows 父进程退出，返回本用例配置的等待结果。

        该定义仅用于外层用例：记录 Windows 句柄操作的内核 API 替身。

        Args:
            _handle (int): 测试提供的 Windows 进程句柄。
            _timeout (int): 进程等待接口使用的超时值。

        Returns:
            int: 供外层用例断言使用的预设结果。
        """
        return self.wait_result

    def CloseHandle(self, handle: int) -> bool:
        """记录句柄关闭，供检查成功和失败路径都释放资源。

        该定义仅用于外层用例：记录 Windows 句柄操作的内核 API 替身。

        Args:
            handle (int): 测试提供的 Windows 进程句柄。

        Returns:
            bool: 供外层用例断言使用的预设结果。
        """
        self.closed.append(handle)
        return True


def test_owner_exit_requests_server_shutdown() -> None:
    """所有者退出后应触发一次 Uvicorn 关闭。"""
    shutdown_calls: list[str] = []

    thread = start_parent_process_monitor(
        lambda: shutdown_calls.append("shutdown"),
        environment={"AYAYA_PARENT_PID": "1234"},
        current_pid=5678,
        platform="win32",
        waiter=lambda _pid: None,
        thread_factory=_ImmediateThread,
    )

    assert thread is not None
    assert thread.daemon is True
    assert shutdown_calls == ["shutdown"]


def test_missing_owner_pid_disables_monitor() -> None:
    """独立后端未绑定所有者时不应创建监控线程。"""
    thread = start_parent_process_monitor(
        lambda: None,
        environment={},
        current_pid=os.getpid(),
        platform="win32",
        waiter=lambda _pid: None,
        thread_factory=_ImmediateThread,
    )

    assert thread is None


@pytest.mark.parametrize("value", ["", "abc", "0", "-1"])
def test_invalid_owner_pid_is_rejected(value: str) -> None:
    """受管后端不得静默忽略非法所有者 PID。

    Args:
        value (str): 当前需要校验或转换的值。
    """
    with pytest.raises(ParentProcessMonitorError):
        start_parent_process_monitor(
            lambda: None,
            environment={"AYAYA_PARENT_PID": value},
            current_pid=5678,
            platform="win32",
            waiter=lambda _pid: None,
            thread_factory=_ImmediateThread,
        )


def test_windows_waiter_closes_process_handle() -> None:
    """等待完成后必须关闭 Windows 进程句柄。"""
    kernel32 = _FakeKernel32()

    WindowsProcessWaiter(kernel32=kernel32, get_last_error=lambda: 0).wait(1234)

    assert kernel32.closed == [99]


def test_windows_wait_failure_still_closes_handle() -> None:
    """等待失败不得泄漏已经打开的 Windows 句柄。"""
    kernel32 = _FakeKernel32(wait_result=0xFFFFFFFF)

    with pytest.raises(ParentProcessMonitorError):
        WindowsProcessWaiter(kernel32=kernel32, get_last_error=lambda: 5).wait(1234)

    assert kernel32.closed == [99]


def test_default_windows_monitor_opens_handle_before_starting_thread() -> None:
    """无法打开所有者时应在服务线程启动前直接失败。"""
    kernel32 = _FakeKernel32(handle=0)
    waiter = WindowsProcessWaiter(kernel32=kernel32, get_last_error=lambda: 87)
    thread_created = False

    def thread_factory(**_kwargs: object) -> _ImmediateThread:
        """创建可由用例控制执行时机的测试监控线程。

        该定义仅用于外层用例：无法打开所有者时应在服务线程启动前直接失败。

        Args:
            _kwargs (object): 传递给被调用函数的其他关键字参数。
        """
        nonlocal thread_created
        thread_created = True
        raise AssertionError("句柄打开失败时不应创建线程")

    with pytest.raises(ParentProcessMonitorError, match="无法打开"):
        start_parent_process_monitor(
            lambda: None,
            environment={"AYAYA_PARENT_PID": "1234"},
            current_pid=5678,
            platform="win32",
            process_waiter=waiter,
            thread_factory=thread_factory,
        )

    assert thread_created is False


@pytest.mark.skipif(sys.platform != "win32", reason="仅验证 Windows 进程句柄")
def test_real_windows_process_exit_wakes_monitor() -> None:
    """真实 Windows 子进程退出后应唤醒监控线程。"""
    owner = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.2)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    stopped = threading.Event()
    try:
        thread = start_parent_process_monitor(
            stopped.set,
            environment={"AYAYA_PARENT_PID": str(owner.pid)},
            current_pid=os.getpid(),
            platform="win32",
        )

        assert thread is not None
        assert stopped.wait(timeout=3)
    finally:
        if owner.poll() is None:
            owner.terminate()
        owner.wait(timeout=3)
