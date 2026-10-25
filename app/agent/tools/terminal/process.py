"""管道与 Unix PTY 的内部进程 Adapter，不持有任务或聊天状态。"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Protocol

from app.agent.tools.terminal.windows_job import WindowsJob

logger = logging.getLogger(__name__)


class TerminalOperations(Protocol):
    """统一普通管道与各平台 PTY 的系统终端操作。

    Attributes:
        pid (int): 受管根进程编号。
    """

    pid: int

    async def read(self, size: int) -> bytes:
        """等待输出，流闭合时返回空字节。

        Args:
            size (int): 本次读取的最大字节数。

        Returns:
            bytes: 当前输出或 EOF。
        """
        ...

    async def write(self, data: bytes) -> None:
        """发送输入；失败时不承诺已发送字节数，调用方不得自动重试。

        Args:
            data (bytes): 原始输入，不自动追加换行。
        """
        ...

    async def wait(self) -> int:
        """等待根进程退出，不负责数据库终态。

        Returns:
            int: 根进程真实退出码。
        """
        ...

    async def interrupt(self) -> None:
        """请求受管进程及其终端前台命令结束。"""
        ...

    def kill(self) -> None:
        """强制结束当前 Adapter 所有的进程树。"""
        ...

    def close(self) -> None:
        """释放进程和终端句柄，允许重复调用。"""
        ...


class PipeProcess:
    """使用普通管道双向通信，Windows Job 或 Unix 进程组负责回收。"""

    def __init__(self, process: asyncio.subprocess.Process, job: WindowsJob) -> None:
        """保存已受管的子进程。

        Args:
            process (asyncio.subprocess.Process): 已启动但尚未交给任务管理器的进程。
            job (WindowsJob): Windows 进程树句柄；Unix 上不执行系统调用。
        """
        self.process = process
        self.job = job
        self.pid = process.pid

    @classmethod
    async def spawn(cls, argv: list[str], cwd: str) -> PipeProcess:
        """先建立进程树管理，再允许命令开始执行。

        Args:
            argv (list[str]): 直接执行的程序和参数。
            cwd (str): 已校验的工作目录。

        Returns:
            PipeProcess: stdin、stdout 均可持续使用的进程。
        """
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004) if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        job = None
        try:
            # 1. Windows 挂起启动的进程加入 Job 后才恢复；Unix 子进程已属于独立进程组。
            job = WindowsJob(process.pid)
            if os.name == "nt":
                job.resume_suspended_process(process.pid)
            return cls(process, job)
        except BaseException:
            if job is not None:
                job.terminate()
                job.close()
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise

    async def read(self, size: int) -> bytes:
        """由唯一输出收集任务读取管道。

        Args:
            size (int): 本次读取的最大字节数。

        Returns:
            bytes: 输出数据或表示 EOF 的空字节。
        """
        assert self.process.stdout is not None
        return await self.process.stdout.read(size)

    async def write(self, data: bytes) -> None:
        """发送原始输入并等待管道接受。

        Args:
            data (bytes): 不补换行的 UTF-8 输入。
        """
        assert self.process.stdin is not None
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def wait(self) -> int:
        """等待根进程退出并收回持有管道的子进程。

        Returns:
            int: 根进程退出码。
        """
        # 1. asyncio 的 wait 也可能等待子进程持有的管道，先观察根进程退出再回收同组子进程。
        while self.process.returncode is None:
            await asyncio.sleep(0.02)
        self.kill()
        return await self.process.wait()

    async def interrupt(self) -> None:
        """先通知进程组中断，由 TerminalTask 等待宽限期后安排强停。"""
        try:
            if os.name == "nt":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(self.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    def kill(self) -> None:
        """强停全部受管子进程，包括仍持有输出管道的子进程。"""
        try:
            if os.name == "nt":
                self.job.terminate()
            else:
                os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        """关闭 stdin 和 Job，并回收 Unix 根进程退出后的同组子进程。"""
        if self.process.stdin is not None:
            self.process.stdin.close()
        self.kill()
        self.job.close()


class UnixPtyProcess(PipeProcess):
    """在独立启动程序中建立 Unix 控制终端，避免后端多线程进程中使用 preexec_fn。"""

    def __init__(self, process: asyncio.subprocess.Process, master: int) -> None:
        """接管已经绑定子进程的 PTY 主端。

        Args:
            process (asyncio.subprocess.Process): 负责建立控制终端并 exec 命令的进程。
            master (int): 父进程唯一持有的 PTY 主端文件描述符。
        """
        super().__init__(process, WindowsJob(process.pid))
        self.master = master

    @classmethod
    async def spawn(cls, argv: list[str], cwd: str) -> UnixPtyProcess:
        """创建 120 列 30 行 PTY，并通过私有启动程序建立控制终端。

        Args:
            argv (list[str]): 实际 Shell 和参数。
            cwd (str): 已校验工作目录。

        Returns:
            UnixPtyProcess: 提供终端语义的进程 Adapter。
        """
        import fcntl
        import pty
        import struct
        import termios

        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
            # 1. 子进程自行 setsid 和绑定控制终端，父进程不在 fork 后执行 Python 回调。
            bootstrap = Path(__file__).with_name("unix_bootstrap.py")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", str(bootstrap), str(slave), *argv,
                cwd=cwd, pass_fds=(slave,),
                stdin=slave, stdout=slave, stderr=slave,
            )
            return cls(process, master)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)

    async def read(self, size: int) -> bytes:
        """在线程中等待终端输出，Linux PTY 的 EIO 表示从端已关闭。

        Args:
            size (int): 最大读取字节数。

        Returns:
            bytes: 当前终端数据或 EOF。
        """
        try:
            return await asyncio.to_thread(os.read, self.master, size)
        except OSError as exc:
            if exc.errno == errno.EIO:
                return b""
            raise

    async def write(self, data: bytes) -> None:
        """完整写入终端主端，不改变输入内容。

        Args:
            data (bytes): 原始 UTF-8 输入和终端控制字符。
        """
        offset = 0
        while offset < len(data):
            offset += await asyncio.to_thread(os.write, self.master, data[offset:])

    async def interrupt(self) -> None:
        """同时通知终端前台进程组和根 Shell，避免只停止等待进程。"""
        try:
            foreground = os.tcgetpgrp(self.master)
            # 1. 从端关闭后可能返回 0；killpg(0) 会杀死后端自己的进程组，必须拒绝。
            if foreground > 0 and foreground != os.getpgrp():
                os.killpg(foreground, signal.SIGHUP)
        except (ProcessLookupError, OSError):
            pass
        await super().interrupt()

    def _session_process_groups(self) -> set[int]:
        """找到受管终端所属操作系统会话中的进程组，包括 Shell 的后台作业。

        Returns:
            set[int]: 只包含当前终端会话的进程组；根进程组始终保留供启动失败时清理。
        """
        groups = {self.pid}
        # 1. 即使进程枚举失败，仍保留终端前台组，维持原有根进程和前台命令清理能力。
        try:
            groups.add(os.tcgetpgrp(self.master))
        except OSError:
            pass
        try:
            # 2. Linux 直接枚举 /proc；macOS 通过 ps 取得 PID，避免依赖两平台不同的会话列格式。
            if sys.platform.startswith("linux"):
                with os.scandir("/proc") as entries:
                    candidates = [int(entry.name) for entry in entries if entry.name.isdecimal()]
            else:
                result = subprocess.run(
                    ["ps", "-A", "-o", "pid="], capture_output=True, text=True,
                    check=True, timeout=2,
                )
                candidates = [int(value) for value in result.stdout.split() if value.isdecimal()]
        except (OSError, subprocess.SubprocessError):
            logger.exception("枚举终端后台作业失败，继续清理根进程组: %s", self.pid)
            return groups
        # 3. 进程可能在枚举后退出；逐项确认 SID，不能按父 PID 或全局进程组猜测任务归属。
        for pid in candidates:
            try:
                if os.getsid(pid) == self.pid:
                    groups.add(os.getpgid(pid))
            except OSError:
                pass
        return groups

    def kill(self) -> None:
        """强停终端会话中所有作业组，防止后台作业继续持有 PTY。"""
        if self.master < 0:
            return
        # 1. Bash 会为后台作业建立独立进程组；根 Shell 退出后仍可通过 SID 找到这些作业。
        for group in self._session_process_groups():
            if group <= 0 or group == os.getpgrp():
                continue
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        # 2. 启动程序可能尚未执行 setsid；此时根 PID 还不是进程组编号，需直接结束该子进程。
        if self.process.returncode is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass

    def close(self) -> None:
        """先清理进程组，再释放 PTY 主端。"""
        self.kill()
        if self.master >= 0:
            os.close(self.master)
            self.master = -1


async def spawn_terminal(argv: list[str], cwd: str, tty: bool) -> TerminalOperations:
    """按平台和 tty 选择内部 Adapter，调用方不接触系统句柄。

    Args:
        argv (list[str]): Shell 和执行参数。
        cwd (str): 工作目录。
        tty (bool): 是否需要真实终端语义。

    Returns:
        TerminalOperations: 已纳入进程树管理的可交互进程。
    """
    if not tty:
        return await PipeProcess.spawn(argv, cwd)
    if os.name == "nt":
        from app.agent.tools.terminal.windows_conpty import WindowsConPtyProcess

        pending = asyncio.create_task(asyncio.to_thread(WindowsConPtyProcess.spawn, argv, cwd))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # 1. 取消线程等待不会取消 CreateProcess；必须接回结果再回收，不能遗留无人管理进程。
            process = await pending
            await dispose_terminal(process)
            raise
    return await UnixPtyProcess.spawn(argv, cwd)


async def dispose_terminal(process: TerminalOperations) -> None:
    """并行排空输出和结束终端，避免 ConPTY 关闭等待无人读取的输出。

    Args:
        process (TerminalOperations): 具体任务或创建失败流程需要释放的实际进程。
    """
    async def drain() -> None:
        """保持终端输出畅通，进程关闭后读到 EOF 即结束。"""
        try:
            while await process.read(65536):
                pass
        except OSError:
            pass

    process.kill()
    try:
        await asyncio.gather(process.wait(), drain())
    finally:
        process.close()
