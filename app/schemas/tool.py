from pydantic import BaseModel

from app.schemas.tool_permissions import ToolPolicy


class ToolInfo(BaseModel):
    """工具信息 VO"""
    name: str
    description: str


class ToolPolicyView(BaseModel):
    """统一工具权限页使用的只读目录投影。"""

    stable_key: str
    source: str
    name: str
    display_name: str
    description: str
    policy: ToolPolicy
    server_id: str | None = None
    server_name: str | None = None
    available: bool = True
    unavailable_reason: str | None = None


class ToolPermissionsUpdate(BaseModel):
    """一次保存当前会话的完整工具策略映射。"""

    tool_permissions: dict[str, ToolPolicy]
