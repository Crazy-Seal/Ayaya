"""
工具管理器

负责工具的注册、查找、热插拔。
"""

import logging
from typing import Any, TYPE_CHECKING

from app.agent.tools.base import BaseTool
from app.agent.models.contracts import ToolDefinition

if TYPE_CHECKING:
    from app.agent.tools.authorization.manager import ToolAuthorization

logger = logging.getLogger(__name__)


class ToolManager:
    """管理当前 Agent 的工具索引，并在生成模型工具列表时检查会话权限。"""

    def __init__(
        self,
        *,
        session_id: str | None = None,
        authorization: "ToolAuthorization | None" = None,
    ):
        """保存会话与授权依赖，建立空工具索引；工具由 Agent 初始化时注册。

        Args:
            session_id (str | None): 当前操作所属的会话编号。
            authorization (ToolAuthorization | None): 按最新设置校验工具权限的统一授权对象。
        """
        self._tools: dict[str, BaseTool] = {}
        self._session_id = session_id
        self._authorization = authorization

    def register(self, tool: BaseTool) -> None:
        """将工具实例按其模型可见名称登记，供后续查找和执行。

        Args:
            tool (BaseTool): 需要注册或执行的工具对象。
        """
        if tool.name in self._tools:
            logger.warning(f"工具 '{tool.name}' 已存在，将被覆盖")
        self._tools[tool.name] = tool
        logger.info(f"注册工具: {tool.name}")

    def unregister(self, name: str) -> bool:
        """注销工具

        Args:
            name (str): 当前对象的名称。

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
        """按模型使用的名称读取当前工具实例。

        Args:
            name (str): 当前对象的名称。
        """
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """检查工具名称是否存在于当前注册表，不在此授予执行权限。

        Args:
            name (str): 当前对象的名称。
        """
        return name in self._tools

    def list_tools(self) -> list[str]:
        """返回当前注册的工具名称，包含尚未经过权限公开筛选的工具。"""
        return list(self._tools.keys())

    def get_all(self) -> list[BaseTool]:
        """返回当前工具实例集合，供 Agent 装配或调用方检查。"""
        return list(self._tools.values())

    def get_tools(self) -> list[ToolDefinition]:
        """将当前允许公开的工具转换为模型请求定义，deny 工具不加入列表。"""
        tools = self._tools.values()
        if self._authorization is not None and self._session_id is not None:
            tools = (
                tool for tool in tools
                if self._authorization.is_exposed(self._session_id, tool)
            )
        return [tool.definition() for tool in tools]

    def clear(self) -> None:
        """清空本 Agent 的工具索引，不改变磁盘上保存的工具权限。"""
        self._tools.clear()
        logger.info("已清空所有工具")

    def __len__(self) -> int:
        """读取当前管理器注册的工具数量，包括尚未经过公开权限筛选的工具。

        Returns:
            int: 工具名称索引的条目数。
        """
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        """按模型使用的工具名称检查注册情况，不在此授予执行权限。

        Args:
            name (str): 当前对象的名称。

        Returns:
            bool: 工具名称已注册时为 True。
        """
        return name in self._tools
