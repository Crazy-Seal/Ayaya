"""由应用持有唯一 Python 子进程，借给各轮主 Agent 使用。"""

import asyncio
import base64
from copy import deepcopy
from multiprocessing import Pipe
from multiprocessing.connection import PipeConnection
import os
import logging
from pathlib import Path
import subprocess
import sys
import uuid
from typing import TypeVar

from app.processes.windows_job import WindowsJob
from app.repl.contracts import ReplCallContext, ReplEndReason, ReplImage, ReplResult, ReplText
from app.repl.contracts import ReplEventHandler, ReplExecutionFinished, ReplPluginEvent
from app.repl.protocol import CapabilityDeclaration, MessageKind, ResultMessage, receive, send

INITIALIZATION_TIMEOUT_SECONDS = 30.0
EXECUTION_TIMEOUT_SECONDS = 120.0
CREATE_SUSPENDED = 0x00000004
logger = logging.getLogger(__name__)
T = TypeVar("T")


async def finish_before_cancelling(operation: asyncio.Task[T]) -> T:
    """让已开始的进程收尾完成后，再传播等待方的取消。

    Args:
        operation (asyncio.Task[T]): 当前组件拥有的收尾任务。

    Returns:
        T: 未被取消时返回收尾结果。

    Raises:
        asyncio.CancelledError: 收尾完成后传播外层取消，或任务本身被取消。
    """
    cancelled = False
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            if operation.cancelled():
                raise
            cancelled = True
    result = operation.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class ReplService:
    """管理唯一 runner 的占用、会话状态及进程回收。

    Attributes:
        _context (ReplCallContext | None): 当前进程所属会话及能力声明快照。
        _busy (bool): 一次执行或重置正在使用共享进程。
    """

    def __init__(self, *, workdir: Path, execution_timeout: float = EXECUTION_TIMEOUT_SECONDS,
                 initialization_timeout: float = INITIALIZATION_TIMEOUT_SECONDS,
                 on_event: ReplEventHandler | None = None) -> None:
        """记录启动设置，首次执行才创建子进程。

        Args:
            workdir (Path): 应用工作区或测试临时目录。
            execution_timeout (float): 单次代码和清理可占用的秒数。
            initialization_timeout (float): 等待能力装配完成的秒数。
            on_event (ReplEventHandler | None): 应用装配提供的插件事件和执行结束接入函数。
        """
        self._workdir = workdir
        self._execution_timeout = execution_timeout
        self._initialization_timeout = initialization_timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._job: WindowsJob | None = None
        self._commands: PipeConnection | None = None
        self._results: PipeConnection | None = None
        self._context: ReplCallContext | None = None
        self._busy = False
        self._closed = False
        self._execution_id = ""
        self._on_event = on_event
        self._work: asyncio.Task[ReplResult] | None = None
        self._cancel_requested = False

    def _acquire(self) -> None:
        """关闭后拒绝执行，已有调用占用时立即报告忙。"""
        if self._closed:
            raise RuntimeError("Python REPL 已关闭")
        if self._busy:
            raise RuntimeError("Python REPL 正在执行，请等待当前调用结束")
        self._busy = True

    async def _start(self, context: ReplCallContext) -> None:
        """在隐藏专用控制台启动 runner，并在放行前关联 Job。

        Args:
            context (ReplCallContext): 启动时要预装的插件能力。
        """
        child_commands, self._commands = Pipe(duplex=False)
        self._results, child_results = Pipe(duplex=False)
        handles = [child_commands.fileno(), child_results.fileno()]
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        startup.lpAttributeList = {"handle_list": handles}
        for handle in handles:
            os.set_handle_inheritable(handle, True)
        try:
            self._process = subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).with_name("runner.py")), *(str(h) for h in handles)],
                cwd=self._workdir, startupinfo=startup, close_fds=True, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_CONSOLE | CREATE_SUSPENDED,
            )
            self._job = WindowsJob(self._process.pid)
            WindowsJob.resume_suspended_process(self._process.pid)
        finally:
            child_commands.close()
            child_results.close()
        assert self._commands is not None
        declarations: list[CapabilityDeclaration] = [
            {"module_root": str(item.declaration.module_root), "module_name": item.declaration.module_name,
             "export_name": item.declaration.export_name, "config": item.declaration.config}
            for item in context.capabilities]
        send(self._commands, {"kind": MessageKind.INITIALIZE, "capabilities": declarations})
        message = await asyncio.wait_for(self._receive(), self._initialization_timeout)
        if message["kind"] == MessageKind.ERROR:
            raise RuntimeError(f"Python 初始化失败：{message['error']}")
        if message["kind"] != MessageKind.READY:
            raise ValueError("Python 初始化阶段收到非就绪消息")

    async def _receive(self) -> ResultMessage:
        """等待独立结果管道交付下一条消息。

        Returns:
            ResultMessage: runner 发送的完整结果消息。

        Raises:
            ValueError: 结果管道收到命令消息。
        """
        assert self._results is not None
        message = await asyncio.to_thread(receive, self._results)
        if (message["kind"] == MessageKind.INITIALIZE or message["kind"] == MessageKind.EXECUTE
                or message["kind"] == MessageKind.INTERRUPT):
            raise ValueError("Python 结果管道收到命令消息")
        return message

    async def execute(self, code: str, context: ReplCallContext) -> ReplResult:
        """取得占用后选择进程，执行代码并等待实际结束。

        Args:
            code (str): 已经通过工具审批的代码。
            context (ReplCallContext): 会话、调用来源及准备好的能力。

        Returns:
            ReplResult: 已收集输出和本次结束原因。
        """
        self._acquire()
        self._execution_id = uuid.uuid4().hex
        self._cancel_requested = False
        work = asyncio.create_task(self._execute(code, context))
        self._work = work
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            # 1. 独立收尾任务保护中断投递、进程回收和应用事件关闭。
            await finish_before_cancelling(asyncio.create_task(self._interrupt_and_wait(work)))
            raise
        finally:
            self._work = None
            self._execution_id = ""
            self._busy = False

    async def _interrupt_and_wait(self, work: asyncio.Task[ReplResult]) -> None:
        """受理工具取消后，等同一次代码及其清理完成。

        Args:
            work (asyncio.Task[ReplResult]): 当前代码和事件收尾任务。
        """
        self._cancel_requested = True
        if not work.done():
            if self._context is None:
                await self._dispose_runner()
            elif self._commands is not None:
                try:
                    send(self._commands, {"kind": MessageKind.INTERRUPT, "execution_id": self._execution_id})
                except OSError:
                    await self._dispose_runner()
        await work

    async def _execute(self, code: str, context: ReplCallContext) -> ReplResult:
        """在受保护的任务中收集结果，超时预算覆盖清理。

        Args:
            code (str): 已通过审批的模型代码。
            context (ReplCallContext): 本次来源和能力声明。

        Returns:
            ReplResult: 真实执行及输出结果。
        """
        outputs: list[ReplText | ReplImage] = []
        execution_id = self._execution_id
        try:
            if self._process is not None and (
                self._process.poll() is not None or self._context is not None and (
                    self._context.session_id != context.session_id or self._context.capabilities != context.capabilities)
            ):
                await self._dispose_runner()
            if self._cancel_requested or self._closed:
                return ReplResult((), ReplEndReason.INTERRUPTED, "执行已被用户终止")
            if self._process is None:
                await self._start(context)
                self._context = deepcopy(context)
            assert self._commands is not None
            assert self._process is not None
            pid = self._process.pid
            send(self._commands, {"kind": MessageKind.EXECUTE, "execution_id": execution_id, "code": code})
            async with asyncio.timeout(self._execution_timeout):
                while True:
                    message = await self._receive()
                    if message["execution_id"] != execution_id:
                        continue
                    if message["kind"] == MessageKind.TEXT:
                        outputs.append(ReplText(message["text"]))
                    elif message["kind"] == MessageKind.IMAGE:
                        outputs.append(ReplImage(base64.b64decode(message["data"]), message["mime_type"]))
                    elif message["kind"] == MessageKind.EVENT:
                        capability = next((item for item in context.capabilities
                                           if item.declaration.export_name == message["capability"]), None)
                        if capability is not None:
                            await self._deliver(ReplPluginEvent(execution_id, context, capability,
                                message["name"], message["payload"]))
                    elif message["kind"] == MessageKind.FINISHED:
                        return ReplResult(tuple(outputs), message["reason"], message["error"], pid)
        except TimeoutError:
            await self._dispose_runner()
            return ReplResult(tuple(outputs), ReplEndReason.TIMEOUT, "Python 执行或初始化超时；状态已清空，下次调用重新创建")
        except (EOFError, OSError, RuntimeError, ValueError) as exc:
            await self._dispose_runner()
            return ReplResult(tuple(outputs), ReplEndReason.CRASHED, f"Python 进程不可用，代码不会自动重放：{exc}")
        except BaseException:
            await self._dispose_runner()
            raise
        finally:
            await self._deliver(ReplExecutionFinished(execution_id, context))

    async def _deliver(self, event: ReplPluginEvent | ReplExecutionFinished) -> None:
        """交付应用事件并记录故障，让执行占用仍可正常释放。

        Args:
            event (ReplPluginEvent | ReplExecutionFinished): 带本次可信来源的事件。
        """
        if self._on_event is not None:
            try:
                await self._on_event(event)
            except Exception:
                logger.exception("REPL 插件事件交付失败")

    async def reset(self) -> None:
        """空闲时关闭进程树，下一次代码重新初始化。"""
        self._acquire()
        try:
            await finish_before_cancelling(asyncio.create_task(self._dispose_runner()))
        finally:
            self._busy = False

    async def _dispose_runner(self) -> None:
        """结束进程树后释放管道，清除所属会话快照。"""
        job, process = self._job, self._process
        connections = (self._commands, self._results)
        self._job = self._process = None
        self._commands = self._results = None
        self._context = None
        try:
            if job is not None:
                await asyncio.to_thread(job.terminate_and_wait)
            if process is not None:
                if process.poll() is None:
                    process.kill()
                await asyncio.to_thread(process.wait)
        finally:
            if job is not None:
                job.close()
            if process is not None and process.stderr is not None:
                process.stderr.close()
            for connection in connections:
                if connection is not None:
                    connection.close()

    async def close(self) -> None:
        """应用退出时封闭入口并销毁进程树。"""
        self._closed = True
        await finish_before_cancelling(asyncio.create_task(self._close()))

    async def _close(self) -> None:
        """结束进程后等待本次调用的应用事件收尾。"""
        await self._dispose_runner()
        work = self._work
        if work is not None:
            await work
