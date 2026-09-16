from typing import Literal

from pydantic import BaseModel, Field, model_validator

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


class ToolAuthorizationResponse(BaseModel):
    """统一工具审批响应。"""

    session_id: str
    request_id: str = Field(min_length=1)
    kind: Literal["approve", "reject", "steer"]
    message: str | None = None

    @model_validator(mode="after")
    def validate_steer_message(self) -> "ToolAuthorizationResponse":
        if self.kind == "steer" and not (self.message or "").strip():
            raise ValueError("引导消息不能为空")
        return self


class ToolContinuationResponse(BaseModel):
    """工具 continuation Adapter 的执行结果。"""

    session_id: str
    request_id: str = Field(min_length=1)
    response: dict


class ToolContinuationClaim(BaseModel):
    """前端执行 continuation 前的原子认领请求。"""

    session_id: str
    request_id: str = Field(min_length=1)
