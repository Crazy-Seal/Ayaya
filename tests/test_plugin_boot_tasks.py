"""通过任务与配置公开入口验证启动声明任务的挂载和完整收尾。"""

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.agent.plugins.base import BasePlugin, PluginBootTask, PluginContext
from app.agent.plugins.registry import PluginRegistry
from app.agent.plugins.runtime import PluginRuntime
from app.agent.runtime.tasks.base import BaseTask
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.crud.sqlite_factory import SqliteFactory
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.chat_settings import ChatSettings
from app.schemas.tool_permissions import ToolPolicy
from app.services.chat_settings_service import ChatSettingsService
from app.services.plugin_boot_task_manager import PluginBootTaskManager
from app.services.plugin_service import PluginService
from app.services.settings_mutation import SettingsMutationCoordinator
from tests.runtime_support import configured_settings


def test_boot_task_count_and_stop_wait_for_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """启动任务是活动任务子集，按编号停止等待资源清理后才返回。

    Args:
        tmp_path (Path): 本用例的隔离任务存储目录。
        monkeypatch (pytest.MonkeyPatch): 在真实结果保存前设置等待屏障。
    """
    async def scenario() -> None:
        """用业务和清理屏障观察统计及停止等待的公开结果。"""
        cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
        saving, release_save = asyncio.Event(), asyncio.Event()
        derived_ids = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(),
            records=TaskRecordDao(SqliteFactory(tmp_path / "state.sqlite3")))
        runtime = PluginRuntime("timer", "s", _supervisor=supervisor,
            _runtime_queue=supervisor.runtime_queue)

        class Listener(BaseTask):
            """在资源释放屏障前保留任务收尾，模拟关闭网络监听。"""

            async def run(self, runtime) -> None:
                """持续等待 Supervisor 停止本任务。

                Args:
                    runtime (TaskRuntime): 已绑定任务来源的调用对象。
                """
                await asyncio.Event().wait()

            async def cleanup(self) -> None:
                """报告开始清理，等待测试放行后释放资源。"""
                cleanup_started.set()
                await release_cleanup.wait()

        class BootListener(Listener):
            """监听期间派生普通任务，确认启动属性不会自动继承。"""

            async def run(self, runtime) -> None:
                """通过实际 PluginRuntime 派生普通后台工作后继续监听。

                Args:
                    runtime (PluginRuntime): 框架绑定的监听任务运行时。
                """
                derived_ids.put_nowait(await runtime.submit_task(Listener(), name="job", title="普通任务", notify_agent=False))
                await super().run(runtime)

        stopping = None
        try:
            boot_id = await supervisor.submit(BootListener(), runtime=runtime, name="listen", title="监听",
                notify_agent=False, is_application_boot_task=True)
            ordinary_id = await asyncio.wait_for(derived_ids.get(), 2)
            assert supervisor.active_task_counts() == (2, 1)
            records = await supervisor.list_tasks("s")
            assert {item.task_id for item in records} == {boot_id, ordinary_id}
            assert all(item.origin_run_id is None for item in records)
            original = supervisor.records.finish_once

            async def save_terminal(*args, **kwargs):
                """在原子保存前等待，使统计覆盖结果提交阶段。

                Args:
                    args (tuple): 任务编号与终态。
                    kwargs (dict): 任务结果正文和错误信息。

                Returns:
                    TaskRecord | None: 实际提交后的任务记录。
                """
                if args[0] == boot_id:
                    saving.set()
                    await release_save.wait()
                return await original(*args, **kwargs)

            monkeypatch.setattr(supervisor.records, "finish_once", save_terminal)
            stopping = asyncio.create_task(supervisor.stop_tasks((boot_id,)))
            await asyncio.wait_for(saving.wait(), 2)
            assert supervisor.active_task_counts() == (2, 1)
            release_save.set()
            await asyncio.wait_for(cleanup_started.wait(), 2)
            assert not stopping.done()
            assert supervisor.active_task_counts() == (1, 0)
            release_cleanup.set()
            await stopping
            assert (await runtime.get_task(ordinary_id)).status == "running"
        finally:
            release_save.set()
            release_cleanup.set()
            if stopping is not None:
                await stopping
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_saved_settings_reconcile_only_boot_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """多会话独立挂载，更新参数等待旧清理，停止和失败后无关保存不重启。

    Args:
        tmp_path (Path): 测试数据库和资源的隔离目录。
        monkeypatch (pytest.MonkeyPatch): 注册本用例的启动任务插件。
    """
    async def scenario() -> None:
        """通过真实设置保存入口核对挂载、替换和禁用。"""
        created: list[tuple[str, str]] = []
        cleaned: list[tuple[str, str]] = []
        cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()

        class Config(BaseModel):
            """以标签区分替换前后的任务资源。"""

            label: str = "first"

        class Task(BaseTask):
            """持有自己的标签，确认清理后才能创建替代任务。"""

            def __init__(self, session: str, label: str) -> None:
                """保存当前任务使用的配置。

                Args:
                    session (str): 任务所属会话。
                    label (str): 已校验的插件标签。
                """
                self.session, self.label = session, label

            async def run(self, runtime) -> None:
                """持续监听或按测试配置产生业务失败。

                Args:
                    runtime (PluginRuntime): 已绑定会话和任务编号的调用对象。
                """
                assert runtime.session_id == self.session
                assert runtime.task_id
                if self.label == "run_failure":
                    raise RuntimeError("监听失败")
                if self.label == "completed":
                    return
                await asyncio.Event().wait()

            async def cleanup(self) -> None:
                """对首次会话资源提供屏障，随后记录清理完成。"""
                if (self.session, self.label) == ("a", "first"):
                    cleanup_started.set()
                    await release_cleanup.wait()
                cleaned.append((self.session, self.label))

        class Plugin(BasePlugin):
            """只有启动声明，Agent 实例构造不参与挂载。"""

            name = "boot_test"
            config_model = Config
            hooks = []

            def __init__(self, *, context: PluginContext) -> None:
                """启动挂载应直接读取类声明。

                Args:
                    context (PluginContext): 普通 Agent 插件的装配材料。
                """
                raise AssertionError("启动任务无需创建插件实例")

            @classmethod
            def on_application_boot_tasks(cls, *, context, config):
                """为已启用会话生成独立任务。

                Args:
                    context (PluginContext): 含当前会话配置的装配材料。
                    config (dict): 当前插件校验后的独立参数副本。

                Returns:
                    tuple[PluginBootTask, ...]: 当前会话的启动任务声明。
                """
                session, label = context.chat_settings.session_id, config["label"]
                created.append((session, label))
                if label == "declaration_failure":
                    raise RuntimeError("声明失败")
                return (PluginBootTask(Task(session, label), name="listener", title="测试监听"),)

            async def execute(self, context) -> None:
                """该插件的任务独立于 Agent 执行钩子。

                Args:
                    context (HookContext): 普通插件钩子上下文。
                """

        monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
        dao = configured_settings("a", "b")
        for session in ("a", "b"):
            settings = ChatSettings(session_id=session, model_name="test", openai_api_key="test",
                openai_base_url="http://localhost", temperature=0.7, system_prompt="test", context_strategy={},
                agent_plugins={Plugin.name: {"enabled": True, "config": {"label": "first"}}})
            dao.update_chat_settings(session, settings)
        mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(tmp_path / "state.sqlite3")))

        def runtime_for(plugin: str, session: str) -> PluginRuntime:
            """为挂载任务提供固定来源，执行编号由 Supervisor 绑定。

            Args:
                plugin (str): 声明所属插件。
                session (str): 当前保存的会话身份。

            Returns:
                PluginRuntime: 尚未绑定任务编号的调用对象。
            """
            return PluginRuntime(plugin, session, _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue)

        plugins = PluginService(dao, mutations)
        mounts = PluginBootTaskManager(settings_dao=dao, plugins=plugins, supervisor=supervisor,
            sqlite_factory=SqliteFactory(tmp_path / "state.sqlite3"), runtime_factory=runtime_for,
            mutation_coordinator=mutations)
        service = ChatSettingsService(dao, plugin_service=plugins, mutation_coordinator=mutations,
            boot_task_manager=mounts)
        changing = None
        try:
            await mounts.start()
            assert created == [("a", "first"), ("b", "first")]
            assert supervisor.active_task_counts() == (2, 2)
            unchanged = dao.get_chat_settings("a")
            unchanged.model_name = "another-model"
            unchanged.agent_plugins[Plugin.name].tool_permissions["local:test"] = ToolPolicy.DENY
            await service.update_chat_settings(unchanged)
            assert created == [("a", "first"), ("b", "first")]
            initial = (await supervisor.list_tasks("a"))[0].task_id
            settings = dao.get_chat_settings("a")
            settings.agent_plugins[Plugin.name].config["label"] = "second"
            changing = asyncio.create_task(service.update_chat_settings(settings))
            await asyncio.wait_for(cleanup_started.wait(), 2)
            assert not changing.done()
            assert ("a", "second") not in created
            release_cleanup.set()
            await changing
            assert ("a", "first") in cleaned
            assert created[-1] == ("a", "second")
            current = next(item.task_id for item in await supervisor.list_tasks("a") if item.status == "running")
            assert current != initial
            await supervisor.stop_tasks((current,))
            await service.update_chat_settings(dao.get_chat_settings("a"))
            assert created.count(("a", "second")) == 1
            for label in ("declaration_failure", "run_failure", "completed"):
                previous_ids = {item.task_id for item in await supervisor.list_tasks("a")}
                settings = dao.get_chat_settings("a")
                settings.agent_plugins[Plugin.name].config["label"] = label
                await service.update_chat_settings(settings)
                if label != "declaration_failure":
                    record = next(item for item in await supervisor.list_tasks("a") if item.task_id not in previous_ids)
                    result = await supervisor.wait(record.task_id, timeout_ms=2000)
                    assert result.status == ("failed" if label == "run_failure" else "completed")
                await supervisor.stop_tasks(tuple(item.task_id for item in await supervisor.list_tasks("a")))
                await service.update_chat_settings(dao.get_chat_settings("a"))
                assert created.count(("a", label)) == 1
            settings = dao.get_chat_settings("b")
            settings.agent_plugins[Plugin.name].enabled = False
            await service.update_chat_settings(settings)
            assert ("b", "first") in cleaned
            assert supervisor.active_task_counts() == (0, 0)
            settings.agent_plugins[Plugin.name].enabled = True
            await service.update_chat_settings(settings)
            assert created.count(("b", "first")) == 2
            fresh = settings.model_copy(deep=True, update={"session_id": "new"})
            await service.add_chat_settings(fresh)
            assert created[-1] == ("new", "first")
            with monkeypatch.context() as failure:
                def reject_save(*args, **kwargs) -> None:
                    """在配置提交前失败，挂载应保持原样。

                    Args:
                        args (tuple): 待保存的会话编号和设置。
                        kwargs (dict): 本次存储调用的其他材料。
                    """
                    raise OSError("配置保存失败")

                failure.setattr(dao, "update_chat_settings", reject_save)
                fresh.agent_plugins[Plugin.name].enabled = False
                with pytest.raises(OSError, match="配置保存失败"):
                    await service.update_chat_settings(fresh)
            assert ("new", "first") not in cleaned
            await service.delete_chat_settings("new")
            # 3. 与实际删除路由相同，由会话清理停止所属任务。
            await supervisor.stop_session_tasks("new")
            assert ("new", "first") in cleaned
            fresh.agent_plugins[Plugin.name].enabled = True
            await service.add_chat_settings(fresh)
            assert created.count(("new", "first")) == 2
        finally:
            release_cleanup.set()
            if changing is not None:
                await changing
            await mounts.close()
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())
