"""为记忆行为测试提供显式配置和外部资源替身。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.agent.plugins.base import PluginContext
from app.agent.plugins.memory import MemoryPlugin
from app.agent.plugins.memory.operation import MemoryOperation
from app.agent.plugins.memory.state import MemoryState
from app.crud.sqlite_factory import SqliteFactory
from app.schemas.chat_settings import ChatSettings


def memory_settings(session_id: str = "session") -> ChatSettings:
    """创建离线测试用的完整会话快照。

    Args:
        session_id (str): 本次测试会话。

    Returns:
        ChatSettings: 使用本机占位地址的模型和角色配置。
    """
    return ChatSettings(session_id=session_id, model_name="test", openai_api_key="test",
                        openai_base_url="http://localhost", temperature=0.7, system_prompt="test",
                        context_strategy={}, agent_plugins={"memory": {"enabled": True}})


def make_memory_plugin(*, sqlite_factory: SqliteFactory, **config) -> MemoryPlugin:
    """经正式构造契约创建插件，测试显式提供会话快照。

    Args:
        sqlite_factory (SqliteFactory): 隔离的存储配置。
        config (dict): 本场景的记忆开关。

    Returns:
        MemoryPlugin: 尚未打开资源的插件。
    """
    return MemoryPlugin(context=PluginContext(sqlite_factory, memory_settings()), **config)


def memory_count(state) -> int:
    """读取 checkpoint 中记忆插件尚未消费的计数。

    Args:
        state (AgentState): 当前场景加载的状态。

    Returns:
        int: 保存的真人输入计数。
    """
    return MemoryState.load(state).long_term_memory_turn_counter


def set_memory_count(state, count: int) -> None:
    """为恢复和迁移场景准备已有记忆批次。

    Args:
        state (AgentState): 即将保存的测试状态。
        count (int): 已有批次的真人输入数量。
    """
    memory = MemoryState.load(state)
    memory.long_term_memory_turn_counter = count
    memory.save(state)


def operation_for(manager):
    """让任务测试替换资源创建边界，保留实际提取、调度和任务清理。

    Args:
        manager (MemoryManager): 已装配外部模型或存储替身的业务对象。

    Returns:
        SimpleNamespace: 具有操作上下文入口的测试依赖。
    """
    @asynccontextmanager
    async def open_operation(session_id, **capabilities):
        """在本次调用范围提供场景的业务对象。

        Args:
            session_id (str): 本次实际调用的会话。
            capabilities (dict): 当前操作需要的记忆类别。

        Yields:
            MemoryManager: 已由测试控制外部资源的对象。
        """
        yield manager
    return SimpleNamespace(open=open_operation)


def bind_memory_manager(monkeypatch, locate) -> None:
    """替换操作内资源创建，按实际会话交付外部记忆替身。

    Args:
        monkeypatch (pytest.MonkeyPatch): 用例结束后恢复入口。
        locate (Callable): 使用会话编号取得本场景记忆对象。
    """
    @asynccontextmanager
    async def open_operation(self, session_id, **capabilities):
        """在每次检索或后台任务中定位当前场景的业务替身。

        Args:
            session_id (str): 实际调用会话。
            capabilities (dict): 本次所需能力。

        Yields:
            MemoryManager: 业务替身。
        """
        yield locate(self, session_id)
    monkeypatch.setattr(MemoryOperation, "open", open_operation)
