"""受管终端任务、增量交互、完整日志与一次性终态通知。"""

from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
from uuid import uuid4

from app.schemas.runtime_types import TaskStatus
from app.agent.tools.terminal.models import TaskLaunchResult
from app.crud.task_record_dao import TaskRecord, TaskRecordDao
from app.runtime import get_data_dir
from app.agent.tools.terminal.process import TerminalProcess, spawn_terminal, dispose_terminal

logger = logging.getLogger(__name__)
TASK_FOREGROUND_PREVIEW_BYTES = 64 * 1024
TASK_OUTPUT_PAGE_DEFAULT_BYTES = 16 * 1024
TASK_OUTPUT_PAGE_MAX_BYTES = 64 * 1024
TASK_RECENT_OUTPUT_BYTES = 256
TASK_STOP_GRACE_SECONDS = 2
TASK_SHUTDOWN_WAIT_SECONDS = 5


@dataclass
class RunningTerminal:
    """保存进程运行期间的交互状态，退出后整项释放。

    Attributes:
        process (TerminalProcess): 操作系统对应的进程 Adapter。
        changed (asyncio.Event): 输出写盘或终态提交后唤醒等待中的调用。
        interaction (asyncio.Lock): 串行输入和游标更新，不阻塞输出收集任务。
        offset (int): 已提取输出的日志字节位置，不表示模型交付位置。
    """

    process: TerminalProcess
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    interaction: asyncio.Lock = field(default_factory=asyncio.Lock)
    offset: int = 0


class TaskSupervisor:
    """统一拥有终端进程、日志与数据库终态，工具调用只决定本次等待。

    Attributes:
        records (TaskRecordDao): 任务事实及终态通知的事务入口。
        spool_dir (Path): 完整输出目录。
        _events (asyncio.Queue[TaskRecord | None]): 已保存的任务记录，由 Scheduler 独占读取。
        _events_closed (bool): 进程收尾后是否已结束记录来源。
    """

    def __init__(self, *, db_path: str | None = None, spool_dir: Path | None = None) -> None:
        """准备持久化和记录队列，应用级共享，不随单轮 Agent 关闭。

        Args:
            db_path (str | None): 当前环境的 SQLite 路径。
            spool_dir (Path | None): 当前环境的任务输出目录。
        """
        self.records = TaskRecordDao(db_path)
        self.spool_dir = Path(spool_dir or get_data_dir() / "tasks")
        self._events: asyncio.Queue[TaskRecord | None] = asyncio.Queue()
        self._events_closed = False
        self._live: dict[str, RunningTerminal] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._force_stops: dict[str, asyncio.Task[None]] = {}
        self._stopping: set[str] = set()
        self._closing = False

    async def next_event(self) -> TaskRecord | None:
        """供 Scheduler 唯一消费任务等待已保存记录，不读取终端日志。

        Returns:
            TaskRecord | None: 下一条记录；关闭后先读完积压，再持续返回空值。
        """
        if self._events_closed and self._events.empty():
            return None
        return await self._events.get()

    async def recover_orphaned(self) -> None:
        """重启后将失去进程所有权的任务标记 orphaned，并创建唯一通知。"""
        for task_id in await self.records.running_ids():
            await self.finish_once(task_id, TaskStatus.ORPHANED, None)

    async def launch(self, session_id: str, command: str, argv: list[str], *, cwd: str,
                     tty: bool = False, yield_time_ms: int = 10000,
                     tool_name: str = "exec_command",
                     origin_run_id: str | None = None, origin_tool_call_id: str | None = None) -> TaskLaunchResult:
        """启动一次受管进程，本次等待预算耗尽后由同一后台任务继续管理。

        Args:
            session_id (str): 任务所属 Agent 聊天编号。
            command (str): 供界面展示的原始命令。
            argv (list[str]): 已按实际 Shell 构造的启动参数。
            cwd (str): 已检查的工作目录。
            tty (bool): 是否使用真实终端。
            yield_time_ms (int): 本次等待预算，0 表示立即返回。
            tool_name (str): Pipeline 提供的创建工具名称。
            origin_run_id (str | None): 创建任务的运行编号。
            origin_tool_call_id (str | None): 创建任务的调用编号。

        Returns:
            TaskLaunchResult: 当前有限输出及已提交状态。
        """
        if not tool_name.strip():
            raise ValueError("新任务必须提供工具名")
        if self._closing:
            raise RuntimeError("任务运行器正在关闭")
        # 1. 日志和进程均创建成功，且运行记录已提交，才将任务身份交给工具。
        task_id = uuid4().hex
        directory = self.spool_dir / task_id
        directory.mkdir(parents=True, exist_ok=True)
        output_path = directory / "output.log"
        output_path.touch()
        process = await spawn_terminal(argv, cwd, tty)
        try:
            await self.records.insert_running(
                task_id, session_id, command, process.pid, str(output_path),
                origin_run_id=origin_run_id, origin_tool_call_id=origin_tool_call_id,
                background=yield_time_ms == 0, tool_name=tool_name,
            )
        except BaseException:
            await dispose_terminal(process)
            raise
        live = RunningTerminal(process)
        self._live[task_id] = live
        self._watchers[task_id] = asyncio.create_task(self._watch(task_id, live, output_path))
        try:
            await self._enqueue_background_record(task_id)
            # 2. 初次启动允许返回同步完成的正文；等待不取消独立输出收集任务。
            async with live.interaction:
                result = await self._observe(session_id, task_id, live, yield_time_ms, initial=True)
            if result.background:
                await self.records.mark_background(task_id)
                await self._enqueue_background_record(task_id)
            return result
        except asyncio.CancelledError:
            # 3. 单轮停止只移交同一进程，不能取消 watcher 或重新执行原命令。
            await self.records.mark_background(task_id)
            await self._enqueue_background_record(task_id)
            raise
        except OSError as exc:
            # 4. 首次读取失败时任务仍由 watcher 管理，保留编号并报告错误，不能谎称进程未启动。
            await self.records.mark_background(task_id)
            await self._enqueue_background_record(task_id)
            record = await self._owned(session_id, task_id)
            return TaskLaunchResult(task_id, record.status == TaskStatus.RUNNING, "", record.status,
                                    record.exit_code, error=f"读取任务输出失败，请勿重复执行命令：{exc}")

    async def _owned(self, session_id: str, task_id: str) -> TaskRecord:
        """先检查持久任务归属，不泄露其他聊天的进程或文件。

        Args:
            session_id (str): 当前调用所属聊天。
            task_id (str): 请求访问的任务。

        Returns:
            TaskRecord: 已确认归属的已提交记录。
        """
        record = await self.get(task_id)
        if record is None or record.session_id != session_id:
            raise KeyError("任务不存在或不属于当前聊天")
        return record

    async def interact(self, session_id: str, task_id: str, *, chars: str = "",
                       yield_time_ms: int = 10000) -> TaskLaunchResult:
        """串行写入和增量读取；返回终态时不附加剩余正文。

        Args:
            session_id (str): 当前 Agent 聊天编号。
            task_id (str): exec_command 创建的稳定任务编号。
            chars (str): 原始输入，空字符串只读取。
            yield_time_ms (int): 等待新输出或终态的预算。

        Returns:
            TaskLaunchResult: 增量、结束提示或明确的输入发送结果。
        """
        record = await self._owned(session_id, task_id)
        live = self._live.get(task_id)
        if record.status != TaskStatus.RUNNING or live is None:
            missing = record.status == TaskStatus.RUNNING and live is None
            return TaskLaunchResult(task_id, record.status == TaskStatus.RUNNING, "", record.status,
                                    record.exit_code,
                                    error="运行器未持有此进程，不能继续交互；请等待状态恢复" if missing else
                                    "任务已结束，输入未发送" if chars else None)
        async with live.interaction:
            record = await self._owned(session_id, task_id)
            error = None
            # 1. 终态提交之后不再写入；写入途中断开时不能承诺输入一个字节也没发送。
            if chars and record.status == TaskStatus.RUNNING:
                try:
                    await live.process.write(chars.encode("utf-8"))
                except (OSError, RuntimeError) as exc:
                    error = f"输入发送中断，可能已有部分输入发送，请勿重试：{exc}"
            elif chars:
                error = "任务已结束，输入未发送"
            try:
                return await self._observe(session_id, task_id, live, yield_time_ms, error=error)
            except OSError as exc:
                # 2. 保留已有输入错误；写入成功但读取失败时直接说明输入已写入，不能建议重发。
                record = await self._owned(session_id, task_id)
                input_detail = f"{error}；" if error else "输入已写入；" if chars else ""
                return TaskLaunchResult(task_id, record.status == TaskStatus.RUNNING, "", record.status,
                                        record.exit_code,
                                        error=f"{input_detail}读取任务输出失败：{exc}")

    async def _observe(self, session_id: str, task_id: str, live: RunningTerminal, wait_ms: int,
                       *, initial: bool = False, error: str | None = None) -> TaskLaunchResult:
        """等待日志变化并提取一页，不让等待阻塞日志写入和终态提交。

        Args:
            session_id (str): 当前聊天编号。
            task_id (str): 受管任务编号。
            live (RunningTerminal): 运行期间的游标和通知事件。
            wait_ms (int): 本次剩余等待预算。
            initial (bool): 初次启动可返回同步完成的有限正文。
            error (str | None): 输入阶段已经产生的错误。

        Returns:
            TaskLaunchResult: 一次观察，不表示输出已经交付模型。
        """
        deadline = asyncio.get_running_loop().time() + wait_ms / 1000
        while True:
            # 1. 先清事件再查状态和文件，防止检查与等待之间丢失输出通知。
            live.changed.clear()
            record = await self._owned(session_id, task_id)
            ended = record.status != TaskStatus.RUNNING
            if ended and not initial:
                return TaskLaunchResult(task_id, False, "", record.status, record.exit_code,
                                        error=error)
            # 1.1. 给页末 UTF-8 字符补齐预留三字节，最终提取量仍不超过 64KiB。
            page = await self.read_output(task_id, live.offset, TASK_OUTPUT_PAGE_MAX_BYTES - 3)
            # 2. 初次 exec 等待完成或预算耗尽；后续读取已有增量时立即返回。
            remaining = deadline - asyncio.get_running_loop().time()
            if ended or remaining <= 0 or (not initial and page["text"]):
                # 2.1. 文件读取期间可能刚提交终态，再确认后才能决定是否返回正文。
                record = await self._owned(session_id, task_id)
                ended = record.status != TaskStatus.RUNNING
                output = page["text"] if initial or not ended else ""
                if output:
                    live.offset = page["next_offset"]
                return TaskLaunchResult(task_id, not ended, output, record.status, record.exit_code,
                                        error=error)
            try:
                await asyncio.wait_for(live.changed.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    async def _watch(self, task_id: str, live: RunningTerminal, output_path: Path) -> None:
        """独立保存完整输出，日志收尾后重试提交终态，最后释放运行资源。

        Args:
            task_id (str): 已保存的任务编号。
            live (RunningTerminal): 当前进程和输出通知。
            output_path (Path): 唯一完整日志路径。
        """
        waiter = asyncio.create_task(live.process.wait())
        failed = False
        exit_code = None
        try:
            try:
                # 1. 增量解码保留半个 UTF-8 字符，日志始终使用 UTF-8，包含终端控制序列。
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                with output_path.open("ab") as spool:
                    while chunk := await live.process.read(TASK_OUTPUT_PAGE_MAX_BYTES):
                        data = decoder.decode(chunk).encode("utf-8")
                        await asyncio.to_thread(spool.write, data)
                        await asyncio.to_thread(spool.flush)
                        live.changed.set()
                    tail = decoder.decode(b"", final=True).encode("utf-8")
                    await asyncio.to_thread(spool.write, tail)
                    await asyncio.to_thread(spool.flush)
            except Exception:
                # 2. 保存失败即终止进程；继续排空终端，让关闭 ConPTY 的等待能够完成。
                failed = True
                logger.exception("终端日志收集失败: %s", task_id)
                live.process.kill()
                try:
                    while await live.process.read(TASK_OUTPUT_PAGE_MAX_BYTES):
                        pass
                except OSError:
                    pass
            try:
                exit_code = await waiter
            except OSError:
                logger.exception("读取终端退出码失败: %s", task_id)
                failed = True
            status = (TaskStatus.FAILED if failed else TaskStatus.STOPPED if task_id in self._stopping
                      else TaskStatus.COMPLETED if exit_code == 0 else TaskStatus.FAILED)
            # 3. 通知和任务终态一起提交；短暂数据库故障不能让进程被错误报告为完成。
            while True:
                try:
                    await self.finish_once(task_id, status, exit_code)
                    live.changed.set()
                    break
                except Exception:
                    logger.exception("终端终态提交失败，将重试: %s", task_id)
                    await asyncio.sleep(0.2)
        finally:
            # 4. 终态提交后才释放运行游标；关闭时取消则留下 running 供下次启动恢复。
            live.process.kill()
            if not waiter.done():
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), 2)
                except (asyncio.TimeoutError, OSError):
                    waiter.cancel()
            live.process.close()
            self._live.pop(task_id, None)
            self._watchers.pop(task_id, None)
            force_stop = self._force_stops.pop(task_id, None)
            if force_stop is not None:
                force_stop.cancel()
            self._stopping.discard(task_id)
            live.changed.set()

    async def _enqueue_background_record(self, task_id: str) -> None:
        """后台状态已经提交后暂存记录，不等待会话或界面处理。

        Args:
            task_id (str): 需要重新读取已提交状态的任务编号。
        """
        record = await self.get(task_id)
        if record is not None and record.background:
            self._events.put_nowait(record)

    async def finish_once(self, task_id: str, status: TaskStatus, exit_code: int | None) -> bool:
        """原子保存终态与 Inbox 通知，成功后暂存记录供 Scheduler 检查。

        Args:
            task_id (str): 任务编号。
            status (TaskStatus): 进程终态。
            exit_code (int | None): 真实退出码，失去所有权时为空。

        Returns:
            bool: 是否首次完成提交。
        """
        # 1. 数据库保证首次终态与唯一通知一起提交，重复提交不再产生记录事件。
        if not await self.records.finish_once(task_id, status, exit_code):
            return False
        # 2. 先让终端调用结束等待，再暂存记录；进程清理不等待会话取得锁。
        live = self._live.get(task_id)
        if live is not None:
            live.changed.set()
        record = await self.get(task_id)
        if record is not None:
            self._events.put_nowait(record)
        return True

    async def get(self, task_id: str) -> TaskRecord | None:
        """读取已提交的持久状态，任务退出或重启后仍可查询。

        Args:
            task_id (str): 任务编号。

        Returns:
            TaskRecord | None: 已提交记录，不存在时为空。
        """
        return await self.records.get(task_id)

    async def list(self, session_id: str) -> list[TaskRecord]:
        """返回指定聊天已移交后台的任务。

        Args:
            session_id (str): Agent 聊天编号。

        Returns:
            list[TaskRecord]: 后台任务记录。
        """
        return [record for record in await self.records.list(session_id) if record.background]

    async def resolve_output(self, session_id: str, reference: str) -> Path:
        """解析任务日志引用，先查归属，再接触文件路径。

        Args:
            session_id (str): 当前聊天编号。
            reference (str): task-output URI。

        Returns:
            Path: 当前聊天拥有的日志路径。
        """
        prefix = "task-output://"
        if not reference.startswith(prefix):
            raise ValueError("无效任务日志引用")
        record = await self._owned(session_id, reference[len(prefix):])
        return Path(record.output_path)

    async def read_output(self, task_id: str, offset: int, limit: int) -> dict:
        """按字节游标读取输出，闭合 UTF-8 字符边界并保留未完成字节。

        Args:
            task_id (str): 受管进程任务标识。
            offset (int): 完整输出文件的起始字节游标。
            limit (int): 读取的最大字节预算。

        Returns:
            dict: 解码正文、下一字节游标和当前是否还有内容。

        Raises:

            ValueError: 无效的输出分页参数。
        """
        # 1. 校验任务和分页预算，以字节偏移读取追加式完整输出。
        record = await self.get(task_id)
        if record is None:
            raise KeyError(task_id)
        if offset < 0 or limit < 1 or limit > TASK_OUTPUT_PAGE_MAX_BYTES:
            raise ValueError("无效的输出分页参数")
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

    async def read_tail(self, task_id: str, limit: int = TASK_RECENT_OUTPUT_BYTES) -> str:
        """读取任务末尾有限输出，跳过从中间开始的 UTF-8 续字节。

        Args:
            task_id (str): 受管进程任务标识。
            limit (int): 读取的最大字节预算。

        Returns:
            str: 有限尾部正文，文件不可用时为展示说明。
        """
        record = await self.get(task_id)
        if record is None:
            raise KeyError(task_id)
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

    async def stop(self, task_id: str) -> bool:
        """先请求停止，再于宽限期后强停受管进程树。

        Args:
            task_id (str): 需要停止的受管任务编号。

        Returns:
            bool: 当前进程仍受本 Supervisor 管理时为真。
        """
        live = self._live.get(task_id)
        if live is None:
            return False
        if task_id in self._stopping:
            return True
        self._stopping.add(task_id)

        async def force_if_running() -> None:
            """宽限期后若终态尚未提交，强停同一任务的进程树。"""
            await asyncio.sleep(TASK_STOP_GRACE_SECONDS)
            if task_id in self._live:
                live.process.kill()

        self._force_stops[task_id] = asyncio.create_task(force_if_running())
        try:
            await live.process.interrupt()
        except (OSError, RuntimeError):
            # 1. 退出与停止同时发生时仍保留已受理标记，强停计时器和 watcher 负责收尾。
            pass
        return True

    async def close(self) -> None:
        """停止进程并等待输出收尾，关闭数据库后结束记录来源。"""
        self._closing = True
        # 1. 终态记录由 watcher 在收尾时入队，不能提前结束来源。
        for task_id in tuple(self._live):
            await self.stop(task_id)
        watchers = tuple(self._watchers.values())
        if watchers:
            _, pending = await asyncio.wait(watchers, timeout=TASK_SHUTDOWN_WAIT_SECONDS)
            for task in pending:
                task.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)
        try:
            await self.records.close()
        finally:
            # 2. 存储关闭失败也通知消费者结束；保留前面的积压记录，重复关闭不追加标记。
            if not self._events_closed:
                self._events_closed = True
                self._events.put_nowait(None)
