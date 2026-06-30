"""
插件管理器

负责插件的生命周期管理和钩子分发。
"""

import asyncio
import logging
from collections import defaultdict
from typing import Any

from app.agent.context import BasePlugin, PluginHook, HookContext

logger = logging.getLogger(__name__)


class PluginManager:
    """插件管理器 - 管理插件生命周期和钩子分发"""

    def __init__(self):
        # 按钩子点组织的插件列表
        self._hooks: dict[PluginHook, list[BasePlugin]] = defaultdict(list)
        # 所有已注册的插件
        self._plugins: dict[str, BasePlugin] = {}
        # Agent 引用（用于插件的 on_register）
        self._agent: Any = None

    async def register(self, plugin: BasePlugin, agent: Any) -> None:
        """注册插件

        Args:
            plugin: 插件实例
            agent: Agent 实例（传递给插件的 on_register）
        """
        if plugin.name in self._plugins:
            logger.warning(f"插件 '{plugin.name}' 已存在，将被覆盖")
            # 先注销旧插件
            await self.unregister(plugin.name)

        # 保存 Agent 引用
        if self._agent is None:
            self._agent = agent

        # 调用插件的初始化方法
        await plugin.on_register(agent)

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

    async def unregister(self, name: str) -> bool:
        """注销插件

        Returns:
            bool: 是否成功注销
        """
        if name not in self._plugins:
            logger.warning(f"插件 '{name}' 不存在，无法注销")
            return False

        plugin = self._plugins[name]

        # 调用插件的清理方法
        await plugin.on_unregister()

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
        state: Any,
        data: Any = None,
        metadata: dict | None = None
    ) -> Any:
        """运行指定钩子的所有插件

        Args:
            hook: 钩子类型
            state: Agent 状态
            data: 钩子相关数据
            metadata: 元数据

        Returns:
            处理后的状态
        """
        plugins = self._hooks.get(hook, [])
        if not plugins:
            return state

        context = HookContext.create(hook, state, data, metadata)

        for plugin in plugins:
            try:
                context = await plugin.execute(context)
                # 更新状态
                state = context.agent_state
            except Exception as e:
                logger.error(f"插件 '{plugin.name}' 在钩子 '{hook}' 执行失败: {e}")
                # 继续执行其他插件

        return state

    def get_plugin(self, name: str) -> BasePlugin | None:
        """获取插件"""
        return self._plugins.get(name)

    def list_plugins(self) -> list[str]:
        """列出所有插件名称"""
        return list(self._plugins.keys())

    def get_hooks_for_plugin(self, name: str) -> list[PluginHook]:
        """获取插件订阅的钩子列表"""
        plugin = self._plugins.get(name)
        if plugin:
            return list(plugin.hooks)
        return []

    def clear(self) -> None:
        """清空所有插件"""
        self._hooks.clear()
        self._plugins.clear()
        logger.info("已清空所有插件")

    async def close(self) -> None:
        """执行插件清理钩子，并移除所有已注册插件。"""
        for name in reversed(self.list_plugins()):
            try:
                await self.unregister(name)
            except Exception:
                logger.exception("插件 '%s' 清理失败", name)

    def __len__(self) -> int:
        return len(self._plugins)

    def __contains__(self, name: str) -> bool:
        return name in self._plugins
