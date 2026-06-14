"""
工具注册表

支持延迟加载，避免循环依赖。
"""

from typing import Callable, Type
import logging

from app.agent_v2.context import BaseTool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """工具注册表 - 支持延迟加载"""

    # 工具注册表：名称 -> 工具类
    _tools: dict[str, Type[BaseTool]] = {}

    # 延迟加载注册表：名称 -> "模块路径:符号名"
    _lazy_tools: dict[str, str] = {}

    @classmethod
    def register(cls, name: str | None = None) -> Callable[[Type[BaseTool]], Type[BaseTool]]:
        """注册工具的装饰器

        Usage:
            @ToolRegistry.register()
            class MyTool(BaseTool):
                name = "my_tool"
                ...

            @ToolRegistry.register("custom_name")
            class MyTool(BaseTool):
                ...
        """
        def decorator(tool_class: Type[BaseTool]) -> Type[BaseTool]:
            tool_name = name or tool_class.name
            cls._tools[tool_name] = tool_class
            logger.debug(f"注册工具: {tool_name}")
            return tool_class
        return decorator

    @classmethod
    def register_lazy(cls, name: str, spec: str) -> None:
        """延迟注册工具

        Args:
            name: 工具名称
            spec: "模块路径:符号名" 格式的规范字符串
        """
        cls._lazy_tools[name] = spec
        logger.debug(f"延迟注册工具: {name} -> {spec}")

    @classmethod
    def get(cls, name: str) -> Type[BaseTool] | None:
        """获取工具类

        Args:
            name: 工具名称

        Returns:
            工具类，如果不存在返回 None
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
        """解析延迟加载的工具"""
        from importlib import import_module

        spec = cls._lazy_tools.get(name)
        if not spec:
            return None

        try:
            module_path, symbol = spec.split(":", 1)
            module = import_module(module_path)
            tool_class = getattr(module, symbol)
            logger.info(f"延迟加载工具: {name}")
            return tool_class
        except Exception as e:
            logger.error(f"延迟加载工具 '{name}' 失败: {e}")
            return None

    @classmethod
    def list_tools(cls) -> list[str]:
        """列出所有工具名称"""
        all_names = set(cls._tools.keys()) | set(cls._lazy_tools.keys())
        return sorted(all_names)

    @classmethod
    def has(cls, name: str) -> bool:
        """检查工具是否存在"""
        return name in cls._tools or name in cls._lazy_tools

    @classmethod
    def clear(cls) -> None:
        """清空注册表"""
        cls._tools.clear()
        cls._lazy_tools.clear()


# ==================== 预注册的延迟加载工具 ====================

# 在这里添加需要延迟加载的工具
LAZY_TOOLS = {
    # 文件操作工具
    "read_file": "app.agent_v2.tools.read_file:ReadFileTool",
    "write_file": "app.agent_v2.tools.write_file:WriteFileTool",
    "edit_file": "app.agent_v2.tools.edit_file:EditFileTool",
    "delete_file": "app.agent_v2.tools.delete_file:DeleteFileTool",
    # 互联网检索
    "access_the_internet": "app.agent_v2.tools.access_the_internet:AccessTheInternetTool",
    # 编码计划
    "update_plan": "app.agent_v2.tools.update_plan:UpdatePlanTool",
    "plan_and_coding": "app.agent_v2.tools.plan_and_coding:PlanAndCodingTool",
    # 记忆检索
    "search_memory": "app.agent_v2.tools.search_memory:SearchMemoryTool",
    "search_diary": "app.agent_v2.tools.search_diary:SearchDiaryTool",
    # PowerShell命令
    "run_ps": "app.agent_v2.tools.run_ps:RunPsTool",
    # 截屏
    "screenshot": "app.agent_v2.tools.screenshot:ScreenshotTool",

}

for name, spec in LAZY_TOOLS.items():
    ToolRegistry.register_lazy(name, spec)
