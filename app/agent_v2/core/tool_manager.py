"""
工具管理器

负责工具的注册、查找、热插拔。
"""

import logging
from typing import Any

from app.agent_v2.context import BaseTool

logger = logging.getLogger(__name__)


class ToolManager:
    """工具管理器 - 支持热插拔"""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """注册工具"""
        if tool.name in self._tools:
            logger.warning(f"工具 '{tool.name}' 已存在，将被覆盖")
        self._tools[tool.name] = tool
        logger.info(f"注册工具: {tool.name}")

    def unregister(self, name: str) -> bool:
        """注销工具

        Returns:
            bool: 是否成功注销
        """
        if name in self._tools:
            del self._tools[name]
            logger.info(f"注销工具: {name}")
            return True
        logger.warning(f"工具 '{name}' 不存在，无法注销")
        return False

    def get(self, name: str) -> BaseTool | None:
        """获取工具"""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """检查工具是否存在"""
        return name in self._tools

    def list_tools(self) -> list[str]:
        """列出所有工具名称"""
        return list(self._tools.keys())

    def get_all(self) -> list[BaseTool]:
        """获取所有工具"""
        return list(self._tools.values())

    def get_openai_tools(self) -> list[dict]:
        """获取 OpenAI 格式的工具列表"""
        return [tool.to_openai_tool() for tool in self._tools.values()]

    def clear(self) -> None:
        """清空所有工具"""
        self._tools.clear()
        logger.info("已清空所有工具")

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
