"""插件注册表。

插件清单由自动扫描 plugins/ 目录生成，按文件名延迟加载。
"""

import logging
import os
import pkgutil
import hashlib
import inspect
import re
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Type

from app.agent.plugins.base import BasePlugin

logger = logging.getLogger(__name__)


class PluginRegistry:
    """插件注册表 - 自动发现插件并延迟加载。"""

    _plugins: dict[str, Type[BasePlugin]] = {}
    _lazy_plugins: dict[str, str] = {}
    discovery_errors: list[str] = []

    @classmethod
    def validate(cls, plugin_class: Type[BasePlugin]) -> None:
        """校验插件类及本地工具声明，防止无效名称和重复能力进入装配。

        Args:
            plugin_class (Type[BasePlugin]): 包入口提供的插件类。

        Raises:
            ValueError: 类、名称或工具声明不满足插件契约。
        """
        if not isinstance(plugin_class, type) or not issubclass(plugin_class, BasePlugin) or inspect.isabstract(plugin_class):
            raise ValueError("插件入口必须提供可实例化的 BasePlugin 子类")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", getattr(plugin_class, "name", "")):
            raise ValueError("插件 name 必须是 1 至 64 位字母、数字、下划线或连字符")
        from pydantic import BaseModel
        if plugin_class.config_model is not None and (not isinstance(plugin_class.config_model, type) or not issubclass(plugin_class.config_model, BaseModel)):
            raise ValueError("config_model 必须提供配置模型类")
        from app.schemas.mcp import StdioMCPConnectionConfig, StreamableHttpMCPConnectionConfig
        server_ids = []
        for server in plugin_class.mcp_servers:
            if type(server) not in (StdioMCPConnectionConfig, StreamableHttpMCPConnectionConfig) or server.id in server_ids:
                raise ValueError("mcp_servers 必须提供不含 enabled 且服务 ID 唯一的连接参数")
            server_ids.append(server.id)
        from app.agent.tools.core.base import BaseTool
        names = []
        for tool_class in plugin_class.tools:
            if not isinstance(tool_class, type) or not issubclass(tool_class, BaseTool) or inspect.isabstract(tool_class):
                raise ValueError("tools 必须提供可实例化的 BaseTool 子类")
            name = getattr(tool_class, "name", "")
            if not name or name in names:
                raise ValueError(f"插件工具名称为空或重复: {name}")
            names.append(name)

    @classmethod
    def discover_external(cls, root: Path) -> list[str]:
        """扫描独立包入口，使用独立模块名支持包内相对导入。

        Args:
            root (Path): 当前应用或测试显式提供的插件目录。

        Returns:
            list[str]: 本次发现错误；调用方据此决定是否执行移除清理。
        """
        errors = []
        for package in sorted(root.iterdir()) if root.exists() else []:
            if not package.is_dir() or package.name.startswith(("_", ".")):
                continue
            entry = package / "__init__.py"
            module_name = "_ayaya_plugin_" + hashlib.sha256(str(package.resolve()).encode()).hexdigest()[:16]
            try:
                if not entry.is_file():
                    raise ValueError("插件包缺少 __init__.py 入口")
                spec = spec_from_file_location(module_name, entry, submodule_search_locations=[str(package)])
                module = module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                candidates = {obj for obj in vars(module).values() if isinstance(obj, type) and issubclass(obj, BasePlugin) and obj is not BasePlugin}
                if len(candidates) != 1:
                    raise ValueError("包入口必须导出唯一插件类")
                plugin_class = candidates.pop()
                cls.validate(plugin_class)
                if cls.has(plugin_class.name):
                    raise ValueError(f"插件名称重复: {plugin_class.name}")
                cls._plugins[plugin_class.name] = plugin_class
            except Exception as exc:
                # 1. 导入失败只释放本包模块，其他插件继续发现；配置由应用层保留。
                for name in list(sys.modules):
                    if name == module_name or name.startswith(module_name + "."):
                        sys.modules.pop(name, None)
                message = f"插件包 {package.name} 加载失败: {exc}"
                errors.append(message)
                logger.error(message)
        return errors

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
            cls.validate(plugin_class)
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


_NON_PLUGIN_MODULES = {"registry", "base", "runtime", "__init__", "tool_adapter"}


def _discover_plugins() -> None:
    """导入注册表模块时扫描内置插件目录，只记录延迟加载目标，跳过内部辅助模块。"""
    plugins_dir = os.path.dirname(__file__)
    for module_info in pkgutil.iter_modules([plugins_dir]):
        mod_name = module_info.name
        if mod_name.startswith("_") or mod_name in _NON_PLUGIN_MODULES:
            continue
        PluginRegistry.register_lazy(mod_name, f"app.agent.plugins.{mod_name}")


_discover_plugins()
