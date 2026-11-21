"""终端具体任务负责进程、输出和平台资源，生命周期由通用 Supervisor 持有。"""

from __future__ import annotations

import asyncio
import codecs
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.errors import InvalidOperationError
from app.agent.runtime.tasks.base import BaseTask
from app.agent.tools.terminal.service import TerminalService
from app.agent.tools.terminal.models import TaskLaunchResult
from app.agent.tools.terminal.process import TerminalOperations, dispose_terminal
from app.crud.task_record_dao import TaskRecordDao
from app.agent.utils.infra.owned_work import complete_owned_work
from app.agent.utils.infra.bounded_operation import run_bounded
from app.schemas.runtime_types import TaskStatus
from app.schemas.task_types import TaskRecord

if TYPE_CHECKING:
    from app.agent.tools.core.runtime import ToolRuntime

TERMINAL_STOP_GRACE_SECONDS = 2
TERMINAL_CLEANUP_TIMEOUT_SECONDS = 5.0
logger = logging.getLogger(__name__)
TASK_OUTPUT_PAGE_MAX_BYTES = 64 * 1024
TASK_OUTPUT_PAGE_DEFAULT_BYTES = 16 * 1024
TASK_RECENT_OUTPUT_BYTES = 256


@dataclass
class TerminalTask(BaseTask):
    """在统一任务已经登记后创建进程，持有终端交互所需的运行资源。

    Attributes:
        command (str): 用户提交的原始命令。
        argv (list[str]): 已按 Shell 构造的进程参数。
        cwd (str): 已检查的工作目录。
        tty (bool): 是否创建真实终端。
        spool_dir (Path): 当前环境的终端输出根目录。
        records (TaskRecordDao): 只用于补充进程信息及实际退出码的持久化入口。
        terminal (TerminalService): 创建实际系统终端的能力。
        process (TerminalOperations | None): 实际终端进程，尚未启动时为空。
        task_id (str | None): run 收到的本次任务编号。
        output_path (Path): 本次完整输出路径，进程准备期间确定。
        exit_code (int | None): 实际退出码，无法确定时为空。
        ready (asyncio.Event): 已完成进程准备或进入清理，前台观察可以查询结果。
        changed (asyncio.Event): 输出写入和资源释放后提醒观察者。
        interaction (asyncio.Lock): 串行终端输入和读取游标更新。
        accepts_input (bool): 进程已准备且尚未开始资源清理。
        offset (int): 已读取完整输出的字节位置。
    """

    command: str
    argv: list[str]
    cwd: str
    tty: bool
    spool_dir: Path
    records: TaskRecordDao
    terminal: TerminalService
    process: TerminalOperations | None = None
    task_id: str | None = None
    output_path: Path = field(init=False)
    exit_code: int | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    interaction: asyncio.Lock = field(default_factory=asyncio.Lock)
    accepts_input: bool = False
    offset: int = 0

    async def run(self, runtime: ToolRuntime) -> None:
        """准备进程、保存终端信息并收集输出，收到取消时中断后回收进程树。

        Args:
            runtime (ToolRuntime): Supervisor 绑定当前任务编号的调用对象。
        """
        self.task_id = runtime.task_id
        try:
            # 1. 等待实际进程创建并接回资源；重复取消也要先完成准备，再交给 cleanup。
            await complete_owned_work(self._prepare_process(runtime))
        finally:
            self.ready.set()

        reader = asyncio.create_task(self._collect())
        waiter = asyncio.create_task(self.process.wait())
        try:
            try:
                await asyncio.shield(reader)
                self.exit_code = await asyncio.shield(waiter)
            except asyncio.CancelledError:
                # 2. 保留输出读取，先中断进程，原有宽限期后再结束进程树。
                await self.process.interrupt()
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), TERMINAL_STOP_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    self.process.kill()
                await reader
                self.exit_code = await waiter
                raise
            if self.exit_code != 0:
                raise RuntimeError(f"终端退出码：{self.exit_code}")
        finally:
            # 3. 输出或进程等待失败时一并回收内部协程，资源随后由 cleanup 释放。
            if not reader.done() or not waiter.done():
                self.process.kill()
            for pending in (reader, waiter):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(reader, waiter, return_exceptions=True)
            self.accepts_input = False
            if self.exit_code is None and waiter.done() and not waiter.cancelled() and waiter.exception() is None:
                self.exit_code = waiter.result()
            await self.records.save_terminal_exit_code(self.task_id, self.exit_code)

    async def _prepare_process(self, runtime: ToolRuntime) -> None:
        """创建进程并保存终端信息，使取消后的清理始终取得实际资源。

        Args:
            runtime (ToolRuntime): 发布已保存任务信息的调用对象。
        """
        # 1. Supervisor 已登记并持有本任务，准备本次完整输出文件。
        directory = self.spool_dir / self.task_id
        directory.mkdir(parents=True, exist_ok=True)
        self.output_path = directory / "output.log"
        self.output_path.touch()
        # 2. 系统创建完成后先持有进程；后续保存失败也能由 cleanup 回收。
        self.process = await self.terminal.spawn(self.argv, self.cwd, self.tty)
        record = await self.records.attach_terminal(self.task_id, command=self.command,
            pid=self.process.pid, output_path=str(self.output_path))
        # 3. 发布已经保存的信息，再允许前台发送输入。
        if record.background:
            await runtime.publish_task_record(self.task_id)
        self.accepts_input = True

    async def _collect(self) -> None:
        """增量解码并保存完整终端输出，每次写入后提醒观察者。"""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        with self.output_path.open("ab") as spool:
            while chunk := await self.process.read(TASK_OUTPUT_PAGE_MAX_BYTES):
                await asyncio.to_thread(spool.write, decoder.decode(chunk).encode("utf-8"))
                await asyncio.to_thread(spool.flush)
                self.changed.set()
            await asyncio.to_thread(spool.write, decoder.decode(b"", final=True).encode("utf-8"))
            await asyncio.to_thread(spool.flush)

    async def cleanup(self) -> None:
        """在共同预算内有限关闭实际终端，最终错误记入日志。"""
        self.accepts_input = False
        self.ready.set()
        try:
            if self.process is not None:
                await run_bounded(lambda: dispose_terminal(self.process),
                    retryable=(OSError, RuntimeError), timeout_seconds=TERMINAL_CLEANUP_TIMEOUT_SECONDS)
                self.process = None
        except Exception:
            logger.exception("终端资源关闭失败：%s", self.task_id)
        finally:
            self.changed.set()


    async def interact(self, runtime: ToolRuntime, *, chars: str = "",
                       yield_time_ms: int = 10000, initial: bool = False) -> TaskLaunchResult:
        """在本次终端的锁内发送输入并读取输出，观察取消不停止任务。

        Args:
            runtime (ToolRuntime): 发起本次观察的会话内工具运行时。
            chars (str): 原样发送的输入，空字符串只读取。
            yield_time_ms (int): 输出观察预算。
            initial (bool): 首次启动允许读取已经结束任务的最后输出。

        Returns:
            TaskLaunchResult: 当前增量及已保存状态，输入或读取失败时带说明。
        """
        await self.ready.wait()
        async with self.interaction:
            record = await runtime.get_task_snapshot(self.task_id)
            error = None
            if chars and record.status == TaskStatus.RUNNING and self.accepts_input:
                try:
                    await self.process.write(chars.encode("utf-8"))
                except (OSError, RuntimeError) as exc:
                    error = f"输入发送中断，可能已有部分输入发送，请勿重试：{exc}"
            elif chars:
                error = "终端已关闭，输入未发送"
            try:
                return await self._observe(runtime, yield_time_ms, initial=initial, error=error)
            except OSError as exc:
                record = await runtime.get_task_snapshot(self.task_id)
                detail = f"{error}；" if error else "输入已写入；" if chars else ""
                return TaskLaunchResult(self.task_id, record.status == TaskStatus.RUNNING, "", record.status,
                    record.exit_code, error=f"{detail}读取任务输出失败：{exc}")

    async def _observe(self, runtime: ToolRuntime, wait_ms: int, *,
                       initial: bool = False, error: str | None = None) -> TaskLaunchResult:
        """等待输出变化或统一终态提交，增量游标不阻塞输出收集。

        Args:
            runtime (ToolRuntime): 当前观察者的会话内查询及等待能力。
            wait_ms (int): 剩余观察预算。
            initial (bool): 首次启动可以返回同步完成的有限输出。
            error (str | None): 输入过程中已经发生的错误。

        Returns:
            TaskLaunchResult: 一次观察结果，终态以 Supervisor 的持久记录为准。
        """
        deadline = asyncio.get_running_loop().time() + wait_ms / 1000
        while True:
            # 1. 先清通知再查询，避免检查与等待之间遗漏输出变化。
            self.changed.clear()
            record = await runtime.get_task_snapshot(self.task_id)
            ended = record.status != TaskStatus.RUNNING
            if ended and not initial:
                return TaskLaunchResult(self.task_id, False, "", record.status, record.exit_code, error=error)
            page = await read_terminal_output(record, self.offset, TASK_OUTPUT_PAGE_MAX_BYTES - 3) if record.output_path else {"text": "", "next_offset": self.offset}
            remaining = deadline - asyncio.get_running_loop().time()
            if ended or remaining <= 0 or (not initial and page["text"]):
                record = await runtime.get_task_snapshot(self.task_id)
                ended = record.status != TaskStatus.RUNNING
                output = page["text"] if initial or not ended else ""
                if output:
                    self.offset = page["next_offset"]
                return TaskLaunchResult(self.task_id, not ended, output, record.status, record.exit_code,
                    error=error or record.error)
            # 2. 观察输出和等待终态共用剩余预算，取消观察只取消等待者。
            changed = asyncio.create_task(self.changed.wait())
            finished = asyncio.create_task(runtime.wait_task(self.task_id, timeout_ms=max(1, int(remaining * 1000))))
            try:
                done, _ = await asyncio.wait((changed, finished), timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if finished in done and finished.result().status == TaskStatus.RUNNING:
                    deadline = 0
            finally:
                for pending in (changed, finished):
                    if not pending.done():
                        pending.cancel()
                await asyncio.gather(changed, finished, return_exceptions=True)


async def read_terminal_output(record: TaskRecord, offset: int, limit: int) -> dict:
    """按字节游标读取输出，闭合 UTF-8 字符边界并保留未完成字节。

    Args:
        record (TaskRecord): 已经保存并确认归属的任务记录。
        offset (int): 完整输出文件的起始字节游标。
        limit (int): 读取的最大字节预算。

    Returns:
        dict: 解码正文、下一字节游标和当前是否还有内容。

    Raises:
        ValueError: 无效的输出分页参数。
        InvalidOperationError: 任务没有终端输出。
    """
    # 1. 校验任务和分页预算，以字节偏移读取追加式完整输出。
    if offset < 0 or limit < 1 or limit > TASK_OUTPUT_PAGE_MAX_BYTES:
        raise ValueError("无效的输出分页参数")
    if record.output_path is None:
        raise InvalidOperationError("此任务没有终端输出")
    with open(record.output_path, "rb") as spool:
        spool.seek(offset)
        data = spool.read(limit)
        size = os.path.getsize(record.output_path)
        # 2. 增量解码 UTF-8；运行中的尾部不完整字符保留到下一页。
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        final = record.status != TaskStatus.RUNNING and spool.tell() == size
        text = decoder.decode(data, final=final)
        # 一页至少闭合末尾字符，避免跨页将同一 UTF-8 字符解成两个替换符。
        # 2.1. 页边界落在字符中间时再读少量字节，将同一字符完整交付。
        while decoder.getstate()[0] and spool.tell() < size:
            data = spool.read(1)
            final = record.status != TaskStatus.RUNNING and spool.tell() == size
            text += decoder.decode(data, final=final)
        # 3. 游标扣除仍在解码器中的字节，调用方不能跳过未交付的尾部。
        next_offset = spool.tell() - len(decoder.getstate()[0])
    return {"text": text, "next_offset": next_offset,
            "has_more": next_offset < size}

async def read_terminal_tail(record: TaskRecord, limit: int = TASK_RECENT_OUTPUT_BYTES) -> str:
    """读取任务末尾有限输出，跳过从中间开始的 UTF-8 续字节。

    Args:
        record (TaskRecord): 已经保存并确认归属的任务记录。
        limit (int): 读取的最大字节预算。

    Returns:
        str: 有限尾部正文，文件不可用时为展示说明。
    """
    if record.output_path is None:
        return record.result or record.error or ""
    # 1. 只读取有限尾部，日志不可用时返回可展示说明。
    try:
        with open(record.output_path, "rb") as spool:
            size = os.path.getsize(record.output_path)
            spool.seek(max(0, size - limit))
            data = spool.read(limit)
    except OSError:
        return "输出文件不可用"
    # 2. 尾部起点可能落在字符中间，先跳过 UTF-8 续字节再增量解码。
    while data and 0x80 <= data[0] <= 0xBF:
        data = data[1:]
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    return decoder.decode(data, final=record.status != TaskStatus.RUNNING).strip()
