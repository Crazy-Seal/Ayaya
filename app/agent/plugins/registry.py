"""插件注册表。

插件清单由自动扫描 plugins/ 目录生成，按文件名延迟加载。
"""

import logging
import os
import pkgutil
from typing import Type

from app.agent.plugins.base import BasePlugin

logger = logging.getLogger(__name__)


class PluginRegistry:
    """插件注册表 - 自动发现插件并延迟加载。"""

    _plugins: dict[str, Type[BasePlugin]] = {}
    _lazy_plugins: dict[str, str] = {}

    @classmethod
    def register_lazy(cls, name: str, spec: str) -> None:
        """延迟注册插件。spec 支持模块路径或 模块路径:符号名。

        Args:
            name (str): 当前对象的名称。
            spec (str): 延迟加载目标，采用模块路径或 模块路径:类名 格式。
        """
        cls._lazy_plugins[name] = spec
        logger.debug("延迟注册插件: %s -> %s", name, spec)

    @classmethod
    def get(cls, name: str) -> Type[BasePlugin] | None:
        """按名称取得插件类，必要时导入延迟注册的模块并缓存成功结果。

        Args:
            name (str): 当前对象的名称。

        Returns:
            Type[BasePlugin] | None: 插件类；名称不存在或加载失败时为空。
        """
        if name in cls._plugins:
            return cls._plugins[name]

        if name in cls._lazy_plugins:
            plugin_class = cls._resolve_lazy(name)
            if plugin_class:
                cls._plugins[name] = plugin_class
                return plugin_class

        return None

    @classmethod
    def _resolve_lazy(cls, name: str) -> Type[BasePlugin] | None:
        """解析延迟加载目标并查找插件类，导入失败记录日志并交回空结果。

        Args:
            name (str): 当前对象的名称。

        Returns:
            Type[BasePlugin] | None: 成功加载的插件类，失败或不存在时为空。
        """
        from importlib import import_module

        spec = cls._lazy_plugins.get(name)
        if not spec:
            return None

        try:
            if ":" in spec:
                module_path, symbol = spec.split(":", 1)
                module = import_module(module_path)
                plugin_class = getattr(module, symbol)
            else:
                module = import_module(spec)
                plugin_class = cls._find_plugin_class(module, name)
            if plugin_class is None:
                logger.error("延迟加载插件 '%s' 失败：模块内未找到 BasePlugin 子类", name)
                return None
            logger.info("延迟加载插件: %s", name)
            return plugin_class
        except Exception as e:
            logger.error("延迟加载插件 '%s' 失败: %s", name, e)
            return None

    @staticmethod
    def _find_plugin_class(module, name: str) -> Type[BasePlugin] | None:
        """在模块中优先按插件名称匹配；只有一个候选类时允许使用该类。

        Args:
            module (Any): 已导入且可能包含插件类的模块。
            name (str): 当前对象的名称。

        Returns:
            Type[BasePlugin] | None: 唯一可确定的插件类，无法确定时为空。
        """
        candidates = [
            obj for obj in vars(module).values()
            if isinstance(obj, type) and issubclass(obj, BasePlugin) and obj is not BasePlugin
        ]
        for obj in candidates:
            if getattr(obj, "name", None) == name:
                return obj
        return candidates[0] if len(candidates) == 1 else None

    @classmethod
    def list_plugins(cls) -> list[str]:
        """合并直接注册和延迟注册的插件名称，供设置页和 Agent 查询。

        Returns:
            list[str]: 排序后的去重插件名称列表。
        """
        all_names = set(cls._plugins.keys()) | set(cls._lazy_plugins.keys())
        return sorted(all_names)

    @classmethod
    def has(cls, name: str) -> bool:
        """检查插件名称是否已直接或延迟注册，不触发模块导入。

        Args:
            name (str): 当前对象的名称。

        Returns:
            bool: 任一注册表包含该名称时为 True。
        """
        return name in cls._plugins or name in cls._lazy_plugins

    @classmethod
    def clear(cls) -> None:
        """清空进程内的直接和延迟插件注册，主要用于重新配置或测试隔离。"""
        cls._plugins.clear()
        cls._lazy_plugins.clear()


_NON_PLUGIN_MODULES = {"registry", "base", "__init__"}


def _discover_plugins() -> None:
    """导入注册表模块时扫描内置插件目录，只记录延迟加载目标，跳过内部辅助模块。"""
    plugins_dir = os.path.dirname(__file__)
    for module_info in pkgutil.iter_modules([plugins_dir]):
        mod_name = module_info.name
        if mod_name.startswith("_") or mod_name in _NON_PLUGIN_MODULES:
            continue
        PluginRegistry.register_lazy(mod_name, f"app.agent.plugins.{mod_name}")


_discover_plugins()
