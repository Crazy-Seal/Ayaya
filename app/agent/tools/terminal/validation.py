"""终端工具共享的危险输入检查和错误观察表达。"""

from app.agent.tools.core.base import ToolResult


# 既有 PowerShell 硬拦截继续适用于初始命令和后续输入；它不是系统沙箱。
DANGEROUS_PATTERNS = (
    "remove-item", "rd /s /q", "rmdir /s /q", "del /f /s /q", "erase /f /s /q", "cipher /w",
    "format-volume", "clear-disk", "diskpart", "remove-partition", "delete partition", "delete volume",
    "stop-computer", "restart-computer", "shutdown", "bootrec", "bcdedit",
    "vssadmin delete shadows", "wmic shadowcopy delete", "wbadmin delete",
    "reg delete", "remove-itemproperty", "set-itemproperty -path hk", "new-itemproperty -path hk",
    "netsh advfirewall set allprofiles state off", "set-mppreference -disablerealtimemonitoring",
    "net user ", "net localgroup administrators", "add-localgroupmember",
)


def validate_input(text: str) -> None:
    """在启动或写入前保留既有危险内容硬拦截。

    Args:
        text (str): 初始命令或准备发送的 stdin。

    Raises:
        ValueError: 命中了既有危险命令规则，输入尚未发送。
    """
    if any(pattern in text.lower() for pattern in DANGEROUS_PATTERNS):
        raise ValueError("检测到潜在危险命令，已阻止执行；输入未发送。")


def failure(message: str, input_status: str = "not_sent") -> ToolResult:
    """构造不会让模型误以为命令已经执行的错误观察。

    Args:
        message (str): 校验或运行器提供的失败原因。
        input_status (str): 校验前失败为 not_sent，进程创建期间失败为 unknown。

    Returns:
        ToolResult: 包含原因、重试提示和停止条件的结果。
    """
    return ToolResult.success(
        f"status: error\nsummary: {message}\ninput_status: {input_status}\n"
        "next_actions: 检查参数、任务状态或权限后再操作；不要自动重放已发送的输入。"
    )
