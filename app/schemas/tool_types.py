"""工具声明、授权和参数中复用的固定业务取值。"""

from enum import StrEnum


class ToolSource(StrEnum):
    """区分权限配置中的工具来源。

    Attributes:
        LOCAL (ToolSource): 本地注册的工具。
        MCP (ToolSource): MCP 服务提供的工具。
    """

    LOCAL = "local"
    MCP = "mcp"


class AuthorizationTiming(StrEnum):
    """决定工具在哪个阶段等待用户批准。

    Attributes:
        BEFORE_EXECUTE (AuthorizationTiming): 执行前审批。
        AFTER_PREPARE (AuthorizationTiming): 完成只读准备后审批。
    """

    BEFORE_EXECUTE = "before_execute"
    AFTER_PREPARE = "after_prepare"


class PlanItemStatus(StrEnum):
    """表示计划中一项工作的进度。

    Attributes:
        PENDING (PlanItemStatus): 等待执行。
        IN_PROGRESS (PlanItemStatus): 正在执行。
        COMPLETED (PlanItemStatus): 已经完成。
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class FileReadUnit(StrEnum):
    """区分文件读取范围的计量方式。

    Attributes:
        LINE (FileReadUnit): 按行读取。
        CHARACTER (FileReadUnit): 按字符读取。
    """

    LINE = "line"
    CHARACTER = "character"


class PatchKind(StrEnum):
    """表示一个补丁文件条目执行的操作。

    Attributes:
        ADD (PatchKind): 新增文件。
        UPDATE (PatchKind): 修改文件。
        DELETE (PatchKind): 删除文件。
    """

    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"


TASK_OUTPUT_URI_PREFIX = "task-output://"
TOOL_RESULT_URI_PREFIX = "tool-result://"

LOCAL_TOOL_KEY_PREFIX = f"{ToolSource.LOCAL}:"
MCP_TOOL_KEY_PREFIX = f"{ToolSource.MCP}:"
