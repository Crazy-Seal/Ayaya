"""
工具注册表

通用注册逻辑位于 tools/core，仅延迟扫描 tools 根目录中的具体工具定义。
"""

from pathlib import Path
import pkgutil
from typing import Type
import logging

from app.agent.tools.core.base import BaseTool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """维护进程级工具类目录，支持直接注册和首次使用时的延迟导入。"""

    # 工具注册表：名称 -> 工具类
    _tools: dict[str, Type[BaseTool]] = {}

    # 延迟加载注册表：名称 -> "模块路径:符号名"
    _lazy_tools: dict[str, str] = {}

    @classmethod
    def register_lazy(cls, name: str, spec: str) -> None:
        """延迟注册工具

        Args:
            name (str): 工具名称
            spec (str): "模块路径:符号名" 格式的规范字符串
        """
        cls._lazy_tools[name] = spec
        logger.debug(f"延迟注册工具: {name} -> {spec}")

    @classmethod
    def get(cls, name: str) -> Type[BaseTool] | None:
        """获取工具类

        Args:
            name (str): 工具名称

        Returns:
            Type[BaseTool] | None: 工具类，如果不存在返回 None
        """
        # 先检查已加载的工具
        if name in cls._tools:
            return cls._tools[name]

        # 检查延迟加载
        if name in cls._lazy_tools:
            tool_class = cls._resolve_lazy(name)
            if tool_class:
                cls._tools[name] = tool_class
                return tool_class

        return None

    @classmethod
    def _resolve_lazy(cls, name: str) -> Type[BaseTool] | None:
        """解析延迟加载的工具。

        spec 支持两种形式：
        - "模块路径:符号名"：直接取该符号（显式 register_lazy 用）。
        - "模块路径"：导入后在模块内查找唯一的 BaseTool 子类（自动扫描用，
          同时兼容 @tool 产出的子类与手写的 BaseTool 子类）。

        Args:
            name (str): 当前对象的名称。
        """
        from importlib import import_module

        spec = cls._lazy_tools.get(name)
        if not spec:
            return None

        try:
            if ":" in spec:
                module_path, symbol = spec.split(":", 1)
                module = import_module(module_path)
                tool_class = getattr(module, symbol)
            else:
                module = import_module(spec)
                tool_class = cls._find_tool_class(module, name)
            if tool_class is None:
                logger.error(f"延迟加载工具 '{name}' 失败：模块内未找到 BaseTool 子类")
                return None
            logger.info(f"延迟加载工具: {name}")
            return tool_class
        except Exception as e:
            logger.error(f"延迟加载工具 '{name}' 失败: {e}")
            return None

    @staticmethod
    def _find_tool_class(module, name: str) -> Type[BaseTool] | None:
        """在模块成员里挑出工具类：优先 .name 匹配，否则取唯一的 BaseTool 子类。

        Args:
            module (Any): 已导入且可能包含插件类的模块。
            name (str): 当前对象的名称。
        """
        candidates = [
            obj for obj in vars(module).values()
            if isinstance(obj, type) and issubclass(obj, BaseTool) and obj is not BaseTool
        ]
        for obj in candidates:
            if getattr(obj, "name", None) == name:
                return obj
        return candidates[0] if len(candidates) == 1 else None

    @classmethod
    def list_tools(cls) -> list[str]:
        """合并直接及延迟注册名称，供会话默认权限补齐和工具装配。"""
        all_names = set(cls._tools.keys()) | set(cls._lazy_tools.keys())
        return sorted(all_names)

    @classmethod
    def has(cls, name: str) -> bool:
        """检查工具是否已登记，不为检查操作触发模块导入。

        Args:
            name (str): 当前对象的名称。
        """
        return name in cls._tools or name in cls._lazy_tools

    @classmethod
    def clear(cls) -> None:
        """清空进程内两类工具登记，供测试隔离或重新装配使用。"""
        cls._tools.clear()
        cls._lazy_tools.clear()


# ==================== 自动扫描发现工具 ====================

# tools/ 根目录中的 Python 模块为工具定义；子目录为支撑代码，不参与自动发现。
# 历史兼容入口继续按名称隐藏，不维护通用模块排除清单。
_HIDDEN_TOOL_MODULES = {"run_ps"}


def _discover_tools() -> None:
    """扫描 tools/ 目录，把每个工具模块按文件名延迟注册（不导入工具模块）。"""
    tools_dir = Path(__file__).resolve().parents[1]
    for module_info in pkgutil.iter_modules([str(tools_dir)]):
        mod_name = module_info.name
        if module_info.ispkg or mod_name.startswith("_") or mod_name in _HIDDEN_TOOL_MODULES:
            continue
        ToolRegistry.register_lazy(mod_name, f"app.agent.tools.{mod_name}")


_discover_tools()
