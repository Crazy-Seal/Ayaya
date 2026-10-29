"""验证记忆任务按需登记、并发去重及面板可读取的完成和失败结果。"""

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.memory.config import MemoryConfig
from app.agent.memory.manager import MemoryManager
from app.agent.memory.memories.diary import DiaryMemory
from app.agent.plugins.base import HookContext, PluginHook
from app.agent.plugins.memory.memory import MemoryPlugin
from app.agent.plugins.memory.tasks.memory_tasks import DiaryTask
from app.agent.plugins.runtime import PluginRuntime
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.state.model import AgentState
from app.agent.state.messages import HumanMessage, text_content
from app.routes.agent import list_background_tasks
from app.schemas.runtime_types import TaskLifetime, TaskStatus


TARGET = date(2026, 10, 25)


def make_manager(session_id: str = "diary-results") -> MemoryManager:
    """装配真实记忆业务对象，以测试替身代替外部模型和存储。

    Args:
        session_id (str): 本用例使用的会话编号。

    Returns:
        MemoryManager: 可执行准备、生成和长期记忆汇总的对象。
    """
    diary = object.__new__(DiaryMemory)
    diary.session_id = session_id
    diary.config = MemoryConfig()
    diary.chat_history_store = SimpleNamespace(
        get_last_chat_date=AsyncMock(return_value=TARGET),
        get_messages_by_date=AsyncMock(return_value=[HumanMessage(content=text_content("昨天的聊天"))]),
    )
    diary.has_diary = AsyncMock(return_value=False)
    diary.generate = AsyncMock()
    manager = object.__new__(MemoryManager)
    manager.session_id = session_id
    manager.config = diary.config
    manager.diary_memory = diary
    manager.chat_history_store = SimpleNamespace(
        get_recent_completed_turns=AsyncMock(return_value=([], [HumanMessage(content=text_content("聊天"))])),
    )
    manager.episodic_memory = SimpleNamespace(add=AsyncMock(return_value=["e1", "e2"]))
    manager.semantic_memory = SimpleNamespace(add=AsyncMock(return_value=["s1"]))
    return manager


async def start_diary(manager: MemoryManager, runtime: PluginRuntime) -> None:
    """通过记忆插件的实际激活入口检查日记并按需提交任务。

    Args:
        manager (MemoryManager): 本轮检查使用的记忆管理器。
        runtime (PluginRuntime): 连接正式任务管理器的插件调用对象。
    """
    plugin = MemoryPlugin(enable_episodic=False, enable_semantic=False)
    data = {"memory": {
        "manager": manager, "protocol_history_cutoff": 42,
        "check_diary": True, "extract_long_term_memory": False,
    }}
    await plugin.execute(HookContext.create(
        PluginHook.ON_INVOKE, AgentState.create_new(manager.session_id), data, runtime=runtime,
    ))


@pytest.mark.parametrize("skip", ["exists", "no_date", "no_messages"])
def test_unneeded_diary_does_not_create_task(tmp_path: Path, skip: str) -> None:
    """已有日记、没有历史日期或没有当日消息时，面板保持空列表。"""
    async def scenario() -> None:
        """通过真实任务列表验证跳过检查没有创建持久任务。"""
        manager = make_manager()
        if skip == "exists":
            manager.diary_memory.has_diary.return_value = True
        elif skip == "no_date":
            manager.diary_memory.chat_history_store.get_last_chat_date.return_value = None
        else:
            manager.diary_memory.chat_history_store.get_messages_by_date.return_value = []
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "tasks.db"))
        runtime = PluginRuntime("memory", manager.session_id, "run", supervisor, supervisor.runtime_queue)
        try:
            await start_diary(manager, runtime)
            assert await supervisor.list_tasks(manager.session_id) == []
            manager.diary_memory.generate.assert_not_awaited()
        finally:
            await supervisor.close()
    asyncio.run(scenario())


def test_diary_deduplicates_across_manager_instances_and_returns_result(tmp_path: Path) -> None:
    """同一会话不同管理器并发检查和生成时，只有一个任务，结束后面板有日期结果。"""
    async def scenario() -> None:
        """挂起生成，覆盖提交前并发检查、缓存更换和生成结束后的数据库去重。"""
        first, second = make_manager(), make_manager()
        release, started = asyncio.Event(), asyncio.Event()

        async def generate(*args, **kwargs) -> None:
            """等待测试允许生成结束，模拟耗时模型请求。"""
            started.set()
            await release.wait()
            first.diary_memory.has_diary.return_value = True
            second.diary_memory.has_diary.return_value = True

        first.diary_memory.generate.side_effect = generate
        second.diary_memory.generate.side_effect = generate
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "tasks.db"))
        runtime = PluginRuntime("memory", first.session_id, "run", supervisor, supervisor.runtime_queue)
        try:
            await asyncio.gather(start_diary(first, runtime), start_diary(second, runtime))
            await asyncio.wait_for(started.wait(), 1)
            await start_diary(second, runtime)
            records = await supervisor.list_tasks(first.session_id)
            assert len(records) == 1
            assert TARGET.isoformat() in records[0].title
            release.set()
            record = await runtime.wait_task(records[0].task_id, timeout_ms=1000)
            assert record.status == TaskStatus.COMPLETED
            assert TARGET.isoformat() in record.result
            assert "成功" in record.result
            response = await list_background_tasks(first.session_id, supervisor)
            assert response["tasks"][0]["result"] == record.result
            await start_diary(second, runtime)
            assert len(await supervisor.list_tasks(first.session_id)) == 1
        finally:
            release.set()
            await supervisor.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stop", [False, True])
def test_diary_failure_or_stop_releases_reservation_for_retry(tmp_path: Path, stop: bool) -> None:
    """日记失败或停止后释放处理中记录，下一次激活可以重试。"""
    async def scenario() -> None:
        """通过正式停止和结果接口验证失败原因及后续重试。"""
        manager = make_manager()
        started = asyncio.Event()

        async def generate(*args, **kwargs) -> None:
            """模拟生成失败或在模型等待中被用户停止。"""
            started.set()
            if stop:
                await asyncio.Event().wait()
            raise RuntimeError("模型连接失败")

        manager.diary_memory.generate.side_effect = generate
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "tasks.db"))
        runtime = PluginRuntime("memory", manager.session_id, "run", supervisor, supervisor.runtime_queue)
        try:
            await start_diary(manager, runtime)
            await asyncio.wait_for(started.wait(), 1)
            record, = await supervisor.list_tasks(manager.session_id)
            if stop:
                await supervisor.stop_task(record.task_id)
            done = await runtime.wait_task(record.task_id, timeout_ms=1000)
            assert done.status == (TaskStatus.STOPPED if stop else TaskStatus.FAILED)
            if not stop:
                assert "模型连接失败" in done.error
            manager.diary_memory.generate.side_effect = None
            await start_diary(manager, runtime)
            records = await supervisor.list_tasks(manager.session_id)
            assert len(records) == 2
            retry = next(item for item in records if item.task_id != record.task_id)
            assert (await runtime.wait_task(retry.task_id, timeout_ms=1000)).status == TaskStatus.COMPLETED
        finally:
            await supervisor.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["model", "save"])
def test_diary_generation_and_save_errors_reach_task_record(tmp_path: Path, failure: str) -> None:
    """真实日记生成入口的模型和保存错误均使后台任务失败。"""
    async def scenario() -> None:
        """保留真实生成流程，仅替换外部模型及数据库读写。"""
        manager = make_manager()
        diary = manager.diary_memory
        diary.generate = DiaryMemory.generate.__get__(diary)
        diary._build_diary_prompt = Mock(return_value="日记提示词")
        diary.chat_history_store.get_messages_before_date = AsyncMock(return_value=[])
        diary.store = SimpleNamespace(get_recent_before_date=AsyncMock(return_value=[]), add=AsyncMock())
        diary.llm = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(text="日记正文")))
        if failure == "model":
            diary.llm.ainvoke.side_effect = RuntimeError("日记模型失败")
        else:
            diary.store.add.side_effect = RuntimeError("日记保存失败")
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "tasks.db"))
        runtime = PluginRuntime("memory", manager.session_id, "run", supervisor, supervisor.runtime_queue)
        try:
            await start_diary(manager, runtime)
            record, = await supervisor.list_tasks(manager.session_id)
            record = await runtime.wait_task(record.task_id, timeout_ms=1000)
            assert record.status == TaskStatus.FAILED
            assert ("日记模型失败" if failure == "model" else "日记保存失败") in record.error
        finally:
            await supervisor.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_diary_check_failure_or_cancellation_releases_reservation(cancel: bool) -> None:
    """准备阶段查询失败或被取消时，其他实例可以重新准备同一天的日记。"""
    async def scenario() -> None:
        """在查询等待期间触发重复检查及失败，随后检查标记已被释放。"""
        first, second = make_manager(), make_manager()
        entered, finish_check = asyncio.Event(), asyncio.Event()

        async def check(target: date) -> bool:
            """模拟日记存在性查询失败或等待期间取消。"""
            entered.set()
            await finish_check.wait()
            raise RuntimeError("日记查询失败")

        first.diary_memory.has_diary.side_effect = check
        work = asyncio.create_task(first.prepare_diary(max_history_id=42))
        await asyncio.wait_for(entered.wait(), 1)
        assert await second.prepare_diary(max_history_id=42) is None
        if cancel:
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
        else:
            finish_check.set()
            with pytest.raises(RuntimeError, match="日记查询失败"):
                await work
        generation = await second.prepare_diary(max_history_id=42)
        assert generation is not None
        generation.release()
    asyncio.run(scenario())


def test_diary_submission_rejection_releases_reservation(tmp_path: Path) -> None:
    """任务管理器关闭后拒绝提交，日期标记仍被释放，后续可以重新准备。"""
    async def scenario() -> None:
        """使用正式提交拒绝路径验证没有遗留无法重试的标记。"""
        manager = make_manager()
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "tasks.db"))
        runtime = PluginRuntime("memory", manager.session_id, "run", supervisor, supervisor.runtime_queue)
        try:
            await supervisor.begin_shutdown()
            await start_diary(manager, runtime)
            assert await supervisor.list_tasks(manager.session_id) == []
            generation = await make_manager().prepare_diary(max_history_id=42)
            assert generation is not None
            generation.release()
        finally:
            await supervisor.close()
    asyncio.run(scenario())


def test_cancelled_diary_submission_keeps_registered_task_reserved(tmp_path: Path, monkeypatch) -> None:
    """提交方在登记中被重复取消，已接管的日记仍保持去重，直到任务清理。"""
    async def scenario() -> None:
        """挂起任务登记，取消提交方并重复激活，确保始终只有一个任务。"""
        manager = make_manager()
        registering, allow_register, generating = asyncio.Event(), asyncio.Event(), asyncio.Event()
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "tasks.db"))
        runtime = PluginRuntime("memory", manager.session_id, "run", supervisor, supervisor.runtime_queue)
        original_insert = supervisor.records.insert_running

        async def insert(*args, **kwargs) -> None:
            """暂停实际登记，让调用方的取消发生在任务交接过程中。"""
            registering.set()
            await allow_register.wait()
            await original_insert(*args, **kwargs)

        async def generate(*args, **kwargs) -> None:
            """让已经登记的日记持续生成，直到任务管理器停止。"""
            generating.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(supervisor.records, "insert_running", insert)
        manager.diary_memory.generate.side_effect = generate
        work = asyncio.create_task(start_diary(manager, runtime))
        try:
            await asyncio.wait_for(registering.wait(), 1)
            work.cancel()
            await asyncio.sleep(0)
            work.cancel()
            await start_diary(make_manager(), runtime)
            allow_register.set()
            with pytest.raises(asyncio.CancelledError):
                await work
            await asyncio.wait_for(generating.wait(), 1)
            await start_diary(make_manager(), runtime)
            assert len(await supervisor.list_tasks(manager.session_id)) == 1
        finally:
            allow_register.set()
            await asyncio.gather(work, return_exceptions=True)
            await supervisor.close()
        generation = await make_manager().prepare_diary(max_history_id=42)
        assert generation is not None
        generation.release()
    asyncio.run(scenario())


def test_diary_reservations_are_isolated_by_session_and_date() -> None:
    """不同会话和日期独立处理，重复清理旧任务不会释放同一天的新任务标记。"""
    async def scenario() -> None:
        """准备多个日记后按次序释放，并检查每个日期独立去重。"""
        first, other_session, other_date = make_manager(), make_manager("other"), make_manager()
        other_date.diary_memory.chat_history_store.get_last_chat_date.return_value = date(2026, 10, 24)
        generations = await asyncio.gather(*(manager.prepare_diary(max_history_id=42) for manager in (
            first, other_session, other_date,
        )))
        try:
            assert all(generation is not None for generation in generations)
            assert await make_manager().prepare_diary(max_history_id=42) is None
            generations[0].release()
            retry = await make_manager().prepare_diary(max_history_id=42)
            try:
                assert retry is not None
                generations[0].release()
                assert await make_manager().prepare_diary(max_history_id=42) is None
            finally:
                retry.release()
        finally:
            for generation in generations:
                generation.release()
    asyncio.run(scenario())


def test_diary_stopped_before_run_releases_reservation(tmp_path: Path, monkeypatch) -> None:
    """生成尚未启动就收到停止请求时，也由任务清理释放日期标记。"""
    async def scenario() -> None:
        """登记后立即停止，确认没有调用模型并可重新准备同一天的日记。"""
        manager = make_manager()
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=str(tmp_path / "tasks.db"))
        runtime = PluginRuntime("memory", manager.session_id, "run", supervisor, supervisor.runtime_queue)
        allow_run = asyncio.Event()
        original_lifecycle = supervisor._run_task_lifecycle

        async def lifecycle(*args) -> None:
            """暂停任务执行，让停止请求确定发生在业务启动之前。"""
            await allow_run.wait()
            await original_lifecycle(*args)

        monkeypatch.setattr(supervisor, "_run_task_lifecycle", lifecycle)
        generation = await manager.prepare_diary(max_history_id=42)
        try:
            task_id = await runtime.submit_task(
                DiaryTask(manager, generation), name="memory.check_diary", title="生成日记",
                lifetime=TaskLifetime.INDEPENDENT, notify_agent=False,
            )
            await supervisor.stop_task(task_id)
            allow_run.set()
            assert (await runtime.wait_task(task_id, timeout_ms=1000)).status == TaskStatus.STOPPED
            manager.diary_memory.generate.assert_not_awaited()
            retry = await make_manager().prepare_diary(max_history_id=42)
            assert retry is not None
            retry.release()
        finally:
            allow_run.set()
            await supervisor.close()
    asyncio.run(scenario())
