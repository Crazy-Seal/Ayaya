"""
插件管理器

负责插件管理和钩子分发。
"""

import logging
from collections import defaultdict
from typing import Any

from app.agent.plugins.runtime import PluginRuntimeFactory
from app.agent.state.model import AgentState

from app.agent.plugins.base import BasePlugin, HookContext, PluginHook

logger = logging.getLogger(__name__)


class PluginManager:
    """登记当前 Agent 使用的插件，并按执行时机分发业务钩子。"""

    def __init__(self, runtime_factory: PluginRuntimeFactory | None = None, *,
                 enable_plugin_hooks: bool = True):
        """建立插件名称和执行时机的内存索引，注册插件时再填充。

        Args:
            runtime_factory (PluginRuntimeFactory | None): 按插件名称创建所属会话调用对象的注入工厂。
            enable_plugin_hooks (bool): 是否执行已登记插件的自动业务钩子。
        """
        # 按钩子点组织的插件列表
        self._hooks: dict[PluginHook, list[BasePlugin]] = defaultdict(list)
        # 所有已注册的插件
        self._plugins: dict[str, BasePlugin] = {}
        self._runtime_factory = runtime_factory
        self.if_enable_plugin_hooks = enable_plugin_hooks

    def register(self, plugin: BasePlugin) -> None:
        """登记插件实例，并按优先级加入对应的业务钩子列表。

        Args:
            plugin (BasePlugin): 当前 Agent 使用的插件实例。
        """
        if plugin.name in self._plugins:
            logger.warning(f"插件 '{plugin.name}' 已存在，将被覆盖")
            # 先注销旧插件
            self.unregister(plugin.name)

        # 注册到钩子列表
        for hook in plugin.hooks:
            # 按优先级插入
            plugins = self._hooks[hook]
            inserted = False
            for i, p in enumerate(plugins):
                if plugin.priority < p.priority:
                    plugins.insert(i, plugin)
                    inserted = True
                    break
            if not inserted:
                plugins.append(plugin)

        self._plugins[plugin.name] = plugin
        logger.info(f"注册插件: {plugin.name} (v{plugin.version}, priority={plugin.priority})")

    def unregister(self, name: str) -> bool:
        """移除插件索引及其业务钩子登记。

        Args:
            name (str): 要移除的插件名称。

        Returns:
            bool: 找到并移除对应插件时为 True。
        """
        if name not in self._plugins:
            logger.warning(f"插件 '{name}' 不存在，无法注销")
            return False

        plugin = self._plugins[name]

        # 从钩子列表中移除
        for hook in plugin.hooks:
            if plugin in self._hooks[hook]:
                self._hooks[hook].remove(plugin)

        del self._plugins[name]
        logger.info(f"注销插件: {name}")
        return True

    async def run_hooks(
        self,
        hook: PluginHook,
        state: AgentState,
        data: Any = None,
        metadata: dict | None = None
    ) -> None:
        """为各插件创建独立上下文，共享本次状态、准备数据和附加信息。

        Args:
            hook (PluginHook): 钩子类型
            state (AgentState): 当前会话的内存状态。
            data (Any): 钩子相关数据
            metadata (dict | None): 元数据

        插件异常会记录并继续执行；异常前的原地修改不会回滚。
        """
        if not self.if_enable_plugin_hooks:
            return
        plugins = self._hooks.get(hook, [])
        if not plugins:
            return

        shared_metadata = metadata if metadata is not None else {}
        for plugin in plugins:
            try:
                # 1. 来源绑定到当前插件；同次调用的准备数据和附加信息沿用原对象。
                runtime = self._runtime_factory(plugin.name) if self._runtime_factory else None
                context = HookContext.create(hook, state, data, shared_metadata, runtime)
                await plugin.execute(context)
            except Exception as e:
                logger.exception(f"插件 '{plugin.name}' 在钩子 '{hook}' 执行失败: {e}")
                # 继续执行其他插件

    def get_plugin(self, name: str) -> BasePlugin | None:
        """按名称读取当前登记的插件实例，不创建新的插件。

        Args:
            name (str): 当前对象的名称。
        """
        return self._plugins.get(name)

    def list_plugins(self) -> list[str]:
        """返回当前管理器的插件名称，供 Agent 查询装配结果。"""
        return list(self._plugins.keys())

    def clear(self) -> None:
        """清空当前 Agent 的插件名称索引和业务钩子列表。"""
        self._hooks.clear()
        self._plugins.clear()
        logger.info("已清空所有插件")

    def __len__(self) -> int:
        """读取当前已注册的插件数量。

        Returns:
            int: 插件名称索引的条目数。
        """
        return len(self._plugins)

    def __contains__(self, name: str) -> bool:
        """按名称检查当前管理器是否已经注册该插件。

        Args:
            name (str): 当前对象的名称。

        Returns:
            bool: 名称在插件索引中时为 True。
        """
        return name in self._plugins
