"""验证启动声明的局部失败、取消与应用关闭期间的所有权交接。"""

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.agent.plugins.base import BasePlugin, PluginBootTask
from app.agent.plugins.registry import PluginRegistry
from app.agent.plugins.runtime import PluginRuntime
from app.agent.runtime.tasks.base import BaseTask
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.sqlite_factory import SqliteFactory
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.chat_settings import ChatSettings
from app.services.chat_settings_service import ChatSettingsService
from app.services.plugin_boot_task_manager import PluginBootTaskManager
from app.services.plugin_service import PluginService
from app.services.settings_mutation import SettingsMutationCoordinator


def environment(tmp_path: Path):
    """装配使用真实存储和配置保存入口的测试应用材料。

    Args:
        tmp_path (Path): 本用例的配置与数据库目录。

    Returns:
        tuple[ChatSettingsDao, TaskSupervisor, PluginBootTaskManager, ChatSettingsService]: 公开测试入口。
    """
    path = tmp_path / "settings.yaml"
    path.write_text("chat_models: []\n", encoding="utf-8")
    dao = ChatSettingsDao(path)
    factory, mutations = SqliteFactory(tmp_path / "state.sqlite3"), SettingsMutationCoordinator()
    supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(factory))

    def runtime_for(plugin: str, session: str) -> PluginRuntime:
        """为应用的声明任务提供可信来源。

        Args:
            plugin (str): 声明所属插件。
            session (str): 已保存会话编号。

        Returns:
            PluginRuntime: 绑定会话和插件的任务提交材料。
        """
        return PluginRuntime(plugin, session, _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue)

    plugins = PluginService(dao, mutations)
    manager = PluginBootTaskManager(settings_dao=dao, plugins=plugins, supervisor=supervisor,
        sqlite_factory=factory, runtime_factory=runtime_for, mutation_coordinator=mutations)
    return dao, supervisor, manager, ChatSettingsService(dao, plugin_service=plugins,
        mutation_coordinator=mutations, boot_task_manager=manager)


def settings_for(plugins: dict) -> ChatSettings:
    """生成完整测试会话配置，保留真实配置模型校验。

    Args:
        plugins (dict): 本次场景使用的插件开关和参数。

    Returns:
        ChatSettings: 可由真实配置 DAO 保存的测试设置。
    """
    return ChatSettings(session_id="s", model_name="test", openai_api_key="test",
        openai_base_url="http://localhost", temperature=0.7, system_prompt="test", context_strategy={},
        agent_plugins=plugins)


def test_default_parameters_do_not_restart_on_unrelated_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """启动时缺省字段和保存后补齐默认字段表示同一参数，监听器保持原编号。

    Args:
        tmp_path (Path): 本用例独立配置和数据库目录。
        monkeypatch (pytest.MonkeyPatch): 注册具有默认参数的插件。
    """
    async def scenario() -> None:
        """模拟发现局部失败跳过批量正规化后，健康插件仍能正常挂载。"""
        dao, supervisor, mounts, service = environment(tmp_path)

        class Config(BaseModel):
            """由配置准备填入监听间隔默认值。"""

            interval: int = 10

        class Task(BaseTask):
            """持续等待应用停止，观察是否被无关保存替换。"""

            async def run(self, runtime) -> None:
                """保留监听资源直到 Supervisor 停止。

                Args:
                    runtime (PluginRuntime): 本次任务绑定的插件调用对象。
                """
                await asyncio.Event().wait()

        class Plugin(BasePlugin):
            """只使用模型填入的默认参数声明监听器。"""

            name = "default_boot_config"
            config_model = Config
            hooks = []

            @classmethod
            def on_application_boot_tasks(cls, *, context, config):
                """创建已取得有效默认参数的监听任务。

                Args:
                    context (PluginContext): 当前会话的独立设置快照。
                    config (dict): 已校验并填入默认值的参数。

                Returns:
                    tuple[PluginBootTask, ...]: 本次需要挂载的监听声明。
                """
                assert config == {"interval": 10}
                return (PluginBootTask(Task(), "listen", "默认监听"),)

            async def execute(self, context) -> None:
                """本插件使用启动声明独立运行。

                Args:
                    context (HookContext): Agent 插件钩子材料。
                """

        monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
        saved = settings_for({Plugin.name: {"enabled": True}})
        dao.add_chat_settings(saved)
        try:
            await mounts.start()
            before = {item.task_id for item in await supervisor.list_tasks("s")}
            await mounts.start()
            saved.temperature = 0.1
            await service.update_chat_settings(saved)
            assert {item.task_id for item in await supervisor.list_tasks("s")} == before
            assert dao.get_chat_settings("s").agent_plugins[Plugin.name].config == {"interval": 10}
        finally:
            await mounts.close()
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["validation", "declaration", "registration"])
def test_boot_failures_keep_other_tasks_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """参数、声明或单项登记失败均不阻止其他任务挂载。

    Args:
        tmp_path (Path): 本次测试的独立配置和数据库目录。
        monkeypatch (pytest.MonkeyPatch): 登记测试插件并注入存储失败。
        failure (str): 发生错误的挂载阶段。
    """
    async def scenario() -> None:
        """通过真实登记失败观察可见任务及终态通知。"""
        dao, supervisor, mounts, service = environment(tmp_path)

        class Task(BaseTask):
            """保留监听到显式停止，以便检查局部挂载结果。"""

            async def run(self, runtime) -> None:
                """持续等待关闭请求。

                Args:
                    runtime (PluginRuntime): 已登记的插件任务调用对象。
                """
                await asyncio.Event().wait()

        class Plugin(BasePlugin):
            """提供健康插件声明。"""

            name = "healthy_boot"
            hooks = []

            @classmethod
            def on_application_boot_tasks(cls, *, context, config):
                """返回一项默认静默的长期任务。

                Args:
                    context (PluginContext): 当前会话的配置快照。
                    config (dict): 已校验的插件配置。

                Returns:
                    tuple[PluginBootTask, ...]: 当前插件需要的监听任务。
                """
                return (PluginBootTask(Task(), "healthy", "正常监听"),)

            async def execute(self, context) -> None:
                """监听任务独立于 Agent 钩子运行。

                Args:
                    context (HookContext): 本次 Agent 插件调用材料。
                """

        class RequiredConfig(BaseModel):
            """模拟应用启动前保存的无效必填参数。"""

            required: str

        class BrokenPlugin(Plugin):
            """在指定阶段失败，验证后续任务仍可运行。"""

            name = "broken_boot"
            config_model = RequiredConfig if failure == "validation" else None

            @classmethod
            def on_application_boot_tasks(cls, *, context, config):
                """声明失败或返回第一项无法登记的任务。

                Args:
                    context (PluginContext): 当前会话的配置快照。
                    config (dict): 已校验的插件配置。

                Returns:
                    tuple[PluginBootTask, ...]: 首项失败后仍应挂载的独立任务。
                """
                if failure == "declaration":
                    raise ValueError("启动声明失败")
                return (PluginBootTask(Task(), "rejected", "拒绝登记"),
                    PluginBootTask(Task(), "survivor", "继续监听", notify_agent=True))

        for plugin in (Plugin, BrokenPlugin):
            monkeypatch.setitem(PluginRegistry._plugins, plugin.name, plugin)
        saved = settings_for({BrokenPlugin.name: {"enabled": True}, Plugin.name: {"enabled": True}})
        dao.add_chat_settings(saved)
        try:
            if failure == "registration":
                original = supervisor.records.insert_running

                async def reject_one(*args, **kwargs):
                    """只让指定任务登记失败，其余提交使用真实 SQLite。

                    Args:
                        args (tuple): 任务编号和会话编号。
                        kwargs (dict): 任务声明与来源材料。
                    """
                    if kwargs["name"] == "rejected":
                        raise OSError("任务登记失败")
                    await original(*args, **kwargs)

                monkeypatch.setattr(supervisor.records, "insert_running", reject_one)
            await mounts.start()
            records = await supervisor.list_tasks("s")
            assert {item.name for item in records} == ({"healthy", "survivor"} if failure == "registration" else {"healthy"})
            assert all(item.background and item.origin_run_id is None for item in records)
            assert next(item for item in records if item.name == "healthy").notify_agent is False
            await supervisor.stop_tasks(tuple(item.task_id for item in records))
            # 1. 参数整理和挂载失败已记住本次尝试，重复启动扫描也不会重建任务。
            await mounts.start()
            assert len(await supervisor.list_tasks("s")) == len(records)
        finally:
            await mounts.close()
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["cancel", "shutdown"])
def test_replacement_ownership_survives_cancel_and_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    """配置更新取消会等既有操作交还所有权，关闭则封闭替代任务挂载。

    Args:
        tmp_path (Path): 本用例隔离配置和数据库目录。
        monkeypatch (pytest.MonkeyPatch): 登记持有清理屏障的插件。
        action (str): 在旧任务清理期间取消配置请求或开始应用关闭。
    """
    async def scenario() -> None:
        """在真实配置锁与任务收尾之间制造确定的交错。"""
        dao, supervisor, mounts, service = environment(tmp_path)
        cleaning, release = asyncio.Event(), asyncio.Event()

        class Task(BaseTask):
            """在清理屏障前保留旧监听资源。"""

            async def run(self, runtime) -> None:
                """持续运行到配置改变。

                Args:
                    runtime (PluginRuntime): 任务来源和通知能力。
                """
                await asyncio.Event().wait()

            async def cleanup(self) -> None:
                """等待测试确认取消或关闭已发生。"""
                cleaning.set()
                await release.wait()

        class Plugin(BasePlugin):
            """配置变化时返回新的监听任务。"""

            name = "replacement_test"
            hooks = []

            @classmethod
            def on_application_boot_tasks(cls, *, context, config):
                """使用标签区分同一插件的不同挂载尝试。

                Args:
                    context (PluginContext): 当前会话设置。
                    config (dict): 插件标签参数。

                Returns:
                    tuple[PluginBootTask, ...]: 当前参数对应的任务。
                """
                return (PluginBootTask(Task(), config["label"], "等待替换"),)

            async def execute(self, context) -> None:
                """启动任务独立于模型钩子。

                Args:
                    context (HookContext): 本轮插件调用材料。
                """

        monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
        saved = settings_for({Plugin.name: {"enabled": True, "config": {"label": "old"}}})
        dao.add_chat_settings(saved)
        changing = closing = None
        try:
            await mounts.start()
            saved.agent_plugins[Plugin.name].config["label"] = "new"
            changing = asyncio.create_task(service.update_chat_settings(saved))
            await asyncio.wait_for(cleaning.wait(), 2)
            if action == "cancel":
                changing.cancel()
            else:
                closing = asyncio.create_task(mounts.close())
            await asyncio.sleep(0)
            assert not changing.done()
            if closing is not None:
                assert not closing.done()
            release.set()
            if action == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await changing
            else:
                await changing
                await closing
            records = await supervisor.list_tasks("s")
            assert {item.name for item in records} == ({"old", "new"} if action == "cancel" else {"old"})
            assert dao.get_chat_settings("s").agent_plugins[Plugin.name].config == {"label": "new"}
        finally:
            release.set()
            if changing is not None:
                await asyncio.gather(changing, return_exceptions=True)
            if closing is not None:
                await closing
            await mounts.close()
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())
