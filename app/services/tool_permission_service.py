"""统一工具目录与会话权限的应用服务。"""

from app.agent.tools.authorization.manager import ToolAuthorization
from app.schemas.tool import ToolPolicyView
from app.schemas.tool_permissions import ToolPolicy


class ToolPermissionService:
    """在工具权限路由与统一授权对象之间转换接口模型。"""
    def __init__(
        self,
        authorization: ToolAuthorization,
    ) -> None:
        """保存授权对象，目录和权限均由它按会话读取。

        Args:
            authorization (ToolAuthorization): 按最新设置校验工具权限的统一授权对象。
        """
        self._authorization = authorization

    def list_policies(self, session_id: str) -> list[ToolPolicyView]:
        """将当前工具目录和权限转换为设置页响应模型。

        Args:
            session_id (str): 当前操作所属的会话编号。

        Returns:
            list[ToolPolicyView]: 本地及 MCP 工具的权限展示记录。
        """
        return [
            ToolPolicyView(**entry.__dict__)
            for entry in self._authorization.list_policies(session_id)
        ]

    async def set_policies(
        self,
        session_id: str,
        policies: dict[str, ToolPolicy],
    ) -> list[ToolPolicyView]:
        """交给授权对象校验并保存权限，再转换最新目录供设置页展示。

        Args:
            session_id (str): 当前操作所属的会话编号。
            policies (dict[str, ToolPolicy]): 按 local:名称 或 mcp:服务编号:名称 保存的工具权限。

        Returns:
            list[ToolPolicyView]: 保存后的权限展示记录。
        """
        return [
            ToolPolicyView(**entry.__dict__)
            for entry in await self._authorization.set_policies(session_id, policies)
        ]
