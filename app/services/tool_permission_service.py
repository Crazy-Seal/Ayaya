"""统一工具目录与会话权限的应用服务。"""

from app.agent.tools.authorization.manager import ToolAuthorization
from app.schemas.tool import ToolPolicyView
from app.schemas.tool_permissions import ToolPolicy


class ToolPermissionService:
    def __init__(
        self,
        authorization: ToolAuthorization,
    ) -> None:
        self._authorization = authorization

    def list_policies(self, session_id: str) -> list[ToolPolicyView]:
        return [
            ToolPolicyView(**entry.__dict__)
            for entry in self._authorization.list_policies(session_id)
        ]

    async def set_policies(
        self,
        session_id: str,
        policies: dict[str, ToolPolicy],
    ) -> list[ToolPolicyView]:
        return [
            ToolPolicyView(**entry.__dict__)
            for entry in await self._authorization.set_policies(session_id, policies)
        ]
