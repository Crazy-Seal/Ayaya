"""通过工具和运行器 Interface 验证真实进程、增量及持久通知。"""

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from app.agent.runtime.session_inbox import SessionInbox
from app.agent.tools.core.base import ToolContext
from app.agent.tools.exec_command import ExecCommandArguments, ExecCommandTool
from app.agent.tools.write_stdin import WriteStdinArguments, WriteStdinTool
from app.agent.tools.read_file import read_file
from app.agent.tools.results.normalizer import ToolResultNormalizer
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.runtime_types import TaskStatus
from app.orchestration.task_supervisor import TaskSupervisor


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
        record = await supervisor.get(task_id)
        if record.status != TaskStatus.RUNNING:
            return record
        assert asyncio.get_running_loop().time() < deadline, "真实终端没有按时退出"
        await asyncio.sleep(0.01)


def context_for(supervisor: TaskSupervisor, session_id: str = "chat") -> ToolContext:
    """构造仅使用临时存储的工具上下文。

    Args:
        supervisor (TaskSupervisor): 临时任务运行器。
        session_id (str): 用于测试任务归属的聊天编号。

    Returns:
        ToolContext: 无模型和生产存储依赖的上下文。
    """
    return ToolContext(session_id, None, lambda *_: None, lambda: {}, lambda _: None,
                       task_launcher=supervisor, origin_tool_name="exec_command")


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
        supervisor = TaskSupervisor(db_path=path, spool_dir=tmp_path / "logs")
        inbox = SessionInbox("chat", path)
        try:
            command = "import sys; print('READY', flush=True); value=input(); print('RESULT:'+value, flush=True)"
            started = await supervisor.launch("chat", "interactive", [sys.executable, "-u", "-c", command],
                                              cwd=str(tmp_path), tty=tty, yield_time_ms=0, tool_name="exec_command")
            observed = started.output
            while "READY" not in observed:
                chunk = await supervisor.interact("chat", started.task_id, yield_time_ms=3000)
                assert chunk.background, (chunk, observed)
                observed += chunk.output
            sent = await supervisor.interact("chat", started.task_id, chars="hello\r" if tty and os.name == "nt" else "hello\n", yield_time_ms=3000)
            assert sent.input_status == "sent"
            ended = await wait_ended(supervisor, started.task_id)
            assert ended.status == TaskStatus.COMPLETED and ended.exit_code == 0
            assert ended.tool_name == "exec_command"
            polled = await supervisor.interact("chat", started.task_id, yield_time_ms=0)
            assert not polled.background and polled.output == ""
            rejected = await supervisor.interact("chat", started.task_id, chars="again\n", yield_time_ms=0)
            assert rejected.input_status == "not_sent" and rejected.error
            notices = await inbox.read_task_terminals()
            assert len(notices) == 1
            payload = notices[0].payload
            assert payload["result"] == "" and payload["tool_name"] == "exec_command"
            assert payload["exit_code"] == 0 and payload["output_ref"] == f"task-output://{started.task_id}"
            page = await read_file().execute({"path": payload["output_ref"]}, context_for(supervisor))
            assert "RESULT:hello" in page.text and "READY" in page.text
            assert "task-output://" not in started.render() and "task-output://" not in polled.render()
            with pytest.raises(KeyError):
                await supervisor.interact("other", started.task_id, chars="secret", yield_time_ms=0)
            with pytest.raises(KeyError):
                await supervisor.resolve_output("other", payload["output_ref"])
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
        supervisor = TaskSupervisor(db_path=str(tmp_path / "db"), spool_dir=tmp_path / "logs")
        try:
            command = (
                "import sys; sys.stdout.write('Hel'); sys.stdout.flush(); sys.stdin.read(1); "
                "sys.stdout.write('lo,Wo'); sys.stdout.flush(); sys.stdin.read(1); "
                "sys.stdout.write('rld!'); sys.stdout.flush(); sys.stdin.read(1)"
            )
            started = await supervisor.launch("chat", "chunks", [sys.executable, "-u", "-c", command],
                                              cwd=str(tmp_path), yield_time_ms=0)
            first = started.output or (await supervisor.interact("chat", started.task_id, yield_time_ms=3000)).output
            assert first == "Hel"
            assert (await supervisor.interact("chat", started.task_id, yield_time_ms=0)).output == ""
            second = await supervisor.interact("chat", started.task_id, chars="a", yield_time_ms=3000)
            assert second.output == "lo,Wo"
            third = await supervisor.interact("chat", started.task_id, chars="b", yield_time_ms=3000)
            assert third.output == "rld!"
            assert first + second.output + third.output == "Hello,World!"
            assert (await supervisor.get(started.task_id)).status == TaskStatus.RUNNING
            await supervisor.interact("chat", started.task_id, chars="c", yield_time_ms=3000)
            await wait_ended(supervisor, started.task_id)
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
        supervisor = TaskSupervisor(db_path=str(tmp_path / "db"), spool_dir=tmp_path / "logs")
        try:
            context = context_for(supervisor)
            result = await ExecCommandTool().execute({"cmd": "echo ok"}, context)
            assert "ok" in result.text and "task-output://" not in result.text
            assert "tool-result://" not in ToolResultNormalizer().normalize(result, session_id="chat").text
            invalid = await WriteStdinTool().execute({"task_id": "unknown", "yield_time_ms": -1}, context)
            assert "status: error" in invalid.text
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
        supervisor = TaskSupervisor(db_path=str(tmp_path / "db"), spool_dir=tmp_path / "logs")
        text = "中文\n" * 10000
        path = tmp_path / "large.log"
        path.write_text(text, encoding="utf-8", newline="")
        try:
            await supervisor.records.insert_running("large", "chat", "test", 1, str(path), tool_name="other_tool")
            context = context_for(supervisor)
            first = await read_file().execute({"path": "task-output://large"}, context)
            second = await read_file().execute({"path": "task-output://large", "start": 6000}, context)
            assert first.text.split("\n", 1)[1] == text[:6000]
            assert second.text.split("\n", 1)[1] == text[6000:12000]
            assert "next_start: 12000" in second.text
            denied = await read_file().execute({"path": "task-output://large"}, context_for(supervisor, "other"))
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
    from app.orchestration.agent_runtime_scheduler import AgentRuntimeScheduler

    async def scenario() -> None:
        """在真实 Inbox 中领取多种工具的完成通知，再走模型消息转换。"""
        path = str(tmp_path / "db")
        records, inbox = TaskRecordDao(path), SessionInbox("chat", path)
        try:
            for index in range(90):
                name = "exec_command" if index % 2 == 0 else "other_tool"
                await records.insert_running(f"task-{index}", "chat", "command", 1, "unused", tool_name=name)
                await records.finish_once(f"task-{index}", TaskStatus.COMPLETED, 0)
            claims = await inbox.read_task_terminals()
            messages = AgentRuntimeScheduler._task_result_messages(claims)
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
def test_tool_permission_rename_preserves_saved_decisions(legacy: str) -> None:
    """新权限优先，旧 deny 不会因改名或隐藏恢复入口被绕过。

    Args:
        legacy (str): 旧 run_ps 保存的权限。
    """
    from app.agent.tools.authorization.manager import ToolAuthorization
    from app.agent.tools.authorization.models import ToolIdentity
    from app.crud.chat_settings_dao import _reconcile_local_tool_permissions

    settings = {"tool_permissions": {"local:run_ps": legacy}}
    assert _reconcile_local_tool_permissions(settings)
    assert settings["tool_permissions"]["local:exec_command"] == legacy
    assert settings["tool_permissions"]["local:write_stdin"] == ("deny" if legacy == "deny" else "ask")
    assert not _reconcile_local_tool_permissions(settings)
    settings["tool_permissions"]["local:exec_command"] = "deny"
    authorization = ToolAuthorization(lambda _: SimpleNamespace(tool_permissions=settings["tool_permissions"]))
    assert authorization.get_policy("chat", ToolIdentity.local("run_ps")) == "deny"


def test_old_call_is_hidden_but_can_resume_without_exposing_run_ps() -> None:
    """兼容工具只在按旧名称查找时出现，新模型和工具目录均不公开它。"""
    from app.agent.core.tool_manager import ToolManager
    from app.agent.tools.core.registry import ToolRegistry

    manager = ToolManager()
    manager.register(ExecCommandTool())
    assert manager.get("run_ps").name == "run_ps"
    assert "run_ps" not in ToolRegistry.list_tools()
    assert "run_ps" not in [definition.name for definition in manager.get_tools()]


@pytest.mark.parametrize("tty", [False, True])
def test_registration_failure_reclaims_started_process(tmp_path: Path, monkeypatch, tty: bool) -> None:
    """数据库登记失败必须清理真实进程和输出通道，PTY 收尾不能死锁。

    Args:
        tmp_path (Path): 隔离工作目录。
        monkeypatch (pytest.MonkeyPatch): 注入任务登记失败。
        tty (bool): 普通管道或真实 PTY。
    """
    async def scenario() -> None:
        """让子进程持续输出，真实验证未创建 watcher 时的失败回收。"""
        supervisor = TaskSupervisor(db_path=str(tmp_path / "db"), spool_dir=tmp_path / "logs")

        async def reject_record(*args, **kwargs) -> None:
            """模拟进程已创建但任务记录无法保存。

            Args:
                args (tuple): 被拒绝的记录位置参数。
                kwargs (dict): 被拒绝的记录关键字参数。
            """
            raise OSError("injected registration failure")

        monkeypatch.setattr(supervisor.records, "insert_running", reject_record)
        try:
            with pytest.raises(OSError, match="injected registration"):
                await asyncio.wait_for(supervisor.launch(
                    "chat", "output", [sys.executable, "-u", "-c", "while True: print('x'*10000)"],
                    cwd=str(tmp_path), tty=tty, yield_time_ms=0,
                ), 5)
            assert await supervisor.list("chat") == []
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
        supervisor = TaskSupervisor(db_path=str(tmp_path / "db"), spool_dir=tmp_path / "logs")

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
                chunk = await supervisor.interact("chat", task_id, yield_time_ms=1000)
                assert chunk.background, (chunk, output)
                output += chunk.output
            return output

        try:
            started = await supervisor.launch(
                "chat", "persistent PowerShell", [shell.executable, "-NoLogo", "-NoProfile", "-NoExit", "-Command", "Write-Output ('START'+123)"],
                cwd=str(tmp_path), tty=True, yield_time_ms=0,
            )
            await until(started.task_id, "START123", started.output)
            first = await supervisor.interact("chat", started.task_id, chars="$saved=17; Write-Output ('VALUE'+$saved)\r", yield_time_ms=1000)
            await until(started.task_id, "VALUE17", first.output)
            second = await supervisor.interact("chat", started.task_id, chars="Write-Output ('NEXT'+($saved+1))\r", yield_time_ms=1000)
            await until(started.task_id, "NEXT18", second.output)
            await supervisor.interact("chat", started.task_id, chars="exit\r", yield_time_ms=0)
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
        supervisor = TaskSupervisor(db_path=str(tmp_path / "db"), spool_dir=tmp_path / "logs")
        try:
            result = await supervisor.launch(
                "chat", "short", [sys.executable, "-u", "-c", "print('FINAL_OUTPUT')"],
                cwd=str(tmp_path), tty=tty, yield_time_ms=3000,
            )
            assert not result.background and result.exit_code == 0
            assert "FINAL_OUTPUT" in result.output
            assert (await supervisor.interact("chat", result.task_id, yield_time_ms=0)).output == ""
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
        supervisor = TaskSupervisor(db_path=path, spool_dir=tmp_path / "logs")
        log = tmp_path / "log"
        log.write_text("output", encoding="utf-8")
        names = {"one": "exec_command", "two": "other_tool", "old": None}
        try:
            for task_id, name in names.items():
                await supervisor.records.insert_running(task_id, "chat", "test", 1, str(log), tool_name=name)
                await supervisor.finish_once(task_id, TaskStatus.COMPLETED, 0)
        finally:
            await supervisor.close()
        restored = TaskSupervisor(db_path=path, spool_dir=tmp_path / "logs")
        try:
            response = await list_background_tasks("chat", restored)
            assert {row["task_id"]: row["tool_name"] for row in response["tasks"]} == names
        finally:
            await restored.close()

    asyncio.run(scenario())
