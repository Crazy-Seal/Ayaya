from tests.memory_support import bind_memory_manager
from app.crud.sqlite_factory import SqliteFactory
"""验证记忆查询工具由记忆插件提供，沿用插件配置、执行与审批流程。"""

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.plugins.memory import MemoryPlugin
from app.agent.runtime import agent_factory
from app.agent.state import AgentState
from app.agent.tools.authorization.catalog import LocalToolCatalogAdapter
from app.agent.tools.authorization.manager import ToolAuthorization, descriptor_for_tool
from app.agent.tools.authorization.models import AuthorizationRequired, Granted, ToolIdentity, ToolInvocation
from app.agent.tools.core.base import ToolContext, ToolResult
from app.agent.tools.core.registry import ToolRegistry
from app.crud.chat_settings_dao import ChatSettingsDao
from app.schemas.chat_settings import ChatSettings
from app.schemas.tool_permissions import ToolPolicy
from app.services.chat_settings_service import ChatSettingsService
from app.services.plugin_service import PluginService


MEMORY_TOOL_NAMES = {"search_memory", "search_diary"}


@pytest.mark.asyncio
@pytest.mark.parametrize("is_child", [False, True])
async def test_memory_queries_are_exposed_only_to_parent_agents(tmp_path: Path, repl_service, is_child: bool) -> None:
    """正式工厂装配记忆插件后，查询工具只向主 Agent 提供。

    Args:
        tmp_path (Path): 本用例隔离的数据库目录。
        repl_service (ReplService): 尚未启动执行进程的应用 REPL。
        is_child (bool): 是否装配子 Agent。
    """
    from datetime import datetime, timezone
    from app.schemas.subagent_types import SubagentDefinition
    from tests.memory_support import memory_settings

    settings = memory_settings()
    identities = frozenset(ToolIdentity.local(name, plugin_name="memory") for name in MEMORY_TOOL_NAMES)
    definition = SubagentDefinition(
        "child", settings.session_id, "助手", "完成委派任务", identities, True, datetime.now(timezone.utc),
    ) if is_child else None
    agent = agent_factory.build_agent(
        settings, subagent=definition, sqlite_factory=SqliteFactory(tmp_path / "state.sqlite3"),
        repl_service=repl_service,
    )
    try:
        await agent.initialize()
        queries = [tool for tool in agent.tool_manager.get_all()
                   if getattr(getattr(tool, "identity", None), "plugin_name", None) == "memory"]
        assert {tool.identity for tool in queries} == identities
        exposed = {tool.name for tool in agent.tool_manager.get_tools()}
        for tool in queries:
            assert (tool.name in exposed) is (not is_child)
            assert (agent.tool_manager.get(tool.name) is tool) is (not is_child)
            assert tool.name not in agent.tool_manager.get_delegatable_tool_identities()
        for name in ("python", "python_reset"):
            assert agent.tool_manager.has(name)
            assert (name in exposed) is (not is_child)
            assert (agent.tool_manager.get(name) is not None) is (not is_child)
    finally:
        await agent.close()


def test_memory_queries_are_declared_only_by_memory_plugin() -> None:
    """插件声明两个查询工具，独立工具注册表不再发现或装配它们。"""
    assert {tool.name for tool in MemoryPlugin.tools} == MEMORY_TOOL_NAMES
    assert all(not ToolRegistry.has(name) for name in MEMORY_TOOL_NAMES)
    assert all(ToolRegistry.get(name) is None for name in MEMORY_TOOL_NAMES)


def test_memory_query_permissions_and_catalog_belong_to_plugin(tmp_path: Path) -> None:
    """新权限只补到记忆插件，设置目录也仅返回带记忆插件归属的查询工具。

    Args:
        tmp_path (Path): Pytest 提供的隔离会话配置目录。
    """
    path = tmp_path / "settings.yaml"
    path.write_text("chat_models: []\n", encoding="utf-8")
    service = ChatSettingsService(ChatSettingsDao(path))
    settings = ChatSettings(session_id="memory-catalog", model_name="test", openai_api_key="test",
                            openai_base_url="http://localhost", temperature=0.7, system_prompt="test",
                            context_strategy={}, agent_plugins={"memory": {"enabled": True}})
    normalized = service.normalize(settings)
    assert all(f"local:{name}" not in normalized.tool_permissions for name in MEMORY_TOOL_NAMES)
    assert normalized.agent_plugins[MemoryPlugin.name].tool_permissions == {
        f"local:{name}": ToolPolicy.ASK for name in MEMORY_TOOL_NAMES
    }
    catalog = LocalToolCatalogAdapter(PluginService().local_tools)
    queries = [entry for entry in catalog.list_entries(normalized) if entry.name in MEMORY_TOOL_NAMES]
    assert {entry.name for entry in queries} == MEMORY_TOOL_NAMES
    assert all(entry.plugin_name == MemoryPlugin.name and entry.available for entry in queries)
    assert {entry.stable_key for entry in queries} == {
        ToolIdentity.local(name, plugin_name=MemoryPlugin.name).stable_key for name in MEMORY_TOOL_NAMES
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled, policy", [(False, ToolPolicy.ALLOW), (True, ToolPolicy.ASK),
                                             (True, ToolPolicy.ALLOW), (True, ToolPolicy.DENY)])
async def test_memory_queries_follow_plugin_switch_and_permissions(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, policy: ToolPolicy,
 repl_service) -> None:
    """真实 Agent 装配查询工具，执行使用会话编号，审批和停用继续检查插件权限。

    Args:
        repl_service (ReplService): 测试 fixture 提供的懒启动原生 Python 资源。
        monkeypatch (pytest.MonkeyPatch): 替换记忆服务以保持测试离线且不访问生产存储。
        enabled (bool): 当前会话是否启用记忆插件。
        policy (ToolPolicy): 两个查询工具在插件卡片中保存的权限。
    """
    from app.agent.plugins.memory import manager as memory_manager_module

    memory = SimpleNamespace(search=AsyncMock(return_value="用户喜欢音乐"),
                             search_diary=AsyncMock(return_value="今天完成了任务"))
    sessions = []

    def get_memory(session_id: str, sqlite_factory: SqliteFactory):
        """记录工具使用的会话并返回隔离的记忆查询服务。

        Args:
            session_id (str): 工具执行时绑定的会话编号。

        Returns:
            SimpleNamespace: 仅提供异步记忆和日记查询的测试服务。
        """
        sessions.append(session_id)
        return memory

    bind_memory_manager(monkeypatch, lambda self, session: get_memory(session, self.sqlite_factory))
    settings = ChatSettings(session_id="memory-tools", model_name="test", openai_api_key="test",
                            openai_base_url="http://localhost", temperature=0.7, system_prompt="test",
                            context_strategy={}, agent_plugins={MemoryPlugin.name: {
                                "enabled": enabled,
                                "tool_permissions": {f"local:{name}": policy for name in MEMORY_TOOL_NAMES},
                            }})
    authorization = ToolAuthorization(lambda _: settings)
    agent = agent_factory.build_agent(settings, tool_authorization=authorization, sqlite_factory=SqliteFactory(), repl_service=repl_service)
    try:
        await agent.initialize()
        queries = {tool.identity.name: tool for tool in agent.tool_manager.get_all()
                   if getattr(getattr(tool, "identity", None), "plugin_name", None) == MemoryPlugin.name}
        assert set(queries) == (MEMORY_TOOL_NAMES if enabled else set())
        assert all(not agent.tool_manager.has(name) for name in MEMORY_TOOL_NAMES)
        if not enabled:
            assert sessions == []
            return

        state = AgentState.create_new(settings.session_id)
        context = ToolContext(session_id=settings.session_id, state=state)
        arguments = {"search_memory": {"query": "  音乐  "},
                     "search_diary": {"start": "2026-10-20", "end": "2026-10-22"}}
        exposed = {definition.name for definition in agent.tool_manager.get_tools()}
        for name, tool in queries.items():
            assert (tool.name in exposed) is (policy is not ToolPolicy.DENY)
            assert tool.compressible
            invocation = ToolInvocation(session_id=settings.session_id, tool_call_id=f"query-{name}",
                                        descriptor=descriptor_for_tool(tool), arguments=arguments[name])
            outcome = authorization.authorize(invocation)
            if policy is ToolPolicy.DENY:
                assert isinstance(outcome, ToolResult)
                continue
            if policy is ToolPolicy.ASK:
                assert isinstance(outcome, AuthorizationRequired)
                approved = authorization.resolve_authorization(invocation, decision={"kind": "approve"},
                                                                authorization_snapshot=outcome.resume_state)
                assert isinstance(approved, Granted)
                # 1. 审批后停用插件，已经创建的工具和旧审批材料也必须失去权限。
                settings.agent_plugins[MemoryPlugin.name].enabled = False
                assert isinstance(authorization.resolve_authorization(
                    invocation, decision={"kind": "approve"}, authorization_snapshot=outcome.resume_state,
                ), ToolResult)
                settings.agent_plugins[MemoryPlugin.name].enabled = True
            else:
                assert isinstance(outcome, Granted)
            result = await tool.execute(arguments[name], context)
            assert ("用户喜欢音乐" if name == "search_memory" else "今天完成了任务") in result.text

        if policy is not ToolPolicy.DENY:
            assert sessions == [settings.session_id] * 2
            memory.search.assert_awaited_once_with(query="音乐", memory_type="all", top_k=5)
            memory.search_diary.assert_awaited_once_with(date(2026, 10, 20), date(2026, 10, 22))
        else:
            assert sessions == []
    finally:
        await agent.close()
