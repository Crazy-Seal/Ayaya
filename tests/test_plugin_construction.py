"""通过实际插件装配验证依赖注入、配置快照和具体插件的可移除性。"""

import builtins
from pathlib import Path
from textwrap import dedent

import pytest

from app.agent.agent import Agent, AgentConfig
from app.agent.plugins.base import BasePlugin, HookContext, PluginContext, PluginHook
from app.agent.plugins.registry import PluginRegistry
from app.agent.plugins.tool_adapter import PluginToolAdapter
from app.agent.state import AgentState
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.crud.sqlite_factory import SqliteFactory
from app.schemas.chat_settings import ChatSettings


def make_agent(settings: ChatSettings, factory: SqliteFactory, plugins: list[str]) -> Agent:
    """用隔离存储创建尚未初始化的 Agent，配置变更由各测试控制。

    Args:
        settings (ChatSettings): 创建时的会话设置。
        factory (SqliteFactory): 当前测试独立的数据库连接工厂。
        plugins (list[str]): 本次启用的测试插件名称。

    Returns:
        Agent: 等待创建插件的执行对象。
    """
    return Agent(AgentConfig(
        session_id=settings.session_id, model_name=settings.model_name,
        api_key=settings.openai_api_key, base_url=settings.openai_base_url,
        context_strategy=settings.context_strategy, plugins=plugins,
    ), sqlite_factory=factory, chat_settings=settings)


def make_settings() -> ChatSettings:
    """提供离线构造和快照验证所需的完整会话设置。

    Returns:
        ChatSettings: 使用本机占位模型地址的会话配置。
    """
    return ChatSettings(
        session_id="construction", model_name="old-model", openai_api_key="test",
        openai_base_url="http://127.0.0.1:1/v1", temperature=0.1,
        system_prompt="测试", context_strategy={},
    )


@pytest.mark.asyncio
async def test_external_plugin_works_when_memory_cannot_be_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """记忆包不可导入时，独立插件仍能装配、执行钩子并使用宿主指定的数据库。

    Args:
        tmp_path (Path): 本次测试的插件目录和数据库位置。
        monkeypatch (pytest.MonkeyPatch): 隔离注册表并模拟记忆插件已移除。
    """
    package = tmp_path / "external"
    package.mkdir()
    (package / "__init__.py").write_text(dedent('''
        from app.agent.plugins.base import BasePlugin, PluginHook

        class ExternalPlugin(BasePlugin):
            """用宿主配置将当前模型记录到插件自己的业务表。"""
            name = "external"
            hooks = [PluginHook.BEFORE_LLM]

            def __init__(self, *, context):
                """绑定本次会话依赖。

                Args:
                    context (PluginContext): 本次配置快照和数据库工厂。
                """
                self._settings = context.chat_settings
                self._factory = context.sqlite_factory

            async def execute(self, context):
                """保存当前模型并向 Agent 暴露执行结果。

                Args:
                    context (HookContext): 当前钩子的会话状态。
                """
                async with self._factory.connection() as connection:
                    await connection.execute("CREATE TABLE plugin_models (name TEXT)")
                    await connection.execute("INSERT INTO plugin_models VALUES (?)", (self._settings.model_name,))
                    await connection.commit()
                context.agent_state.set_extra("external.model", self._settings.model_name)
    '''), encoding="utf-8")
    monkeypatch.setattr(PluginRegistry, "_plugins", {})
    monkeypatch.setattr(PluginRegistry, "_lazy_plugins", {})
    original_import = builtins.__import__

    def import_without_memory(name, globals=None, locals=None, fromlist=(), level=0):
        """模拟插件包被移除，其余模块使用正常导入过程。

        Args:
            name (str): 待导入的模块名。
            globals (dict | None): 调用方的模块命名空间。
            locals (dict | None): 调用方的局部命名空间。
            fromlist (tuple): 待导入的成员名称。
            level (int): 相对导入层级。

        Returns:
            ModuleType: 正常导入的模块。

        Raises:
            ModuleNotFoundError: 调用方试图依赖记忆插件包。
        """
        if name == "app.agent.plugins.memory" or name.startswith("app.agent.plugins.memory."):
            raise ModuleNotFoundError("记忆插件已移除")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_memory)
    assert PluginRegistry.discover_external(tmp_path) == []
    factory = SqliteFactory(tmp_path / "isolated.sqlite3")
    agent = make_agent(make_settings(), factory, ["external"])
    state = AgentState.create_new("construction")
    try:
        await agent.initialize()
        assert agent.get_plugins() == ["external"]
        await agent.plugin_manager.run_hooks(PluginHook.BEFORE_LLM, state)
        assert state.get_extra("external.model") == "old-model"
    finally:
        await agent.close()
    async with factory.connection() as connection:
        cursor = await connection.execute("SELECT name FROM plugin_models")
        assert await cursor.fetchall() == [("old-model",)]


@pytest.mark.asyncio
async def test_plugins_and_new_agents_receive_independent_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """插件私有修改互不影响，旧执行保留原配置，新 Agent 使用修改后的配置。

    Args:
        tmp_path (Path): 本次 Agent 的隔离存储目录。
        monkeypatch (pytest.MonkeyPatch): 登记本次测试专用的插件类。
    """
    class SnapshotTool(BaseTool):
        """返回插件装配工具时选择的模型信息。"""
        name = "snapshot"
        description = "读取本次模型快照"
        parameters_schema = {"type": "object", "properties": {}}

        def __init__(self, text: str) -> None:
            """保存工具独立使用的配置材料。

            Args:
                text (str): 插件名称和当前模型。
            """
            self._text = text

        async def execute(self, args: dict, context: ToolContext) -> ToolResult:
            """返回构造时的模型信息。

            Args:
                args (dict): 本工具的空参数。
                context (ToolContext): 实际工具调用的会话信息。

            Returns:
                ToolResult: 构造时保存的模型信息。
            """
            return ToolResult.success(self._text)

    class SnapshotPlugin(BasePlugin):
        """观察构造依赖在多个插件和多个 Agent 之间的隔离。"""
        name = "first"
        hooks = [PluginHook.BEFORE_LLM]

        def __init__(self, *, context: PluginContext) -> None:
            """在首个插件中修改私有设置，检验其他插件取得的快照。

            Args:
                context (PluginContext): 框架为本插件单独创建的依赖上下文。
            """
            assert context.chat_settings is not None
            self._settings = context.chat_settings
            if self.name == "first":
                self._settings.model_name = "plugin-local"

        def create_tools(self) -> tuple[BaseTool, ...]:
            """在任何钩子运行前创建已经取得配置的工具。

            Returns:
                tuple[BaseTool, ...]: 使用当前插件快照的查询工具。
            """
            return (SnapshotTool(f"{self.name}:{self._settings.model_name}"),)

        async def execute(self, context: HookContext) -> None:
            """将插件当前持有的模型配置写入可观察状态。

            Args:
                context (HookContext): 本次钩子的共享会话状态。
            """
            context.agent_state.set_extra(self.name, self._settings.model_name)

    class OtherPlugin(SnapshotPlugin):
        """保持宿主提供的原始配置，用于观察前一个插件的影响。"""
        name = "second"

    for plugin in (SnapshotPlugin, OtherPlugin):
        monkeypatch.setitem(PluginRegistry._plugins, plugin.name, plugin)
    settings = make_settings()
    factory = SqliteFactory(tmp_path / "snapshots.sqlite3")
    old_agent = make_agent(settings, factory, ["first", "second"])
    settings.model_name = "new-model"
    new_agent = make_agent(settings, factory, ["first", "second"])
    try:
        for agent, model in ((old_agent, "old-model"), (new_agent, "new-model")):
            await agent.initialize()
            state = AgentState.create_new(settings.session_id)
            await agent.plugin_manager.run_hooks(PluginHook.BEFORE_LLM, state)
            assert state.get_extra("first") == "plugin-local"
            assert state.get_extra("second") == model
            outputs = [
                (await tool.execute({}, ToolContext(settings.session_id, state))).text
                for tool in agent.tool_manager.get_all()
                if isinstance(tool, PluginToolAdapter)
            ]
            assert outputs == ["first:plugin-local", f"second:{model}"]
        assert settings.model_name == "new-model"
    finally:
        await old_agent.close()
        await new_agent.close()


@pytest.mark.asyncio
async def test_plugin_without_custom_constructor_accepts_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """沿用基类构造函数的普通插件，可在独立 Agent 中省略完整会话配置。

    Args:
        tmp_path (Path): 独立 Agent 的隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 登记本次测试的普通插件。
    """
    class SimplePlugin(BasePlugin):
        """仅使用业务钩子，沿用框架的默认构造契约。"""
        name = "simple"
        hooks = [PluginHook.BEFORE_LLM]

        async def execute(self, context: HookContext) -> None:
            """将本插件已经执行的事实交给会话状态。

            Args:
                context (HookContext): 本次插件执行的会话状态。
            """
            context.agent_state.set_extra("simple.visited", True)

    monkeypatch.setitem(PluginRegistry._plugins, SimplePlugin.name, SimplePlugin)
    settings = make_settings()
    agent = Agent(AgentConfig(
        session_id=settings.session_id, model_name=settings.model_name, api_key="test",
        context_strategy=settings.context_strategy, plugins=[SimplePlugin.name],
    ), sqlite_factory=SqliteFactory(tmp_path / "simple.sqlite3"))
    try:
        await agent.initialize()
        assert agent.get_plugins() == [SimplePlugin.name]
        state = AgentState.create_new(settings.session_id)
        await agent.plugin_manager.run_hooks(PluginHook.BEFORE_LLM, state)
        assert state.get_extra("simple.visited") is True
    finally:
        await agent.close()


def test_memory_plugin_validates_its_required_settings(tmp_path: Path) -> None:
    """记忆插件自行拒绝缺少会话配置的构造请求。

    Args:
        tmp_path (Path): 本次构造使用的隔离数据库位置。
    """
    from app.agent.plugins.memory import MemoryPlugin

    with pytest.raises(ValueError, match="需要显式提供本次会话配置"):
        MemoryPlugin(context=PluginContext(SqliteFactory(tmp_path / "memory.sqlite3")))
