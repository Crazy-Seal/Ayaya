"""统一工具权限配置模型。"""

from enum import Enum


class ToolPolicy(str, Enum):
    """会话对单个工具采用的授权策略。"""

    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


TOOL_PERMISSIONS_SCHEMA_VERSION = 1
