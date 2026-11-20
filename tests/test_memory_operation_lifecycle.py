"""验证操作配置跨轮次稳定、按需打开资源和目录锁的实际释放顺序。"""

import asyncio
from datetime import date
import gc
from pathlib import Path
from types import SimpleNamespace
import weakref
from threading import Event

import pytest

from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.plugins.base import HookContext, PluginHook
from app.schemas.runtime_types import CheckpointType
from app.agent.plugins.memory.config import MemoryConfig, MemoryPluginConfig
from app.agent.plugins.memory.memories.diary import DiaryGeneration
from app.agent.plugins.memory.operation import MemoryOperation
from app.agent.plugins.memory import operation as operation_module
from app.agent.plugins.memory import manager as manager_module
from app.agent.plugins.memory.memories import episodic as episodic_module
from app.agent.plugins.memory.tasks.memory_tasks import DiaryTask
from app.agent.state import AgentState
from app.agent.state.messages import HumanMessage, text_content
from app.agent.utils.infra.owned_work import complete_owned_work
from app.crud.sqlite_factory import SqliteFactory
from tests.memory_support import bind_memory_manager, make_memory_plugin, memory_count, memory_settings, set_memory_count


def test_failed_sqlite_initialization_does_not_open_chroma(monkeypatch, tmp_path: Path) -> None:
    """情景记忆建表失败时，尚未创建需要长期关闭的向量客户端。

    Args:
        monkeypatch (pytest.MonkeyPatch): 替换本次初始化的数据库和向量客户端。
        tmp_path (Path): 本用例独立的存储目录。
    """
    from unittest.mock import Mock

    chroma = Mock()
    monkeypatch.setattr(episodic_module, "EpisodicChromaStore", chroma)
    monkeypatch.setattr(episodic_module, "EpisodicSqliteStore", Mock(side_effect=OSError("SQLite unavailable")))
    with pytest.raises(OSError, match="SQLite unavailable"):
        episodic_module.EpisodicMemory(
            "session", MemoryConfig(), memory_settings(),
            sqlite_factory=SqliteFactory(tmp_path / "state.sqlite3"),
        )
    chroma.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["search", "add"])
@pytest.mark.parametrize("ending", ["error", "branch_cancel", "caller_cancel"])
async def test_parallel_failure_waits_for_sibling_before_closing(
    monkeypatch, tmp_path: Path, action: str, ending: str,
) -> None:
    """一个分支失败或自行取消后，另一分支结束并关闭资源才放行同目录操作。

    Args:
        monkeypatch (pytest.MonkeyPatch): 用可控制执行进度的记忆组件替代外部服务。
        tmp_path (Path): 两次操作共同使用的隔离目录。
        action (str): 本次执行记忆检索或提取。
        ending (str): 情景分支报错、自行取消或调用者重复取消。
    """
    started, failed = asyncio.Event(), asyncio.Event()
    finish = Event()
    loop = asyncio.get_running_loop()
    events: list[str] = []

    async def fail(*args, **kwargs):
        """等语义工作启动后，让情景分支失败或取消。"""
        await started.wait()
        failed.set()
        if ending == "caller_cancel":
            await asyncio.Event().wait()
        if ending == "branch_cancel":
            raise asyncio.CancelledError
        raise RuntimeError("episodic failed")

    def semantic_work():
        """在线程中保持语义工作运行，完成后返回检索或提取结果。"""
        loop.call_soon_threadsafe(started.set)
        finish.wait()
        events.append("semantic-finished")
        return [] if action == "search" else ["saved"]

    async def run_semantic(*args, **kwargs):
        """沿用语义组件的线程等待方式，收到取消时仍等待实际工作结束。"""
        return await complete_owned_work(asyncio.to_thread(semantic_work))

    async def close_episodic():
        """记录操作退出时关闭情景组件的顺序。"""
        events.append("episodic-closed")

    async def close_semantic():
        """记录操作退出时关闭语义组件的顺序。"""
        events.append("semantic-closed")

    from unittest.mock import Mock
    monkeypatch.setattr(manager_module, "EpisodicMemory", Mock(return_value=SimpleNamespace(
        search=fail, add=fail, close=close_episodic,
    )))
    monkeypatch.setattr(manager_module, "Mem0SemanticMemory", Mock(return_value=SimpleNamespace(
        search=run_semantic, add=run_semantic, close=close_semantic,
    )))
    operation = MemoryOperation(
        memory_settings(), MemoryConfig(mem0_qdrant_path=str(tmp_path / "qdrant")),
        MemoryPluginConfig(), SqliteFactory(tmp_path / "state.sqlite3"),
    )

    async def first():
        """从正式操作上下文执行并行工作，退出时由上下文关闭资源。"""
        async with operation.open("first", episodic=True, semantic=True) as manager:
            if action == "search":
                return await manager.search("query", "all")
            return await manager.add([HumanMessage(content=text_content("用户消息"))])

    async def second():
        """等待前次工作和资源关闭后，再进入相同语义目录。"""
        async with operation.open("second", semantic=True):
            events.append("second-opened")

    work = asyncio.create_task(first())
    following = None
    try:
        await asyncio.wait_for(failed.wait(), 1)
        if ending == "caller_cancel":
            work.cancel()
            await asyncio.sleep(0)
            work.cancel()
        following = asyncio.create_task(second())
        # 1. 让分支结束和同目录排队均得到调度，仍在运行的工作应继续占有资源。
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert events == []
        assert not work.done() and not following.done()
    finally:
        # 2. 释放语义分支并收回全部任务，断言失败时也能结束测试资源。
        finish.set()
        tasks = [work] if following is None else [work, following]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    error = outcomes[0]
    if ending != "error":
        assert isinstance(error, asyncio.CancelledError)
    else:
        assert isinstance(error, RuntimeError)
        assert "episodic failed" in str(error)
        if action == "add":
            assert "部分失败" in str(error) and "语义记忆：处理 1 条" in str(error)
    assert outcomes[1] is None
    assert events.index("semantic-finished") < events.index("semantic-closed")
    assert events.index("semantic-finished") < events.index("episodic-closed")
    assert events.index("semantic-closed") < events.index("second-opened")


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["EpisodicMemory", "Mem0SemanticMemory"])
async def test_failed_extractor_construction_is_reported_once(monkeypatch, tmp_path: Path, component: str) -> None:
    """提取组件构造失败只尝试一次，另一组件仍完成并保留已保存数量。

    Args:
        monkeypatch (pytest.MonkeyPatch): 指定本次构造失败的组件。
        tmp_path (Path): 当前用例的独立存储目录。
        component (str): 构造失败的情景或语义组件。
    """
    from unittest.mock import AsyncMock, Mock

    successful = SimpleNamespace(add=AsyncMock(return_value=["saved"]), close=AsyncMock())
    failed = Mock(side_effect=OSError("storage unavailable"))
    monkeypatch.setattr(manager_module, "EpisodicMemory", Mock(return_value=successful))
    monkeypatch.setattr(manager_module, "Mem0SemanticMemory", Mock(return_value=successful))
    monkeypatch.setattr(manager_module, component, failed)
    operation = MemoryOperation(
        memory_settings(), MemoryConfig(mem0_qdrant_path=str(tmp_path / "qdrant")),
        MemoryPluginConfig(), SqliteFactory(tmp_path / "state.sqlite3"),
    )
    async with operation.open("session", episodic=True, semantic=True) as manager:
        with pytest.raises(RuntimeError, match="部分失败.*storage unavailable") as error:
            await manager.add([HumanMessage(content=text_content("用户消息"))])
    label = "语义记忆：处理 1 条" if component == "EpisodicMemory" else "情景记忆：已保存 1 条"
    assert label in str(error.value)
    failed.assert_called_once()
    successful.add.assert_awaited_once()
    successful.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_submitted_task_keeps_snapshot_after_agent_closes(monkeypatch, tmp_path: Path) -> None:
    """原 Agent 被关闭和回收、配置被修改后，任务继续使用提交时模型和环境。"""
    seen = []

    class Manager:
        """记录后台任务实际打开资源时收到的配置。"""

        def __init__(self, session_id, config, settings, *, options, sqlite_factory):
            """记录本次任务的独立配置材料。"""
            seen.append((session_id, settings.model_name, settings.name, config.semantic_extraction_model, options))

        async def generate_diary(self, generation):
            """模拟任务保存已经固定日期的日记。"""
            return generation.target_date.isoformat()

        async def close(self):
            """记录本次任务已关闭其业务资源。"""
            seen.append("closed")

    monkeypatch.setattr(operation_module, "MemoryManager", Manager)
    monkeypatch.setenv("MEM0_EXTRACTION_MODEL", "old-extractor")
    settings = memory_settings()
    settings.name = "旧角色"
    agent = Agent(AgentConfig("session", "test", "test", ContextStrategyConfig(),
                              plugins=[{"name": "memory", "config": {}}]),
                  SqliteFactory(tmp_path / "state.sqlite3"), chat_settings=settings)
    await agent.initialize()
    plugin = agent.plugin_manager.get_plugin("memory")
    task = DiaryTask(plugin._operation, DiaryGeneration("session", date(2026, 11, 1), [], 7))
    reference = weakref.ref(agent)
    await agent.close()
    del agent, plugin
    gc.collect()
    assert reference() is None
    settings.model_name = "new-model"
    settings.name = "新角色"
    monkeypatch.setenv("MEM0_EXTRACTION_MODEL", "new-extractor")
    assert await task.run(SimpleNamespace()) == "2026-11-01"
    assert seen[0][:4] == ("session", "test", "旧角色", "old-extractor")
    assert seen[0][4].enable_diary and not seen[0][4].enable_semantic
    assert seen[-1] == "closed"


@pytest.mark.asyncio
async def test_directory_waits_for_cancelled_operation_cleanup(monkeypatch, tmp_path: Path) -> None:
    """重复取消清理等待时，后续操作仍等待实际关闭完成才取得相同目录。"""
    opened = []
    closing, release_close, active = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Manager:
        """提供可暂停的关闭过程，以观察目录锁的所有权。"""

        def __init__(self, session_id, *args, **kwargs):
            """记录资源何时开始创建。"""
            self.session_id = session_id
            opened.append(session_id)

        async def close(self):
            """首个操作收到取消后，等待实际资源关闭完成。"""
            if self.session_id == "first":
                closing.set()
                await release_close.wait()

    monkeypatch.setattr(operation_module, "MemoryManager", Manager)
    operation = MemoryOperation(memory_settings(), MemoryConfig(mem0_qdrant_path=str(tmp_path)),
                                MemoryPluginConfig(), SqliteFactory(tmp_path / "state.sqlite3"))

    async def first():
        """占用目录并等待测试取消。"""
        async with operation.open("first", semantic=True):
            active.set()
            await asyncio.Event().wait()

    async def second():
        """排队后打开相同目录，随后正常关闭。"""
        async with operation.open("second", semantic=True):
            return "done"

    task = asyncio.create_task(first())
    await active.wait()
    task.cancel()
    await closing.wait()
    following = asyncio.create_task(second())
    try:
        task.cancel()
        await asyncio.sleep(0)
        assert opened == ["first"]
        assert not following.done()
    finally:
        release_close.set()
        result = await asyncio.gather(task, following, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert result[1] == "done"
    assert opened == ["first", "second"]


@pytest.mark.asyncio
async def test_failed_resource_creation_releases_directory(monkeypatch, tmp_path: Path) -> None:
    """资源创建失败后，相同目录的下一次操作可以正常打开。"""
    attempts = []

    class Manager:
        """首次创建失败，后续创建成功。"""

        def __init__(self, session_id, *args, **kwargs):
            """模拟本地客户端初始化失败。"""
            attempts.append(session_id)
            if session_id == "failed":
                raise RuntimeError("open failed")

        async def close(self):
            """正常操作结束时释放资源。"""

    monkeypatch.setattr(operation_module, "MemoryManager", Manager)
    operation = MemoryOperation(memory_settings(), MemoryConfig(mem0_qdrant_path=str(tmp_path)),
                                MemoryPluginConfig(), SqliteFactory(tmp_path / "state.sqlite3"))
    with pytest.raises(RuntimeError, match="open failed"):
        async with operation.open("failed", semantic=True):
            pass
    async with asyncio.timeout(1):
        async with operation.open("retry", semantic=True):
            pass
    assert attempts == ["failed", "retry"]


@pytest.mark.asyncio
async def test_disabled_plugin_preserves_counter_then_enabled_plugin_continues(tmp_path: Path) -> None:
    """禁用期间输入保存保留原计数，重新启用后的真人输入接着累计。"""
    from app.agent.core.plugin_manager import PluginManager
    from app.agent.state.manager import SessionStateManager

    state = AgentState.create_new("counting")
    set_memory_count(state, 4)
    factory = SqliteFactory(tmp_path / "state.sqlite3")
    manager = SessionStateManager("counting", factory)
    plugins = PluginManager()
    try:
        manager.append_human_input(state, "禁用期间的消息", None)
        await plugins.run_hooks(PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data={})
        await manager.save(state, checkpoint_type=CheckpointType.INTERMEDIATE)
        state = await manager.load()
        assert memory_count(state) == 4
        plugins.register(make_memory_plugin(sqlite_factory=factory, enable_diary=False,
                                            enable_episodic=False, enable_semantic=False))
        manager.append_human_input(state, "重新启用后的消息", None)
        await plugins.run_hooks(PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state, data={})
        await manager.save(state, checkpoint_type=CheckpointType.INTERMEDIATE)
        assert memory_count(await manager.load()) == 5
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_real_qdrant_client_can_reopen_after_semantic_close(tmp_path: Path) -> None:
    """实际本地 Qdrant 客户端关闭后，后续操作能再次打开同一路径。"""
    from qdrant_client import QdrantClient
    from unittest.mock import Mock
    from app.agent.plugins.memory.memories.semantic_mem0 import Mem0SemanticMemory

    semantic = object.__new__(Mem0SemanticMemory)
    semantic._memory = SimpleNamespace(
        vector_store=SimpleNamespace(client=QdrantClient(path=str(tmp_path / "qdrant"))),
        llm=SimpleNamespace(client=Mock()), embedding_model=SimpleNamespace(client=Mock()), close=Mock(),
    )
    await semantic.close()
    semantic._memory.llm.client.close.assert_called_once()
    semantic._memory.embedding_model.client.close.assert_called_once()
    semantic._memory.close.assert_called_once()
    reopened = QdrantClient(path=str(tmp_path / "qdrant"))
    try:
        assert reopened.get_collections().collections == []
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_real_mem0_operation_releases_storage_for_next_operation(tmp_path: Path) -> None:
    """实际 Mem0 查询退出后，历史库与向量目录可以由下一次操作重新打开。"""
    config = MemoryConfig(
        mem0_qdrant_path=str(tmp_path / "qdrant"), embedding_api_key="test",
        embedding_model="test", embedding_base_url="http://127.0.0.1:1/v1",
        embedding_dimension=3, semantic_extraction_api_key="test",
        semantic_extraction_base_url="http://127.0.0.1:1/v1",
    )
    operation = MemoryOperation(memory_settings(), config, MemoryPluginConfig(), SqliteFactory(tmp_path / "state.sqlite3"))
    for _ in range(2):
        async with operation.open("session", semantic=True) as manager:
            assert await manager.semantic_memory.get_all() == []
    assert (tmp_path / "history.db").is_file()


@pytest.mark.asyncio
async def test_real_chroma_close_preserves_other_client_and_allows_reopen(tmp_path: Path) -> None:
    """两个索引客户端共享目录时，关闭一个仍可使用另一个，全部关闭后可重新打开。"""
    from app.agent.plugins.memory.store.episodic_chroma_store import EpisodicChromaStore
    from app.agent.models.embedding_client import EmbeddingConfig

    config = EmbeddingConfig(api_key="test", base_url="http://127.0.0.1:1", model="test")
    first = EpisodicChromaStore("memory_close_test", config, str(tmp_path))
    second = EpisodicChromaStore("memory_close_test", config, str(tmp_path))
    try:
        await first.close()
        assert await second.count() == 0
    finally:
        await second.close()
    reopened = EpisodicChromaStore("memory_close_test", config, str(tmp_path))
    try:
        assert await reopened.count() == 0
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_failed_batch_preparation_keeps_old_count_and_adds_current_human(monkeypatch) -> None:
    """历史准备失败时保留到期批次，同时把将要提交的真人输入计入同一状态。"""
    from unittest.mock import AsyncMock
    manager = SimpleNamespace(get_protocol_history_cutoff=AsyncMock(side_effect=OSError("history unavailable")))
    bind_memory_manager(monkeypatch, lambda operation, session: manager)
    plugin = make_memory_plugin(sqlite_factory=SqliteFactory(), enable_diary=False, enable_semantic=False)
    state = AgentState.create_new("session")
    set_memory_count(state, 10)
    state.add_user_message("本次真人输入")
    data = {}
    await plugin.execute(HookContext.create(PluginHook.BEFORE_ACTIVATION_INPUT_COMMIT, state,
                                            data))
    assert memory_count(state) == 11
    assert data == {}


@pytest.mark.asyncio
async def test_failed_sdk_construction_closes_acquired_qdrant(monkeypatch, tmp_path: Path) -> None:
    """Mem0 装配失败时，已经取得的 Qdrant 目录仍可立即重新打开。"""
    from unittest.mock import Mock
    from qdrant_client import QdrantClient
    from app.agent.plugins.memory.memories import semantic_mem0

    monkeypatch.setattr(semantic_mem0, "Memory", SimpleNamespace(from_config=Mock(side_effect=RuntimeError("SDK init failed"))))
    config = MemoryConfig(mem0_qdrant_path=str(tmp_path / "qdrant"))
    with pytest.raises(RuntimeError, match="SDK init failed"):
        semantic_mem0._create_mem0(config)
    reopened = QdrantClient(path=config.mem0_qdrant_path)
    try:
        assert reopened.get_collections().collections == []
    finally:
        reopened.close()


def test_authorization_requires_explicit_plugin_enablement(tmp_path: Path) -> None:
    """缺少插件记录时拒绝调用，显式启用后可用，再次关闭后立即拒绝。

    Args:
        tmp_path (Path): 本用例独立的会话配置目录。
    """
    from app.agent.tools.authorization.manager import ToolAuthorization
    from app.crud.chat_settings_dao import ChatSettingsDao
    from app.schemas.chat_settings import AgentPluginSettings
    from app.schemas.tool_identity import ToolIdentity
    from app.schemas.tool_permissions import ToolPolicy

    path = tmp_path / "settings.yaml"
    path.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(path)
    settings = memory_settings()
    settings.agent_plugins = {}
    dao.add_chat_settings(settings)
    authorization = ToolAuthorization(dao.get_chat_settings)
    identity = ToolIdentity.local("search_diary", plugin_name="memory")
    assert authorization.get_policy("session", identity) == ToolPolicy.DENY
    settings.agent_plugins = {"memory": AgentPluginSettings(enabled=True)}
    dao.update_chat_settings("session", settings)
    assert authorization.get_policy("session", identity) == ToolPolicy.ASK
    settings.agent_plugins = {"memory": AgentPluginSettings(enabled=False)}
    dao.update_chat_settings("session", settings)
    assert authorization.get_policy("session", identity) == ToolPolicy.DENY
