from app.services.channel_subscription_service import ChannelSubscriptionService
import asyncio
from contextlib import closing
import ctypes
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from app.agent.core.event_router import AgentEvent, EventType
from app.agent.state.manager import StateManager
from app.agent.state.messages import TaskResultMessage
from app.orchestration.agent_runtime_scheduler import AgentRuntimeScheduler
from app.interaction.interaction_event_hub import InteractionEventHub
from app.agent.runtime.session_inbox import SessionInbox
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.runtime_types import TaskStatus
from app.orchestration.task_supervisor import TaskSupervisor


def test_task_list_excludes_foreground_until_same_process_becomes_background(tmp_path) -> None:
    """验证任务入口不计前台命令，超时后才展示同一进程的后台任务。

    Args:
        tmp_path (Path): 测试专用持久记录与输出目录。
    """
    async def scenario() -> None:
        """通过任务启动与列表接口检查前后台展示边界，不依赖私有字段。"""
        supervisor = TaskSupervisor(db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks")
        try:
            # 1. 正常前台完成仍保留可按身份查询的输出，但不进入后台任务列表。
            foreground = await supervisor.launch(
                "s", "quick", [sys.executable, "-c", "print('done')"],
                cwd=str(tmp_path), background=False, timeout_sec=3,
            )
            assert not foreground.background
            assert await supervisor.list("s") == []
            # 2. 超时移交后台后，列表包含返回的同一个 task_id，不重新启动进程。
            timeout = await supervisor.launch(
                "s", "slow", [sys.executable, "-c", "import time; time.sleep(2)"],
                cwd=str(tmp_path), background=False, timeout_sec=0.05,
            )
            assert timeout.background
            assert [record.task_id for record in await supervisor.list("s")] == [timeout.task_id]
        finally:
            await supervisor.close()

        # 3. 模式与同一任务身份都能跨重启恢复，完成的前台命令不会重新进入后台列表。
        restarted = TaskSupervisor(db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks")
        try:
            assert (await restarted.get(foreground.task_id)).background is False
            assert (await restarted.get(timeout.task_id)).background is True
            assert [record.task_id for record in await restarted.list("s")] == [timeout.task_id]
        finally:
            await restarted.close()

    asyncio.run(scenario())


def test_background_task_terminal_notification_is_once_and_output_survives_restart(tmp_path) -> None:
    """验证任务终态只通知一次且输出重启后可查。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：任务终态只通知一次且输出重启后可查。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(db_path=db_path, spool_dir=tmp_path / "tasks")
        try:
            result = await supervisor.launch(
                "s", "print('hello')",
                [sys.executable, "-c", "print('hello')"],
                cwd=str(tmp_path), background=True, timeout_sec=1,
                origin_run_id="run-1", origin_tool_call_id="call-1",
            )
            task_id = result.task_id
            assert task_id
            for _ in range(100):
                record = await supervisor.get(task_id)
                if record and record.status != "running":
                    break
                await asyncio.sleep(0.02)
            assert record is not None and record.status == "completed"
            assert (record.origin_run_id, record.origin_tool_call_id, record.output_ref) == (
                "run-1", "call-1", task_id,
            )
            page = await supervisor.read_output(task_id, 0, 1024)
            assert "hello" in page["text"]
            await supervisor.finish_once(task_id, "completed", 0)
            inbox = SessionInbox("s", db_path)
            try:
                input_record = await inbox.read_next()
                assert input_record is not None and input_record.kind == "task_terminal"
                assert input_record.payload["task_id"] == task_id
                assert [item.id for item in await inbox.read_task_terminals()] == [input_record.id]
            finally:
                await inbox.close()
        finally:
            await supervisor.close()
        restarted = TaskSupervisor(db_path=db_path, spool_dir=tmp_path / "tasks")
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert (await restarted.get(task_id)).status == "completed"
        assert "hello" in (await restarted.read_output(task_id, 0, 1024))["text"]
        await restarted.close()

    asyncio.run(scenario())


def test_terminal_notification_failure_rolls_back_task_and_retry_notifies_once(tmp_path: Path) -> None:
    """验证通知插入失败不留下任务终态，重试后通知内容正确且只保存一次。

    Args:
        tmp_path (Path): 测试专用数据库和输出目录。
    """
    async def scenario() -> None:
        """从任务 DAO 和 Inbox 公开入口验证真实 SQLite 故障后的回滚与重试。"""
        path = str(tmp_path / "state.sqlite3")
        records = TaskRecordDao(path)
        inbox = SessionInbox("s", path)
        try:
            # 1. 只拒绝任务通知插入，让任务终态更新先执行，再检查两项一起回滚。
            await records.insert_running("task", "s", "测试命令", 1, str(tmp_path / "out"))
            with closing(sqlite3.connect(path)) as inspection:
                inspection.execute(
                    "CREATE TRIGGER reject_terminal BEFORE INSERT ON session_inbox "
                    "WHEN NEW.kind = 'task_terminal' BEGIN SELECT RAISE(ABORT, 'reject terminal'); END",
                )
                inspection.commit()
            with pytest.raises(sqlite3.IntegrityError, match="reject terminal"):
                await records.finish_once("task", TaskStatus.COMPLETED, 0, "完整结果")
            record = await records.get("task")
            assert record is not None
            assert record.status == "running" and record.exit_code is None and record.ended_at is None
            assert await inbox.read_task_terminals() == ()
            with closing(sqlite3.connect(path)) as inspection:
                assert inspection.execute("SELECT COUNT(*) FROM session_inbox").fetchone()[0] == 0
                inspection.execute("DROP TRIGGER reject_terminal")
                inspection.commit()
            # 2. 去掉故障后可以首次结束，重复回调或不存在的任务不会追加通知。
            assert await records.finish_once("task", TaskStatus.COMPLETED, 0, "完整结果")
            assert not await records.finish_once("task", TaskStatus.FAILED, 1, "重复结果")
            assert not await records.finish_once("missing", TaskStatus.COMPLETED, 0, "不存在")
            record = await records.get("task")
            assert record is not None
            assert record.status == "completed" and record.exit_code == 0 and record.ended_at is not None
            notices = await inbox.read_task_terminals()
            assert len(notices) == 1
            notice = notices[0]
            assert notice.origin_channel == "system" and notice.delivery_mode == "immediate"
            assert notice.payload == {
                "task_id": "task", "task_name": "测试命令", "status": "completed", "result": "",
                "tool_name": None, "exit_code": 0, "output_ref": "task-output://task",
            }
            # 3. 直接核对持久记录数量及默认排队标记，不能靠读取结果掩盖重复插入。
            with closing(sqlite3.connect(path)) as inspection:
                assert inspection.execute("SELECT COUNT(*) FROM session_inbox").fetchone()[0] == 1
                created_at, queue_ready = inspection.execute(
                    "SELECT created_at, queue_ready FROM session_inbox",
                ).fetchone()
                assert created_at.endswith("+00:00") and queue_ready == 0
        finally:
            await inbox.close()
            await records.close()

    asyncio.run(scenario())


def test_spool_write_failure_marks_task_failed_and_notifies_once(tmp_path, monkeypatch) -> None:
    """验证输出写入失败会终止任务并仅通知一次。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
        monkeypatch (pytest.MonkeyPatch): Pytest 依赖替换 fixture。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    original_open = Path.open

    def fail_spool_open(path, mode="r", *args, **kwargs):
        """在追加输出时注入文件写入失败。

        Args:
            path (对象): 测试输出文件路径。
            mode (对象): 测试输出文件的打开模式。
            args (tuple): 测试工具参数或转发的位置参数。
            kwargs (dict): 转发给被替换调用的关键字参数。
        """
        if path.name == "output.log" and mode == "ab":
            raise OSError("模拟磁盘写入失败")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_spool_open)

    async def scenario() -> None:
        """在隔离异步环境中安排并验证：输出写入失败会终止任务并仅通知一次。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(db_path=db_path, spool_dir=tmp_path / "tasks")
        try:
            result = await supervisor.launch(
                "s", "print('hello')", [sys.executable, "-c", "print('hello')"],
                cwd=str(tmp_path), background=True, timeout_sec=1,
            )
            for _ in range(100):
                record = await supervisor.get(result.task_id)
                if record is not None and record.status != "running":
                    break
                await asyncio.sleep(0.02)
            assert record is not None and record.status == "failed"
            inbox = SessionInbox("s", db_path)
            try:
                inputs = await inbox.read_activation_batch()
                assert len(inputs) == 1 and inputs[0].payload["status"] == "failed"
            finally:
                await inbox.close()
        finally:
            await supervisor.close()

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object 行为")
def test_immediate_child_is_in_job_and_stops_with_parent(tmp_path) -> None:
    """验证立即启动的子进程也受 Job 约束并随父进程停止。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：立即启动的子进程也受 Job 约束并随父进程停止。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        pid_file = tmp_path / "child.pid"
        script = (
            "import pathlib,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
            "creationflags=0x200); "
            f"pid_path=pathlib.Path({str(pid_file)!r}); "
            "staging=pid_path.with_suffix('.tmp'); staging.write_text(str(child.pid)); staging.replace(pid_path); "
            "time.sleep(30)"
        )
        supervisor = TaskSupervisor(
            db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks",
        )
        try:
            result = await supervisor.launch(
                "s", "spawn child", [sys.executable, "-c", script],
                cwd=str(tmp_path), background=True, timeout_sec=1,
            )
            # 1.1. 子进程编号以重命名原子发布，文件存在时内容已经写完，避免偶发读到空文件。
            for _ in range(100):
                if pid_file.exists():
                    break
                await asyncio.sleep(0.02)
            assert pid_file.exists()
            child_pid = int(pid_file.read_text())
            assert await supervisor.stop(result.task_id)
            for _ in range(200):
                record = await supervisor.get(result.task_id)
                if record is not None and record.status != "running":
                    break
                await asyncio.sleep(0.02)
            assert record is not None and record.status == "stopped"
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.WaitForSingleObject.restype = ctypes.c_uint32
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x00100000, False, child_pid)
            if handle:
                try:
                    assert kernel.WaitForSingleObject(handle, 3000) == 0
                finally:
                    kernel.CloseHandle(handle)
        finally:
            await supervisor.close()

    asyncio.run(scenario())


def test_existing_task_table_adds_origin_columns_without_rewriting_records(tmp_path) -> None:
    """验证既有任务表只补字段而不重写历史记录。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    db_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE task_records (task_id TEXT PRIMARY KEY, session_id TEXT, "
            "command TEXT, status TEXT, pid INTEGER, exit_code INTEGER, "
            "output_path TEXT, created_at TEXT, ended_at TEXT)"
        )
        db.execute(
            "INSERT INTO task_records VALUES ('old', 's', 'old command', 'completed', "
            "NULL, 0, 'old.log', '2026-01-01', '2026-01-01')"
        )

    async def scenario() -> None:
        """在隔离异步环境中安排并验证：既有任务表只补字段而不重写历史记录。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        supervisor = TaskSupervisor(db_path=str(db_path), spool_dir=tmp_path / "tasks")
        try:
            record = await supervisor.get("old")
            assert record is not None and record.command == "old command"
            assert record.origin_run_id is None and record.origin_tool_call_id is None
            assert record.output_ref == "old"
        finally:
            await supervisor.close()

    asyncio.run(scenario())


def test_output_pages_preserve_utf8_characters_across_byte_boundaries(tmp_path) -> None:
    """验证按字节分页仍保留完整 UTF-8 字符。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：按字节分页仍保留完整 UTF-8 字符。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        supervisor = TaskSupervisor(
            db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks",
        )
        try:
            result = await supervisor.launch(
                "s", "print('你好')",
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write('你好'.encode('utf-8'))"],
                cwd=str(tmp_path), background=False, timeout_sec=3,
            )
            cursor = 0
            chunks = []
            while True:
                page = await supervisor.read_output(result.task_id, cursor, 1)
                assert page["next_offset"] > cursor
                chunks.append(page["text"])
                cursor = page["next_offset"]
                if not page["has_more"]:
                    break
            assert "".join(chunks).strip() == "你好"
            assert await supervisor.read_tail(result.task_id) == "你好"
        finally:
            await supervisor.close()

    asyncio.run(scenario())


def test_running_output_keeps_incomplete_utf8_bytes_for_next_page(tmp_path) -> None:
    """验证运行中不完整 UTF-8 字节留给下一页。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：运行中不完整 UTF-8 字节留给下一页。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        supervisor = TaskSupervisor(
            db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks",
        )
        try:
            command = (
                "import sys,time; "
                "sys.stdout.buffer.write(bytes([0xe4])); sys.stdout.flush(); "
                "open('partial-ready','w').close(); sys.stdin.readline(); "
                "sys.stdout.buffer.write(bytes([0xbd,0xa0])); sys.stdout.flush()"
            )
            result = await supervisor.launch(
                "s", "write split utf8", [sys.executable, "-c", command],
                cwd=str(tmp_path), background=True, timeout_sec=1,
            )
            for _ in range(100):
                record = await supervisor.get(result.task_id)
                if record is not None and record.status == "running" and (tmp_path / "partial-ready").exists():
                    break
                await asyncio.sleep(0.01)
            page = await supervisor.read_output(result.task_id, 0, 1)
            assert page["text"] == ""
            assert page["next_offset"] == 0
            await supervisor.interact("s", result.task_id, chars="\n", yield_time_ms=0)
            for _ in range(100):
                if (await supervisor.get(result.task_id)).status == "completed":
                    break
                await asyncio.sleep(0.01)
            completed = await supervisor.read_output(result.task_id, page["next_offset"], 1)
            assert completed["text"] == "你"
        finally:
            await supervisor.close()

    asyncio.run(scenario())


def test_foreground_result_is_bounded_but_spool_keeps_full_output(tmp_path) -> None:
    """验证前台返回限制预览大小而完整输出仍保存。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：前台返回限制预览大小而完整输出仍保存。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        supervisor = TaskSupervisor(
            db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks",
        )
        try:
            result = await supervisor.launch(
                "s", "large output", [sys.executable, "-c", "print('x' * 100000)"],
                cwd=str(tmp_path), background=False, timeout_sec=3,
            )
            assert len(result.output.encode("utf-8")) <= 64 * 1024
            assert result.has_more
            assert "task-output://" not in result.render()
            record = await supervisor.get(result.task_id)
            assert record is not None
            assert (tmp_path / "tasks" / result.task_id / "output.log").stat().st_size > 100000
        finally:
            await supervisor.close()

    asyncio.run(scenario())


def test_explicit_stop_ends_process_and_records_stopped_status(tmp_path) -> None:
    """验证显式停止结束受管进程并记录 stopped。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：显式停止结束受管进程并记录 stopped。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        supervisor = TaskSupervisor(
            db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks",
        )
        try:
            result = await supervisor.launch(
                "s", "sleep", [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=str(tmp_path), background=True, timeout_sec=1,
            )
            assert await supervisor.stop(result.task_id)
            for _ in range(200):
                record = await supervisor.get(result.task_id)
                if record is not None and record.status != "running":
                    break
                await asyncio.sleep(0.02)
            assert record is not None and record.status == "stopped"
        finally:
            await supervisor.close()

    asyncio.run(scenario())


def test_terminal_notification_wakes_idle_agent_on_desktop_channel(tmp_path) -> None:
    """验证终态通知唤醒空闲 Agent 并只发给桌面。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：终态通知唤醒空闲 Agent 并只发给桌面。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        delivered = asyncio.Event()
        supervisor = TaskSupervisor(db_path=db_path, spool_dir=tmp_path / "tasks")

        class FakeAgent:
            """提供隔离测试替身，用于验证：终态通知唤醒空闲 Agent 并只发给桌面。"""
            async def run(self, message, *, inbox_input=None, **kwargs):
                """持久交付本次真人输入，建立当前进程的会话激活资格。

                Args:
                    message (str): 新真人正文。
                    inbox_input (InboxEnvelope): 当前输入读取。
                    **kwargs (dict): 其余运行参数。

                Yields:
                    AgentEvent: 正常完成事件。
                """
                manager = StateManager("s", db_path)
                try:
                    state = await manager.load()
                    state.add_user_message(message)
                    await manager.save(state, checkpoint_type="completed", inbox_inputs=(inbox_input,))
                finally:
                    await manager.close()
                yield AgentEvent(EventType.DONE, None)

            async def run_runtime_inputs(self, messages, *, inbox_inputs, turn_control=None, before_next_llm=None):
                """验证后台通知批次并产生测试回复。

                Args:
                    messages (消息序列): 需要转换或交付的领域消息集合。
                    inbox_inputs (对象): 随本次 checkpoint 原子交付的读取快照集合。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                manager = StateManager("s", db_path)
                try:
                    state = await manager.load()
                    for message in messages:
                        state.append_input(message)
                    await manager.save(
                        state, checkpoint_type="completed", inbox_inputs=inbox_inputs,
                    )
                finally:
                    await manager.close()
                delivered.set()
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        subscriptions = ChannelSubscriptionService()
        scheduler = AgentRuntimeScheduler(
            subscriptions=subscriptions, agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SessionInbox(session, db_path),
            state_manager_factory=lambda session: StateManager(session, db_path),
            task_supervisor=supervisor,
        )
        hub = InteractionEventHub(scheduler, subscriptions=subscriptions)
        await hub.start()
        await scheduler.start()
        supervisor.on_terminal = scheduler.notify_task_terminal
        from app.interaction.interaction_contract import ChannelCommand
        desktop = hub.subscribe("s", "desktop")
        await anext(desktop)
        try:
            # 1. 受理不代表开始执行；先观察首轮完整结束，再检查后台通知启动的新轮次。
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "启动本次会话"}))
            initial = []
            while len(initial) < 2:
                event = await asyncio.wait_for(anext(desktop), 1)
                if event.kind in {"run_started", "run_completed"}:
                    initial.append(event.kind)
            assert initial == ["run_started", "run_completed"]
            # 2. 后台任务结束后的输出必须属于新轮次，不能混入会话激活时的完成事件。
            result = await supervisor.launch(
                "s", "print('done')", [sys.executable, "-c", "print('done')"],
                cwd=str(tmp_path), background=True, timeout_sec=1,
            )
            await asyncio.wait_for(delivered.wait(), 3)
            observed = []
            while len(observed) < 2:
                event = await asyncio.wait_for(anext(desktop), 1)
                if event.kind in {"run_started", "run_completed"}:
                    observed.append(event)
            assert [item.kind for item in observed] == ["run_started", "run_completed"]
            assert all(item.origin_channel == "desktop" for item in observed)
            assert (await supervisor.get(result.task_id)).status == "completed"
            inbox = SessionInbox("s", db_path)
            try:
                assert await inbox.read_next() is None
            finally:
                await inbox.close()
        finally:
            await desktop.aclose()
            await supervisor.close()
            await hub.close()
            await scheduler.close()

    asyncio.run(scenario())


def test_restart_marks_unfinished_task_orphaned_once(tmp_path) -> None:
    """验证重启后的遗留任务只标记并通知 orphaned 一次。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：重启后的遗留任务只标记并通知 orphaned 一次。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(db_path=db_path, spool_dir=tmp_path / "tasks")
        db = await supervisor.records._get_db()
        output_path = tmp_path / "tasks" / "old" / "output.log"
        output_path.parent.mkdir(parents=True)
        output_path.write_text("unfinished", encoding="utf-8")
        await db.execute(
            "INSERT INTO task_records "
            "(task_id, session_id, command, status, output_path, created_at) "
            "VALUES ('old', 's', 'long command', 'running', ?, '2026-01-01')",
            (str(output_path),),
        )
        await db.commit()
        await supervisor.close()
        restarted = TaskSupervisor(db_path=db_path, spool_dir=tmp_path / "tasks")
        try:
            await restarted.recover_orphaned()
            await restarted.recover_orphaned()
            assert (await restarted.get("old")).status == "orphaned"
            inbox = SessionInbox("s", db_path)
            try:
                notices = await inbox.read_task_terminals()
                assert [item.payload["task_id"] for item in notices] == ["old"]
            finally:
                await inbox.close()
        finally:
            await restarted.close()

    asyncio.run(scenario())


def test_active_turn_receives_multiple_task_terminals_at_next_model_boundary(tmp_path) -> None:
    """验证活动轮次在下一次模型调用前收到合并终态。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：活动轮次在下一次模型调用前收到合并终态。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        reached_boundary = asyncio.Event()
        continue_model = asyncio.Event()
        injected = asyncio.Event()
        supervisor = TaskSupervisor(db_path=db_path, spool_dir=tmp_path / "tasks")

        class FakeAgent:
            """提供隔离测试替身，用于验证：活动轮次在下一次模型调用前收到合并终态。"""
            async def run(
                self, message, images=None, *, origin_channel="desktop",
                inbox_input=None, turn_control=None, before_next_llm=None, on_input_committed=None,
            ):
                """按场景屏障产生 Agent 事件并提交测试输入。

                Args:
                    message (对象): 真人文本或类型声明对应的内部通知。
                    images (对象): 真人附件的图片 data URL 列表。
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    inbox_input (对象): 随本次 checkpoint 原子交付的单条读取快照。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。
                    on_input_committed (Callable | None): 真人输入实际提交后的观察回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                manager = StateManager("s", db_path)
                try:
                    state = await manager.load()
                    human_message = state.add_user_message(message, origin_channel=origin_channel)
                    await manager.save(
                        state, checkpoint_type="intermediate", inbox_inputs=(inbox_input,),
                    )
                    # 1. 替身遵循真实 Agent：只有事务成功后才发布输入事实。
                    if on_input_committed is not None:
                        await on_input_committed(human_message)
                    reached_boundary.set()
                    yield AgentEvent(EventType.TEXT_CHUNK, "处理中")
                    await continue_model.wait()
                    assert before_next_llm is not None
                    await before_next_llm(state)
                    assert isinstance(state.messages[-1], TaskResultMessage)
                    assert state.messages[-1].task_name == "2 个后台任务"
                    injected.set()
                    yield AgentEvent(EventType.DONE, None)
                finally:
                    await manager.close()

            async def commit_runtime_inputs(self, state, messages, inputs):
                """将测试内部输入与读取快照原子保存。

                Args:
                    state (AgentState): 当前 Agent 状态。
                    messages (消息序列): 需要转换或交付的领域消息集合。
                    inputs (对象): 待交付内部通知的读取快照集合。
                """
                for item in messages:
                    state.append_input(item)
                manager = StateManager("s", db_path)
                try:
                    await manager.save(
                        state, checkpoint_type="intermediate", inbox_inputs=inputs,
                    )
                finally:
                    await manager.close()

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        scheduler = AgentRuntimeScheduler(
           agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SessionInbox(session, db_path),
            state_manager_factory=lambda session: StateManager(session, db_path),
            task_supervisor=supervisor,
        )
        await scheduler.start()
        try:
            from app.interaction.interaction_contract import ChannelCommand
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "启动任务"}))
            await asyncio.wait_for(reached_boundary.wait(), 1)
            task_ids = []
            for index in range(2):
                result = await supervisor.launch(
                    "s", f"print({index})", [sys.executable, "-c", f"print({index})"],
                    cwd=str(tmp_path), background=True, timeout_sec=1,
                )
                task_ids.append(result.task_id)
            for _ in range(100):
                records = await asyncio.gather(*(supervisor.get(task_id) for task_id in task_ids))
                if all(record is not None and record.status == "completed" for record in records):
                    break
                await asyncio.sleep(0.02)
            continue_model.set()
            await asyncio.wait_for(injected.wait(), 2)
            manager = StateManager("s", db_path)
            try:
                state = await manager.load()
                assert isinstance(state.messages[-1], TaskResultMessage)
                assert all(task_id in state.messages[-1].task_id for task_id in task_ids)
            finally:
                await manager.close()
            inbox = SessionInbox("s", db_path)
            try:
                assert await inbox.read_next() is None
            finally:
                await inbox.close()
        finally:
            continue_model.set()
            await scheduler.close()
            await supervisor.close()

    asyncio.run(scenario())


def test_idle_task_notification_is_committed_before_pending_human_turn(tmp_path) -> None:
    """验证空闲激活时任务通知先于同批真人消息提交。

    Args:
        tmp_path (Path): Pytest 创建的隔离临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：空闲激活时任务通知先于同批真人消息提交。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        db_path = str(tmp_path / "state.sqlite3")
        supervisor = TaskSupervisor(db_path=db_path, spool_dir=tmp_path / "tasks")
        done = asyncio.Event()
        result = await supervisor.launch(
            "s", "print('ready')", [sys.executable, "-c", "print('ready')"],
            cwd=str(tmp_path), background=True, timeout_sec=1,
        )
        for _ in range(100):
            record = await supervisor.get(result.task_id)
            if record is not None and record.status == "completed":
                break
            await asyncio.sleep(0.02)
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert record is not None and record.status == "completed"

        class FakeAgent:
            """提供隔离测试替身，用于验证：空闲激活时任务通知先于同批真人消息提交。"""
            async def run(
                self, message, images=None, *, origin_channel="desktop",
                inbox_input=None, inbox_inputs=None, runtime_inputs=(),
                turn_control=None, before_next_llm=None, on_input_committed=None,
            ):
                """按场景屏障产生 Agent 事件并提交测试输入。

                Args:
                    message (对象): 真人文本或类型声明对应的内部通知。
                    images (对象): 真人附件的图片 data URL 列表。
                    origin_channel (对象): 输入或本轮回复所属的渠道。
                    inbox_input (对象): 随本次 checkpoint 原子交付的单条读取快照。
                    inbox_inputs (对象): 随本次 checkpoint 原子交付的读取快照集合。
                    runtime_inputs (对象): 同次激活中先于真人输入追加的内部通知。
                    turn_control (对象): 当前 run 的停止与引导控制器。
                    before_next_llm (对象): 下一次模型调用前注入内部通知的异步回调。
                    on_input_committed (Callable | None): 真人输入实际提交后的观察回调。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                assert len(runtime_inputs) == 1
                assert isinstance(runtime_inputs[0], TaskResultMessage)
                assert len(inbox_inputs) == 2
                manager = StateManager("s", db_path)
                try:
                    state = await manager.load()
                    state.append_input(runtime_inputs[0])
                    human_message = state.add_user_message(message, origin_channel=origin_channel)
                    await manager.save(
                        state, checkpoint_type="intermediate", inbox_inputs=inbox_inputs,
                    )
                    # 1. 替身遵循真实 Agent：只有事务成功后才发布输入事实。
                    if on_input_committed is not None:
                        await on_input_committed(human_message)
                finally:
                    await manager.close()
                done.set()
                yield AgentEvent(EventType.DONE, None)

            async def run_runtime_inputs(self, *args, **kwargs):
                """验证后台通知批次并产生测试回复。

                Args:
                    args (tuple): 测试工具参数或转发的位置参数。
                    kwargs (dict): 转发给被替换调用的关键字参数。

                Yields:
                    AgentEvent: 当前测试场景的预设分片或事件。
                """
                raise AssertionError("任务通知不应单独激活")
                yield AgentEvent(EventType.DONE, None)

            async def close(self):
                """完成替身清理而不访问真实服务。"""
                pass

        scheduler = AgentRuntimeScheduler(
           agent_factory=lambda _session: FakeAgent(),
            inbox_factory=lambda session: SessionInbox(session, db_path),
            state_manager_factory=lambda session: StateManager(session, db_path),
        )
        await scheduler.start()
        try:
            from app.interaction.interaction_contract import ChannelCommand
            await scheduler.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "继续"}))
            await asyncio.wait_for(done.wait(), 2)
            manager = StateManager("s", db_path)
            try:
                state = await manager.load()
                assert isinstance(state.messages[-2], TaskResultMessage)
                assert state.messages[-1].text == "继续"
            finally:
                await manager.close()
        finally:
            await scheduler.close()
            await supervisor.close()

    asyncio.run(scenario())
