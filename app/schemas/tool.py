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
    plugin_name: str | None = None
    policy_key: str = ""


class ToolPermissionsUpdate(BaseModel):
    """一次保存当前会话的完整工具策略映射。"""

    tool_permissions: dict[str, ToolPolicy]


class ToolListResponse(BaseModel):
    """向工具列表接口交付已注册工具的名称和说明。

    Attributes:
        tools (list[ToolInfo]): 当前已注册的工具。
    """

    tools: list[ToolInfo]


class ToolPermissionsResponse(BaseModel):
    """向权限设置页交付当前会话的工具目录。

    Attributes:
        tools (list[ToolPolicyView]): 工具权限及可用状态。
    """

    tools: list[ToolPolicyView]
