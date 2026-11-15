"""在独立进程主线程执行模型代码，控制线程只负责接收请求。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import ctypes
from ctypes import wintypes
import os
import importlib
import queue
import signal
import sys
import threading
import traceback
from multiprocessing.connection import PipeConnection
from types import FrameType
from typing import cast

from protocol import ExecuteCommand, MessageKind, RunnerPhase, receive
from repl_api import JsonValue, ReplEndReason, ReplHost
from runner_output import NativeOutput, ResultWriter, TextOutput

CODE_FILENAME = "<ayaya-python>"


class CapabilityHost:
    """把能力来源固定在输出封装中，向插件提供当前执行接口。"""

    def __init__(self, runner: Runner, export_name: str) -> None:
        """保存本进程 runner 和准备好的能力标识。

        Args:
            runner (Runner): 本能力借用的执行和输出实现。
            export_name (str): 父端可核对的能力暴露名。
        """
        self.runner = runner
        self.export_name = export_name

    @property
    def execution_id(self) -> str:
        """返回当前执行编号，供能力启动监听时捕获。

        Returns:
            str: 当前调用编号，空闲时为空。
        """
        return self.runner.active_id

    def emit_image(self, image_bytes: bytes) -> None:
        """把观察结果直接加入本次工具结果。

        Args:
            image_bytes (bytes): PNG 或 JPEG 编码图片。
        """
        self.runner.writer.image(image_bytes)

    def publish_event(self, execution_id: str, name: str, payload: dict[str, JsonValue]) -> None:
        """仅交付当前执行的事件，并复制发布时的载荷。

        Args:
            execution_id (str): 事件所属的调用编号。
            name (str): 框架支持的事件名称。
            payload (dict[str, JsonValue]): 插件提供的展示字段。
        """
        if execution_id and execution_id == self.execution_id:
            self.runner.writer.emit({"kind": MessageKind.EVENT, "execution_id": execution_id,
                                     "capability": self.export_name, "name": name, "payload": deepcopy(payload)})

    def request_interrupt(self, execution_id: str) -> None:
        """把绑定编号的中断交给 runner。

        Args:
            execution_id (str): 异步回调捕获的执行编号。
        """
        self.runner.request_interrupt(execution_id)

    def raise_if_interrupted(self, execution_id: str) -> None:
        """在能力方法边界检查当前中断。

        Args:
            execution_id (str): 方法所属执行编号。
        """
        self.runner.raise_if_interrupted(execution_id)

    def defer_cleanup(self, execution_id: str, callback: Callable[[], object]) -> None:
        """登记当前调用结束前要尝试的资源释放。

        Args:
            execution_id (str): 清理所属执行编号。
            callback (Callable[[], object]): 同步清理动作。
        """
        if execution_id and execution_id == self.execution_id:
            self.runner.cleanups.append(callback)


class Runner:
    """隔离模型命名空间，并将匹配当前调用的 Ctrl+C 转为执行结果。"""

    def __init__(self, commands: PipeConnection, results: PipeConnection) -> None:
        """准备输出和持久状态，主线程随后开始接收执行。

        Args:
            commands (PipeConnection): 父端命令读端。
            results (PipeConnection): 子端结果写端。
        """
        self.commands = commands
        self.writer = ResultWriter(results)
        self.native = (NativeOutput(self.writer, 1), NativeOutput(self.writer, 2))
        sys.stdout = TextOutput(self.writer, 1)
        sys.stderr = TextOutput(self.writer, 2)
        self.namespace: dict[str, object] = {"__name__": "__repl__", "display_image": self.writer.display_image}
        self.pending: queue.Queue[ExecuteCommand | None] = queue.Queue()
        self.phase = RunnerPhase.IDLE
        self.active_id = ""
        self.interrupted_id = ""
        self.cleanups: list[Callable[[], object]] = []
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
        self.kernel.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        self.kernel.SetConsoleCtrlHandler.restype = wintypes.BOOL

    def request_interrupt(self, execution_id: str) -> None:
        """只受理当前排队或执行中的一次请求，向专用控制台发送 Ctrl+C。

        Args:
            execution_id (str): 请求停止的执行编号。
        """
        accepted = (execution_id == self.active_id and bool(execution_id)
                    and self.phase in {RunnerPhase.SCHEDULED, RunnerPhase.EXECUTING}
                    and self.interrupted_id != execution_id)
        if accepted:
            self.interrupted_id = execution_id
        self.writer.emit({"kind": MessageKind.INTERRUPT_ACK, "execution_id": execution_id, "accepted": accepted})
        if accepted and self.phase == RunnerPhase.EXECUTING:
            if not self.kernel.GenerateConsoleCtrlEvent(0, 0):
                raise ctypes.WinError(ctypes.get_last_error())

    def raise_if_interrupted(self, execution_id: str) -> None:
        """在受控方法边界或开始执行前交付已受理的中断。

        Args:
            execution_id (str): 正在检查的执行编号。

        Raises:
            KeyboardInterrupt: 当前执行已收到中断请求。
        """
        if execution_id == self.active_id and execution_id == self.interrupted_id:
            raise KeyboardInterrupt("执行已被用户终止")

    def _handle_sigint(self, signum: int, frame: FrameType | None) -> None:
        """在执行阶段交付匹配编号的信号，异常处理和清理已有阶段边界保护。

        Args:
            signum (int): Windows 投递的信号编号。
            frame (FrameType | None): 主线程收到信号时的调用栈。
        """
        if self.phase == RunnerPhase.EXECUTING:
            self.raise_if_interrupted(self.active_id)

    def _read_commands(self) -> None:
        """持续读取命令，在主线程忙于模型代码时仍可受理中断。"""
        try:
            while True:
                command = receive(self.commands)
                if command["kind"] == MessageKind.INTERRUPT:
                    self.request_interrupt(command["execution_id"])
                elif command["kind"] == MessageKind.EXECUTE:
                    self.active_id = command["execution_id"]
                    self.interrupted_id = ""
                    self.phase = RunnerPhase.SCHEDULED
                    self.pending.put(command)
                else:
                    raise ValueError("Python 执行阶段收到非执行命令")
        except (EOFError, OSError, ValueError):
            self.pending.put(None)

    def _execute(self, command: ExecuteCommand) -> None:
        """捕获本次代码结束，收齐输出后恢复等待。

        Args:
            command (ExecuteCommand): 已绑定当前执行编号的模型代码。
        """
        self.writer.execution_id = self.active_id
        self.cleanups = []
        reason, error = ReplEndReason.COMPLETED, ""
        try:
            # 1. 外层捕获覆盖执行入口和阶段交接，内层结束时先保护异常处理与清理。
            try:
                self.phase = RunnerPhase.EXECUTING
                self.raise_if_interrupted(self.active_id)
                exec(compile(command["code"], CODE_FILENAME, "exec"), self.namespace, self.namespace)
            finally:
                self.phase = RunnerPhase.CLEANUP
            self.raise_if_interrupted(self.active_id)
        except KeyboardInterrupt:
            reason, error = ReplEndReason.INTERRUPTED, "执行已被用户终止，请停止后续操作并结束本轮"
        except BaseException:
            reason, error = ReplEndReason.ERROR, traceback.format_exc()
        # 1. 当前代码已结束，全部清理逆序尝试；故障随结果交付。
        cleanup_errors = []
        for callback in reversed(self.cleanups):
            try:
                callback()
            except BaseException:
                cleanup_errors.append(traceback.format_exc())
        self.cleanups.clear()
        error = "\n".join(filter(None, [error, *cleanup_errors]))
        for stream in self.native:
            stream.drain()
        self.phase = RunnerPhase.IDLE
        self.active_id = ""
        self.writer.emit({"kind": MessageKind.FINISHED, "execution_id": self.writer.execution_id,
                          "reason": reason, "error": error, "pid": os.getpid()})
        self.writer.execution_id = ""

    def run(self) -> None:
        """安装主线程信号处理，独立读取命令并逐次执行。"""
        signal.signal(signal.SIGINT, self._handle_sigint)
        if not self.kernel.SetConsoleCtrlHandler(None, False):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            initialization = receive(self.commands)
            if initialization["kind"] != MessageKind.INITIALIZE:
                raise ValueError("Python 初始化阶段需要能力声明")
            declarations = initialization["capabilities"]
            for declaration in declarations:
                root = declaration["module_root"]
                if root not in sys.path:
                    sys.path.insert(0, root)
            for declaration in declarations:
                module = importlib.import_module(declaration["module_name"])
                factory = cast(Callable[[dict[str, JsonValue], ReplHost], object], module.create)
                name = declaration["export_name"]
                self.namespace[name] = factory(declaration["config"], CapabilityHost(self, name))
        except BaseException:
            self.writer.emit({"kind": MessageKind.ERROR, "execution_id": "", "error": traceback.format_exc()})
            self.writer.flush()
            return
        threading.Thread(target=self._read_commands, daemon=True).start()
        self.writer.emit({"kind": MessageKind.READY, "execution_id": ""})
        while (command := self.pending.get()) is not None:
            self._execute(command)


def main() -> None:
    """用继承句柄连接父进程的两个私有管道。"""
    Runner(PipeConnection(int(sys.argv[1]), readable=True, writable=False),
           PipeConnection(int(sys.argv[2]), readable=False, writable=True)).run()


if __name__ == "__main__":
    main()
