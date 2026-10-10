"""后台 PowerShell 任务、完整输出和一次性终态通知。"""

from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import signal
import subprocess
from typing import Awaitable, Callable
from uuid import uuid4

from app.schemas.runtime_types import (
    TaskStatus,
)
from app.crud.task_record_dao import TaskRecord, TaskRecordDao
from app.runtime import get_data_dir
from app.services.windows_job import WindowsJob

logger = logging.getLogger(__name__)

# 输出预算各有用途，不因为默认值相同就把前台预览和分页接口合为一个设置。
TASK_FOREGROUND_PREVIEW_BYTES = 16 * 1024
TASK_OUTPUT_PAGE_DEFAULT_BYTES = 16 * 1024
TASK_OUTPUT_PAGE_MAX_BYTES = 64 * 1024
TASK_TERMINAL_SUMMARY_BYTES = 4096
TASK_RECENT_OUTPUT_BYTES = 256
TASK_STOP_GRACE_SECONDS = 2
TASK_SHUTDOWN_WAIT_SECONDS = 5


@dataclass(frozen=True)
class TaskLaunchResult:
    """返回任务标识、是否仍在后台运行，以及前台完成时的有限输出。

    Attributes:
        task_id (str): 受管进程任务标识。
        background (bool): 此次调用是否停止等待并由 Supervisor 继续管理进程。
        output (str): 前台完成时的有限输出，后台返回时为空。
    """
    task_id: str
    background: bool
    output: str


class TaskSupervisor:
    """持有 run_ps 进程树和输出收集任务，统一管理前台与后台生命周期。

    Attributes:
        records (TaskRecordDao): 任务持久记录 DAO。
        spool_dir (Path): 任务完整输出目录。
        on_terminal (Callable[[str], Awaitable[None]] | None): 持久终态提交后唤醒所属会话的回调。
        on_status (Callable[[TaskRecord], Awaitable[None]] | None): 提交任务状态后发布界面状态的回调。
        _jobs (dict[str, WindowsJob]): 按任务标识保存 Windows Job 所有权。
        _processes (dict[str, asyncio.subprocess.Process]): 按任务标识保存同一子进程句柄。
        _force_stops (dict[str, asyncio.Task[None]]): 宽限期后的进程树强停任务。
        _watchers (dict[str, asyncio.Task[None]]): 完整输出收集与终态提交任务。
        _stopping (set[str]): 已经接受显式停止的任务标识。
    """
    def __init__(
        self,
        *,
        db_path: str | None = None,
        spool_dir: Path | None = None,
        on_terminal: Callable[[str], Awaitable[None]] | None = None,
        on_status: Callable[[TaskRecord], Awaitable[None]] | None = None,
    ) -> None:
        """装配任务持久记录、输出目录和状态回调，进程句柄只在本进程内保存。

        Args:
            db_path (str | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
            spool_dir (Path | None): 任务完整输出目录。
            on_terminal (Callable[[str], Awaitable[None]] | None): 持久终态提交后唤醒所属会话的回调。
            on_status (Callable[[TaskRecord], Awaitable[None]] | None): 提交任务状态后发布界面状态的回调。
        """
        self.records = TaskRecordDao(db_path)
        self.spool_dir = Path(spool_dir or get_data_dir() / "tasks")
        self.on_terminal = on_terminal
        self.on_status = on_status
        self._jobs: dict[str, WindowsJob] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._force_stops: dict[str, asyncio.Task[None]] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._stopping: set[str] = set()

    async def recover_orphaned(self) -> None:
        """将重启前仍 running 的任务标记为状态未知并创建一次终态通知。

        """
        for task_id in await self.records.running_ids():
            await self.finish_once(task_id, TaskStatus.ORPHANED, None)

    async def launch(
        self, session_id: str, command: str, argv: list[str], *,
        cwd: str, background: bool, timeout_sec: int,
        origin_run_id: str | None = None,
        origin_tool_call_id: str | None = None,
    ) -> TaskLaunchResult:
        """创建受管进程并持续收集完整输出，前台超时只停止等待而不另起进程。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            command (str): 需要执行或展示的 PowerShell 命令正文。
            argv (list[str]): 已经构造好的进程参数列表。
            cwd (str): 命令执行工作目录。
            background (bool): 是否立即后台运行。
            timeout_sec (int): 前台等待预算，超时后保留同一进程。
            origin_run_id (str | None): 发起进程任务的运行轮次标识。
            origin_tool_call_id (str | None): 发起任务的工具调用标识。

        Returns:
            TaskLaunchResult: 同一进程的任务标识、后台运行标记和前台有限输出。
        """
        # 1. 生成任务标识并建立追加式输出文件，前台与后台共用这一份输出。
        task_id = uuid4().hex
        directory = self.spool_dir / task_id
        directory.mkdir(parents=True, exist_ok=True)
        output_path = directory / "output.log"
        output_path.touch()
        # 2. 非交互启动进程并合并输出；Windows 先挂起，加入 Job 后才放行线程。
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004)  # 使用 CREATE_SUSPENDED 先挂起线程
            if os.name == "nt" else 0,
        )
        job = None
        try:
            job = WindowsJob(process.pid)
            if os.name == "nt":
                job.resume_suspended_process(process.pid)
        # 2.1. Job 创建或恢复线程失败时立即终止进程，不能留下一棵无人管理的进程树。
        except BaseException:
            if job is not None:
                job.terminate()
                job.close()
            else:
                process.kill()
            await process.wait()
            raise
        try:
            # 3. 保存任务记录，记录失败时清理进程；随后仅保留同一进程句柄。
            await self.records.insert_running(
                task_id, session_id, command, process.pid, str(output_path),
                origin_run_id=origin_run_id,
                origin_tool_call_id=origin_tool_call_id,
                background=background,
            )
        except BaseException:
            job.terminate()
            job.close()
            await process.wait()
            raise
        self._jobs[task_id] = job
        self._processes[task_id] = process
        # 4. 独立 watcher 持续收集输出，即使前台等待超时仍继续运行。
        watcher = asyncio.create_task(self._watch(task_id, process, output_path))
        self._watchers[task_id] = watcher
        if self.on_status is not None:
            record = await self.get(task_id)
            if record is not None and record.background:
                try:
                    await self.on_status(record)
                except Exception:
                    logger.exception("发布后台任务启动状态失败: %s", task_id)
        # 5. 显式后台立即返回；前台 shield watcher，超时只停止等待而不取消输出收集。
        if background:
            return TaskLaunchResult(task_id, True, "")
        try:
            await asyncio.wait_for(asyncio.shield(watcher), timeout=timeout_sec)
        except asyncio.TimeoutError:
            # 5.1. 超时只移交同一任务，先持久模式再发布状态；前台进程不提前占后台徽标。
            await self.records.mark_background(task_id)
            if self.on_status is not None:
                record = await self.get(task_id)
                if record is not None:
                    try:
                        await self.on_status(record)
                    except Exception:
                        logger.exception("发布前台转后台状态失败: %s", task_id)
            return TaskLaunchResult(task_id, True, "")
        # 6. 前台完成只返回有限尾部预览，完整日志仍可按 task_id 分页查询。
        max_result_bytes = TASK_FOREGROUND_PREVIEW_BYTES
        size = output_path.stat().st_size
        with output_path.open("rb") as spool:
            spool.seek(max(0, size - max_result_bytes))
            output = spool.read().decode("utf-8", errors="replace").strip()
        if size > max_result_bytes:
            output = f"[输出过长，以下为末尾片段；完整输出见 task_id={task_id}]\n{output}"
        return TaskLaunchResult(task_id, False, output or "(无输出)")

    async def _watch(
        self, task_id: str, process: asyncio.subprocess.Process, output_path: Path
    ) -> None:
        """追加完整输出并提交首次终态，输出收集失败时结束进程并通知失败。

        Args:
            task_id (str): 受管进程任务标识。
            process (asyncio.subprocess.Process): 当前受管子进程句柄。
            output_path (Path): 追加式完整输出文件路径。
        """
        try:
            # 1. 持续以追加方式保存全部输出，磁盘写入在线程中完成，避免阻塞模型循环。
            with output_path.open("ab") as spool:
                assert process.stdout is not None
                while chunk := await process.stdout.read(64 * 1024):
                    await asyncio.to_thread(spool.write, chunk)
                    await asyncio.to_thread(spool.flush)
            # 2. 输出闭合后等待进程退出，显式停止优先记录 stopped。
            exit_code = await process.wait()
            status: TaskStatus = (
                TaskStatus.STOPPED if task_id in self._stopping else
                TaskStatus.COMPLETED if exit_code == 0 else TaskStatus.FAILED
            )
            await self.finish_once(task_id, status, exit_code)
        # 3. 输出收集失败时终止进程树并提交 failed，不能让任务继续无人读取输出。
        except Exception:
            logger.exception("后台任务输出收集失败: %s", task_id)
            job = self._jobs.get(task_id)
            if job is not None and os.name == "nt":
                job.terminate()
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            await self.finish_once(task_id, TaskStatus.FAILED, process.returncode)
        # 4. 释放 Job 和进程引用，并取消尚未触发的强停计时器。
        finally:
            job = self._jobs.pop(task_id, None)
            if job is not None:
                job.close()
            self._watchers.pop(task_id, None)
            self._processes.pop(task_id, None)
            force_stop = self._force_stops.pop(task_id, None)
            if force_stop is not None:
                force_stop.cancel()
            self._stopping.discard(task_id)

    async def finish_once(
        self, task_id: str, status: TaskStatus, exit_code: int | None
    ) -> bool:
        """保存有限输出摘要和唯一终态通知，提交之后才发布状态并唤醒会话。

        Args:
            task_id (str): 受管进程任务标识。
            status (TaskStatus): 任务终态或查询状态。
            exit_code (int | None): 进程退出码，状态未知时为空。

        Returns:
            bool: 首次终态持久提交成功时为真，回调失败不撤销终态。
        """
        # 1. 查询仍 running 的任务并读取有限尾部摘要，完整输出不进入 SQLite。
        record = await self.records.get(task_id)
        if record is None or record.status != TaskStatus.RUNNING:
            return False
        summary = ""
        try:
            with open(record.output_path, "rb") as spool:
                spool.seek(max(0, os.path.getsize(record.output_path) - TASK_TERMINAL_SUMMARY_BYTES))
                summary = spool.read().decode("utf-8", errors="replace")
        except OSError:
            pass
        # 2. DAO 原子提交终态和唯一 Inbox 通知，重复回调在此结束。
        if not await self.records.finish_once(task_id, status, exit_code, summary):
            return False
        # 3. 提交后才发布可见状态；展示失败不撤销已经保存的终态。
        if self.on_status is not None:
            record = await self.get(task_id)
            if record is not None and record.background:
                try:
                    await self.on_status(record)
                except Exception:
                    logger.exception("发布后台任务终态失败: %s", task_id)
        # 4. 唤醒会话消费持久通知，唤醒失败后启动恢复仍能找到 Inbox。
        if self.on_terminal is not None:
            try:
                await self.on_terminal(record.session_id)
            except Exception:
                logger.exception("唤醒后台任务会话失败: %s", record.session_id)
        return True

    async def get(self, task_id: str) -> TaskRecord | None:
        """查询任务持久记录，进程退出或后端重启后仍可读取。

        Args:
            task_id (str): 受管进程任务标识。

        Returns:
            TaskRecord | None: 任务持久记录，记录不存在时为空。
        """
        return await self.records.get(task_id)

    async def list(self, session_id: str) -> list[TaskRecord]:
        """查询指定会话已转入后台的任务，前台记录仍可按 task_id 查询输出。

        Args:
            session_id (str): 共享 Ayaya 会话标识。

        Returns:
            list[TaskRecord]: 该会话的运行和已结束后台任务记录。
        """
        return [record for record in await self.records.list(session_id) if record.background]

    async def read_output(self, task_id: str, offset: int, limit: int) -> dict:
        """按字节游标读取输出，闭合 UTF-8 字符边界并保留未完成字节。

        Args:
            task_id (str): 受管进程任务标识。
            offset (int): 完整输出文件的起始字节游标。
            limit (int): 读取的最大字节预算。

        Returns:
            dict: 解码正文、下一字节游标和当前是否还有内容。

        Raises:
            KeyError: 输入、持久状态或系统调用不满足当前执行约束。
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

        Raises:
            KeyError: 输入、持久状态或系统调用不满足当前执行约束。
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
        """先请求进程结束，宽限期后根据输出收集任务状态强停整个 Windows Job。

        Args:
            task_id (str): 受管进程任务标识。

        Returns:
            bool: 当前 Supervisor 持有任务且已接受停止请求时为真。
        """
        # 1. 只停止本进程仍持有的任务，重复停止请求幂等确认。
        job = self._jobs.get(task_id)
        process = self._processes.get(task_id)
        if job is None or process is None:
            return False
        if task_id in self._stopping:
            return True
        # 2. 先标记停止原因，再请求退出，watcher 据此保存 stopped 终态。
        self._stopping.add(task_id)
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass

        async def force_if_running() -> None:
            """宽限两秒后终止仍未结束的受管进程树，包括持有输出管道的子进程。

            """
            await asyncio.sleep(TASK_STOP_GRACE_SECONDS)
            watcher = self._watchers.get(task_id)
            if watcher is not None and not watcher.done():
                if os.name == "nt":
                    job.terminate()
                elif process.returncode is None:
                    process.kill()

        # 3. 两秒宽限后仍由 watcher 判断进程树是否结束，子进程持有管道时也要强停。
        self._force_stops[task_id] = asyncio.create_task(force_if_running())
        return True

    async def close(self) -> None:
        """停止受管任务并等待输出收尾，最后关闭持久连接。

        """
        # 1. 对仍持有的任务请求停止，等待输出收尾后再关闭任务数据库。
        for task_id in tuple(self._jobs):
            await self.stop(task_id)
        if self._watchers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tuple(self._watchers.values()), return_exceptions=True),
                    timeout=TASK_SHUTDOWN_WAIT_SECONDS,
                )
            except asyncio.TimeoutError:
                pass
        await self.records.close()
