"""
工具管理器

负责工具的注册、查找、热插拔。
"""

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from app.agent.tools.core.base import BaseTool
from app.agent.models.contracts import ToolDefinition
from app.schemas.tool_identity import ToolIdentity
from app.schemas.subagent_types import SUBAGENT_PARENT_TOOL_NAMES

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
        tool_whitelist: frozenset[ToolIdentity] | None = None,
    ):
        """保存会话与授权依赖，建立空工具索引；工具由 Agent 初始化时注册。

        Args:
            session_id (str | None): 当前操作所属的会话编号。
            authorization (ToolAuthorization | None): 按最新设置校验工具权限的统一授权对象。
            tool_whitelist (frozenset[ToolIdentity] | None): 子普通工具可见范围，None 沿用主 Agent 行为。
        """
        self._tools: dict[str, BaseTool] = {}
        self._session_id = session_id
        self._authorization = authorization
        self._tool_whitelist = tool_whitelist
        self._framework_tools: set[str] = set()
        self._visible_tool_identities: dict[str, ToolIdentity] = {}
        self._visible_identity_view: Mapping[str, ToolIdentity] = MappingProxyType(self._visible_tool_identities)

    def register_framework_tool(self, tool: BaseTool) -> None:
        """由 Agent 装配专用父子通信入口，不受普通工具白名单或真人审批控制。"""
        self.register(tool)
        self._framework_tools.add(tool.name)
        self._visible_tools()

    def is_framework_tool(self, name: str) -> bool:
        """判断工具是否通过 register_framework_tool 注册"""
        return name in self._framework_tools

    def _allowed(self, tool: BaseTool) -> bool:
        """对模型目录和实际工具查找执行同一白名单及递归限制。"""
        if tool.name in self._framework_tools:
            return True
        if self._tool_whitelist is None:
            return True
        identity = getattr(tool, "identity", None) or ToolIdentity.local(tool.name)
        return tool.name not in SUBAGENT_PARENT_TOOL_NAMES and identity in self._tool_whitelist

    def register(self, tool: BaseTool) -> None:
        """将工具实例按其模型可见名称登记，供后续查找和执行。

        Args:
            tool (BaseTool): 需要注册或执行的工具对象。

        Raises:
            ValueError: 公开名称已被另一个工具占用，保留原工具的注册。
        """
        if tool.name in self._tools:
            raise ValueError(f"工具公开名称冲突：{tool.name}")
        self._tools[tool.name] = tool
        self._visible_tools()
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
            self._framework_tools.discard(name)
            self._visible_tools()
            logger.info(f"注销工具: {name}")
            return True
        logger.warning(f"工具 '{name}' 不存在，无法注销")
        return False

    def get(self, name: str) -> BaseTool | None:
        """按模型使用的名称读取当前工具实例。

        Args:
            name (str): 当前对象的名称。
        """
        # 1. 实际调用前刷新目录，让创建工具读取本次权限和热插拔后的工具身份。
        self._visible_tools()
        tool = self._tools.get(name)
        return tool if tool is not None and self._allowed(tool) else None

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

    def _visible_tools(self) -> list[BaseTool]:
        """按白名单和当前权限筛选工具，并同步创建工具读取的身份目录。

        Returns:
            list[BaseTool]: 当前允许模型看见的工具实例。
        """
        visible = [tool for tool in self._tools.values() if self._allowed(tool) and (
            self._authorization is None or self._session_id is None or self.is_framework_tool(tool.name)
            or self._authorization.is_exposed(self._session_id, tool))]
        self._visible_tool_identities.clear()
        self._visible_tool_identities.update({
            tool.name: getattr(tool, "identity", None) or ToolIdentity.local(tool.name) for tool in visible
        })
        return visible

    def get_tools(self) -> list[ToolDefinition]:
        """将当前允许公开的工具转换为模型请求定义，deny 工具不加入列表。"""
        return [tool.definition() for tool in self._visible_tools()]

    def get_visible_tool_identities(self) -> Mapping[str, ToolIdentity]:
        """提供持续更新的只读身份目录，供创建工具作为专用依赖保存。

        登记、注销、生成模型目录及实际查找工具时更新同一份映射，创建工具
        因而使用当前公开名称和稳定身份。目录只持有身份数据。

        Returns:
            Mapping[str, ToolIdentity]: 当前可见的公开名称及身份，随工具目录刷新。
        """
        self._visible_tools()
        return self._visible_identity_view

    def clear(self) -> None:
        """清空本 Agent 的工具索引，不改变磁盘上保存的工具权限。"""
        self._tools.clear()
        self._framework_tools.clear()
        self._visible_tool_identities.clear()
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
