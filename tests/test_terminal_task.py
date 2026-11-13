"""通过具体任务及真实工具验证进程执行和取消收尾。"""
from app.crud.sqlite_factory import SqliteFactory

from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator

import asyncio
import sys
import threading
import pytest

from tests.terminal_harness import TerminalHarness
from tests.runtime_support import configured_settings
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.schemas.runtime_types import TaskStatus


def test_tool_runtime_submits_and_observes_terminal_task(tmp_path):
    """具体任务负责进程和输出，通用 Supervisor 保存并管理同一次执行。"""
    async def scenario():
        """通过两个公共入口观察同一个进程任务及其持久终态。"""
        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        terminal = TerminalHarness(supervisor, spool_dir=tmp_path / "logs")
        try:
            result = await terminal.launch_terminal("s", "print('Hello, World!')",
                [sys.executable, "-c", "print('Hello, World!')"], cwd=str(tmp_path), yield_time_ms=2000)
            record = await supervisor.wait(result.task_id, timeout_ms=2000)
            assert record.status == TaskStatus.COMPLETED
            assert record.exit_code == 0 and record.pid is not None
            assert record.command == "print('Hello, World!')"
            assert record.tool_name == "exec_command"
            assert "Hello, World!" in result.output
            page = await terminal.read_output(result.task_id, 0, 1024)
            assert "Hello, World!" in page["text"]
            assert supervisor.active_task_count() == 0
            assert await supervisor.list_tasks("s") == []
        finally:
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_repeated_stop_keeps_process_creation_owned_until_cleanup(tmp_path, monkeypatch):
    """进程创建在线程中继续时，多次取消仍接回进程并交给具体任务清理。"""
    from app.agent.tools.exec_command import ExecCommandTool
    from app.agent.tools.core.base import ToolContext
    from app.agent.tools.core.runtime import ToolRuntime
    from app.agent.state.model import AgentState

    monkeypatch.setattr("app.agent.tools.exec_command.WORKDIR", tmp_path)

    async def scenario():
        """替代操作系统创建入口，以线程门闩观察真实任务生命周期。"""
        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        runtime = ToolRuntime("s", "run", "exec_command", "call", _supervisor=supervisor, _runtime_queue=queue)
        context = ToolContext("s", AgentState(session_id="s"), runtime=runtime)
        started, release = threading.Event(), threading.Event()

        class Process:
            """代表尚未完成创建的系统进程资源。"""
            pid = 123
            closed = False

            def kill(self):
                """模拟清理时结束进程。"""

            async def wait(self):
                """报告模拟进程已经退出。"""
                return 0

            async def read(self, limit):
                """模拟进程输出已排空。

                Args:
                    limit (int): 读取字节预算。

                Returns:
                    bytes: 空字节表示输出结束。
                """
                return b""

            def close(self):
                """记录系统资源实际释放。"""
                self.closed = True

        process = Process()

        async def spawn(self, argv, cwd, tty):
            """让系统创建在线程中延迟完成。

            Args:
                argv (list[str]): 实际启动参数。
                cwd (str): 工作目录。
                tty (bool): 终端模式。

            Returns:
                Process: 线程实际创建出来的进程资源。
            """
            def create():
                """即使外层等待取消，系统创建仍继续执行。"""
                started.set()
                release.wait(3)
                return process
            return await asyncio.to_thread(create)

        monkeypatch.setattr("app.agent.tools.terminal.service.TerminalService.spawn", spawn)
        launching = asyncio.create_task(ExecCommandTool().execute({"cmd": "echo preparing", "tty": True, "yield_time_ms": 0}, context))
        closing = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            launching.cancel()
            with pytest.raises(asyncio.CancelledError):
                await launching
            record = (await supervisor.list_tasks("s"))[0]
            await supervisor.stop_task(record.task_id)
            closing = asyncio.create_task(supervisor.close())
            await asyncio.sleep(0.05)
            assert supervisor.active_task_count() == 1
            assert not closing.done()
            assert (await supervisor.get_task_snapshot(record.task_id)).status == TaskStatus.RUNNING
        finally:
            release.set()
            await asyncio.gather(launching, return_exceptions=True)
            if closing is not None:
                await closing
                await supervisor.records.close()
            else:
                await supervisor.close()
                await supervisor.records.close()
        assert process.closed
        assert supervisor.active_task_count() == 0
    asyncio.run(scenario())
