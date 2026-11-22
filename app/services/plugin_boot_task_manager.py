"""根据已保存的会话插件配置协调启动声明任务的挂载。"""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
import logging
from typing import Any

from app.agent.plugins.base import PluginContext
from app.agent.plugins.registry import PluginRegistry
from app.agent.plugins.runtime import PluginRuntime
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.utils.infra.owned_work import complete_owned_work
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.sqlite_factory import SqliteFactory
from app.schemas.chat_settings import ChatSettings
from app.services.plugin_service import PluginService
from app.services.settings_mutation import SettingsMutationCoordinator

logger = logging.getLogger(__name__)


@dataclass
class _PluginMount:
    """记录已尝试的插件参数和 Supervisor 接管的任务编号。

    Attributes:
        config (dict[str, Any]): 本次有效参数；准备失败时保存原始输入，以识别后续变化。
        task_ids (list[str]): 已登记任务，实际状态由 Supervisor 保存。
    """

    config: dict[str, Any]
    task_ids: list[str] = field(default_factory=list)


class PluginBootTaskManager:
    """协调每个会话的启动声明任务，执行和资源收尾交给 Supervisor。

    Attributes:
        _mounts (dict[tuple[str, str], _PluginMount]): 会话、插件对应的最近一次挂载尝试。
        _closing (bool): 应用开始关闭后封闭新的任务挂载。
    """

    def __init__(self, *, settings_dao: ChatSettingsDao, plugins: PluginService,
                 supervisor: TaskSupervisor, sqlite_factory: SqliteFactory,
                 runtime_factory: Callable[[str, str], PluginRuntime],
                 mutation_coordinator: SettingsMutationCoordinator) -> None:
        """借用应用创建的配置、任务和存储依赖。

        Args:
            settings_dao (ChatSettingsDao): 提供启动时保存的全部会话设置。
            plugins (PluginService): 校验插件参数并填入默认值。
            supervisor (TaskSupervisor): 接管任务执行、结果保存和清理。
            sqlite_factory (SqliteFactory): 交给插件声明的应用存储工厂。
            runtime_factory (Callable[[str, str], PluginRuntime]): 按插件名、会话编号绑定调用来源。
            mutation_coordinator (SettingsMutationCoordinator): 与配置保存共享的挂载排序锁。
        """
        self._settings_dao, self._plugins = settings_dao, plugins
        self._supervisor, self._sqlite_factory = supervisor, sqlite_factory
        self._runtime_factory, self._mutations = runtime_factory, mutation_coordinator
        self._mounts: dict[tuple[str, str], _PluginMount] = {}
        self._closing = False

    async def start(self) -> None:
        """在运行时和遗留任务整理完成后挂载所有已保存会话的启用插件。"""
        async with self._mutations.lock:
            for settings in self._settings_dao.list_chat_settings():
                await self.sync_session(settings)

    async def sync_session(self, settings: ChatSettings) -> None:
        """按已保存配置完成挂载变化，再向调用者传播取消。

        调用者持有共享配置写锁，使参数保存、旧任务清理和新任务登记顺序一致。

        Args:
            settings (ChatSettings): 文件保存成功后的当前会话配置。
        """
        await complete_owned_work(self._sync_session(settings))

    async def _sync_session(self, settings: ChatSettings) -> None:
        """停止已禁用任务，并只为首次启用或参数改变的插件挂载。

        Args:
            settings (ChatSettings): 本次已提交的会话设置。
        """
        if self._closing:
            return
        enabled = {name: item for name, item in settings.agent_plugins.items() if item.enabled}
        # 1. 禁用或移除后等待旧任务的保存与清理，再释放挂载关系。
        for key in tuple(self._mounts):
            if key[0] == settings.session_id and key[1] not in enabled:
                await self._supervisor.stop_tasks(self._mounts[key].task_ids)
                del self._mounts[key]
        # 2. 保留已经尝试的参数，完成、失败或用户停止后等待明确的配置变化。
        for name, item in enabled.items():
            if self._closing:
                return
            key = (settings.session_id, name)
            previous = self._mounts.get(key)
            if previous is not None and previous.config == item.config:
                continue
            prepared = None
            try:
                prepared = self._plugins.prepare_plugin(name, item)
            except Exception:
                logger.exception("准备插件启动参数失败：会话 %s，插件 %s", settings.session_id, name)
            config = prepared.config if prepared is not None else item.config
            if previous is not None and previous.config == config:
                continue
            if previous is not None:
                await self._supervisor.stop_tasks(previous.task_ids)
            if self._closing:
                return
            mount = _PluginMount(deepcopy(config))
            self._mounts[key] = mount
            if prepared is None:
                continue
            try:
                plugin = PluginRegistry.get(name)
                if plugin is None:
                    raise ValueError(f"插件未加载: {name}")
                context = PluginContext(self._sqlite_factory, settings.model_copy(deep=True))
                declarations = plugin.on_application_boot_tasks(context=context, config=deepcopy(prepared.config))
            except Exception:
                logger.exception("创建插件启动任务失败：会话 %s，插件 %s", settings.session_id, name)
                continue
            # 3. 每项登记独立处理，成功任务立即交给 Supervisor 持有。
            for declaration in declarations:
                if self._closing:
                    return
                try:
                    task_id = await self._supervisor.submit(declaration.task,
                        runtime=self._runtime_factory(name, settings.session_id),
                        name=declaration.name, title=declaration.title, notify_agent=declaration.notify_agent,
                        background=True, is_application_boot_task=True)
                    mount.task_ids.append(task_id)
                except Exception:
                    logger.exception("登记插件启动任务失败：会话 %s，插件 %s，任务 %s",
                        settings.session_id, name, declaration.name)

    def remove_session(self, session_id: str) -> None:
        """配置删除成功后释放挂载关系，所属任务沿用 Scheduler 的会话清理。

        调用者持有共享配置写锁；返回后继续执行既有父会话删除收尾。

        Args:
            session_id (str): 已删除配置的会话编号。
        """
        for key in tuple(self._mounts):
            if key[0] == session_id:
                del self._mounts[key]

    async def close(self) -> None:
        """封闭挂载入口，等待配置同步交还所有权后释放挂载索引。

        具体任务仍由应用稍后关闭 Supervisor 时完成结果保存和资源清理。
        """
        self._closing = True
        async with self._mutations.lock:
            self._mounts.clear()
