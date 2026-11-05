"""通过工具和终端服务验证真实进程、增量输出及持久通知。"""
from tests.terminal_harness import TerminalHarness
from tests.runtime_support import configured_settings

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from app.agent.state.manager import SessionStateManager
from app.agent.tools.core.base import ToolContext
from app.agent.tools.exec_command import ExecCommandArguments, ExecCommandTool
from app.agent.tools.write_stdin import WriteStdinArguments, WriteStdinTool
from app.agent.tools.read_file import read_file
from app.agent.tools.results.normalizer import ToolResultNormalizer
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.runtime_types import TaskStatus
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor


async def wait_ended(supervisor: TaskSupervisor, task_id: str):
    """等待真实进程终态提交，失败时给出可定位的超时。

    Args:
        supervisor (TaskSupervisor): 隔离运行器。
        task_id (str): 待结束的任务编号。

    Returns:
        TaskRecord: 已提交的终态记录。
    """
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        record = await supervisor.get_task_record(task_id)
        if record.status != TaskStatus.RUNNING:
            return record
        assert asyncio.get_running_loop().time() < deadline, "真实终端没有按时退出"
        await asyncio.sleep(0.01)


def context_for(terminal: TerminalHarness, session_id: str = "chat") -> ToolContext:
    """构造仅使用临时存储的工具上下文。

    Args:
        terminal (TerminalHarness): 当前测试的终端专用操作入口。
        session_id (str): 用于测试任务归属的聊天编号。

    Returns:
        ToolContext: 无模型和生产存储依赖的上下文。
    """
    return ToolContext(
        session_id=session_id,
        state=None,
        runtime=terminal.runtime(session_id),
        origin_tool_name="exec_command",
    )


@pytest.mark.parametrize("tty", [False, True])
def test_duplex_terminal_input_completion_and_full_log(tmp_path: Path, tty: bool) -> None:
    """管道和真实 PTY 都能交互，结束后只返回状态，通知引用可读完整日志。

    Args:
        tmp_path (Path): 临时任务和数据库目录。
        tty (bool): 是否启用当前操作系统真实 PTY。
    """
    async def scenario() -> None:
        """以等待输入的真实 Python 程序验证两种 Adapter。"""
        path = str(tmp_path / "db.sqlite3")
        supervisor = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=path)
        terminal_for_supervisor = TerminalHarness(supervisor, spool_dir=tmp_path / "logs")
        inbox = SessionStateManager("chat", path)
        try:
            command = "import sys; print('READY', flush=True); value=input(); print('RESULT:'+value, flush=True)"
            started = await terminal_for_supervisor.launch_terminal("chat", "interactive", [sys.executable, "-u", "-c", command],
                                              cwd=str(tmp_path), tty=tty, yield_time_ms=0, tool_name="exec_command")
            observed = started.output
            assert started.task_id in started.render() and "仍在运行" in started.render()
            while "READY" not in observed:
                chunk = await terminal_for_supervisor.interact("chat", started.task_id, yield_time_ms=3000)
                assert chunk.background, (chunk, observed)
                observed += chunk.output
            sent = await terminal_for_supervisor.interact("chat", started.task_id, chars="hello\r" if tty and os.name == "nt" else "hello\n", yield_time_ms=3000)
            assert sent.error is None
            ended = await wait_ended(supervisor, started.task_id)
            assert ended.status == TaskStatus.COMPLETED and ended.exit_code == 0
            assert ended.tool_name == "exec_command"
            polled = await terminal_for_supervisor.interact("chat", started.task_id, yield_time_ms=0)
            assert not polled.background and polled.output == ""
            rejected = await terminal_for_supervisor.interact("chat", started.task_id, chars="again\n", yield_time_ms=0)
            assert "输入未发送" in rejected.error
            finished = await WriteStdinTool().execute({"task_id": started.task_id}, context_for(terminal_for_supervisor))
            assert "已完成" in finished.text and "退出码 0" in finished.text
            assert "RESULT:hello" not in finished.text and started.task_id not in finished.text
            notices = await inbox.read_task_terminals()
            assert len(notices) == 1
            payload = notices[0].payload
            assert payload["result"] == "" and payload["tool_name"] == "exec_command"
            assert payload["exit_code"] == 0 and payload["output_ref"] == f"task-output://{started.task_id}"
            page = await read_file().execute({"path": payload["output_ref"]}, context_for(terminal_for_supervisor))
            assert "RESULT:hello" in page.text and "READY" in page.text
            assert "task-output://" not in started.render() and "task-output://" not in polled.render()
            with pytest.raises(KeyError):
                await terminal_for_supervisor.interact("other", started.task_id, chars="secret", yield_time_ms=0)
            denied = await read_file().execute({"path": payload["output_ref"]}, context_for(terminal_for_supervisor, "other"))
            assert "任务不存在或不属于当前聊天" in denied.text
        finally:
            await inbox.close()
            await supervisor.close()

    asyncio.run(scenario())


def test_pipe_reads_only_increment_and_does_not_add_newline(tmp_path: Path) -> None:
    """使用输入作为输出屏障，验证增量不重复且输入不会隐式补换行。

    Args:
        tmp_path (Path): 临时运行目录。
    """
    async def scenario() -> None:
        """让真实子进程每读取一个字节才输出下一段。"""
        supervisor = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "db"))
        terminal_for_supervisor = TerminalHarness(supervisor, spool_dir=tmp_path / "logs")
        try:
            command = (
                "import sys; sys.stdout.write('Hel'); sys.stdout.flush(); sys.stdin.read(1); "
                "sys.stdout.write('lo,Wo'); sys.stdout.flush(); sys.stdin.read(1); "
                "sys.stdout.write('rld!'); sys.stdout.flush(); sys.stdin.read(1)"
            )
            started = await terminal_for_supervisor.launch_terminal("chat", "chunks", [sys.executable, "-u", "-c", command],
                                              cwd=str(tmp_path), yield_time_ms=0)
            first = started.output or (await terminal_for_supervisor.interact("chat", started.task_id, yield_time_ms=3000)).output
            assert first == "Hel"
            assert (await terminal_for_supervisor.interact("chat", started.task_id, yield_time_ms=0)).output == ""
            context = context_for(terminal_for_supervisor)
            second = await WriteStdinTool().execute(
                {"task_id": started.task_id, "chars": "a", "yield_time_ms": 3000}, context,
            )
            assert "仍在运行" in second.text and started.task_id not in second.text
            third = await WriteStdinTool().execute(
                {"task_id": started.task_id, "chars": "b", "yield_time_ms": 3000}, context,
            )
            assert "仍在运行" in third.text and started.task_id not in third.text
            assert first + second.text.split("\n\n", 1)[1] + third.text.split("\n\n", 1)[1] == "Hello,World!"
            assert (await supervisor.get_task_record(started.task_id)).status == TaskStatus.RUNNING
            await terminal_for_supervisor.interact("chat", started.task_id, chars="c", yield_time_ms=3000)
            await wait_ended(supervisor, started.task_id)
        finally:
            await supervisor.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_stage", ["write", "read"])
def test_stdin_errors_explain_input_in_plain_text(tmp_path: Path, monkeypatch, failure_stage: str) -> None:
    """输入部分发送或发送后读取失败时，用错误正文说明实际情况，不暴露状态枚举。

    Args:
        tmp_path (Path): 临时进程工作目录和日志目录。
        monkeypatch (pytest.MonkeyPatch): 仅在本次交互期间注入 I/O 故障。
        failure_stage (str): 在写入途中或后续读取日志时制造故障。
    """
    from app.agent.tools.terminal.process import PipeProcess

    async def scenario() -> None:
        """使用真实管道进程确认输入已产生作用，并检查模型收到的错误说明。"""
        supervisor = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "db"))
        terminal_for_supervisor = TerminalHarness(supervisor, spool_dir=tmp_path / "logs")
        original_write = PipeProcess.write

        async def partial_write(process: PipeProcess, data: bytes) -> None:
            """向真实进程发送首字节后报错，模拟发送途中断开。

            Args:
                process (PipeProcess): 当前真实管道进程。
                data (bytes): 本次工具准备发送的完整输入。
            """
            await original_write(process, data[:1])
            raise BrokenPipeError("injected write failure")

        async def reject_read(task_id: str, offset: int, limit: int) -> dict:
            """只拒绝交互阶段读取日志，不影响后台输出收集。

            Args:
                task_id (str): 当前任务编号。
                offset (int): 本次输出读取起点。
                limit (int): 本次读取字节上限。

            Raises:
                OSError: 模拟任务日志暂时不可读取。
            """
            raise OSError("injected read failure")

        try:
            command = (
                "import sys; print('READY',flush=True); value=sys.stdin.read(1); "
                "print('GOT:'+value,flush=True); sys.stdin.read(1)"
            )
            started = await terminal_for_supervisor.launch_terminal(
                "chat", "input error", [sys.executable, "-u", "-c", command],
                cwd=str(tmp_path), yield_time_ms=0,
            )
            observed = started.output
            while "READY" not in observed:
                observed += (await terminal_for_supervisor.interact("chat", started.task_id, yield_time_ms=3000)).output
            with monkeypatch.context() as patcher:
                if failure_stage == "write":
                    patcher.setattr(PipeProcess, "write", partial_write)
                else:
                    patcher.setattr("app.agent.tools.terminal.task.read_terminal_output", reject_read)
                result = await WriteStdinTool().execute(
                    {"task_id": started.task_id, "chars": "a", "yield_time_ms": 3000}, context_for(terminal_for_supervisor),
                )
            expected = "可能已有部分输入发送" if failure_stage == "write" else "输入已写入"
            assert expected in result.text and f"injected {failure_stage} failure" in result.text
            assert started.task_id not in result.text
            observed = result.text
            while "GOT:a" not in observed:
                observed += (await terminal_for_supervisor.interact("chat", started.task_id, yield_time_ms=3000)).output
        finally:
            await supervisor.close()

    asyncio.run(scenario())


def test_tool_defaults_dynamic_shell_and_short_and_long_results(tmp_path: Path, monkeypatch) -> None:
    """默认等待均为十秒，短输出无引用，长输出交由统一处理器外置。

    Args:
        tmp_path (Path): 隔离工具工作目录。
        monkeypatch (pytest.MonkeyPatch): 将工具工作区限制在临时目录。
    """
    import app.agent.tools.exec_command as module
    from app.agent.tools.terminal.shell import detect_shell

    monkeypatch.setattr(module, "WORKDIR", tmp_path)
    assert ExecCommandArguments(cmd="echo ok").yield_time_ms == 10000
    assert WriteStdinArguments(task_id="task").yield_time_ms == 10000
    assert "session" not in json.dumps(WriteStdinTool.parameters_schema)
    definition = ExecCommandTool().definition()
    assert detect_shell().executable in definition.description

    async def scenario() -> None:
        """通过工具执行真实短命令，再经统一处理器验证长结果。"""
        supervisor = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "db"))
        terminal_for_supervisor = TerminalHarness(supervisor, spool_dir=tmp_path / "logs")
        try:
            context = context_for(terminal_for_supervisor)
            result = await ExecCommandTool().execute({"cmd": "echo ok"}, context)
            assert "ok" in result.text and "task-output://" not in result.text
            assert "tool-result://" not in ToolResultNormalizer().normalize(result, session_id="chat").text
            invalid = await WriteStdinTool().execute({"task_id": "unknown", "yield_time_ms": -1}, context)
            assert invalid.text.startswith("错误：")
            large = "x" * 60000
            from app.agent.tools.core.base import ToolResult

            normalized = ToolResultNormalizer().normalize(ToolResult.success(large), session_id="chat")
            assert "tool-result://" in normalized.text
        finally:
            await supervisor.close()

    asyncio.run(scenario())


def test_task_log_pages_are_bounded_and_preserve_character_offsets(tmp_path: Path) -> None:
    """超长单行日志按字符推进，分页不生成新的工具结果文件。

    Args:
        tmp_path (Path): 隔离数据目录。
    """
    async def scenario() -> None:
        """在真实任务记录引用下读取包含中文和换行的长日志。"""
        supervisor = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "db"))
        terminal_for_supervisor = TerminalHarness(supervisor, spool_dir=tmp_path / "logs")
        text = "中文\n" * 10000
        path = tmp_path / "large.log"
        path.write_text(text, encoding="utf-8", newline="")
        try:
            await supervisor.records.insert_running("large", "chat", "test", 1, str(path), tool_name="other_tool")
            context = context_for(terminal_for_supervisor)
            first = await read_file().execute({"path": "task-output://large"}, context)
            second = await read_file().execute({"path": "task-output://large", "start": 6000}, context)
            assert first.text.split("\n", 1)[1] == text[:6000]
            assert second.text.split("\n", 1)[1] == text[6000:12000]
            assert "next_start: 12000" in second.text
            denied = await read_file().execute({"path": "task-output://large"}, context_for(terminal_for_supervisor, "other"))
            assert "错误" in denied.text
        finally:
            await supervisor.close()

    asyncio.run(scenario())


def test_terminal_state_is_hidden_until_notice_transaction_commits(tmp_path: Path, monkeypatch) -> None:
    """终态已 UPDATE 但通知未写入时，同连接查询不能提前观察到完成。

    Args:
        tmp_path (Path): 隔离 SQLite 目录。
        monkeypatch (pytest.MonkeyPatch): 在通知写入前安排并发屏障。
    """
    import app.crud.task_record_dao as module

    async def scenario() -> None:
        """暂停真实事务，在提交前后分别验证查询的完成状态。"""
        records = TaskRecordDao(str(tmp_path / "db"))
        entered, release = asyncio.Event(), asyncio.Event()
        original = module.append_task_terminal_in_transaction

        async def delayed_notice(*args, **kwargs) -> None:
            """DAO 已更新终态后等待测试放行，再写入同一事务的通知。

            Args:
                args (tuple): 原通知写入函数的位置参数。
                kwargs (dict): 原通知写入函数的关键字参数。
            """
            entered.set()
            await release.wait()
            await original(*args, **kwargs)

        monkeypatch.setattr(module, "append_task_terminal_in_transaction", delayed_notice)
        try:
            await records.insert_running("t", "chat", "test", 1, str(tmp_path / "log"), tool_name="other_tool")
            finishing = asyncio.create_task(records.finish_once("t", TaskStatus.COMPLETED, 0))
            await entered.wait()
            query = asyncio.create_task(records.get("t"))
            await asyncio.sleep(0)
            assert not query.done()
            release.set()
            assert await finishing
            assert (await query).status == TaskStatus.COMPLETED
        finally:
            release.set()
            await records.close()

    asyncio.run(scenario())


def test_many_completion_notices_keep_all_tool_names_and_references(tmp_path: Path) -> None:
    """批量通知超过旧总预算后仍保留每个任务的工具名和日志引用。

    Args:
        tmp_path (Path): 隔离任务和 Inbox 数据库。
    """
    from app.agent.state.manager import task_result_messages

    async def scenario() -> None:
        """在真实 Inbox 中领取多种工具的完成通知，再走模型消息转换。"""
        path = str(tmp_path / "db")
        records, inbox = TaskRecordDao(path), SessionStateManager("chat", path)
        try:
            for index in range(90):
                name = "exec_command" if index % 2 == 0 else "other_tool"
                await records.insert_running(f"task-{index}", "chat", "command", 1, "unused", tool_name=name)
                await records.finish_once(f"task-{index}", TaskStatus.COMPLETED, 0)
            claims = await inbox.read_task_terminals()
            messages = task_result_messages(claims)
            text = "\n".join(message.result for message in messages)
            assert len(messages) > 1
            assert "other_tool" in text and "exec_command" in text
            for index in range(90):
                assert f"task-output://task-{index}" in text
        finally:
            await records.close()
            await inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("legacy", ["allow", "ask", "deny"])
def test_terminal_permissions_ignore_removed_tool_settings(legacy: str) -> None:
    """当前终端工具按自身权限执行，已删除工具的设置不参与授权。

    Args:
        legacy (str): 配置中残留的已删除工具权限。
    """
    from app.agent.tools.authorization.manager import ToolAuthorization
    from app.agent.tools.authorization.models import ToolIdentity
    from app.services.chat_settings_service import ChatSettingsService
    from app.crud.chat_settings_dao import ChatSettingsDao
    from app.schemas.chat_settings import ChatSettings

    original = ChatSettings(session_id="chat", model_name="test", openai_api_key="test",
                            openai_base_url="http://localhost", temperature=0.7, system_prompt="test",
                            context_strategy={}, tool_permissions={"local:run_ps": legacy})
    authorization = ToolAuthorization(lambda _: original)
    for name in ("exec_command", "write_stdin"):
        assert authorization.get_policy("chat", ToolIdentity.local(name)) == "ask"
    service = ChatSettingsService(ChatSettingsDao())
    settings = service.normalize(original)
    assert settings.tool_permissions["local:exec_command"] == "ask"
    assert settings.tool_permissions["local:write_stdin"] == "ask"
    assert service.normalize(settings) == settings
    settings.tool_permissions["local:exec_command"] = "deny"
    authorization = ToolAuthorization(lambda _: settings)
    assert authorization.get_policy("chat", ToolIdentity.local("exec_command")) == "deny"


def test_old_tool_name_cannot_be_resolved_or_exposed() -> None:
    """工具管理器和公开目录只提供当前工具，不再解析旧调用名称。"""
    from app.agent.core.tool_manager import ToolManager
    from app.agent.tools.core.registry import ToolRegistry

    manager = ToolManager()
    manager.register(ExecCommandTool())
    assert manager.get("run_ps") is None
    assert "run_ps" not in ToolRegistry.list_tools()
    assert "run_ps" not in [definition.name for definition in manager.get_tools()]


@pytest.mark.parametrize("tty", [False, True])
def test_registration_failure_prevents_process_creation(tmp_path: Path, monkeypatch, tty: bool) -> None:
    """数据库登记失败时保留原始异常，不启动具体任务。

    Args:
        tmp_path (Path): 隔离工作目录。
        monkeypatch (pytest.MonkeyPatch): 注入任务登记失败。
        tty (bool): 普通管道或真实 PTY。
    """
    async def scenario() -> None:
        """从终端公共入口提交，验证登记失败不会留下任务。"""
        supervisor = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "db"))
        terminal_for_supervisor = TerminalHarness(supervisor, spool_dir=tmp_path / "logs")

        async def reject_record(*args, **kwargs) -> None:
            """模拟初始任务记录无法保存。

            Args:
                args (tuple): 被拒绝的记录位置参数。
                kwargs (dict): 被拒绝的记录关键字参数。
            """
            raise OSError("injected registration failure")

        monkeypatch.setattr(supervisor.records, "insert_running", reject_record)
        try:
            with pytest.raises(OSError, match="injected registration"):
                await asyncio.wait_for(terminal_for_supervisor.launch_terminal(
                    "chat", "output", [sys.executable, "-u", "-c", "while True: print('x'*10000)"],
                    cwd=str(tmp_path), tty=tty, yield_time_ms=0,
                ), 5)
            assert await supervisor.list_tasks("chat") == []
        finally:
            await supervisor.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("tty", [False, True])
def test_terminal_metadata_failure_reclaims_created_process(tmp_path: Path, monkeypatch, tty: bool) -> None:
    """实际进程创建后保存终端信息失败，仍结束进程并保存失败终态。

    Args:
        tmp_path (Path): 隔离工作目录。
        monkeypatch (pytest.MonkeyPatch): 注入终端信息保存故障。
        tty (bool): 普通管道或真实 PTY。
    """
    async def scenario() -> None:
        """使用实际进程及临时 SQLite，观察保存失败后的公开任务状态。"""
        supervisor = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "db"))
        terminal = TerminalHarness(supervisor, spool_dir=tmp_path / "logs")
        created_pids = []

        async def reject_metadata(task_id, **metadata):
            """在系统已返回实际 PID 后使数据库补充操作失败。

            Args:
                task_id (str): 已登记的通用任务编号。
                metadata (dict): 实际进程及其输出信息。

            Raises:
                OSError: 模拟数据库无法保存终端信息。
            """
            created_pids.append(metadata["pid"])
            raise OSError("injected terminal metadata failure")

        monkeypatch.setattr(supervisor.records, "attach_terminal", reject_metadata)
        try:
            result = await asyncio.wait_for(terminal.launch_terminal(
                "chat", "long process", [sys.executable, "-u", "-c", "import time; time.sleep(60)"],
                cwd=str(tmp_path), tty=tty, yield_time_ms=2000,
            ), 5)
            record = await supervisor.wait(result.task_id, timeout_ms=2000)
            assert record.status == TaskStatus.FAILED
            assert "injected terminal metadata failure" in record.error
            assert created_pids
            for pid in created_pids:
                if os.name == "nt":
                    import ctypes
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
                    kernel.OpenProcess.restype = ctypes.c_void_p
                    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                    kernel.WaitForSingleObject.restype = ctypes.c_uint32
                    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                    handle = kernel.OpenProcess(0x00100000, False, pid)
                    if handle:
                        try:
                            assert kernel.WaitForSingleObject(handle, 0) == 0
                        finally:
                            kernel.CloseHandle(handle)
                else:
                    with pytest.raises(ProcessLookupError):
                        os.kill(pid, 0)
            assert supervisor.active_task_count() == 0
        finally:
            await supervisor.close()

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="Windows 持续 Shell 专项验证")
def test_powershell_pty_keeps_variables_across_commands(tmp_path: Path) -> None:
    """同一真实 PowerShell PTY 中先后两次输入共享变量，直到显式退出。

    Args:
        tmp_path (Path): 隔离进程工作目录和日志。
    """
    from app.agent.tools.terminal.shell import detect_shell

    async def scenario() -> None:
        """用输出标志等待命令完成，不依赖 Shell 提示符文本。"""
        shell = detect_shell()
        if shell.kind not in {"pwsh", "powershell"}:
            pytest.skip("当前主机没有 PowerShell")
        supervisor = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "db"))
        terminal_for_supervisor = TerminalHarness(supervisor, spool_dir=tmp_path / "logs")

        async def until(task_id: str, marker: str, initial: str = "") -> str:
            """等到输出标志出现，若进程已提前结束则明确失败。

            Args:
                task_id (str): 同一持续 Shell 的编号。
                marker (str): 由 Shell 计算输出的标志。
                initial (str): 写入操作已经提取的输出。

            Returns:
                str: 观察到标志之前的全部本次输出。
            """
            output = initial
            deadline = asyncio.get_running_loop().time() + 8
            while marker not in output:
                assert asyncio.get_running_loop().time() < deadline, output
                chunk = await terminal_for_supervisor.interact("chat", task_id, yield_time_ms=1000)
                assert chunk.background, (chunk, output)
                output += chunk.output
            return output

        try:
            started = await terminal_for_supervisor.launch_terminal(
                "chat", "persistent PowerShell", [shell.executable, "-NoLogo", "-NoProfile", "-NoExit", "-Command", "Write-Output ('START'+123)"],
                cwd=str(tmp_path), tty=True, yield_time_ms=0,
            )
            await until(started.task_id, "START123", started.output)
            first = await terminal_for_supervisor.interact("chat", started.task_id, chars="$saved=17; Write-Output ('VALUE'+$saved)\r", yield_time_ms=1000)
            await until(started.task_id, "VALUE17", first.output)
            second = await terminal_for_supervisor.interact("chat", started.task_id, chars="Write-Output ('NEXT'+($saved+1))\r", yield_time_ms=1000)
            await until(started.task_id, "NEXT18", second.output)
            await terminal_for_supervisor.interact("chat", started.task_id, chars="exit\r", yield_time_ms=0)
            assert (await wait_ended(supervisor, started.task_id)).status == TaskStatus.COMPLETED
        finally:
            await supervisor.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("tty", [False, True])
def test_short_command_exit_does_not_preserve_terminal(tmp_path: Path, tty: bool) -> None:
    """初次同步完成能返回最后正文，tty 不会让已结束的命令保留为活终端。

    Args:
        tmp_path (Path): 隔离运行目录。
        tty (bool): 真实 PTY 或普通管道。
    """
    async def scenario() -> None:
        """等待真实短命令退出后，检查工具初次和后续查询的区别。"""
        supervisor = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "db"))
        terminal_for_supervisor = TerminalHarness(supervisor, spool_dir=tmp_path / "logs")
        try:
            result = await terminal_for_supervisor.launch_terminal(
                "chat", "short", [sys.executable, "-u", "-c", "print('FINAL_OUTPUT')"],
                cwd=str(tmp_path), tty=tty, yield_time_ms=3000,
            )
            assert not result.background and result.exit_code == 0
            assert "FINAL_OUTPUT" in result.output
            assert (await terminal_for_supervisor.interact("chat", result.task_id, yield_time_ms=0)).output == ""
        finally:
            await supervisor.close()

    asyncio.run(scenario())


def test_task_list_exposes_persisted_creator_names_and_legacy_unknown(tmp_path: Path) -> None:
    """任务查询保留不同创建工具名，旧记录保持空值供前端显示未知。

    Args:
        tmp_path (Path): 临时数据库和日志目录。
    """
    from app.routes.agent import list_background_tasks

    async def scenario() -> None:
        """直接穿过真实任务查询入口及 DAO，不以 UI 假数据替代后端字段验证。"""
        path = str(tmp_path / "db")
        supervisor = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=path)
        log = tmp_path / "log"
        log.write_text("output", encoding="utf-8")
        names = {"one": "exec_command", "two": "other_tool", "old": None}
        try:
            for task_id, name in names.items():
                await supervisor.records.insert_running(task_id, "chat", "test", 1, str(log), tool_name=name)
                await supervisor.commit_terminal_state_once(task_id, TaskStatus.COMPLETED)
        finally:
            await supervisor.close()
        restored = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), db_path=path)
        try:
            response = await list_background_tasks("chat", restored)
            assert {row["task_id"]: row["tool_name"] for row in response["tasks"]} == names
        finally:
            await restored.close()

    asyncio.run(scenario())
